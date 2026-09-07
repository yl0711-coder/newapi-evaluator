from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from shared.network import guarded_transport
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


ROOT = Path(__file__).resolve().parent
QUESTIONS_PATH = ROOT / "questions.json"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
SYSTEM_PROMPT = "请完成用户的任务，给出可核查的解题依据和明确结论；遵守题目要求的输出格式。"


def normalize_url(base_url: str, protocol: str) -> str:
    parts = urlsplit(base_url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("端点地址必须是有效的 http 或 https URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("端点地址不能包含账号、密码、查询参数或片段；API Key 请单独填写")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("端点端口无效") from exc
    suffix = "/messages" if protocol == "anthropic" else "/chat/completions"
    path = parts.path.rstrip("/")
    if path.endswith(("/chat/completions", "/messages")):
        if not path.endswith(suffix):
            raise ValueError("完整端点路径与所选协议不一致")
    else:
        path = (path or "/v1") + suffix
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class EndpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    base_url: str = Field(min_length=1)
    api_key: SecretStr
    model: str = Field(min_length=1)
    protocol: Literal["openai", "anthropic"]
    thinking_mode: Literal["disabled", "enabled", "adaptive"] = "enabled"
    thinking_budget_tokens: int = Field(default=1024, ge=1024, le=131071)

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, key: SecretStr) -> SecretStr:
        value = key.get_secret_value().strip()
        if not value or "\n" in value or "\r" in value:
            raise ValueError("API Key 不能为空或包含换行")
        return SecretStr(value)

    @model_validator(mode="after")
    def validate_url(self) -> EndpointConfig:
        try:
            httpx.URL(normalize_url(self.base_url, self.protocol))
        except httpx.InvalidURL as exc:
            raise ValueError("端点地址不被 HTTP 客户端支持") from exc
        return self


class TestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: EndpointConfig
    max_tokens: int = Field(default=8192, ge=1, le=131072)
    timeout_seconds: float = Field(default=120, ge=1, le=600)

    @model_validator(mode="after")
    def validate_budget(self) -> TestRequest:
        if (self.endpoint.protocol == "anthropic"
                and self.endpoint.thinking_mode == "enabled"
                and self.endpoint.thinking_budget_tokens >= self.max_tokens):
            raise ValueError("手动思考预算必须小于最大输出 token 数")
        return self


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=100_000)


class Question(BaseModel):
    id: str
    title: str
    difficulty: str
    reasoning_required: bool
    prompt: str


class CompletionCheck(BaseModel):
    status: Literal["complete", "truncated", "empty", "unknown", "error"]
    finish_reason: str | None = None
    detail: str


class ReasoningCheck(BaseModel):
    status: Literal["returned", "missing", "redacted", "not_required", "error"]
    source: str | None = None
    field_present: bool = False
    characters: int = 0
    redacted_blocks: int = 0
    reported_tokens: int | None = None
    detail: str


class TestResult(BaseModel):
    question_id: str
    title: str
    prompt: str
    reasoning_required: bool
    http_status: int | None = None
    headers_time_ms: float | None = None
    first_byte_ms: float | None = None
    total_time_ms: float = 0
    response_bytes: int = 0
    completion: CompletionCheck
    reasoning: ReasoningCheck
    answer: str = ""
    reasoning_text: str = ""
    raw_response: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


def read_questions() -> list[Question]:
    return [Question(**item) for item in json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))]


CHANNEL_FIELD_ALIASES = {
    "base_url": {
        "base_url", "baseurl", "api_base", "apibase", "url", "endpoint", "host",
        "openai_api_base", "openai_base_url", "anthropic_base_url", "deepseek_base_url",
    },
    "api_key": {
        "api_key", "apikey", "key", "token", "secret", "authorization",
        "openai_api_key", "anthropic_api_key", "deepseek_api_key",
    },
    "model": {"model", "model_name", "modelname", "deployment", "engine", "openai_model"},
    "protocol": {"protocol", "api_type", "api_protocol"},
}


def configuration_objects(value: Any) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            objects.append(current)
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return objects


