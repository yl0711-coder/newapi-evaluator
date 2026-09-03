from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from time import perf_counter
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
QUESTIONS_PATH = ROOT / "questions.json"
WEB_PATH = ROOT / "web"

SYSTEM_PROMPT = "请直接完成任务。答案要清楚、紧凑；推理题给出必要推导，不要重复题目。"
ANTHROPIC_VERSION = "2023-06-01"

MODEL_FAMILIES = [
    {
        "provider": "Codex",
        "protocol": "openai",
        "official_base_url": "",
        "models": [
            ("gpt-5.6-sol", "GPT-5.6 Sol"),
            ("gpt-5.6-terra", "GPT-5.6 Terra"),
        ],
    },
    {
        "provider": "Claude",
        "protocol": "anthropic",
        "official_base_url": "",
        "models": [
            ("claude-fable-5", "Fable 5"),
            ("claude-opus-5", "Opus 5"),
            ("claude-sonnet-5", "Sonnet 5"),
        ],
    },
    {
        "provider": "智谱",
        "protocol": "openai",
        "official_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": [
            ("glm-5.2", "GLM-5.2"),
            ("glm-5.3", "GLM-5.3"),
            ("glm-5.3-flash", "GLM-5.3-Flash"),
        ],
    },
    {
        "provider": "Kimi",
        "protocol": "openai",
        "official_base_url": "https://api.moonshot.cn/v1",
        "models": [("kimi-k3", "Kimi-K3")],
    },
    {
        "provider": "DeepSeek",
        "protocol": "openai",
        "official_base_url": "https://api.deepseek.com",
        "models": [
            ("deepseek-v4-pro", "DeepSeek-V4-Pro"),
            ("deepseek-v4-flash", "DeepSeek-V4-Flash"),
            ("deepseek-v4-flash-0731", "DeepSeek-V4-Flash-0731"),
            ("deepseek-v4-pro-0813", "DeepSeek-V4-Pro-0813"),
        ],
    },
]

PRESETS = [
    {
        "id": model,
        "label": label,
        "provider": family["provider"],
        "protocol": family["protocol"],
        "official_base_url": family["official_base_url"],
        "model": model,
    }
    for family in MODEL_FAMILIES
    for model, label in family["models"]
]


class EndpointConfig(BaseModel):
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    model: str = Field(min_length=1)
    protocol: Literal["openai", "anthropic"]


class CompareRequest(BaseModel):
    candidate: EndpointConfig
    reference: EndpointConfig


class ExtractRequest(BaseModel):
    text: str


CHANNEL_FIELD_ALIASES = {
    "base_url": {"base_url", "baseurl", "api_base", "apibase", "url", "endpoint", "host"},
    "api_key": {"api_key", "apikey", "key", "token", "secret", "authorization"},
}


def load_questions() -> list[dict[str, Any]]:
    with QUESTIONS_PATH.open("r", encoding="utf-8") as file:
        questions = json.load(file)
    if len(questions) != 5:
        raise RuntimeError("题库必须恰好包含 5 道题。")
    return questions


QUESTIONS = load_questions()


def _normalized_http_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("地址必须是完整的 http:// 或 https:// URL。")
    return normalized


