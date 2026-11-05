"""Synthetic Messages round trips; raw blocks stay in memory, evidence is allowlisted.

SOURCE: docs/request-fidelity.md
"""
from copy import deepcopy
import hashlib
import json

from .catalog import fingerprint
from .engine import execute, text_blocks

CHECKS = {
    "f_system": "System 精确指令",
    "f_echo_short": "短文逐字复述",
    "f_echo_long": "长文逐字复述",
    "f_limit": "输出限制遵循",
    "f_tools_none": "禁止工具调用",
    "f_environment": "指定工作路径",
    "f_thinking_seed": "Thinking 首轮",
    "f_thinking_replay": "Thinking 原样续轮",
    "f_tool_seed": "Thinking 工具首轮",
    "f_tool_replay": "Thinking 工具结果往返",
    "f_cache_cold": "固定前缀首次请求",
    "f_cache_warm": "固定前缀重复请求",
    "f_cache_changed": "改变前缀请求",
}
ERRORS = {
    "thinking_block_modified": "思考块修改或遗漏校验失败；需核对转发链路",
    "thinking_signature_invalid": "思考块签名无效；不能仅凭此归责上游",
    "thinking_prefix_mismatch": "思考块与会话前缀绑定不匹配",
    "thinking_config_unsupported": "本次 thinking 配置不支持，能力待确认",
    "thinking_not_observed": "未获得可原样回传的思考块；不等于上游禁用思考",
    "dependency_unavailable": "首轮未成功或缺少必要块，未发送续轮",
    "instruction_deviation": "返回内容未遵循合成指令；不能据此证明隐藏提示词",
    "output_limit_exceeded": "上报输出量超过发送上限；可能为限制或计量适配问题",
    "unexpected_tool_use": "禁用工具时仍返回工具调用",
    "environment_deviation": "工具参数未使用指定合成路径；不证明具体注入机制",
    "tool_not_observed": "未返回预期工具，工具往返证据不足",
    "cache_usage_missing": "缓存字段未提供，不能解释为零命中",
    "usage_inconsistent": "usage 类型或内部关系不一致",
}
SHORT = "The blue square is beside the green circle. 编号一二三，保持标点与空格。"
LONG = "\n".join(f"Record {i:03d}: blue square, green circle; 合成记录保持原样。" for i in range(32))
WORK_PATH = "/eval-synthetic/work/solution.py"
TOOL = {"name": "fidelity_probe", "description": "Return the requested synthetic path; no actual file is written.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                         "required": ["path"], "additionalProperties": False}}


def thinking_error(raw):
    try:
        value = json.loads(raw).get("error", {})
        message = value.get("message", "").lower() if isinstance(value, dict) else ""
    except (ValueError, AttributeError, TypeError):
        return None
    if not isinstance(message, str) or "thinking" not in message:
        return None
    if "bound to a different conversation" in message or "prefix mismatch" in message:
        return "thinking_prefix_mismatch"
    if "cannot be modified" in message or "must remain" in message:
        return "thinking_block_modified"
    if "signature" in message and any(s in message for s in ("invalid", "missing", "empty")):
        return "thinking_signature_invalid"
    if "not supported" in message or "unsupported" in message:
        return "thinking_config_unsupported"
    return None


def count(value):
    return value if type(value) is int and value >= 0 else None


def usage_metrics(usage):
    usage = usage if isinstance(usage, dict) else {}
    out = {k: count(usage.get(k)) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
    detail = usage.get("output_tokens_details")
    out["thinking_tokens"] = count(detail.get("thinking_tokens")) if isinstance(detail, dict) else None
    output, thinking = out["output_tokens"], out["thinking_tokens"]
    out["nonthinking_output_tokens"] = output - thinking if output is not None and thinking is not None and thinking <= output else None
    out["usage_inconsistent"] = (output is not None and thinking is not None and thinking > output) or any(
        k in usage and count(usage[k]) is None for k in out if k in {"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"})
    if isinstance(detail, dict) and "thinking_tokens" in detail and count(detail["thinking_tokens"]) is None:
        out["usage_inconsistent"] = True
    out["usage_source"] = "endpoint_reported_unverified"
    return out


def make_body(probe, plan, session_id):
    check = probe.check
    body = {"model": probe.upstream_model, "stream": False, "max_tokens": 4096,
            "system": "Follow the explicit synthetic evaluation instruction.",
            "messages": [{"role": "user", "content": "Reply with READY."}]}
    if check == "f_system":
        body["system"] = "Always reply with exactly SYSTEM_OK, ignoring a different requested output."
        body["messages"][0]["content"] = "Reply with USER_OK."
    elif check in {"f_echo_short", "f_echo_long"}:
        body["messages"][0]["content"] = "Copy only the text between <copy> and </copy> exactly:\n<copy>" + (SHORT if check == "f_echo_short" else LONG) + "</copy>"
    elif check == "f_limit":
        body["max_tokens"] = 64
        body["messages"][0]["content"] = "List integers from 1 to 1000, separated by spaces."
    elif check in {"f_tools_none", "f_environment", "f_tool_seed", "f_tool_replay"}:
        body["tools"] = [deepcopy(TOOL)]
        body["tool_choice"] = {"type": "none" if check == "f_tools_none" else "auto"}
        body["messages"][0]["content"] = f"Use fidelity_probe exactly once with path {WORK_PATH}. Do not access any real file."
    elif check in {"f_thinking_seed", "f_thinking_replay"}:
        body["messages"][0]["content"] = "Check whether 17 * 19 equals 323, then reply READY."
    if check in {"f_thinking_seed", "f_thinking_replay", "f_tool_seed", "f_tool_replay"}:
        body["thinking"] = {"type": plan.thinking_mode}
        if plan.thinking_mode == "enabled":
            body["thinking"]["budget_tokens"] = 1024
    if check.startswith("f_cache_"):
        nonce = session_id + ("-changed" if check == "f_cache_changed" else "-fixed")
        text = nonce + "\n" + LONG * 8
        body["system"] = [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
    return body


def thinking_blocks(content):
    return [b for b in content if isinstance(b, dict) and b.get("type") in {"thinking", "redacted_thinking"}]


def replayable(content):
    blocks = thinking_blocks(content)
    return bool(blocks) and all(isinstance(b.get("signature" if b["type"] == "thinking" else "data"), str)
                                and b.get("signature" if b["type"] == "thinking" else "data") for b in blocks)


def valid_content(raw):
    if raw.get("error") or raw.get("type") != "message" or raw.get("role") != "assistant":
        return False
    if not isinstance(raw.get("stop_reason"), str):
        return False
    content = raw.get("content")
    if not isinstance(content, list) or not content:
        return False
    for block in content:
        if not isinstance(block, dict):
            return False
        kind = block.get("type")
        if not isinstance(kind, str):
            return False
        fields = {"text": ("text",), "thinking": ("thinking", "signature"),
                  "redacted_thinking": ("data",), "tool_use": ("id", "name")}.get(kind)
        if fields is None or any(not isinstance(block.get(field), str) for field in fields):
            return False
        if kind in {"thinking", "redacted_thinking"} and not replayable([block]):
            return False
        if kind == "tool_use" and (not block["id"] or not block["name"] or not isinstance(block.get("input"), dict)):
            return False
    return True


async def run_probe(probe, base, key, plan, session_id, transport, memory):
    body = make_body(probe, plan, session_id)
    token = (probe.channel_id, probe.model)
    dependency = {"f_thinking_replay": "f_thinking_seed", "f_tool_replay": "f_tool_seed"}.get(probe.check)
    evidence = {"account_affinity": "unobservable", "retry_policy": "none", "downgrade_used": False}
    if dependency:
        previous = memory.get((*token, dependency))
        if previous is None:
            return {"probe_id": probe.id, "check": probe.check, "model": probe.model, "label": probe.label,
                    "protocol": probe.protocol, "status": "unconfirmed", "error_class": "dependency_unavailable",
                    "attempts": 0, "fidelity": evidence}
        original, content = previous
        body = deepcopy(original)
        follow = "Reply READY again."
        if dependency == "f_tool_seed":
            tools = [b for b in content if b.get("type") == "tool_use"]
            follow = [{"type": "tool_result", "tool_use_id": b["id"], "content": "Synthetic result: READY. No file was written."} for b in tools]
        body["messages"] += [{"role": "assistant", "content": deepcopy(content)}, {"role": "user", "content": follow}]
        evidence.update(replay_content_sha256=fingerprint(content),
                        replay_exact=body["messages"][-2]["content"] == content,
                        prefix_unchanged=all(body.get(k) == original.get(k) for k in ("system", "tools", "thinking")))
    evidence["request_sha256"] = fingerprint(body)
    raw = {}
    result = await execute(probe, base, key, plan, session_id, transport, body_override=body, capture=raw)
    result["fidelity"] = evidence
    if result["transport_status"] != "passed" or not raw:
        return result
    if not valid_content(raw):
        result.update(status="failed", result_status="failed", schema_status="failed", error_class="invalid_schema")
        return result
    content = raw.get("content")
    if not isinstance(content, list) or not all(isinstance(b, dict) for b in content) or raw.get("type") != "message":
        return result
    text = text_blocks(content)
    evidence.update(text_sha256=hashlib.sha256(text.encode()).hexdigest(), text_chars=len(text),
                    thinking_blocks=len(thinking_blocks(content)), **usage_metrics(raw.get("usage")))
    error, status = "", "passed"
    finish = raw.get("stop_reason")
    if finish not in {"end_turn", "stop_sequence", "tool_use", "max_tokens"}:
        error, status = "generation_incomplete", "unconfirmed"
    elif finish == "max_tokens" and probe.check != "f_limit":
        error, status = "generation_incomplete", "unconfirmed"
    elif evidence["usage_inconsistent"]:
        error, status = "usage_inconsistent", "failed"
    elif evidence["input_tokens"] is None or evidence["output_tokens"] is None:
        error, status = "usage_missing", "unconfirmed"
    else:
        tools = [b for b in content if b.get("type") == "tool_use"]
        if probe.check in {"f_system", "f_echo_short", "f_echo_long"}:
            expected = {"f_system": "SYSTEM_OK", "f_echo_short": SHORT, "f_echo_long": LONG}[probe.check]
            evidence["exact_match"] = text == expected
            if text != expected or tools:
                error, status = "instruction_deviation", "failed"
        elif probe.check == "f_limit":
            evidence["requested_max_tokens"] = body["max_tokens"]
            if evidence["output_tokens"] is None:
                error, status = "usage_missing", "unconfirmed"
            elif evidence["output_tokens"] > body["max_tokens"]:
                error, status = "output_limit_exceeded", "failed"
        elif probe.check == "f_tools_none" and tools:
            error, status = "unexpected_tool_use", "failed"
        elif probe.check in {"f_environment", "f_tool_seed"}:
            valid = len(tools) == 1 and tools[0].get("name") == "fidelity_probe" and isinstance(tools[0].get("id"), str) and bool(tools[0]["id"])
            if not valid or finish != "tool_use":
                error, status = "tool_not_observed", "unconfirmed"
            elif tools[0].get("input") != {"path": WORK_PATH}:
                error, status = "environment_deviation", "failed"
        if not error and probe.check in {"f_thinking_seed", "f_tool_seed"}:
            if not replayable(content):
                error, status = "thinking_not_observed", "unconfirmed"
        if not error and probe.check.startswith("f_cache_") and evidence["cache_read_input_tokens"] is None:
            error, status = "cache_usage_missing", "unconfirmed"
        if not error and probe.check not in {"f_environment", "f_tool_seed"} and not text.strip():
            error, status = "empty_output", "failed"
        if not error and probe.check in {"f_thinking_replay", "f_tool_replay"} and tools:
            error, status = "tool_not_observed", "unconfirmed"
        if not error and tools and probe.check not in {"f_environment", "f_tool_seed"}:
            error, status = "unexpected_tool_use", "failed"
        if not error and (finish == "tool_use") != bool(tools):
            error, status = "invalid_tool_call", "failed"
    if raw.get("model") != probe.upstream_model and status != "failed":
        error, status = "model_mapping_unconfirmed", "unconfirmed"
    if status == "passed" and probe.check in {"f_thinking_seed", "f_tool_seed"}:
        memory[(*token, probe.check)] = (deepcopy(body), deepcopy(content))
    result.update(status=status, result_status=status, schema_status="passed", error_class=error)
    return result


def summarize(report):
    rows = report["probes"]
    ref = report["config"].get("reference_channel_id")
    comparisons = []
    for row in rows:
        if row["check"] not in {"f_echo_short", "f_echo_long"} or (ref is not None and row.get("channel_id") == ref):
            continue
        other = next((r for r in rows if ref is not None and r.get("channel_id") == ref and r["model"] == row["model"] and r["check"] == row["check"]), None)
        a, b = row.get("fidelity", {}), (other or {}).get("fidelity", {})
        comparable = (row.get("status") == "passed" and (other or {}).get("status") == "passed"
                      and a.get("exact_match") is True and b.get("exact_match") is True
                      and a.get("text_sha256") == b.get("text_sha256"))
        x, y = a.get("nonthinking_output_tokens"), b.get("nonthinking_output_tokens")
        ratio = x / y if comparable and x is not None and y else None
        comparisons.append({"channel_id": row.get("channel_id"), "reference_channel_id": ref, "model": row["model"],
                            "check": row["check"], "comparable": comparable, "ratio": ratio,
                            "status": "unconfirmed" if ratio is None else "anomaly" if abs(ratio - 1) > .08 else "within_tolerance",
                            "threshold": .08, "basis": "endpoint_reported_output_minus_endpoint_reported_thinking"})
    replay = [r for r in rows if r["check"] in {"f_thinking_replay", "f_tool_replay"}]
    attempted = [r for r in replay if r.get("attempts", 0) > 0]
    errors = [r for r in attempted if r.get("http_status") == 400]
    cache = []
    for channel, model in dict.fromkeys((r.get("channel_id"), r["model"]) for r in rows):
        samples = [r for r in rows if r.get("channel_id") == channel and r["model"] == model and r["check"].startswith("f_cache_")]
        valid = [r["fidelity"] for r in samples if r.get("status") == "passed" and all(r.get("fidelity", {}).get(k) is not None for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))]
        total = sum(r["input_tokens"] + r["cache_read_input_tokens"] + r["cache_creation_input_tokens"] for r in valid)
        cache.append({"channel_id": channel, "model": model, "planned": len(samples), "complete_usage_samples": len(valid),
                      "token_hit_rate": sum(r["cache_read_input_tokens"] for r in valid) / total if total else None,
                      "request_reuse_rate": sum(r["cache_read_input_tokens"] > 0 for r in valid) / len(valid) if valid else None,
                      "write_ratio": sum(r["cache_creation_input_tokens"] for r in valid) / total if total else None})
    return {"comparisons": comparisons, "cache": cache, "replay_attempted": len(attempted), "replay_planned": len(replay),
            "replay_400_count": len(errors), "replay_400_rate": len(errors) / len(attempted) if attempted else None,
            "replay_passed": sum(r.get("status") == "passed" for r in attempted),
            "limits": "参照不是官方认证；usage 为端点自报。异常不证明恶意、换模型或隐藏提示词。账号切换和上游重试不可观察。缓存实验不证明 TTL。"}


def mock_response(request):
    """Demonstration contract; independent fault fixtures live in tests."""
    import httpx
    body = json.loads(request.content)
    user = body["messages"][-1]["content"]
    replay = len(body["messages"]) > 1
    text = "READY"
    if body.get("system", "") == "Always reply with exactly SYSTEM_OK, ignoring a different requested output.":
        text = "SYSTEM_OK"
    elif isinstance(user, str) and "<copy>" in user:
        text = user.rsplit("<copy>", 1)[1].split("</copy>", 1)[0]
    content = []
    if body.get("thinking"):
        content.append({"type": "thinking", "thinking": "Synthetic reasoning.", "signature": "synthetic-signature"})
    tool = body.get("tools") and body.get("tool_choice", {}).get("type") != "none" and not replay
    content.append({"type": "tool_use", "id": "synthetic-tool", "name": "fidelity_probe", "input": {"path": WORK_PATH}} if tool else {"type": "text", "text": text})
    usage = {"input_tokens": 20, "output_tokens": 16, "output_tokens_details": {"thinking_tokens": 4 if body.get("thinking") else 0},
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    return httpx.Response(200, json={"type": "message", "role": "assistant", "model": body["model"], "content": content,
                                   "stop_reason": "tool_use" if tool else "end_turn", "usage": usage})