def aliased_value(objects: list[dict[str, Any]], field: str) -> str:
    aliases = CHANNEL_FIELD_ALIASES[field]
    for item in objects:
        for key, value in item.items():
            normalized_key = str(key).lower().replace("-", "_").replace(".", "_")
            if normalized_key not in aliases:
                continue
            if isinstance(value, list):
                value = value[0] if value else ""
            if value not in (None, "", [], {}):
                return str(value).strip()
    return ""


def key_value_object(text: str) -> dict[str, str]:
    values = {}
    for source_line in text.splitlines():
        line = source_line.strip().lstrip("-").strip()
        match = re.match(r"^(?:export\s+)?([\w.-]+)\s*[=:]\s*(.+)$", line, re.I)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("\"',`")
    return values


def normalized_imported_url(value: str) -> str:
    url = value.strip().strip("\"',`").rstrip(".,;:!?)")
    if url and not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return re.sub(r"/(chat/completions|responses|messages|models)/?$", "", url.rstrip("/"), flags=re.I)


def extract_channel_information(text: str) -> dict[str, Any]:
    raw = text.strip()
    if not raw:
        raise ValueError("请先粘贴渠道连接信息")
    objects = []
    if raw.startswith(("{", "[")):
        try:
            objects.extend(configuration_objects(json.loads(raw)))
        except (json.JSONDecodeError, RecursionError):
            pass
    objects.append(key_value_object(raw))
    base_url = aliased_value(objects, "base_url")
    api_key = aliased_value(objects, "api_key")
    model = aliased_value(objects, "model")
    protocol_value = aliased_value(objects, "protocol").lower()
    if not base_url:
        match = re.search(r"https?://[^\s'\"<>{}\[\]，。；、]+", raw, re.I)
        base_url = match.group(0) if match else ""
    if not api_key:
        bearer = re.search(r"(?:Authorization\s*:\s*Bearer|x-api-key\s*:?)\s*([^\s'\"\\]+)", raw, re.I)
        named = re.search(r"(?:api[_-]?key|token|secret)\s*[=:]\s*['\"]?([^\s'\",}]+)", raw, re.I)
        sk_key = re.search(r"(?<![\w.-])(sk-[A-Za-z0-9][A-Za-z0-9._-]*)", raw)
        match = bearer or named or sk_key
        api_key = match.group(1) if match else ""
    if not model:
        match = re.search(r"(?:\"?model(?:_name)?\"?)\s*[=:]\s*['\"]?([^\s'\",}]+)", raw, re.I)
        model = match.group(1) if match else ""
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:].strip()
    protocol = ""
    if "anthropic" in protocol_value or "claude" in protocol_value:
        protocol = "anthropic"
    elif "openai" in protocol_value or "chat" in protocol_value:
        protocol = "openai"
    elif re.search(r"anthropic-version|x-api-key", raw, re.I) or re.search(r"/messages(?:\s|['\"]|$)", raw, re.I):
        protocol = "anthropic"
    elif re.search(r"chat/completions|Authorization\s*:\s*Bearer", raw, re.I):
        protocol = "openai"
    normalized_url = normalized_imported_url(base_url) if base_url else ""
    return {
        "base_url": normalized_url,
        "api_key": api_key.strip().strip("\"',`"),
        "model": model.strip().strip("\"',`"),
        "protocol": protocol,
        "has_url": bool(normalized_url),
        "has_key": bool(api_key),
        "has_model": bool(model),
        "has_protocol": bool(protocol),
    }


def text_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if any(not isinstance(block, dict) for block in value):
            raise ValueError("内容块必须是对象")
        return "\n".join(text_content(block.get("text")) for block in value
                         if block.get("type") == "text")
    raise ValueError("文本字段必须是字符串或文本块列表")