def chat_completions_url(base_url: str) -> str:
    normalized = _normalized_http_url(base_url)
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def anthropic_messages_url(base_url: str) -> str:
    normalized = _normalized_http_url(base_url)
    if normalized.endswith("/messages"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/messages"
    return f"{normalized}/v1/messages"


def endpoint_url(config: EndpointConfig) -> str:
    if config.protocol == "anthropic":
        return anthropic_messages_url(config.base_url)
    return chat_completions_url(config.base_url)


def build_headers(config: EndpointConfig) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if config.protocol == "anthropic":
        headers["x-api-key"] = config.api_key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    else:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return headers


def _configuration_objects(value: Any) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    if isinstance(value, dict):
        objects.append(value)
        for child in value.values():
            objects.extend(_configuration_objects(child))
    elif isinstance(value, list):
        for child in value:
            objects.extend(_configuration_objects(child))
    return objects


def _aliased_value(objects: list[dict[str, Any]], field: str) -> str:
    aliases = CHANNEL_FIELD_ALIASES[field]
    for item in objects:
        for key, value in item.items():
            if str(key).lower().replace("-", "_") not in aliases:
                continue
            if isinstance(value, list):
                value = value[0] if value else ""
            if value not in (None, "", [], {}):
                return str(value).strip()
    return ""


def _key_value_object(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for source_line in text.splitlines():
        line = source_line.strip().lstrip("-").strip()
        match = re.match(r"^([\w.-]+)\s*[=:]\s*(.+)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("\"',`")
    return values


def _normalized_channel_url(value: str) -> str:
    url = value.strip().strip("\"',`").rstrip(".,;:!?)")
    if url and not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return re.sub(r"/(chat/completions|responses|messages|models)/?$", "", url.rstrip("/"), flags=re.I)


def extract_channel_credentials(text: str) -> dict[str, Any]:
    raw = text.strip()
    if not raw:
        raise ValueError("请先粘贴渠道连接信息。")

    objects: list[dict[str, Any]] = []
    if raw.startswith(("{", "[")):
        try:
            objects.extend(_configuration_objects(json.loads(raw)))
        except json.JSONDecodeError:
            pass
    objects.append(_key_value_object(raw))

    base_url = _aliased_value(objects, "base_url")
    api_key = _aliased_value(objects, "api_key")

    if not base_url:
        url_match = re.search(r"https?://[^\s'\"<>{}\[\]，。；、]+", raw, re.I)
        base_url = url_match.group(0) if url_match else ""
    if not api_key:
        bearer_match = re.search(r"(?:Authorization:\s*Bearer|x-api-key:)\s*([^\s'\"\\]+)", raw, re.I)
        named_match = re.search(r"(?:api[_-]?key|token|secret)\s*[=:]\s*['\"]?([^\s'\",}]+)", raw, re.I)
        sk_match = re.search(r"(?<![\w.-])(sk-[A-Za-z0-9][A-Za-z0-9._-]*)", raw)
        match = bearer_match or named_match or sk_match
        api_key = match.group(1) if match else ""
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:].strip()

    normalized_url = _normalized_channel_url(base_url) if base_url else ""
    return {
        "base_url": normalized_url,
        "api_key": api_key.strip().strip("\"',`"),
        "has_url": bool(normalized_url),
        "has_key": bool(api_key),
    }


def build_payload(config: EndpointConfig, question: dict[str, Any]) -> dict[str, Any]:
    if config.protocol == "anthropic":
        return {
            "model": config.model.strip(),
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": question["prompt"]}],
            "stream": True,
            "max_tokens": question["max_tokens"],
        }
    return {
        "model": config.model.strip(),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question["prompt"]},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": question["max_tokens"],
    }


