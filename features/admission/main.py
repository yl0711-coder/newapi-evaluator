from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from time import perf_counter, time
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator


ROOT = Path(__file__).resolve().parent
QUESTIONS_PATH = ROOT / "questions.json"
WEB_PATH = ROOT / "web"

SYSTEM_PROMPT = "请直接完成任务。答案要清楚、紧凑；推理题给出必要推导，不要重复题目。"
ANTHROPIC_VERSION = "2023-06-01"
INTER_QUESTION_DELAY_SECONDS = 3
GPT6_MODEL = "gpt-6-astra"
GPT6_OUTPUT_LIMIT = 4096
GPT6_REASONING_EFFORT = "low"
RESPONSES_TOTAL_TIMEOUT_SECONDS = 240
AdmissionProtocol = Literal["openai", "responses", "anthropic"]


def required_protocol(model: str) -> str | None:
    return "responses" if model.strip() == GPT6_MODEL else None

MODEL_FAMILIES = [
    {
        "provider": "Codex",
        "protocol": "responses",
        "official_base_url": "",
        "models": [(GPT6_MODEL, "GPT-6 Astra")],
    },
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
        "required_protocol": required_protocol(model),
        **({"max_output_tokens": GPT6_OUTPUT_LIMIT, "reasoning_effort": GPT6_REASONING_EFFORT}
           if model == GPT6_MODEL else {}),
    }
    for family in MODEL_FAMILIES
    for model, label in family["models"]
]


class EndpointConfig(BaseModel):
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    model: str = Field(min_length=1)
    protocol: AdmissionProtocol

    @model_validator(mode="after")
    def enforce_model_protocol(self):
        self.model = self.model.strip()
        self.protocol = required_protocol(self.model) or self.protocol
        return self


class CompareRequest(BaseModel):
    candidate: EndpointConfig
    reference: EndpointConfig
    rounds: int = Field(default=2, ge=1, le=5)


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


def select_question_variant(question: dict[str, Any], seed: str) -> dict[str, Any]:
    choices: list[tuple[str, str]] = [("original", str(question["prompt"]))]
    for index, variant in enumerate(question.get("variants") or [], 1):
        if isinstance(variant, str) and variant.strip():
            choices.append((f"variant-{index}", variant))
        elif isinstance(variant, dict) and str(variant.get("prompt") or "").strip():
            choices.append((str(variant.get("id") or f"variant-{index}"), str(variant["prompt"])))
    digest = hashlib.sha256(f"{seed}:{question['id']}".encode()).digest()
    variant_id, prompt = choices[int.from_bytes(digest[:4], "big") % len(choices)]
    return {**question, "prompt": prompt, "variant_id": variant_id}


def _normalized_http_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("地址必须是完整的 http:// 或 https:// URL。")
    return normalized


def openai_api_base_url(base_url: str) -> str:
    normalized = _normalized_http_url(base_url)
    parsed = urlparse(normalized)
    if parsed.path not in {"", "/"}:
        return normalized
    if parsed.hostname == "api.deepseek.com":
        return normalized
    if parsed.hostname == "open.bigmodel.cn":
        return f"{normalized}/api/paas/v4"
    return f"{normalized}/v1"


def chat_completions_url(base_url: str) -> str:
    normalized = openai_api_base_url(base_url)
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
    if config.protocol == "responses":
        base = re.sub(r"/(chat/completions|responses|messages)/?$", "", config.base_url.strip().rstrip("/"))
        return f"{openai_api_base_url(base)}/responses"
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
    if config.protocol == "responses":
        payload = {
            "model": config.model,
            "instructions": SYSTEM_PROMPT,
            "input": question["prompt"],
            "stream": True,
            "store": False,
            "max_output_tokens": question["max_tokens"],
        }
        if config.model == GPT6_MODEL:
            payload["max_output_tokens"] = max(GPT6_OUTPUT_LIMIT, question["max_tokens"])
            payload["reasoning"] = {"effort": GPT6_REASONING_EFFORT}
        return payload
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
    actual_model: str = "",
    upstream_request_id: str = "",
    system_fingerprint: str = "",
    response_status: str = "",
    protocol_error: str = "",
    refused: bool = False,
    final_content: str | None = None,
    block_content: str | None = None,
    block_key: tuple[int, int] | None = None,
    reasoning_tokens: int | None = None,
    input_tokens: int | None = None,
) -> dict[str, Any]:
    return {
        "content": content,
        "reasoning": reasoning,
        "finish_reason": finish_reason,
        "format": response_format,
        "recognized": recognized,
        "raw": raw,
        "output_tokens": output_tokens,
        "actual_model": actual_model,
        "upstream_request_id": upstream_request_id,
        "system_fingerprint": system_fingerprint,
        "response_status": response_status,
        "protocol_error": protocol_error,
        "refused": refused,
        "final_content": final_content,
        "block_content": block_content,
        "block_key": block_key,
        "reasoning_tokens": reasoning_tokens,
        "input_tokens": input_tokens,
    }