def parse_response(raw: bytes) -> Any:
    body = json.loads(raw)
    pending = [(body, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            raise ValueError("响应 JSON 嵌套超过本地 32 层解析上限；原始响应保留供复核")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return body


def inspect_response(
    body: dict[str, Any], protocol: str, reasoning_required: bool,
) -> tuple[CompletionCheck, ReasoningCheck, str, str]:
    if not isinstance(body, dict) or body.get("error"):
        raise ValueError("上游响应不是成功的消息对象")
    redacted_blocks = 0
    refusal = False
    tool_call = False
    incomplete_sources = [body]
    if protocol == "openai":
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("响应缺少有效的 choices[0]")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("响应缺少有效的 message 对象")
        finish_reason = choice.get("finish_reason")
        answer = text_content(message.get("content"))
        source = "reasoning_content" if "reasoning_content" in message else None
        field_present = source is not None
        reasoning_text = text_content(message.get("reasoning_content"))
        refusal = bool(message.get("refusal"))
        tool_call = bool(message.get("tool_calls") or message.get("function_call"))
        incomplete_sources.extend([choice, message])
        normal_finish = finish_reason == "stop"
    else:
        blocks = body.get("content")
        if not isinstance(blocks, list) or any(not isinstance(block, dict) for block in blocks):
            raise ValueError("响应缺少有效的 content 内容块列表")
        thinking = [block for block in blocks if block.get("type") == "thinking"]
        redacted_blocks = sum(block.get("type") == "redacted_thinking" for block in blocks)
        field_present = bool(thinking or redacted_blocks)
        source = "thinking" if thinking else ("redacted_thinking" if redacted_blocks else None)
        reasoning_text = "\n\n".join(text_content(block.get("thinking")) for block in thinking)
        answer = text_content(blocks)
        finish_reason = body.get("stop_reason")
        tool_call = any(block.get("type") in {"tool_use", "server_tool_use"} for block in blocks)
        normal_finish = finish_reason == "end_turn"

    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ValueError("结束原因必须是字符串或 null")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    token_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details")
    token_value = token_details.get("reasoning_tokens") if isinstance(token_details, dict) else None
    reported_tokens = token_value if isinstance(token_value, int) and not isinstance(token_value, bool) and token_value >= 0 else None
    incomplete = any(item.get("incomplete") is True or item.get("status") == "incomplete"
                     or bool(item.get("incomplete_details")) for item in incomplete_sources)
    if finish_reason in {"length", "max_tokens", "model_context_window_exceeded"} or incomplete:
        completion = CompletionCheck(status="truncated", finish_reason=finish_reason,
                                     detail="上游报告了长度限制、上下文限制或未完成标记；请检查原始响应。")
    elif refusal or tool_call or not normal_finish:
        completion = CompletionCheck(status="unknown", finish_reason=finish_reason,
                                     detail="没有正常结束的充分证据，或出现拒答/工具调用；需要复核。")
    elif not answer.strip():
        completion = CompletionCheck(status="empty", finish_reason=finish_reason,
                                     detail="上游报告正常结束，但未返回非空最终答案。")
    else:
        completion = CompletionCheck(status="complete", finish_reason=finish_reason,
                                     detail="最终答案非空且上游报告正常结束；未发现显式截断信号。")

    if redacted_blocks:
        reasoning_status = "redacted"
        reasoning_detail = "存在不可见的 redacted_thinking 内容块；不能验证其内容完整性。"
    elif reasoning_text.strip():
        reasoning_status = "returned"
        reasoning_detail = "接口返回了可见推理内容。内容可能是摘要；请结合响应结束状态和原文复核。"
    elif reasoning_required:
        reasoning_status = "missing"
        if reported_tokens:
            reasoning_detail = f"未返回独立推理文本；usage 报告消耗了 {reported_tokens} 个 reasoning tokens。请直接查看上游原始回答，人工判断正文是否包含推导。"
        else:
            reasoning_detail = "未返回独立推理文本，usage 也没有提供正数 reasoning_tokens。请直接查看上游原始回答，人工判断正文是否包含推导。"
    else:
        reasoning_status = "not_required"
        reasoning_detail = "短答基线不要求推理内容，不计入推理返回率。"
    reasoning = ReasoningCheck(
        status=reasoning_status, source=source, field_present=field_present,
        characters=len(reasoning_text), redacted_blocks=redacted_blocks,
        reported_tokens=reported_tokens, detail=reasoning_detail,
    )
    return completion, reasoning, answer, reasoning_text


def build_request(
    endpoint: EndpointConfig, question: Question, max_tokens: int,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    key = endpoint.api_key.get_secret_value()
    headers = {"Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "model": endpoint.model, "max_tokens": max_tokens, "stream": False,
        "messages": [{"role": "user", "content": question.prompt}],
    }
    if endpoint.protocol == "openai":
        headers["Authorization"] = f"Bearer {key}"
        payload["messages"].insert(0, {"role": "system", "content": SYSTEM_PROMPT})
    else:
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        payload["system"] = SYSTEM_PROMPT
        payload["thinking"] = {"type": endpoint.thinking_mode}
        if endpoint.thinking_mode == "enabled":
            payload["thinking"]["budget_tokens"] = endpoint.thinking_budget_tokens
    return normalize_url(endpoint.base_url, endpoint.protocol), headers, payload


async def call_non_stream(
    client: httpx.AsyncClient, endpoint: EndpointConfig, question: Question,
    max_tokens: int, timeout_seconds: float,
) -> TestResult:
    result = TestResult(
        question_id=question.id, title=question.title, prompt=question.prompt,
        reasoning_required=question.reasoning_required,
        completion=CompletionCheck(status="error", detail="请求未完成"),
        reasoning=ReasoningCheck(status="error", detail="请求未完成，无法检查推理"),
    )
    start = perf_counter()
    chunks: list[bytes] = []
    url, headers, payload = build_request(endpoint, question, max_tokens)

    async def receive() -> None:
        async with client.stream("POST", url, headers=headers, json=payload,
                                 timeout=timeout_seconds) as response:
            result.http_status = response.status_code
            result.headers_time_ms = (perf_counter() - start) * 1000
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                if result.first_byte_ms is None:
                    result.first_byte_ms = (perf_counter() - start) * 1000
                remaining = MAX_RESPONSE_BYTES - result.response_bytes
                chunks.append(chunk[:remaining])
                result.response_bytes += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    raise ValueError("响应超过本地 8 MiB 读取上限，样本仅保留前缀；不能判断上游完整性")

    try:
        await asyncio.wait_for(receive(), timeout=timeout_seconds)
        result.total_time_ms = (perf_counter() - start) * 1000
        raw = b"".join(chunks)
        if result.http_status != 200:
            raise ValueError(f"上游返回 HTTP {result.http_status}；详情见原始响应")
        body = parse_response(raw)
        result.completion, result.reasoning, result.answer, result.reasoning_text = inspect_response(
            body, endpoint.protocol, question.reasoning_required,
        )
        if isinstance(body.get("usage"), dict):
            result.usage = body["usage"]
    except (asyncio.TimeoutError, httpx.TimeoutException):
        result.error = f"请求超时（每题总时限 {timeout_seconds:g} 秒）；已收到的响应片段保留供复核"
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, UnicodeError, RecursionError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if result.total_time_ms == 0:
            result.total_time_ms = (perf_counter() - start) * 1000
        result.raw_response = b"".join(chunks).decode("utf-8", errors="replace")
    if result.error:
        result.completion = CompletionCheck(status="error", detail=result.error)
        result.reasoning = ReasoningCheck(status="error", detail="响应未能完成有效解析，推理返回情况无法确认。")
    secret = endpoint.api_key.get_secret_value()
    for field in ("answer", "reasoning_text", "error"):
        setattr(result, field, redact_secret(getattr(result, field), secret))
    result.raw_response = redact_response(result.raw_response, secret)
    result.usage = redact_secret(result.usage, secret)
    result.completion.detail = redact_secret(result.completion.detail, secret)
    result.completion.finish_reason = redact_secret(result.completion.finish_reason, secret)
    return result


def summarize(results: list[TestResult]) -> dict[str, Any]:
    total = len(results)
    successful = [result for result in results if result.error is None]
    required = [result for result in results if result.reasoning_required]
    returned = [result for result in required if result.reasoning.status == "returned"]
    reasoning_complete_count = sum(result.completion.status == "complete" for result in returned)
    complete_count = sum(result.completion.status == "complete" for result in results)
    times = [result.total_time_ms for result in successful]
    reasoning_token_values = [result.reasoning.reported_tokens for result in results
                              if result.reasoning.reported_tokens is not None]
    return {
        "total": total, "success_count": len(successful), "error_count": total - len(successful),
        "complete_count": complete_count, "complete_rate": complete_count / total if total else 0,
        "truncated_count": sum(result.completion.status == "truncated" for result in results),
        "empty_count": sum(result.completion.status == "empty" for result in results),
        "unknown_count": sum(result.completion.status == "unknown" for result in results),
        "reasoning_expected": len(required), "reasoning_returned": len(returned),
        "reasoning_return_rate": len(returned) / len(required) if required else 0,
        "reasoning_complete_count": reasoning_complete_count,
        "reasoning_complete_rate": reasoning_complete_count / len(required) if required else 0,
        "reasoning_token_responses": len(reasoning_token_values),
        "reasoning_tokens_total": sum(reasoning_token_values),
        "min_time_ms": min(times) if times else None,
        "median_time_ms": median(times) if times else None,
        "max_time_ms": max(times) if times else None,
    }


def redact_secret(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]").replace(
            json.dumps(secret, ensure_ascii=True)[1:-1], "[REDACTED]",
        )
    if isinstance(value, list):
        return [redact_secret(item, secret) for item in value]
    if isinstance(value, dict):
        return {redact_secret(key, secret): redact_secret(item, secret) for key, item in value.items()}
    return value


def redact_response(raw: str, secret: str) -> str:
    try:
        body = parse_response(raw.encode("utf-8"))
    except (ValueError, RecursionError):
        return redact_secret(raw, secret)
    redacted = redact_secret(body, secret)
    return raw if redacted == body else json.dumps(redacted, ensure_ascii=False)


app = FastAPI(docs_url=None, redoc_url=None)
app.state.upstream_transport = None


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/questions")
async def get_questions() -> list[Question]:
    return read_questions()


@app.post("/api/extract-channel")
async def extract_channel(body: ExtractRequest) -> dict[str, Any]:
    try:
        return extract_channel_information(body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/test")
async def run_test(req: TestRequest) -> dict[str, Any]:
    results = []
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                 transport=app.state.upstream_transport or guarded_transport()) as client:
        for question in read_questions():
            results.append(await call_non_stream(
                client, req.endpoint, question, req.max_tokens, req.timeout_seconds,
            ))
    endpoint_info = req.endpoint.model_dump(exclude={"api_key"})
    endpoint_info["request_url"] = normalize_url(req.endpoint.base_url, req.endpoint.protocol)
    report = {
        "endpoint": {key: redact_secret(value, req.endpoint.api_key.get_secret_value())
                     for key, value in endpoint_info.items()},
        "settings": {"max_tokens": req.max_tokens, "timeout_seconds": req.timeout_seconds},
        "summary": summarize(results),
        "results": [result.model_dump() for result in results],
        "notes": [
            "正常结束表示答案非空、结束原因正常且没有显式未完成标记；不证明答案正确或内容未被渠道修改。",
            "可见推理可能是摘要。无法仅靠单渠道响应验证模型未公开的内部推理是否完整。",
            "独立推理字段、usage 中的 reasoning_tokens 与答案正文分别展示；工具不替代人工判断正文是否包含推导。",
            "响应正常结束率以全部测试为分母；推理相关比例仅以要求推理的题目为分母，均包含请求失败。",
            "全部请求使用 stream=false；首字节是非流式响应体首字节时间，不是首 token 时间。",
            "延迟统计仅包含有效解析的响应；每题测一次，最小/中位/最大耗时仅供本轮参考。",
            "有效 JSON 中的 Key 回显经解析后脱敏，必要时重新序列化；无效 JSON 或中断片段仅做文本替换，下载前请复核。",
        ],
    }
    return report


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8092, log_level="error")
