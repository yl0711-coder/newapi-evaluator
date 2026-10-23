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

from shared.redaction import scrub


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
RAW_RESPONSE_MAX_BYTES = 16_384
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
    if len(questions) != 6:
        raise RuntimeError("题库必须恰好包含 6 道题。")
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
    upstream_error_code: str = "",
    upstream_error_message: str = "",
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
        "upstream_error_code": upstream_error_code,
        "upstream_error_message": upstream_error_message,
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


def _upstream_error(payload: dict[str, Any], *, allow_top_level=False) -> dict[str, str]:
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    error = response.get("error") or payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        if code is None:
            code = error.get("type")
        message = error.get("message")
    elif isinstance(error, str):
        code, message = payload.get("code"), error
    elif payload.get("type") == "error" or allow_top_level:
        code, message = payload.get("code"), payload.get("message")
    else:
        code = message = None
    return {"upstream_error_code": str(code) if code is not None else "",
            "upstream_error_message": str(message) if message is not None else ""}


def parse_stream_line(line: str, protocol: str | None = None) -> dict[str, Any] | None:
    value = line.strip().lstrip("\ufeff")
    if not value or value.startswith(":") or value in {"event", "id", "retry"} or value.startswith(("event:", "id:", "retry:")):
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
    if payload.get("type") == "error" or payload.get("error"):
        return _stream_event(response_format={"openai": "chat_delta", "anthropic": "anthropic", "responses": "responses"}.get(protocol, "error"), protocol_error="upstream_error",
                             **_upstream_error(payload), **metadata)
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
            **_upstream_error(payload),
            **metadata,
        )
    if event_type.startswith("response."):
        return _stream_event(response_format="responses", **metadata)
    if output_tokens is not None:
        return _stream_event(response_format="usage", output_tokens=output_tokens, **metadata)
    return _stream_event(recognized=False, raw=value)


def _utf8_prefix(value: str, limit: int) -> str:
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _evidence_text(value: str) -> str:
    # A JSON escape may decode to a lone surrogate. Keep that escape visible
    # without turning an upstream HTTP error into a local UTF-8 failure.
    return value.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _evidence_bytes(value: str) -> int:
    # Reports are JSON: escaped controls must count toward the storage budget.
    return len(json.dumps(value, ensure_ascii=False)[1:-1].encode("utf-8"))