def _response_metadata(payload: dict[str, Any]) -> dict[str, str]:
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    return {
        "actual_model": str(payload.get("model") or response.get("model") or message.get("model") or ""),
        "upstream_request_id": str(payload.get("id") or response.get("id") or message.get("id") or ""),
        "system_fingerprint": str(
            payload.get("system_fingerprint") or response.get("system_fingerprint") or ""
        ),
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
    metadata = _response_metadata(payload)
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
            **metadata,
        )

    event_type = str(payload.get("type") or "")
    if event_type in {"response.output_text.delta", "response.refusal.delta", "response.reasoning_text.delta",
                      "response.reasoning_summary_text.delta"} and not isinstance(payload.get("delta"), str):
        return _stream_event(response_format="responses", protocol_error="protocol_error")
    block_key = None
    if event_type in {"response.output_text.delta", "response.refusal.delta", "response.output_text.done", "response.refusal.done"}:
        block_key = (payload.get("output_index", 0), payload.get("content_index", 0))
        if any(type(index) is not int or index < 0 for index in block_key):
            return _stream_event(response_format="responses", protocol_error="protocol_error")
    if event_type == "error":
        return _stream_event(response_format="responses", protocol_error="upstream_error", **metadata)
    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
        return _stream_event(
            content=_text_value(payload.get("delta")),
            refused=event_type == "response.refusal.delta",
            block_key=block_key,
            response_format="responses",
            output_tokens=output_tokens,
            **metadata,
        )
    if event_type in {"response.output_text.done", "response.refusal.done"}:
        content = payload.get("text" if event_type == "response.output_text.done" else "refusal")
        if not isinstance(content, str):
            return _stream_event(response_format="responses", protocol_error="protocol_error")
        return _stream_event(response_format="responses", block_content=content, block_key=block_key,
                             refused=event_type == "response.refusal.done", **metadata)
    if event_type in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
        return _stream_event(
            reasoning=_text_value(payload.get("delta")),
            response_format="responses",
            output_tokens=output_tokens,
            **metadata,
        )
    if event_type == "content_block_delta":
        delta = payload.get("delta") or {}
        return _stream_event(
            content=_text_value(delta.get("text")),
            reasoning=_text_value(delta.get("thinking")),
            response_format="anthropic",
            output_tokens=output_tokens,
            **metadata,
        )
    if event_type == "message_delta":
        delta = payload.get("delta") or {}
        return _stream_event(
            finish_reason=str(delta.get("stop_reason") or ""),
            response_format="anthropic",
            output_tokens=output_tokens,
            **metadata,
        )
    if event_type in {"message_start", "message_stop", "content_block_start", "content_block_stop"}:
        content_block = payload.get("content_block") or {}
        return _stream_event(
            content=_text_value(content_block.get("text")),
            reasoning=_text_value(content_block.get("thinking")),
            finish_reason="stop" if event_type == "message_stop" else "",
            response_format="anthropic",
            output_tokens=output_tokens,
            **metadata,
        )
    if event_type in {"response.completed", "response.incomplete", "response.failed"}:
        response = payload.get("response")
        if not isinstance(response, dict):
            return _stream_event(response_format="responses", protocol_error="protocol_error")
        if any(value is not None and not isinstance(value, dict)
               for value in (response.get("usage"), response.get("incomplete_details"))):
            return _stream_event(response_format="responses", protocol_error="protocol_error")
        incomplete_details = response.get("incomplete_details") or {}
        status = str(response.get("status") or "")
        usage = response.get("usage") or {}
        details = usage.get("output_tokens_details") or {}
        token_count = lambda value: value if type(value) is int and value >= 0 else None
        content = []
        refused = False
        if response.get("output") is not None and not isinstance(response["output"], list):
            return _stream_event(response_format="responses", protocol_error="protocol_error")
        for item in response.get("output") or []:
            if item.get("type") == "message":
                if not isinstance(item.get("content"), list):
                    return _stream_event(response_format="responses", protocol_error="protocol_error")
                for part in item.get("content") or []:
                    field = {"output_text": "text", "refusal": "refusal"}.get(part.get("type"))
                    if field and not isinstance(part.get(field), str):
                        return _stream_event(response_format="responses", protocol_error="protocol_error")
                    if part.get("type") == "output_text":
                        content.append(_text_value(part.get("text")))
                    if part.get("type") == "refusal":
                        content.append(_text_value(part.get("refusal")))
                    refused = refused or part.get("type") == "refusal"
        return _stream_event(
            finish_reason=str(incomplete_details.get("reason") or status),
            response_format="responses",
            output_tokens=output_tokens,
            response_status=status,
            protocol_error=("protocol_error" if status != event_type.removeprefix("response.") else
                            "upstream_error" if response.get("error") or status == "failed" else ""),
            final_content="".join(content) if isinstance(response.get("output"), list) else None,
            refused=refused,
            input_tokens=token_count(usage.get("input_tokens")),
            reasoning_tokens=token_count(details.get("reasoning_tokens")),
            **metadata,
        )
    if event_type.startswith("response."):
        return _stream_event(response_format="responses", **metadata)
    if output_tokens is not None:
        return _stream_event(response_format="usage", output_tokens=output_tokens, **metadata)
    return _stream_event(recognized=False, raw=value)


