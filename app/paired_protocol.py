"""双端准入的版本化协议适配与严格 SSE 终态解析。"""
from __future__ import annotations

import json
from typing import Any

from . import protocol

SUPPORTED_PROTOCOLS = {"openai", "anthropic"}
ADAPTER_VERSIONS = {
    "openai": "paired-openai-chat-completions-v1.0.0",
    "anthropic": "paired-anthropic-messages-v1.0.0",
}


class AdapterError(ValueError):
    pass


def validate_protocol(name: str) -> None:
    if name not in SUPPORTED_PROTOCOLS:
        raise AdapterError(f"unsupported_protocol:{name}")


def adapt_request(
    protocol_name: str, model: str, canonical_request: dict[str, Any],
) -> dict[str, Any]:
    validate_protocol(protocol_name)
    messages = [dict(message) for message in canonical_request.get("messages") or []]
    if not messages:
        raise AdapterError("messages_required")
    max_tokens = int(canonical_request["max_tokens"])
    stream = bool(canonical_request.get("stream", True))
    temperature = canonical_request.get("temperature")
    if protocol_name == "openai":
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
        token_field = "max_completion_tokens" \
            if protocol.payload_variant(protocol_name, model) == "reasoning" else "max_tokens"
        body[token_field] = max_tokens
        if token_field == "max_tokens" and temperature is not None:
            body["temperature"] = temperature
        return body

    leading_system: list[str] = []
    conversation: list[dict[str, Any]] = []
    seen_conversation = False
    for message in messages:
        role = str(message.get("role") or "")
        content = message.get("content")
        if not isinstance(content, str):
            raise AdapterError("text_messages_only")
        if role == "system" and not seen_conversation:
            leading_system.append(content)
            continue
        seen_conversation = True
        if role not in {"user", "assistant"} or role == "system":
            raise AdapterError("anthropic_role_not_representable")
        conversation.append({"role": role, "content": content})
    if not conversation:
        raise AdapterError("anthropic_conversation_required")
    body = {
        "model": model,
        "messages": conversation,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if leading_system:
        body["system"] = "\n".join(leading_system)
    if temperature is not None:
        body["temperature"] = temperature
    return body


def new_stream_state(protocol_name: str) -> dict[str, Any]:
    validate_protocol(protocol_name)
    return {
        "protocol": protocol_name,
        "text": "",
        "models": [],
        "finish_reasons": [],
        "terminal_event": False,
        "malformed_events": 0,
        "data_events": 0,
        "unknown_events": 0,
    }


def _remember_model(state: dict[str, Any], value: Any) -> None:
    model = str(value or "").strip()
    if model and model not in state["models"]:
        state["models"].append(model)


def consume_sse_line(state: dict[str, Any], line: str) -> str:
    if not line.startswith("data:"):
        return ""
    payload = line[5:].strip()
    state["data_events"] += 1
    if state["protocol"] == "openai" and payload == "[DONE]":
        state["terminal_event"] = True
        return ""
    try:
        event = json.loads(payload)
    except (TypeError, ValueError):
        state["malformed_events"] += 1
        return ""
    if not isinstance(event, dict):
        state["malformed_events"] += 1
        return ""
    if state["protocol"] == "openai":
        _remember_model(state, event.get("model"))
        text_parts: list[str] = []
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                state["malformed_events"] += 1
                continue
            delta = choice.get("delta") or {}
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str):
                text_parts.append(content)
            finish = choice.get("finish_reason")
            if finish is not None:
                state["finish_reasons"].append(str(finish))
        delta_text = "".join(text_parts)
        state["text"] += delta_text
        return delta_text

    event_type = str(event.get("type") or "")
    if event_type == "message_start":
        message = event.get("message") or {}
        _remember_model(state, message.get("model") if isinstance(message, dict) else None)
        if isinstance(message, dict) and message.get("stop_reason"):
            state["finish_reasons"].append(str(message["stop_reason"]))
        return ""
    if event_type == "message_delta":
        delta = event.get("delta") or {}
        if isinstance(delta, dict) and delta.get("stop_reason"):
            state["finish_reasons"].append(str(delta["stop_reason"]))
        return ""
    if event_type == "content_block_delta":
        delta = event.get("delta") or {}
        if isinstance(delta, dict) and delta.get("type") == "text_delta" \
                and isinstance(delta.get("text"), str):
            state["text"] += delta["text"]
            return delta["text"]
        state["unknown_events"] += 1
        return ""
    if event_type == "message_stop":
        state["terminal_event"] = True
        return ""
    state["unknown_events"] += 1
    return ""


def finalize_stream(state: dict[str, Any]) -> dict[str, Any]:
    models = list(state["models"])
    reasons = list(state["finish_reasons"])
    truncated = any(
        reason == "length" if state["protocol"] == "openai"
        else reason == "max_tokens"
        for reason in reasons
    )
    normal_terminal = bool(state["terminal_event"] and reasons)
    return {
        **state,
        "actual_model": models[0] if len(models) == 1 else "",
        "identity_conflict": len(models) > 1,
        "identity_missing": not models,
        "normal_terminal": normal_terminal,
        "truncated": truncated,
    }


def adapter_selfcheck() -> tuple[bool, list[str]]:
    errors: list[str] = []
    openai = new_stream_state("openai")
    consume_sse_line(openai, 'data: {"model":"m","choices":[{"delta":{"content":"OK"},"finish_reason":null}]}')
    consume_sse_line(openai, 'data: {"model":"m","choices":[{"delta":{},"finish_reason":"stop"}]}')
    consume_sse_line(openai, "data: [DONE]")
    if not finalize_stream(openai)["normal_terminal"]:
        errors.append("openai_terminal")
    anthropic = new_stream_state("anthropic")
    consume_sse_line(anthropic, 'data: {"type":"message_start","message":{"model":"m"}}')
    consume_sse_line(anthropic, 'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"OK"}}')
    consume_sse_line(anthropic, 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}')
    consume_sse_line(anthropic, 'data: {"type":"message_stop"}')
    if not finalize_stream(anthropic)["normal_terminal"]:
        errors.append("anthropic_terminal")
    truncated = new_stream_state("openai")
    consume_sse_line(
        truncated,
        'data: {"model":"m","choices":[{"delta":{"content":"x"},"finish_reason":"length"}]}',
    )
    consume_sse_line(truncated, "data: [DONE]")
    if not finalize_stream(truncated)["truncated"]:
        errors.append("openai_truncation")
    missing_model = new_stream_state("openai")
    consume_sse_line(
        missing_model,
        'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"stop"}]}',
    )
    consume_sse_line(missing_model, "data: [DONE]")
    if not finalize_stream(missing_model)["identity_missing"]:
        errors.append("openai_missing_model")
    conflicting = new_stream_state("openai")
    for model in ("m1", "m2"):
        consume_sse_line(
            conflicting,
            f'data: {{"model":"{model}","choices":[{{"delta":{{}},"finish_reason":null}}]}}',
        )
    if not finalize_stream(conflicting)["identity_conflict"]:
        errors.append("openai_conflicting_model")
    malformed = new_stream_state("anthropic")
    consume_sse_line(malformed, "data: {not-json}")
    if finalize_stream(malformed)["malformed_events"] != 1:
        errors.append("anthropic_malformed_event")
    missing_terminal = new_stream_state("anthropic")
    consume_sse_line(
        missing_terminal,
        'data: {"type":"message_start","message":{"model":"m"}}',
    )
    consume_sse_line(
        missing_terminal,
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
    )
    if finalize_stream(missing_terminal)["normal_terminal"]:
        errors.append("anthropic_missing_terminal")
    return not errors, errors