def _evidence_prefix(value: str, limit: int) -> str:
    candidate = _utf8_prefix(value, limit)
    if _evidence_bytes(candidate) <= limit:
        return candidate
    low, high = 0, len(candidate)
    while low < high:
        middle = (low + high + 1) // 2
        if _evidence_bytes(candidate[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return candidate[:low]


def _evidence_suffix(value: str, limit: int) -> str:
    candidate = value.encode("utf-8")[-limit:].decode("utf-8", errors="ignore") if limit else ""
    return _evidence_prefix(candidate[::-1], limit)[::-1]


def _json_string_spans(value):
    """Locate decoded JSON strings while keeping their original wire spans."""
    decoder, spans = json.JSONDecoder(), []

    def whitespace(index):
        while index < len(value) and value[index] in " \t\r\n":
            index += 1
        return index

    def visit(index, path):
        index = whitespace(index)
        if value[index] == "{":
            index = whitespace(index + 1)
            while value[index] != "}":
                key_start = index
                key, index = decoder.raw_decode(value, index)
                spans.append((None, key_start, index, key))
                index = visit(whitespace(index) + 1, (*path, key))
                index = whitespace(index)
                if value[index] == ",":
                    index = whitespace(index + 1)
                else:
                    break
            return index + 1
        if value[index] == "[":
            index, number = whitespace(index + 1), 0
            while value[index] != "]":
                index = whitespace(visit(index, (*path, number)))
                number += 1
                if value[index] == ",":
                    index = whitespace(index + 1)
                else:
                    break
            return index + 1
        start = index
        item, index = decoder.raw_decode(value, index)
        if isinstance(item, str):
            spans.append((path, start, index, item))
        return index

    payload = json.loads(value)
    visit(0, ())
    return payload, spans


def _logical_text_channel(payload, path):
    if path is None or not isinstance(payload, dict):
        return None

    def text_leaf(prefix):
        if path[:len(prefix)] != prefix:
            return False
        tail = path[len(prefix):]
        # These are exactly the string/list/text shapes accepted by _text_value.
        return not tail or (type(tail[0]) is int and (len(tail) == 1 or tail[1:] == ("text",)))

    for root in (("choices", 0, "delta"), ("choices", 0, "message")):
        for field in ("content", "reasoning_content"):
            if text_leaf((*root, field)):
                return "reasoning" if field == "reasoning_content" else "content"
    if text_leaf(("choices", 0, "text")):
        return "content"
    event_type = payload.get("type")
    if path == ("delta",):
        if event_type in {"response.output_text.delta", "response.refusal.delta"}:
            return "content"
        if event_type in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
            return "reasoning"
    if text_leaf(("delta", "text")) or text_leaf(("content_block", "text")):
        return "content"
    if text_leaf(("delta", "thinking")) or text_leaf(("content_block", "thinking")):
        return "reasoning"
    # Buffered completion text is a separate copy, not another delta. Its
    # adjacent output parts still need the same credential protection.
    if path[:2] == ("response", "output") and path[-1:] in {("text",), ("refusal",)}:
        return "final_content"
    return None


class _ProtocolRawRedactor:
    """Delay only unresolved credential prefixes before publishing raw lines."""

    def __init__(self, sink, forms):
        self.sink, self.forms = sink, forms
        self.line = self.frame = ""
        self.records, self.pending = [], {}
        self.buffered_bytes = self.serial = 0

    @staticmethod
    def _mask(slot, start, end):
        if start < end:
            slot["masks"].append((start, end))

    def _match(self, slot, channel):
        previous = self.pending.get(channel, []) if channel else []
        prefix = "".join(item[2] for item in previous)
        combined = prefix + slot["value"]
        for secret in self.forms:
            index = combined.find(secret)
            while index >= 0:
                end = index + len(secret)
                for old_slot, position, _ in previous[index:min(end, len(prefix))]:
                    self._mask(old_slot, position, position + 1)
                self._mask(slot, max(0, index - len(prefix)), max(0, end - len(prefix)))
                index = combined.find(secret, index + 1)
        if channel:
            keep = 0
            for secret in self.forms:
                for size in range(min(len(secret) - 1, len(combined)), keep, -1):
                    if combined.endswith(secret[:size]):
                        keep = size
                        break
            if keep:
                start = max(0, len(slot["value"]) - keep)
                tail = [(slot, index, slot["value"][index]) for index in range(start, len(slot["value"]))]
                self.pending[channel] = (previous + tail)[-keep:]
            else:
                self.pending.pop(channel, None)

    @staticmethod
    def _render(record):
        value = record["raw"]
        for slot in reversed(record["slots"]):
            if not slot["masks"]:
                continue
            # Replace only the wire characters covered by a credential. A
            # decoded/re-serialized string would also rewrite harmless escapes
            # and could introduce an unencodable lone surrogate.
            token = record["raw"][slot["start"]:slot["end"]]
            boundaries, index = [1], 1
            while index < len(token) and token[index] != '"':
                if token[index:index + 2] == "\\u":
                    code = int(token[index + 2:index + 6], 16)
                    end = index + 6
                    if (0xD800 <= code <= 0xDBFF and token[end:end + 2] == "\\u"
                            and 0xDC00 <= int(token[end + 2:end + 6], 16) <= 0xDFFF):
                        end += 6
                    index = end
                else:
                    index += 2 if token[index] == "\\" else 1
                boundaries.append(index)
            parts, cursor = [], 0
            for start, end in sorted(slot["masks"]):
                start, end = boundaries[start], boundaries[end]
                if end <= cursor:
                    continue
                if start > cursor:
                    parts.append(token[cursor:start])
                    parts.append("[REDACTED]")
                elif not parts or cursor == 0:
                    parts.append("[REDACTED]")
                cursor = end
            parts.append(token[cursor:])
            value = value[:slot["start"]] + "".join(parts) + value[slot["end"]:]
        return value

    def _hide_pending(self):
        for refs in self.pending.values():
            for slot, index, _ in refs:
                self._mask(slot, index, index + 1)
        self.pending.clear()

    async def _flush(self):
        held = {slot["record"] for refs in self.pending.values() for slot, _, _ in refs}
        while self.records and self.records[0]["id"] not in held:
            record = self.records.pop(0)
            self.buffered_bytes -= len(record["raw"].encode("utf-8"))
            await self.sink(self._render(record))

    async def _record(self, raw):
        segments, pieces, wire_index, text_index = [], [], 0, 0
        # Python splitlines also splits U+0085/U+2028/U+2029 inside valid JSON
        # strings. The SSE wire only recognizes CR and LF as line endings.
        for line in re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", raw):
            offset = len(line) - len(line.lstrip("\ufeff"))
            if line[offset:].startswith("data:"):
                offset += 5
                piece = line[offset:].rstrip("\r\n")
                segments.append((text_index, text_index + len(piece), wire_index + offset))
                pieces.append(piece)
                text_index += len(piece) + 1
            wire_index += len(line)
        text = "\n".join(pieces) if pieces else raw

        def wire_position(index):
            if not segments:
                return index
            for start, end, wire_start in segments:
                if start <= index <= end:
                    return wire_start + index - start
            raise ValueError("JSON string crosses an SSE data boundary")

        unparsed_payload = False
        try:
            payload, spans = _json_string_spans(text)
        except (ValueError, TypeError, IndexError, RecursionError):
            payload, spans = None, []
            stripped = text.strip()
            unparsed_payload = bool(stripped and stripped != "[DONE]"
                                    and (pieces or stripped.startswith(("{", "[", '"'))))
            if unparsed_payload:
                # An incomplete JSON tail has no trustworthy logical path.
                # It may continue any held delta, so hide those prefixes before
                # releasing them rather than forgetting their association.
                self._hide_pending()
            # Pretty printed error JSON can span several physical lines.
            # Complete JSON string literals are safe to decode independently.
            for match in re.finditer(r'"(?:[^"\\]|\\.)*(?:"|$)', text):
                try:
                    spans.append((None, match.start(), match.end(), json.loads(match.group())))
                except ValueError:
                    # A cancelled/malformed body may end inside a string or
                    # escape. Decode its valid prefix without inventing a
                    # closing quote in the displayed upstream text.
                    fragment = match.group().rstrip("\r\n")
                    for trim in range(min(7, len(fragment))):
                        prefix = fragment[:-trim] if trim else fragment
                        try:
                            decoded = json.loads(prefix + '"')
                        except ValueError:
                            continue
                        spans.append((None, match.start(), match.start() + len(prefix), decoded, False))
                        break
        record = {"id": self.serial, "raw": raw, "slots": []}
        self.serial += 1
        self.records.append(record)
        self.buffered_bytes += len(raw.encode("utf-8"))
        for span in spans:
            path, start, end, value = span[:4]
            slot = {"record": record["id"], "start": wire_position(start), "end": wire_position(end),
                    "value": value, "masks": [], "closed": span[4] if len(span) > 4 else True}
            record["slots"].append(slot)
            self._match(slot, "unparsed" if unparsed_payload else _logical_text_channel(payload, path))
            if unparsed_payload:
                # A subsequent malformed/valid frame could complete this
                # string's prefix too. Do not publish an ambiguous prefix.
                self._hide_pending()
        if self.buffered_bytes > RAW_RESPONSE_MAX_BYTES:
            # A stalled prefix followed by unbounded metadata must not hold
            # an unbounded raw queue. Hide that unresolved prefix before release.
            self._hide_pending()
        await self._flush()

    async def _line(self, value):
        if self.frame or value.lstrip("\ufeff").startswith(("data:", "event:")):
            complete_line = False
            if value.lstrip("\ufeff").startswith("data:") and not any(line.startswith("data:") for line in re.split(r"\r\n|\r|\n", self.frame)):
                try:
                    json.loads(value.lstrip("\ufeff")[5:])
                    complete_line = True
                except (ValueError, RecursionError):
                    pass
            self.frame += value
            if complete_line or not value.strip() or len(self.frame) > 1_048_576:
                await self._record(self.frame)
                self.frame = ""
        else:
            await self._record(value)

    async def feed(self, value):
        self.line += value
        while match := re.search(r"\r\n|\r|\n", self.line):
            if match.group() == "\r" and match.end() == len(self.line):
                break
            end = match.end()
            await self._line(self.line[:end])
            self.line = self.line[end:]
        if len(self.line) > 1_048_576:
            await self._line(self.line)
            self.line = ""

    async def finish(self):
        if self.line:
            await self._line(self.line)
            self.line = ""
        if self.frame:
            await self._record(self.frame)
            self.frame = ""
        self.pending.clear()
        await self._flush()


class _RawResponseCapture:
    """Keep a bounded, credential-safe preview without changing protocol text."""

    def __init__(self, queue, question_id, side, secrets):
        self.queue, self.question_id, self.side = queue, question_id, side
        self.secrets = tuple(set(secret for secret in secrets if secret))
        self.forms = tuple({form for secret in self.secrets
                            for form in (secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1])})
        self.keep = max(map(len, self.forms), default=1) - 1
        self.pending = self.head = self.tail = ""
        self.size = 0
        self.truncated = self.finished = False
        self.protocol_redactor = _ProtocolRawRedactor(self._feed_wire, self.forms)

    async def _emit(self, value, *, replace=False):
        await self.queue.put({"type": "raw_response", "question_id": self.question_id,
                              "side": self.side, "raw_response": value,
                              "raw_response_truncated": self.truncated,
                              "raw_response_replace": replace})

    async def _accept(self, value):
        if not value:
            return
        remaining = max(0, RAW_RESPONSE_MAX_BYTES - self.size)
        delta = _evidence_prefix(value, remaining)
        self.size += _evidence_bytes(value)
        was_truncated = self.truncated
        self.truncated = self.size > RAW_RESPONSE_MAX_BYTES
        half = RAW_RESPONSE_MAX_BYTES // 2
        self.head = _evidence_prefix(self.head + value, half)
        self.tail = _evidence_suffix(self.tail + value, half)
        if delta or self.truncated != was_truncated:
            await self._emit(delta)

    async def feed(self, value):
        await self.protocol_redactor.feed(value)

    async def _feed_wire(self, value):
        buffer = self.pending + value
        cutoff = max(0, len(buffer) - self.keep)
        for secret in self.forms:
            index = buffer.find(secret)
            while index >= 0:
                if index < cutoff < index + len(secret):
                    cutoff = index
                index = buffer.find(secret, index + 1)
        self.pending = buffer[cutoff:]
        await self._accept(scrub(buffer[:cutoff], self.forms))

    async def finish(self):
        if self.finished:
            return
        self.finished = True
        await self.protocol_redactor.finish()
        await self._accept(scrub(self.pending, self.forms))
        self.pending = ""
        if self.truncated:
            # Joining the retained halves can create a new credential after
            # JSON decoding, even when the join falls inside a Unicode escape.
            # Recheck this bounded representation before publishing it.
            parts = []
            async def collect(value):
                parts.append(value)
            preview = _ProtocolRawRedactor(collect, self.forms)
            await preview.feed(self.head + self.tail)
            await preview.finish()
            safe = scrub("".join(parts), self.forms)
            await self._emit(_evidence_prefix(safe, RAW_RESPONSE_MAX_BYTES), replace=True)


async def _response_lines(response, on_raw):
    if on_raw is None:
        async for line in response.aiter_lines():
            yield line
        return
    pending = ""
    async for value in response.aiter_text():
        await on_raw(value)
        pending += value
        while match := re.search(r"\r\n|\r|\n", pending):
            if match.group() == "\r" and match.end() == len(pending):
                break
            if match.start() > 1_048_576:
                raise ValueError("上游流单行超过本地读取上限")
            yield pending[:match.start()]
            pending = pending[match.end():]
        if len(pending) > 1_048_576:
            raise ValueError("上游流单行超过本地读取上限")
    if pending:
        yield pending.removesuffix("\r")


async def stream_events(response: httpx.Response, protocol: str, on_raw=None) -> AsyncIterator[dict[str, Any]]:
    if protocol != "responses":
        async for line in _response_lines(response, on_raw):
            parsed = parse_stream_line(line, protocol)
            if parsed is not None:
                yield parsed
        return
    data: list[str] = []
    size = 0
    async for line in _response_lines(response, on_raw):
        line = line.lstrip("\ufeff")
        if not line:
            if data:
                if "\n".join(data) == "[DONE]":
                    data, size = [], 0
                    continue
                try:
                    parsed = parse_stream_line("\n".join(data), protocol)
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
    before_send=None,
    *,
    secrets=(),
) -> None:
    if start_gate is not None:
        await start_gate.wait()
    evidence = {"http_status": None, "upstream_error_code": "", "upstream_error_message": "", "upstream_error_truncated": False}
    credentials = (config.api_key, *secrets)
    if config.protocol != "responses":
        await _measure_side(side, config, question, queue, client, before_send, evidence, credentials)
        return
    started_ms = round(time() * 1000)
    try:
        await asyncio.wait_for(_measure_side(side, config, question, queue, client, before_send, evidence, credentials),
                               timeout=RESPONSES_TOTAL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        payload = build_payload(config, question)
        await queue.put({"type": "side_finished", "question_id": question["id"], "side": side,
                         "ok": False, "status": "error", "transport_completed": False,
                         "request_started_ms": started_ms, "request_protocol": config.protocol,
                         "max_output_tokens": payload["max_output_tokens"],
                         "reasoning_effort": payload.get("reasoning", {}).get("effort"),
                         **evidence,
                         "error_category": "total_timeout", "error": "Responses 请求超过总时限。"})


async def _measure_side(
    side: str,
    config: EndpointConfig,
    question: dict[str, Any],
    queue: asyncio.Queue[dict[str, Any]],
    client: httpx.AsyncClient,
    before_send=None,
    evidence=None,
    secrets=(),
) -> None:
    evidence = evidence if evidence is not None else {"http_status": None, "upstream_error_code": "", "upstream_error_message": "", "upstream_error_truncated": False}
    capture = _RawResponseCapture(queue, question["id"], side, (config.api_key, *secrets))
    started_at = perf_counter()
    request_started_ms = round(time() * 1000)
    first_chunk_at: float | None = None
    first_answer_at: float | None = None
    last_output_at: float | None = None
    answer_parts: list[str] = []
    reasoning_parts: list[str] = []
    unrecognized = False
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
    if before_send is not None:
        await before_send(side)
    try:
        async with client.stream(
            "POST",
            endpoint_url(config),
            headers=build_headers(config),
            json=payload,
        ) as response:
            evidence["http_status"] = response.status_code
            header_request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
            if header_request_id:
                upstream_request_ids.add(header_request_id)
            if response.status_code >= 400:
                error_body = ""
                error_body_size = 0
                async for value in response.aiter_text():
                    await capture.feed(value)
                    error_body += _utf8_prefix(value, max(0, 1_048_576 - error_body_size))
                    error_body_size += len(value.encode("utf-8"))
                await capture.finish()
                try:
                    error_payload = json.loads(error_body) if error_body_size <= 1_048_576 else None
                except (ValueError, TypeError):
                    error_payload = None
                if isinstance(error_payload, dict):
                    for field, value in _upstream_error(error_payload, allow_top_level=True).items():
                        safe = _evidence_text(scrub(value, capture.secrets))
                        evidence[field] = _evidence_prefix(safe, RAW_RESPONSE_MAX_BYTES)
                        evidence["upstream_error_truncated"] |= evidence[field] != safe
                raise RuntimeError(f"HTTP {response.status_code}")
            async for parsed in stream_events(response, config.protocol, on_raw=capture.feed):
                now = perf_counter()
                if not parsed["recognized"]:
                    unrecognized = unrecognized or bool(parsed["raw"])
                    continue
                for field in ("upstream_error_code", "upstream_error_message"):
                    if parsed[field]:
                        safe = _evidence_text(scrub(parsed[field], capture.secrets))
                        evidence[field] = _evidence_prefix(safe, RAW_RESPONSE_MAX_BYTES)
                        evidence["upstream_error_truncated"] |= evidence[field] != safe
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

        await capture.finish()
        finished_at = perf_counter()
        answer = "".join(answer_parts)
        reasoning = "".join(reasoning_parts)
        recognized_chars = len(answer) + len(reasoning)
        displayed_chars = recognized_chars
        first_visible_at = first_chunk_at
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
        if protocol_error:
            ok, status, error = False, protocol_error, ""
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
        elif unrecognized:
            ok = False
            status = "unrecognized"
            error = ""
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
                **evidence,
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
        await capture.finish()
        raise
    except Exception as exc:
        await capture.finish()
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
                **evidence,
                "error_category": _exception_category(exc, output_chunk_count > 0),
                "error": scrub(_redact(str(exc), config.api_key), capture.secrets),
            }
        )