async def stream_events(response: httpx.Response, protocol: str) -> AsyncIterator[dict[str, Any]]:
    if protocol != "responses":
        async for line in response.aiter_lines():
            parsed = parse_stream_line(line)
            if parsed is not None:
                yield parsed
        return
    data: list[str] = []
    size = 0
    async for line in response.aiter_lines():
        line = line.lstrip("\ufeff")
        if not line:
            if data:
                if "\n".join(data) == "[DONE]":
                    data, size = [], 0
                    continue
                try:
                    parsed = parse_stream_line("\n".join(data))
                except (AttributeError, TypeError, KeyError, IndexError):
                    parsed = None
                if parsed is None or parsed["format"] != "responses":
                    yield _stream_event(response_format="responses", protocol_error="protocol_error")
                else:
                    yield parsed
            data, size = [], 0
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
            size += len(line)
            if size > 1_048_576:
                raise ValueError("Responses 流事件超过本地读取上限")
        elif not line.startswith((":", "event:", "id:", "retry:")):
            yield _stream_event(response_format="responses", protocol_error="protocol_error")
    if data:
        yield _stream_event(response_format="responses", protocol_error="protocol_error")


def _redact(message: str, api_key: str) -> str:
    cleaned = message.replace(api_key, "[已隐藏]") if api_key else message
    return cleaned[:500]


def _exception_category(exc: Exception, had_output: bool) -> str:
    if had_output:
        return "stream_interrupted"
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, httpx.PoolTimeout):
        return "pool_timeout"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connection_failed"
    match = re.search(r"HTTP\s+(\d{3})", str(exc))
    if match:
        status = int(match.group(1))
        if status == 429:
            return "http_429"
        if status >= 500:
            return "http_5xx"
        return f"http_{status}"
    return "transport_error"


