"""SOURCE: docs/protocol-admission.md and the pinned primary references therein."""
from dataclasses import dataclass
import hashlib
import json

PROTOCOLS = {
    "openai": {"name": "Chat Completions", "basic": "chat_json", "stream": "chat_stream"},
    "responses": {"name": "Responses", "basic": "responses_json", "stream": "responses_stream", "tool": "responses_tool"},
    "anthropic": {"name": "Claude Messages", "basic": "messages_json", "stream": "messages_stream", "tool": "messages_tool"},
}
CHECKS = {
    "responses_json": ("responses", "/responses", False, "text", "Responses 非流式"),
    "responses_stream": ("responses", "/responses", True, "text", "Responses 流式"),
    "responses_tool": ("responses", "/responses", False, "tool", "Responses 工具格式"),
    "alpha_search": ("alpha_search", "/alpha/search", False, "search", "Alpha Search"),
    "chat_json": ("openai", "/chat/completions", False, "text", "Chat 非流式"),
    "chat_stream": ("openai", "/chat/completions", True, "text", "Chat 流式"),
    "messages_json": ("anthropic", "/messages", False, "text", "Messages 非流式"),
    "messages_stream": ("anthropic", "/messages", True, "text", "Messages 流式"),
    "messages_tool": ("anthropic", "/messages", False, "tool", "Messages 工具格式"),
}


@dataclass(frozen=True)
class Probe:
    id: str
    check: str
    model: str
    upstream_model: str
    protocol: str
    path: str
    stream: bool
    purpose: str
    label: str


def probes(plan):
    return [Probe(f"m{i}-{check}", check, model.model, model.upstream_model or model.model, *CHECKS[check])
            for i, model in enumerate(plan.models) for check in CHECKS]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def request_body(probe, session_id):
    # Independent synthetic inputs are product probe definitions; no user history is copied.
    prompt = "Reply with READY."
    parameters = {"type": "object", "properties": {"marker": {"type": "string", "enum": ["ready"]}},
                  "required": ["marker"], "additionalProperties": False}
    if probe.protocol == "alpha_search":
        return {"id": session_id, "model": probe.upstream_model,
                "input": "查找 RFC 9110 HTTP Semantics 的标准页面。",
                "commands": {"search_query": [{"q": "RFC 9110 HTTP Semantics", "domains": ["rfc-editor.org"]}],
                             "response_length": "short"},
                "settings": {"allowed_callers": ["direct"], "external_web_access": True}, "max_output_tokens": 2048}
    if probe.protocol == "responses":
        value = {"model": probe.upstream_model, "input": prompt, "stream": probe.stream,
                 "store": False, "max_output_tokens": 2048}
        if probe.purpose == "tool":
            value.update(tools=[{"type": "function", "name": "protocol_probe", "description": "Return the marker.",
                                 "parameters": parameters, "strict": True}],
                         tool_choice={"type": "function", "name": "protocol_probe"})
        return value
    value = {"model": probe.upstream_model, "messages": [{"role": "user", "content": prompt}],
             "stream": probe.stream, "max_tokens": 256}
    if probe.protocol == "openai":
        value.pop("max_tokens")
        value["max_completion_tokens"] = 2048
        if probe.stream:
            value["stream_options"] = {"include_usage": True}
    elif probe.purpose == "tool":
        value.update(tools=[{"name": "protocol_probe", "description": "Return the marker.", "input_schema": parameters}],
                     tool_choice={"type": "tool", "name": "protocol_probe"})
    return value