async def run_question(
    body: CompareRequest,
    question: dict[str, Any],
    client: httpx.AsyncClient,
    before_send=None,
) -> AsyncIterator[dict[str, Any]]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    start_gate = asyncio.Event()
    tasks = [
        asyncio.create_task(measure_side("candidate", body.candidate, question, queue, client, start_gate, before_send,
                                         secrets=(body.reference.api_key,))),
        asyncio.create_task(measure_side("reference", body.reference, question, queue, client, start_gate, before_send,
                                         secrets=(body.candidate.api_key,))),
    ]
    await asyncio.sleep(0)
    start_gate.set()
    finished = 0
    event_task = None
    try:
        while finished < 2:
            event_task = asyncio.create_task(queue.get())
            pending = [task for task in tasks if not task.done()]
            await asyncio.wait([event_task, *pending], return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                if task.done():
                    task.result()
            event = await event_task
            event_task = None
            if event["type"] == "side_finished":
                finished += 1
            yield event
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        # Cancellation still checkpoints already received upstream bytes. The
        # caller owns terminal state; cancelled requests remain unconfirmed.
        received = []
        if event_task is not None:
            event_task.cancel()
            result = await asyncio.gather(event_task, return_exceptions=True)
            if isinstance(result[0], dict):
                received.extend(result)
            event_task = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        while not queue.empty():
            received.append(queue.get_nowait())
        for event in received:
            if event["type"] == "raw_response":
                yield event
        raise
    finally:
        if event_task is not None:
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)
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