async def measure_side(
    side: str,
    config: EndpointConfig,
    question: dict[str, Any],
    queue: asyncio.Queue[dict[str, Any]],
    client: httpx.AsyncClient,
    start_gate: asyncio.Event | None = None,
) -> None:
    if start_gate is not None:
        await start_gate.wait()
    if config.protocol != "responses":
        await _measure_side(side, config, question, queue, client)
        return
    started_ms = round(time() * 1000)
    try:
        await asyncio.wait_for(_measure_side(side, config, question, queue, client),
                               timeout=RESPONSES_TOTAL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        payload = build_payload(config, question)
        await queue.put({"type": "side_finished", "question_id": question["id"], "side": side,
                         "ok": False, "status": "error", "transport_completed": False,
                         "request_started_ms": started_ms, "request_protocol": config.protocol,
                         "max_output_tokens": payload["max_output_tokens"],
                         "reasoning_effort": payload.get("reasoning", {}).get("effort"),
                         "error_category": "total_timeout", "error": "Responses 请求超过总时限。"})


async def _measure_side(
    side: str,
    config: EndpointConfig,
    question: dict[str, Any],
    queue: asyncio.Queue[dict[str, Any]],
    client: httpx.AsyncClient,
) -> None:
    started_at = perf_counter()
    request_started_ms = round(time() * 1000)
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
    actual_models: set[str] = set()
    upstream_request_ids: set[str] = set()
    system_fingerprints: set[str] = set()
    maximum_output_pause_ms: int | None = None
    response_status = protocol_error = ""
    refused = False
    reasoning_tokens = input_tokens = None
    text_blocks: dict[tuple[int, int], str] = {}
    text_recovered = False
    payload = build_payload(config, question)
    request_metrics = ({"request_protocol": config.protocol,
                        "max_output_tokens": payload["max_output_tokens"],
                        "reasoning_effort": payload.get("reasoning", {}).get("effort")}
                       if config.protocol == "responses" else {})
    try:
        async with client.stream(
            "POST",
            endpoint_url(config),
            headers=build_headers(config),
            json=payload,
        ) as response:
            header_request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
            if header_request_id:
                upstream_request_ids.add(header_request_id)
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {response.status_code}: {_redact(body, config.api_key)}")
            async for parsed in stream_events(response, config.protocol):
                now = perf_counter()
                if first_wire_event_at is None:
                    first_wire_event_at = now
                if not parsed["recognized"]:
                    if parsed["raw"]:
                        fallback_parts.append(parsed["raw"])
                    continue
                response_formats.add(parsed["format"])
                if parsed["actual_model"]:
                    actual_models.add(parsed["actual_model"])
                if parsed["upstream_request_id"]:
                    upstream_request_ids.add(parsed["upstream_request_id"])
                if parsed["system_fingerprint"]:
                    system_fingerprints.add(parsed["system_fingerprint"])
                if parsed["output_tokens"] is not None:
                    reported_output_tokens = parsed["output_tokens"]
                if parsed["finish_reason"]:
                    finish_reason = parsed["finish_reason"]
                if config.protocol == "responses" and response_status and (parsed["content"] or parsed["reasoning"] or parsed["block_content"] is not None):
                    protocol_error = "protocol_error"
                if parsed["response_status"]:
                    if response_status:
                        protocol_error = "protocol_error"
                    response_status = parsed["response_status"]
                protocol_error = protocol_error or parsed["protocol_error"]
                refused = refused or parsed["refused"]
                if parsed["reasoning_tokens"] is not None:
                    reasoning_tokens = parsed["reasoning_tokens"]
                if parsed["input_tokens"] is not None:
                    input_tokens = parsed["input_tokens"]
                content_delta = parsed["content"]
                reasoning_delta = parsed["reasoning"]
                if config.protocol == "responses":
                    block_key = parsed["block_key"]
                    if block_key is not None:
                        previous = text_blocks.get(block_key, "")
                        if parsed["block_content"] is not None:
                            complete = parsed["block_content"]
                            if complete.startswith(previous):
                                content_delta = complete[len(previous):]
                                text_recovered = text_recovered or bool(content_delta)
                            else:
                                protocol_error = "protocol_error"
                        text_blocks[block_key] = previous + content_delta
                    if parsed["final_content"] is not None:
                        previous = "".join(answer_parts)
                        complete = parsed["final_content"]
                        if complete.startswith(previous):
                            content_delta = complete[len(previous):]
                            text_recovered = text_recovered or bool(content_delta)
                        else:
                            protocol_error = "protocol_error"
                if not content_delta and not reasoning_delta:
                    continue
                if first_chunk_at is None:
                    first_chunk_at = now
                if last_output_at is not None:
                    pause_ms = round((now - last_output_at) * 1000)
                    maximum_output_pause_ms = max(maximum_output_pause_ms or 0, pause_ms)
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
        if config.protocol == "responses" and text_recovered:
            tokens_per_second = None
            speed_data_valid = False
            speed_invalid_reason = "正文由结束事件补全，无法计算流式 Token/s"
        elif config.protocol == "responses" and reasoning_tokens != 0:
            tokens_per_second = None
            speed_data_valid = False
            speed_invalid_reason = "输出 Token 含推理或缺少推理明细，无法计算正文 Token/s"
        elif tokens_per_second is not None:
            speed_data_valid = True
            speed_invalid_reason = ""
        elif reported_output_tokens is None:
            speed_data_valid = False
            speed_invalid_reason = "上游未提供输出 Token 数"
        elif output_chunk_count <= 1:
            speed_data_valid = False
            speed_invalid_reason = "流式粒度不足"
        else:
            speed_data_valid = False
            speed_invalid_reason = "有效输出时长不足"

        normalized_finish_reason = finish_reason.lower()
        truncated_reasons = {"length", "max_tokens", "max_output_tokens", "incomplete"}
        if config.protocol == "responses" and protocol_error:
            ok, status, error = False, protocol_error, "上游失败或 Responses 流事件不符合协议。"
        elif config.protocol == "responses" and response_status not in {"completed", "incomplete"}:
            ok, status, error = False, "incomplete_stream", "未收到完整的 Responses 结束事件。"
        elif config.protocol == "responses" and (refused or normalized_finish_reason == "content_filter"):
            ok, status, error = False, "refused", "上游拒绝回答或内容过滤中止。"
        elif normalized_finish_reason in truncated_reasons:
            ok = False
            status = "truncated"
            error = "达到上游输出上限，回答可能不完整。"
        elif config.protocol == "responses" and response_status == "incomplete":
            ok, status, error = False, "incomplete_response", "上游标记回答未完成。"
        elif config.protocol == "responses" and not answer.strip():
            ok, status, error = False, "empty", "上游未返回答案正文。"
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
                "transport_completed": True,
                "answer_reviewed": False,
                "answer_equivalent": None,
                "request_started_ms": request_started_ms,
                "ttft_ms": ttft_ms,
                "first_answer_ms": first_answer_ms,
                "total_ms": total_ms,
                "chars": displayed_chars,
                "chars_per_second": chars_per_second,
                "output_tokens": reported_output_tokens,
                **request_metrics,
                **({"input_tokens": input_tokens, "reasoning_tokens": reasoning_tokens,
                    "response_status": response_status,
                    "text_recovered_from_completion": text_recovered,
                    "protocol_completed": response_status == "completed" and not protocol_error}
                   if config.protocol == "responses" else {}),
                "tokens_per_second": tokens_per_second,
                "speed_data_valid": speed_data_valid,
                "speed_invalid_reason": speed_invalid_reason,
                "maximum_output_pause_ms": maximum_output_pause_ms,
                "finish_reason": finish_reason,
                "response_format": ",".join(sorted(response_formats)),
                "actual_model": sorted(actual_models)[0] if len(actual_models) == 1 else "",
                "actual_model_values": sorted(actual_models),
                "upstream_request_id": sorted(upstream_request_ids)[0] if len(upstream_request_ids) == 1 else "",
                "system_fingerprint": sorted(system_fingerprints)[0] if len(system_fingerprints) == 1 else "",
                "error_category": status if not ok else "",
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
                "transport_completed": False,
                "answer_reviewed": False,
                "answer_equivalent": None,
                "request_started_ms": request_started_ms,
                **request_metrics,
                "error_category": _exception_category(exc, output_chunk_count > 0),
                "error": _redact(str(exc), config.api_key),
            }
        )


