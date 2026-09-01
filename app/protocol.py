"""协议适配：把 openai / anthropic 的差异收敛成统一的请求与解析。"""
import json
import re
from typing import Any

from .config import HTTP_TIMEOUT


def models_url(protocol: str, base_url: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/v1/models" if protocol == "openai" else f"{base}/v1/models"


def chat_url(protocol: str, base_url: str) -> str:
    base = base_url.rstrip("/")
    if protocol == "anthropic":
        return f"{base}/v1/messages"
    return f"{base}/v1/chat/completions"


def headers(protocol: str, key: str) -> dict[str, str]:
    if protocol == "anthropic":
        return {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    return {"Authorization": f"Bearer {key}", "content-type": "application/json"}


# OpenAI o 系列 / gpt-5 一类推理模型的请求体差异：
#   - 不接受 max_tokens，要用 max_completion_tokens（给了 max_tokens 直接 400）
#   - 不接受 temperature（只允许默认值）
# 名字匹配只是「先猜一次省一个来回」，真正兜底的是 probes 里的「400 就换一种再试」——
# 中转站经常改写模型名（改成 my-o3-proxy 之类），光靠名字判会漏。
_REASONING_NAME = re.compile(
    r"(^|[^a-z0-9])(o[1-4]|gpt-5|deepseek-r\d|qwq|glm-z\d)([^a-z0-9]|$)", re.I)


def is_reasoning_model(model: str) -> bool:
    """按模型名猜是不是推理模型。猜错没关系，probes 会用 400 重试纠正。"""
    return bool(_REASONING_NAME.search(model or ""))


def normalize_model_id(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (model or "").strip().casefold()).strip("-")


def models_compatible(requested: str, actual: str) -> bool:
    return bool(requested and actual) and normalize_model_id(requested) == normalize_model_id(actual)


def payload_variant(protocol: str, model: str) -> str:
    """初次尝试用哪种请求体口径。"""
    if protocol == "anthropic":
        return "standard"
    return "reasoning" if is_reasoning_model(model) else "standard"


def chat_payload(
    protocol: str, model: str, prompt: str = "", *,
    stream: bool = False, max_tokens: int = 256,
    temperature: float = 0.0, json_mode: bool = False,
    tools: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    variant: str = "standard",
    send_temperature: bool = True,
) -> dict[str, Any]:
    """统一构造对话请求体。temperature=0 便于复检时结果可比。

    variant="reasoning" 用于 OpenAI o 系列一类模型：换成 max_completion_tokens、
    并且不发 temperature。发错了上游会返回 400，probes 会换一种口径重试一次。

    注意 max_tokens 的语义：**思考 token 也算在里面**（Anthropic 的 thinking 计入
    max_tokens，o 系列的 reasoning tokens 计入 max_completion_tokens）。所以硬题的
    预算要按「思考 + 正文」一起给，见 config.HARD_TOKEN_FLOOR。
    """
    conversation = messages or [{"role": "user", "content": prompt}]
    if protocol == "anthropic":
        body: dict[str, Any] = {
            "model": model,
            "messages": conversation,
            "max_tokens": max_tokens,
        }
        if send_temperature:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [_anthropic_tool(t) for t in tools]
    else:
        body = {
            "model": model,
            "messages": conversation,
        }
        if variant == "reasoning":
            body["max_completion_tokens"] = max_tokens
            # temperature 不发：o 系列只接受默认值，发 0 会被拒
        else:
            body["max_tokens"] = max_tokens
            if send_temperature:
                body["temperature"] = temperature
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
    if stream:
        body["stream"] = True
    return body


_PARAMETER_REJECTION_HINTS = (
    "deprecated", "unsupported", "not supported", "does not support",
    "invalid", "may only be set", "only the default", "default value",
    "missing required",
)


def _error_description(body: str) -> str:
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return body or ""
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return " ".join(str(error.get(key) or "") for key in ("param", "message"))
    return str(error or body or "")


def rejects_temperature(status: int, body: str) -> bool:
    """上游是否明确拒绝 temperature；模糊的 4xx 不触发降参。"""
    low = _error_description(body).lower()
    return status in (400, 422) and "temperature" in low \
        and any(hint in low for hint in _PARAMETER_REJECTION_HINTS)


def rejects_token_parameter(status: int, body: str) -> bool:
    """上游是否明确拒绝两种 OpenAI 输出预算参数之一。"""
    low = _error_description(body).lower()
    mentions_token_parameter = "max_tokens" in low or "max_completion_tokens" in low
    return status in (400, 422) and mentions_token_parameter \
        and any(hint in low for hint in _PARAMETER_REJECTION_HINTS)


def other_variant(variant: str) -> str:
    return "standard" if variant == "reasoning" else "reasoning"


def _anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """OpenAI 的 function 描述转成 Anthropic 的 tool 描述。"""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": tool.get("parameters", {"type": "object", "properties": {}}),
    }


def parse_tool_calls(protocol: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """取出模型发起的工具调用，统一成 [{name, args}]。解析不出来就返回空表。"""
    out: list[dict[str, Any]] = []
    if protocol == "anthropic":
        for part in data.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "tool_use":
                out.append({"name": str(part.get("name") or ""),
                            "args": part.get("input") or {}})
        return out
    choices = data.get("choices") or []
    msg = (choices[0].get("message") or {}) if choices else {}
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        raw = fn.get("arguments")
        args: Any = {}
        if isinstance(raw, str):
            try:
                args = json.loads(raw)
            except ValueError:
                args = {}
        elif isinstance(raw, dict):
            args = raw
        out.append({"name": str(fn.get("name") or ""), "args": args})
    return out


def parse_finish(protocol: str, data: dict[str, Any]) -> tuple[str, bool]:
    """返回 (结束原因, 是否因为撞上 token 上限而被截断)。

    这个信号对硬题是刚需。**思考 token 算在 max_tokens 里**，推理模型可能光 thinking
    就烧穿预算，正文返回空的。拿不到 <solution> 就记 0 分的话，报告里跟「真的答错」
    长得一模一样，从分数上完全看不出来 —— 那是系统性压低所有推理模型。
    所以截断要单独识别出来，记成「未测到」而不是答错。

    另外顺手把 reasoning token 数捞出来（有的上游会报），好在报告里说明预算烧在哪。
    """
    if protocol == "anthropic":
        stop = str(data.get("stop_reason") or "")
        return stop, stop == "max_tokens"
    choices = data.get("choices") or []
    reason = str((choices[0].get("finish_reason") or "")) if choices else ""
    return reason, reason == "length"


def reasoning_tokens(data: dict[str, Any]) -> int:
    """上游报告的思考 token 数，没报就返回 0。

    OpenAI 放在 usage.completion_tokens_details.reasoning_tokens；
    有些兼容实现放在 usage.reasoning_tokens。两个都认。
    """
    usage = data.get("usage") or {}
    detail = usage.get("completion_tokens_details") or {}
    for src in (detail.get("reasoning_tokens"), usage.get("reasoning_tokens")):
        try:
            if src is not None:
                return max(int(src), 0)
        except (TypeError, ValueError):
            continue
    return 0


def parse_reply(protocol: str, data: dict[str, Any]) -> tuple[str, str, dict[str, int]]:
    """返回 (正文, 上游实际返回的 model, usage)。usage 缺字段时置 -1 以便后续判定。"""
    usage_raw = data.get("usage") or {}
    if protocol == "anthropic":
        parts = data.get("content") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        usage = {
            "prompt": int(usage_raw.get("input_tokens", -1)),
            "completion": int(usage_raw.get("output_tokens", -1)),
            "cached_read": int(usage_raw.get("cache_read_input_tokens", -1)),
            "cached_write": int(usage_raw.get("cache_creation_input_tokens", -1)),
        }
    else:
        choices = data.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices else {}
        text = msg.get("content") or ""
        usage = {
            "prompt": int(usage_raw.get("prompt_tokens", -1)),
            "completion": int(usage_raw.get("completion_tokens", -1)),
            "cached_read": int((usage_raw.get("prompt_tokens_details") or {}).get(
                "cached_tokens", -1)),
            "cached_write": -1,
        }
    return text, str(data.get("model") or ""), usage


def parse_stream_chunk(protocol: str, line: str) -> tuple[str, bool, str]:
    """解析一行 SSE，返回增量文本、结束标志和响应模型。"""
    if not line.startswith("data:"):
        return "", False, ""
    payload = line[5:].strip()
    if payload == "[DONE]":
        return "", True, ""
    try:
        obj = json.loads(payload)
    except ValueError:
        return "", False, ""
    if protocol == "anthropic":
        actual_model = str((obj.get("message") or {}).get("model") or obj.get("model") or "")
        if obj.get("type") == "message_stop":
            return "", True, actual_model
        if obj.get("type") == "content_block_delta":
            return (obj.get("delta") or {}).get("text", ""), False, actual_model
        return "", False, actual_model
    choices = obj.get("choices") or []
    if not choices:
        return "", False, str(obj.get("model") or "")
    delta = choices[0].get("delta") or {}
    done = bool(choices[0].get("finish_reason"))
    return delta.get("content") or "", done, str(obj.get("model") or "")


def list_models(protocol: str, data: Any) -> list[str]:
    """从 /v1/models 响应里取出模型名列表。"""
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    names = []
    for it in items:
        if isinstance(it, dict):
            name = it.get("id") or it.get("model") or it.get("model_name")
            if name:
                names.append(str(name))
        elif isinstance(it, str):
            names.append(it)
    return names


TIMEOUT = HTTP_TIMEOUT