def _text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _usage_output_tokens(payload: dict[str, Any]) -> int | None:
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    usage_candidates = [payload.get("usage"), response.get("usage"), message.get("usage")]
    for usage in usage_candidates:
        if not isinstance(usage, dict):
            continue
        for key in ("completion_tokens", "output_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
    return None


def _stream_event(
    *,
    content: str = "",
    reasoning: str = "",
    finish_reason: str = "",
    response_format: str = "unknown",
    recognized: bool = True,
    raw: str = "",
    output_tokens: int | None = None,
) -> dict[str, Any]:
    return {
        "content": content,
        "reasoning": reasoning,
        "finish_reason": finish_reason,
        "format": response_format,
        "recognized": recognized,
        "raw": raw,
        "output_tokens": output_tokens,
    }


def parse_stream_line(line: str) -> dict[str, Any] | None:
    value = line.strip().lstrip("\ufeff")
    if not value or value.startswith(":") or value.startswith("event:"):
        return None
    if value.startswith("data:"):
        value = value[5:].strip()
    if value == "[DONE]":
        return None
    if not value.startswith("{"):
        return _stream_event(recognized=False, raw=value)
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return _stream_event(recognized=False, raw=value)
    if not isinstance(payload, dict):
        return _stream_event(recognized=False, raw=value)
    output_tokens = _usage_output_tokens(payload)
    choices = payload.get("choices") or []
    if choices:
        choice = choices[0]
        delta = choice.get("delta") or {}
        message = choice.get("message") or {}
        content = (
            _text_value(delta.get("content"))
            or _text_value(message.get("content"))
            or _text_value(choice.get("text"))
        )
        reasoning = (
            _text_value(delta.get("reasoning_content"))
            or _text_value(message.get("reasoning_content"))
        )
        response_format = "chat_delta" if choice.get("delta") is not None else "chat_message"
        return _stream_event(
            content=content,
            reasoning=reasoning,
            finish_reason=str(choice.get("finish_reason") or ""),
            response_format=response_format,
            output_tokens=output_tokens,
        )

    event_type = str(payload.get("type") or "")
    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
        return _stream_event(
            content=_text_value(payload.get("delta")),
            response_format="responses",
            output_tokens=output_tokens,
        )
    if event_type in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
        return _stream_event(
            reasoning=_text_value(payload.get("delta")),
            response_format="responses",
            output_tokens=output_tokens,
        )
    if event_type == "content_block_delta":
        delta = payload.get("delta") or {}
        return _stream_event(
            content=_text_value(delta.get("text")),
            reasoning=_text_value(delta.get("thinking")),
            response_format="anthropic",
            output_tokens=output_tokens,
        )
    if event_type == "message_delta":
        delta = payload.get("delta") or {}
        return _stream_event(
            finish_reason=str(delta.get("stop_reason") or ""),
            response_format="anthropic",
            output_tokens=output_tokens,
        )
    if event_type in {"message_start", "message_stop", "content_block_start", "content_block_stop"}:
        content_block = payload.get("content_block") or {}
        return _stream_event(
            content=_text_value(content_block.get("text")),
            reasoning=_text_value(content_block.get("thinking")),
            finish_reason="stop" if event_type == "message_stop" else "",
            response_format="anthropic",
            output_tokens=output_tokens,
        )
    if event_type in {"response.completed", "response.incomplete", "response.failed"}:
        response = payload.get("response") or {}
        incomplete_details = response.get("incomplete_details") or {}
        return _stream_event(
            finish_reason=str(incomplete_details.get("reason") or response.get("status") or ""),
            response_format="responses",
            output_tokens=output_tokens,
        )
    if output_tokens is not None:
        return _stream_event(response_format="usage", output_tokens=output_tokens)
    return _stream_event(recognized=False, raw=value)


def _redact(message: str, api_key: str) -> str:
    cleaned = message.replace(api_key, "[已隐藏]") if api_key else message
    return cleaned[:500]


async def measure_side(
    side: str,
    config: EndpointConfig,
    question: dict[str, Any],
    queue: asyncio.Queue[dict[str, Any]],
    client: httpx.AsyncClient,
) -> None:
    started_at = perf_counter()
    first_chunk_at: float | None = None
    first_answer_at: float | None = None
    last_output_at: float | None = None
    first_wire_event_at: float | None = None
    answer_parts: list[str] = []
    reasoning_parts: list[str] = []
    fallback_parts: list[str] = []
    output_chunk_count = 0
    finish_reason = ""
    reported_output_tokens: int | None = None
    response_formats: set[str] = set()
    try:
        async with client.stream(
            "POST",
            endpoint_url(config),
            headers=build_headers(config),
            json=build_payload(config, question),
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {response.status_code}: {_redact(body, config.api_key)}")
            async for line in response.aiter_lines():
                parsed = parse_stream_line(line)
                if parsed is None:
                    continue
                now = perf_counter()
                if first_wire_event_at is None:
                    first_wire_event_at = now
                if not parsed["recognized"]:
                    if parsed["raw"]:
                        fallback_parts.append(parsed["raw"])
                    continue
                response_formats.add(parsed["format"])
                if parsed["output_tokens"] is not None:
                    reported_output_tokens = parsed["output_tokens"]
                if parsed["finish_reason"]:
                    finish_reason = parsed["finish_reason"]
                content_delta = parsed["content"]
                reasoning_delta = parsed["reasoning"]
                if not content_delta and not reasoning_delta:
                    continue
                if first_chunk_at is None:
                    first_chunk_at = now
                if content_delta and first_answer_at is None:
                    first_answer_at = now
                last_output_at = now
                output_chunk_count += 1
                answer_parts.append(content_delta)
                reasoning_parts.append(reasoning_delta)
                await queue.put(
                    {
                        "type": "chunk",
                        "question_id": question["id"],
                        "side": side,
                        "content": content_delta,
                        "reasoning": reasoning_delta,
                    }
                )

        finished_at = perf_counter()
        answer = "".join(answer_parts)
        reasoning = "".join(reasoning_parts)
        recognized_chars = len(answer) + len(reasoning)
        fallback_content = "\n".join(fallback_parts) if recognized_chars == 0 else ""
        if fallback_content:
            await queue.put(
                {
                    "type": "chunk",
                    "question_id": question["id"],
                    "side": side,
                    "content": fallback_content,
                    "reasoning": "",
                    "raw": True,
                }
            )
        displayed_chars = recognized_chars + len(fallback_content)
        first_visible_at = first_chunk_at or (first_wire_event_at if fallback_content else None)
        ttft_ms = round((first_visible_at - started_at) * 1000) if first_visible_at is not None else None
        first_answer_ms = round((first_answer_at - started_at) * 1000) if first_answer_at is not None else None
        total_ms = round((finished_at - started_at) * 1000)
        generation_seconds = (
            last_output_at - first_chunk_at
            if first_chunk_at is not None and last_output_at is not None
            else 0
        )
        chars_per_second = None
        if output_chunk_count > 1 and generation_seconds > 0:
            chars_per_second = round(recognized_chars / generation_seconds, 1)
        tokens_per_second = None
        if (
            reported_output_tokens is not None
            and reported_output_tokens > 1
            and output_chunk_count > 1
            and generation_seconds > 0
        ):
            tokens_per_second = round((reported_output_tokens - 1) / generation_seconds, 1)

        normalized_finish_reason = finish_reason.lower()
        truncated_reasons = {"length", "max_tokens", "max_output_tokens", "incomplete"}
        if normalized_finish_reason in truncated_reasons:
            ok = False
            status = "truncated"
            error = "达到上游输出上限，回答可能不完整。"
        elif fallback_content:
            ok = False
            status = "unrecognized"
            error = "流式格式暂不兼容；已在回答区原样显示上游响应。"
        elif recognized_chars == 0:
            ok = False
            status = "empty"
            error = "上游已结束响应，但没有返回可读取的文本。"
        else:
            ok = True
            status = "completed"
            error = ""

        await queue.put(
            {
                "type": "side_finished",
                "question_id": question["id"],
                "side": side,
                "ok": ok,
                "status": status,
                "ttft_ms": ttft_ms,
                "first_answer_ms": first_answer_ms,
                "total_ms": total_ms,
                "chars": displayed_chars,
                "chars_per_second": chars_per_second,
                "output_tokens": reported_output_tokens,
                "tokens_per_second": tokens_per_second,
                "finish_reason": finish_reason,
                "response_format": ",".join(sorted(response_formats)),
                **({"error": error} if error else {}),
            }
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await queue.put(
            {
                "type": "side_finished",
                "question_id": question["id"],
                "side": side,
                "ok": False,
                "status": "error",
                "error": _redact(str(exc), config.api_key),
            }
        )


async def run_question(
    body: CompareRequest,
    question: dict[str, Any],
    client: httpx.AsyncClient,
) -> AsyncIterator[dict[str, Any]]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks = [
        asyncio.create_task(measure_side("candidate", body.candidate, question, queue, client)),
        asyncio.create_task(measure_side("reference", body.reference, question, queue, client)),
    ]
    finished = 0
    try:
        while finished < 2:
            event = await queue.get()
            if event["type"] == "side_finished":
                finished += 1
            yield event
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def encode_event(event: dict[str, Any]) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")


app = FastAPI(title="模型渠道速度快测", docs_url=None, redoc_url=None)


@app.get("/api/meta")
async def meta() -> JSONResponse:
    public_questions = [
        {
            "id": question["id"],
            "title": question["title"],
            "difficulty": question["difficulty"],
            "prompt": question["prompt"],
        }
        for question in QUESTIONS
    ]
    return JSONResponse({"questions": public_questions, "presets": PRESETS})


@app.post("/api/extract-channel")
async def extract_channel(body: ExtractRequest) -> JSONResponse:
    try:
        extracted = extract_channel_credentials(body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(extracted)


@app.post("/api/compare")
async def compare(body: CompareRequest, request: Request) -> StreamingResponse:
    async def stream() -> AsyncIterator[bytes]:
        yield encode_event({"type": "run_started", "question_count": len(QUESTIONS)})
        timeout = httpx.Timeout(connect=20.0, read=240.0, write=20.0, pool=20.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for index, question in enumerate(QUESTIONS, start=1):
                if await request.is_disconnected():
                    return
                yield encode_event(
                    {
                        "type": "question_started",
                        "question_id": question["id"],
                        "index": index,
                    }
                )
                async for event in run_question(body, question, client):
                    yield encode_event(event)
                yield encode_event(
                    {
                        "type": "question_finished",
                        "question_id": question["id"],
                        "index": index,
                    }
                )
        yield encode_event({"type": "run_finished"})

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/tokens.css", include_in_schema=False)
async def tokens() -> FileResponse:
    return FileResponse(ROOT / "tokens.css", media_type="text/css")


app.mount("/", StaticFiles(directory=WEB_PATH, html=True), name="web")