async def run_question(
    body: CompareRequest,
    question: dict[str, Any],
    client: httpx.AsyncClient,
) -> AsyncIterator[dict[str, Any]]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    start_gate = asyncio.Event()
    tasks = [
        asyncio.create_task(measure_side("candidate", body.candidate, question, queue, client, start_gate)),
        asyncio.create_task(measure_side("reference", body.reference, question, queue, client, start_gate)),
    ]
    await asyncio.sleep(0)
    start_gate.set()
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


async def wait_between_questions() -> None:
    await asyncio.sleep(INTER_QUESTION_DELAY_SECONDS)


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
        total_question_runs = len(QUESTIONS) * body.rounds
        yield encode_event(
            {
                "type": "run_started",
                "question_count": len(QUESTIONS),
                "rounds": body.rounds,
                "total_question_runs": total_question_runs,
            }
        )
        timeout = httpx.Timeout(connect=20.0, read=240.0, write=20.0, pool=20.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for round_number in range(1, body.rounds + 1):
                yield encode_event({"type": "round_started", "round": round_number, "rounds": body.rounds})
                for index, question in enumerate(QUESTIONS, start=1):
                    if await request.is_disconnected():
                        return
                    completed_question_runs = (round_number - 1) * len(QUESTIONS) + index
                    yield encode_event(
                        {
                            "type": "question_started",
                            "question_id": question["id"],
                            "index": index,
                            "round": round_number,
                            "rounds": body.rounds,
                        }
                    )
                    async for event in run_question(body, question, client):
                        yield encode_event({**event, "round": round_number})
                    yield encode_event(
                        {
                            "type": "question_finished",
                            "question_id": question["id"],
                            "index": index,
                            "round": round_number,
                            "rounds": body.rounds,
                            "completed_question_runs": completed_question_runs,
                            "total_question_runs": total_question_runs,
                        }
                    )
                    if completed_question_runs < total_question_runs:
                        yield encode_event(
                            {
                                "type": "question_cooldown",
                                "seconds": INTER_QUESTION_DELAY_SECONDS,
                                "round": round_number,
                                "rounds": body.rounds,
                            }
                        )
                        await wait_between_questions()
                yield encode_event({"type": "round_finished", "round": round_number, "rounds": body.rounds})
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
