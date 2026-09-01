"""单条测试动作。每个 probe 返回统一结构，失败只影响自己，不抛到任务层。"""
import asyncio
import time
from typing import Any

import httpx

from . import evidence, grading, hardbank, protocol
from .config import HARD_HTTP_TIMEOUT
from .security import scrub

# 失败归类，报告与建议引擎都按这套口径
CFG, AUTH, PROTO, LIMIT, TIMEOUT, UPSTREAM, PLATFORM, MISMATCH = (
    "配置", "鉴权", "协议", "限流", "超时", "上游错误", "平台错误", "结果不符合预期",
)
# 撞上 token 上限被截断。单独一类，不并进 MISMATCH ——
# 截断是「没测到」，答错是「测到了但不对」，两件事不能混。
TRUNCATED = "输出被截断"


def completion_status(step_result: dict[str, Any]) -> str:
    if step_result["ok"]:
        return "completed_with_truncation" \
            if step_result["extra"].get("truncated") else "completed"
    reason = step_result.get("reason")
    if reason == TIMEOUT:
        return "timeout"
    if reason in {UPSTREAM, LIMIT}:
        return "upstream_error"
    if reason == CFG:
        return "client_interrupted"
    if reason in {PROTO, AUTH}:
        return "protocol_error"
    if reason == TRUNCATED:
        return "completed_with_truncation"
    return "empty"


def attach_evaluation(
    step_result: dict[str, Any], item: dict[str, Any], *,
    text: str | None = None, tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    status = completion_status(step_result)
    observation = {
        "completion_status": status,
        "text": text if text is not None else step_result["extra"].get("reply", ""),
        "tool_calls": tool_calls or [],
        "finish_reason": "length" if status == "completed_with_truncation" else "stop",
    }
    grade_result = grading.grade(item, observation)
    attribution = evidence.attribution_for(
        completion_status=status, reason=str(step_result.get("reason") or ""))
    eligibility = evidence.assess(
        completion_status=status, attribution=attribution,
        grade_status=grade_result["status"],
        completion_policy=str(item.get("completion_policy") or "answer_sufficient"))
    step_result["extra"].update({
        "item": item["id"], "item_version": item.get("version", 1),
        "template_id": item.get("template_id", item["id"]),
        "template_version": item.get("template_version", 1),
        "item_seed": item.get("seed"), "item_variant": item.get("variant", 1),
        "comparison_key": item.get("comparison_key"),
        "method_id": item.get("method_id"),
        "slice": item.get("slice"), "dim": item.get("dim"),
        "level": item.get("level"),
        "score_domain": item.get("score_domain"),
        "variant": int(item.get("variant") or 1),
        "item_weight": float(item.get("weight") or 1.0),
        "completion_policy": item.get("completion_policy", "answer_sufficient"),
        "completion_status": status, "attribution": attribution,
        "grade": grade_result, "eligibility": eligibility,
    })
    return step_result


async def _chat_post(
    cli: httpx.AsyncClient, cfg: dict[str, Any], prompt: str, *,
    max_tokens: int, json_mode: bool = False,
    tools: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    timeout: float | None = None,
    model: str | None = None,
) -> httpx.Response:
    """发一条对话请求，参数口径不对时换另一种再试一次。

    为什么要重试：OpenAI o 系列一类推理模型**不接受 max_tokens 和 temperature**，
    要用 max_completion_tokens 且不能发 temperature。发错了上游直接 400，
    而平台所有步骤都走这一条路 —— 不重试的话，接一个 o3 会让 40 个请求全军覆没，
    报告上看是「协议不兼容」，实际只是参数名不对。

    按模型名先猜一次（省一个来回），猜错就换。结果缓存在 cfg 上，
    后续请求直接复用已经确认的口径。
    中转站经常改写模型名，光靠名字判会漏，所以真正兜底的是这个重试。
    """
    if not cfg.get("_request_profile_confirmed"):
        lock = cfg.setdefault("_request_profile_lock", asyncio.Lock())
        async with lock:
            if not cfg.get("_request_profile_confirmed"):
                return await _chat_post_negotiated(
                    cli, cfg, prompt, max_tokens=max_tokens,
                    json_mode=json_mode, tools=tools, messages=messages,
                    timeout=timeout, model=model)
    return await _chat_post_negotiated(
        cli, cfg, prompt, max_tokens=max_tokens,
        json_mode=json_mode, tools=tools, messages=messages, timeout=timeout,
        model=model)


def _request_profile(cfg: dict[str, Any]) -> tuple[str, bool]:
    variant = cfg.get("_variant") or protocol.payload_variant(
        cfg["protocol"], cfg["model"])
    return variant, bool(cfg.get("_send_temperature", True))


def _effective_parameters(variant: str, send_temperature: bool) -> dict[str, str]:
    return {
        "token_parameter": "max_completion_tokens" if variant == "reasoning"
                           else "max_tokens",
        "temperature": "0" if send_temperature and variant != "reasoning" else "omitted",
    }


def _set_response_trace(
    resp: httpx.Response, attempts: int, retry_reasons: list[str],
    variant: str, send_temperature: bool,
) -> None:
    resp.extensions["evaluator_attempts"] = attempts
    resp.extensions["evaluator_retry_reasons"] = list(retry_reasons)
    resp.extensions["evaluator_effective_parameters"] = _effective_parameters(
        variant, send_temperature)


def _response_trace(resp: httpx.Response) -> dict[str, Any]:
    return {
        "attempts": int(resp.extensions.get("evaluator_attempts") or 1),
        "retry_reasons": list(resp.extensions.get("evaluator_retry_reasons") or []),
        "effective_parameters": dict(
            resp.extensions.get("evaluator_effective_parameters") or {}),
    }


_RETRY_LABELS = {
    "drop_temperature": "已移除 temperature",
    "use_max_completion_tokens": "已改用 max_completion_tokens",
    "use_max_tokens": "已改用 max_tokens",
    "network_retry": "连接异常后重试",
}


def _recovery_note(trace: dict[str, Any]) -> str:
    reasons = [_RETRY_LABELS.get(reason, reason)
               for reason in trace.get("retry_reasons") or []]
    if not reasons:
        return ""
    return f"；第 {trace['attempts']} 次成功，{'、'.join(reasons)}"


def _is_retryable_network_error(exc: httpx.RequestError) -> bool:
    return isinstance(exc, (httpx.RemoteProtocolError, httpx.ConnectError,
                            httpx.ReadError, httpx.WriteError))


async def _chat_post_negotiated(
    cli: httpx.AsyncClient, cfg: dict[str, Any], prompt: str, *,
    max_tokens: int, json_mode: bool = False,
    tools: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    timeout: float | None = None,
    model: str | None = None,
) -> httpx.Response:
    variant, send_temperature = _request_profile(cfg)
    url = protocol.chat_url(cfg["protocol"], cfg["base_url"])
    headers = protocol.headers(cfg["protocol"], cfg["key"])
    kw: dict[str, Any] = {}
    if timeout is not None:
        kw["timeout"] = timeout

    attempts = 0
    network_retried = False
    retry_reasons: list[str] = []
    seen: set[tuple[str, bool]] = set()
    while attempts < 4:
        profile = (variant, send_temperature)
        if profile in seen:
            break
        seen.add(profile)
        body = protocol.chat_payload(
            cfg["protocol"], model or cfg["model"], prompt, max_tokens=max_tokens,
            json_mode=json_mode, tools=tools, messages=messages, variant=variant,
            send_temperature=send_temperature)
        while True:
            attempts += 1
            try:
                resp = await cli.post(url, headers=headers, json=body, **kw)
                break
            except httpx.RequestError as exc:
                if network_retried or not _is_retryable_network_error(exc):
                    raise
                network_retried = True
                retry_reasons.append("network_retry")
                await asyncio.sleep(0.25)
        if protocol.rejects_temperature(resp.status_code, resp.text) \
                and send_temperature:
            send_temperature = False
            cfg["_send_temperature"] = False
            retry_reasons.append("drop_temperature")
            continue
        if resp.status_code not in (400, 422) \
                or not protocol.rejects_token_parameter(resp.status_code, resp.text) \
                or cfg["protocol"] != "openai":
            cfg["_variant"] = variant
            cfg["_send_temperature"] = send_temperature
            cfg["_request_profile_confirmed"] = True
            _set_response_trace(resp, attempts, retry_reasons, variant, send_temperature)
            return resp
        variant = protocol.other_variant(variant)
        retry_reasons.append(
            "use_max_completion_tokens" if variant == "reasoning" else "use_max_tokens")
    cfg["_variant"] = variant
    cfg["_send_temperature"] = send_temperature
    _set_response_trace(resp, attempts, retry_reasons, variant, send_temperature)
    return resp


def classify(status: int, body: str) -> str:
    """按状态码与响应体归类失败原因。"""
    if status in (401, 403):
        return AUTH
    if status == 404:
        return CFG
    if status == 429:
        return LIMIT
    if status in (408, 504):
        return TIMEOUT
    if 500 <= status < 600:
        return UPSTREAM
    low = (body or "").lower()
    if "model" in low and ("not found" in low or "does not exist" in low):
        return CFG
    if status >= 400:
        return PROTO
    return PLATFORM


def embedded_error(text: str) -> tuple[str, str] | None:
    value = (text or "").strip()
    low = value[:600].lower()
    auth_markers = (
        "anthropic_auth_token 格式不正确", "invalid_auto_token",
        "invalid api key", "authentication failed", "unauthorized",
    )
    config_markers = ("missing api key", "base_url 格式不正确")
    if any(marker in low for marker in auth_markers):
        return AUTH, value
    if any(marker in low for marker in config_markers):
        return CFG, value
    return None


def result(
    step: str, ok: bool, *, reason: str = "", detail: str = "",
    latency: float = 0.0, first_token: float = 0.0,
    usage: dict[str, int] | None = None, extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """probe 的统一返回结构。"""
    return {
        "step": step, "ok": ok, "reason": reason, "detail": detail,
        "latency": round(latency, 3), "first_token": round(first_token, 3),
        "usage": usage or {"prompt": 0, "completion": 0},
        "extra": extra or {}, "ts": time.time(),
    }


def _short(text: str, key: str = "", limit: int = 300) -> str:
    """响应摘要：截断 + 抹掉 Key，避免把原始日志灌进报告。"""
    text = scrub((text or "").strip().replace("\n", " "), key)
    return text[:limit] + ("…" if len(text) > limit else "")


# 上游把自己的 system prompt 泄漏进正文时的特征串。
# 我们**只发一条 user 消息**，所以正文里出现这类自我介绍/接受指令的话，
# 说明上游在转发链路上注入了 system prompt，或者请求模板被改坏了。
INJECTION_MARKERS = (
    "official cli",
    "i'm claude code",
    "i am claude code",
    "you are claude",
    "acknowledged. i'm",
    "acknowledged. i am",
    "as an ai assistant developed by",
    # 不收 "system prompt"：正常的技术回答里会提到它，误报代价太高
    "我是一个由",
    "我将遵守",
    "已收到系统提示",
)


def detect_injection(text: str) -> str:
    """回答里是否混进了上游注入的内容。返回命中的特征串，没有则返回空。"""
    low = (text or "").strip().lower()
    if not low:
        return ""
    # 只看开头一段：注入通常出现在正文最前面
    head = low[:200]
    for mark in INJECTION_MARKERS:
        if mark in head:
            return mark
    return ""


async def probe_auth(cli: httpx.AsyncClient, cfg: dict[str, Any]) -> dict[str, Any]:
    """鉴权 + 模型是否存在。models 接口不可用时不判死，交给后续对话验证。"""
    step = "QA-PROTO-01 鉴权与模型可达性"
    t0 = time.perf_counter()
    url = protocol.models_url(cfg["protocol"], cfg["base_url"])
    try:
        resp = await cli.get(url, headers=protocol.headers(cfg["protocol"], cfg["key"]))
    except httpx.TimeoutException:
        return result(step, False, reason=TIMEOUT,
                      detail="请求模型清单超时", latency=time.perf_counter() - t0)
    except httpx.RequestError as exc:
        return result(step, False, reason=CFG,
                      detail=f"无法连接：{_short(str(exc), cfg['key'])}",
                      latency=time.perf_counter() - t0)
    cost = time.perf_counter() - t0
    if resp.status_code in (401, 403):
        return result(
            step, False, reason=AUTH,
            detail=f"HTTP {resp.status_code}，Key 被拒绝：{_short(resp.text, cfg['key'], 160)}",
            latency=cost)
    if resp.status_code >= 400:
        return result(step, True, reason="",
                      detail=f"模型清单不可用（HTTP {resp.status_code}），改由对话验证模型",
                      latency=cost, extra={"models_listed": False})
    try:
        names = protocol.list_models(cfg["protocol"], resp.json())
    except ValueError:
        return result(step, True,
                      detail="模型清单返回非 JSON，改由对话验证模型",
                      latency=cost, extra={"models_listed": False})
    if names and cfg["model"] not in names:
        return result(step, False, reason=CFG,
                      detail=f"清单中没有 {cfg['model']}（共 {len(names)} 个模型）",
                      latency=cost, extra={"models_listed": True, "model_count": len(names)})
    return result(step, True, detail=f"Key 有效，模型存在（清单 {len(names)} 个）",
                  latency=cost, extra={"models_listed": True, "model_count": len(names)})


async def probe_chat(
    cli: httpx.AsyncClient, cfg: dict[str, Any], *,
    step: str = "基础问答", prompt: str = "用一句话回答：中国的首都是哪里？",
    max_tokens: int = 64, json_mode: bool = False,
) -> dict[str, Any]:
    """非流式对话。同时核对上游返回的 model 是否与申报一致（防换模型）。"""
    t0 = time.perf_counter()
    try:
        resp = await _chat_post(cli, cfg, prompt, max_tokens=max_tokens,
                                json_mode=json_mode)
    except httpx.TimeoutException:
        return result(step, False, reason=TIMEOUT, detail="对话请求超时",
                      latency=time.perf_counter() - t0)
    except httpx.RequestError as exc:
        return result(step, False, reason=CFG,
                      detail=f"请求失败：{_short(str(exc), cfg['key'], 160)}",
                      latency=time.perf_counter() - t0)
    cost = time.perf_counter() - t0
    if resp.status_code >= 400:
        return result(step, False, reason=classify(resp.status_code, resp.text),
                      detail=f"HTTP {resp.status_code}：{_short(resp.text, cfg['key'], 200)}",
                      latency=cost)
    try:
        data = resp.json()
    except ValueError:
        return result(step, False, reason=PROTO,
                      detail=f"响应非 JSON：{_short(resp.text, cfg['key'], 160)}",
                      latency=cost)
    text, actual_model, usage = protocol.parse_reply(cfg["protocol"], data)
    _finish, truncated = protocol.parse_finish(cfg["protocol"], data)
    if not text.strip():
        # 正文空 + 撞上 token 上限 = 预算被思考烧穿，不是「上游返回空」。
        # 分开报，否则接一个推理模型会看到「返回正文为空」，方向全指错。
        if truncated:
            return result(
                step, False, reason=TRUNCATED,
                detail=f"正文为空且撞上 token 上限（{max_tokens}），"
                       f"疑似思考 token 占满预算"
                       + (f"，思考约 {protocol.reasoning_tokens(data)} token"
                          if protocol.reasoning_tokens(data) else ""),
                latency=cost, usage=usage, extra={"truncated": True})
        return result(step, False, reason=MISMATCH, detail="返回正文为空",
                      latency=cost, usage=usage)
    trace = _response_trace(resp)
    extra = {
        "reply": _short(text, cfg["key"], 200),
        "actual_model": actual_model,
        "usage_complete": usage["prompt"] >= 0 and usage["completion"] >= 0,
        **trace,
    }
    if truncated:
        extra["truncated"] = True
    # 模型名不一致只记证据，不直接判失败，由建议引擎决定严重程度
    if actual_model and not protocol.models_compatible(cfg["model"], actual_model):
        extra["model_mismatch"] = True
    return result(step, True,
                  detail=f"返回正常（{len(text)} 字）{_recovery_note(trace)}",
                  latency=cost, usage=usage, extra=extra)


async def probe_stream(
    cli: httpx.AsyncClient, cfg: dict[str, Any], *,
    step: str = "流式输出", prompt: str = "从 1 数到 20，用逗号分隔。",
    max_tokens: int = 200,
    performance_probe: bool = False,
) -> dict[str, Any]:
    if not cfg.get("_request_profile_confirmed"):
        lock = cfg.setdefault("_request_profile_lock", asyncio.Lock())
        async with lock:
            if not cfg.get("_request_profile_confirmed"):
                return await _probe_stream_negotiated(
                    cli, cfg, step=step, prompt=prompt, max_tokens=max_tokens,
                    performance_probe=performance_probe)
    return await _probe_stream_negotiated(
        cli, cfg, step=step, prompt=prompt, max_tokens=max_tokens,
        performance_probe=performance_probe)


async def _probe_stream_negotiated(
    cli: httpx.AsyncClient, cfg: dict[str, Any], *,
    step: str = "流式输出", prompt: str = "从 1 数到 20，用逗号分隔。",
    max_tokens: int = 200,
    performance_probe: bool = False,
) -> dict[str, Any]:
    """流式对话：测首 token 延迟，并判断是否中途断流。"""
    t0 = time.perf_counter()
    first = 0.0
    chunks = 0
    text = ""
    done = False
    actual_models: set[str] = set()
    variant, send_temperature = _request_profile(cfg)
    retry_reasons: list[str] = []
    attempts = 0
    network_retried = False
    while attempts < 4:
        try:
            body = protocol.chat_payload(
                cfg["protocol"], cfg["model"], prompt, stream=True,
                max_tokens=max_tokens, variant=variant,
                send_temperature=send_temperature)
            attempts += 1
            async with cli.stream(
                "POST", protocol.chat_url(cfg["protocol"], cfg["base_url"]),
                headers=protocol.headers(cfg["protocol"], cfg["key"]), json=body,
            ) as resp:
                if resp.status_code >= 400:
                    raw = (await resp.aread()).decode("utf-8", "replace")
                    if protocol.rejects_temperature(resp.status_code, raw) \
                            and send_temperature:
                        send_temperature = False
                        cfg["_send_temperature"] = False
                        retry_reasons.append("drop_temperature")
                        continue
                    if protocol.rejects_token_parameter(resp.status_code, raw):
                        variant = protocol.other_variant(variant)
                        cfg["_variant"] = variant
                        if variant == "reasoning":
                            send_temperature = False
                            cfg["_send_temperature"] = False
                        retry_reasons.append("switch_token_parameter")
                        continue
                    return result(
                        step, False, reason=classify(resp.status_code, raw),
                        detail=f"HTTP {resp.status_code}：{_short(raw, cfg['key'], 200)}",
                        latency=time.perf_counter() - t0,
                        extra={"attempts": attempts, "retry_reasons": retry_reasons,
                               "effective_parameters": _effective_parameters(
                                   variant, send_temperature)})
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    delta, finished, actual_model = protocol.parse_stream_chunk(
                        cfg["protocol"], line)
                    if actual_model:
                        actual_models.add(actual_model)
                    if delta:
                        if not chunks:
                            first = time.perf_counter() - t0
                        chunks += 1
                        text += delta
                    if finished:
                        done = True
                        break
                cfg["_variant"] = variant
                cfg["_send_temperature"] = send_temperature
                cfg["_request_profile_confirmed"] = True
                break
        except httpx.TimeoutException:
            return result(
                step, False, reason=TIMEOUT,
                detail=f"流式超时，已收到 {chunks} 个分片",
                latency=time.perf_counter() - t0, first_token=first,
                extra={"stream_break": True, "attempts": attempts,
                       "retry_reasons": retry_reasons,
                       "effective_parameters": _effective_parameters(
                           variant, send_temperature)})
        except httpx.RequestError as exc:
            if chunks == 0 and not network_retried \
                    and _is_retryable_network_error(exc):
                network_retried = True
                retry_reasons.append("network_retry")
                await asyncio.sleep(0.25)
                continue
            return result(
                step, False, reason=PROTO,
                detail=f"流式中断：{_short(str(exc), cfg['key'], 160)}",
                latency=time.perf_counter() - t0, first_token=first,
                extra={"stream_break": True, "attempts": attempts,
                       "retry_reasons": retry_reasons,
                       "effective_parameters": _effective_parameters(
                           variant, send_temperature)})
    cost = time.perf_counter() - t0
    if chunks == 0:
        return result(step, False, reason=PROTO, detail="未收到任何流式分片（可能不支持 SSE）",
                      latency=cost, extra={"stream_break": True})
    if not done:
        return result(step, False, reason=PROTO,
                      detail=f"流未正常结束，收到 {chunks} 个分片后断开",
                      latency=cost, first_token=first, extra={"stream_break": True})
    completion_tokens = max(round(len(text) / 4), 1)
    prompt_tokens = max(round(len(prompt) / 4), 1)
    generation_time = max(cost - first, 0.001)
    extra = {
        "chunks": chunks,
        "reply": _short(text, cfg["key"], 120),
        "grade_text": text,
        "usage_estimated": True,
        "performance_probe": performance_probe,
        "attempts": attempts,
        "retry_reasons": retry_reasons,
        "effective_parameters": _effective_parameters(variant, send_temperature),
    }
    if actual_models:
        extra["actual_model"] = sorted(actual_models)[0]
        extra["actual_models"] = sorted(actual_models)
        extra["model_drift"] = len(actual_models) > 1
        if any(not protocol.models_compatible(cfg["model"], actual)
               for actual in actual_models):
            extra["model_mismatch"] = True
    if chunks >= 3:
        extra["tokens_per_second"] = round(completion_tokens / generation_time, 2)
    return result(
        step, True,
        detail=f"流式正常，{chunks} 个分片，首 token {first:.2f}s"
               f"{_recovery_note(extra)}",
        latency=cost, first_token=first,
        usage={"prompt": prompt_tokens, "completion": completion_tokens},
        extra=extra,
    )


async def probe_error_handling(cli: httpx.AsyncClient, cfg: dict[str, Any]) -> dict[str, Any]:
    """用一个不存在的模型名请求，正确的上游应当返回 4xx 而不是假装成功。"""
    step = "错误处理"
    fake = "__no_such_model_zzz__"
    t0 = time.perf_counter()
    try:
        resp = await _chat_post(cli, cfg, "hi", max_tokens=16, model=fake)
    except httpx.TimeoutException:
        return result(step, False, reason=TIMEOUT, detail="错误处理探测超时",
                      latency=time.perf_counter() - t0)
    except httpx.RequestError as exc:
        return result(step, False, reason=PROTO,
                      detail=f"错误处理探测失败：{_short(str(exc), cfg['key'], 160)}",
                      latency=time.perf_counter() - t0)
    cost = time.perf_counter() - t0
    # 这一项要判的是「会不会静默兜底」，不是状态码好不好看。
    # 4xx 最规范；5xx（如网关没有可用渠道时返回 503）也是明确拒绝，同样算通过，
    # 只在说明里记一句实际状态码，留给人工看。
    if 400 <= resp.status_code < 500:
        return result(step, True,
                      detail=f"不存在的模型正确返回 HTTP {resp.status_code}", latency=cost)
    if resp.status_code >= 500:
        return result(step, True,
                      detail=f"不存在的模型被拒绝（HTTP {resp.status_code}），"
                             f"未静默兜底；规范做法是返回 4xx",
                      latency=cost, extra={"reject_status": resp.status_code})
    return result(step, False, reason=MISMATCH,
                  detail="请求不存在的模型仍返回成功，上游可能静默兜底到其它模型",
                  latency=cost, extra={"silent_fallback": True})


# ---- 能力探针：题目可确定性判分，便于与基线对比 ----

def _monitor_item(name: str, item_id: str, prompt: str, grader_id: str,
                  config: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name, "id": item_id, "version": 1, "template_id": item_id,
        "template_version": 1, "prompt": prompt, "max_tokens": 240,
        "score_domain": "monitoring", "completion_policy": "whole_response_required",
        "grader": {"id": grader_id, "version": "1.0.0", "config": config},
        "comparison_key": f"{item_id}/v1", "method_id": item_id,
    }


CAPABILITY_PROBES = [
    _monitor_item("数学计算", "MON-MATH-01",
                  "计算 17 乘以 23 等于多少？只回答整数。",
                  "exact_scalar", {"expected": 391}),
    _monitor_item("指令遵循", "MON-INSTR-01",
                  "只输出一个单词：OK。不要标点，不要解释。",
                  "exact_scalar", {"expected": "OK"}),
    _monitor_item("代码能力", "MON-CODE-01",
                  "写一个 Python 函数 add(a, b) 返回两数之和。只输出代码。",
                  "executable_code", {"language": "python", "function": "add",
                                      "tests": [{"args": [2, 3], "expected": 5},
                                                {"args": [-1, 4], "expected": 3}]}),
    _monitor_item("JSON 输出", "MON-JSON-01",
                  '严格输出这个 JSON：{"status":"ok","count":3}',
                  "json_schema_exact", {"expected_value": {"status": "ok", "count": 3}}),
    _monitor_item("常识问答", "MON-FACT-01",
                  "水的沸点在标准大气压下是多少摄氏度？只回答整数。",
                  "exact_scalar", {"expected": 100}),
]


async def probe_eval_item(
    cli: httpx.AsyncClient, cfg: dict[str, Any], dim: str,
    item: dict[str, Any], max_tokens: int,
) -> dict[str, Any]:
    """Execute one item and attach a deterministic grade to the full response."""
    step = f"{dim}·{item['id']}"
    t0 = time.perf_counter()
    base = {"item": item["id"], "level": item.get("level"),
            "item_weight": float(item.get("weight") or 1.0),
            "method_id": item.get("method_id"),
            "tier_item": item.get("tier_item"),
            "specialty_profile": item.get("profile"),
            "evaluation_package": item.get("package"),
            "workload": item.get("workload"),
            "cache_sequence": item.get("cache_sequence"),
            "variant": int(item.get("variant") or 1),
            "input_text": item.get("prompt", "")}
    base["dim"] = dim
    prompts = item.get("turns") or [item["prompt"]]
    conversation: list[dict[str, Any]] = []
    turn_replies: list[str] = []
    usage = {"prompt": 0, "completion": 0, "cached_read": 0, "cached_write": 0}
    text = ""
    actual_model = ""
    calls: list[dict[str, Any]] = []
    truncated = False
    think = 0

    for turn_no, prompt in enumerate(prompts, 1):
        conversation.append({"role": "user", "content": prompt})
        try:
            resp = await _chat_post(
                cli, cfg, prompt, max_tokens=max_tokens,
                json_mode=bool(item.get("json_mode")), tools=item.get("tools"),
                messages=conversation)
        except httpx.TimeoutException:
            return result(step, False, reason=TIMEOUT,
                          detail=f"第 {turn_no} 轮评测请求超时",
                          latency=time.perf_counter() - t0,
                          extra={**base, "turn": turn_no})
        except httpx.RequestError as exc:
            return result(step, False, reason=CFG,
                          detail=f"第 {turn_no} 轮请求失败："
                                 f"{_short(str(exc), cfg['key'], 160)}",
                          latency=time.perf_counter() - t0,
                          extra={**base, "turn": turn_no})
        if resp.status_code >= 400:
            return result(step, False, reason=classify(resp.status_code, resp.text),
                          detail=f"第 {turn_no} 轮 HTTP {resp.status_code}："
                                 f"{_short(resp.text, cfg['key'], 200)}",
                          latency=time.perf_counter() - t0,
                          extra={**base, "turn": turn_no})
        try:
            data = resp.json()
        except ValueError:
            return result(step, False, reason=PROTO,
                          detail=f"第 {turn_no} 轮响应非 JSON："
                                 f"{_short(resp.text, cfg['key'], 160)}",
                          latency=time.perf_counter() - t0,
                          extra={**base, "turn": turn_no})

        text, actual_model, turn_usage = protocol.parse_reply(cfg["protocol"], data)
        calls = protocol.parse_tool_calls(cfg["protocol"], data)
        _finish, truncated = protocol.parse_finish(cfg["protocol"], data)
        think += protocol.reasoning_tokens(data)
        for key in usage:
            value = turn_usage[key]
            if value < 0:
                usage[key] = -1
            elif usage[key] >= 0:
                usage[key] += value
        turn_replies.append(_short(text, cfg["key"], 240))
        upstream_error = embedded_error(text)
        if upstream_error:
            reason, raw = upstream_error
            return result(
                step, False, reason=reason,
                detail=f"第 {turn_no} 轮上游以成功状态返回错误正文："
                       f"{_short(raw, cfg['key'], 140)}",
                latency=time.perf_counter() - t0, usage=usage,
                extra={**base, "turn": turn_no, "embedded_error": True,
                       "reply": _short(text, cfg["key"], 200)},
            )
        if turn_no < len(prompts):
            if not text.strip():
                return result(step, False, reason=MISMATCH,
                              detail=f"第 {turn_no} 轮返回正文为空",
                              latency=time.perf_counter() - t0, usage=usage,
                              extra={**base, "turn": turn_no,
                                     "turn_replies": turn_replies})
            conversation.append({"role": "assistant", "content": text})

    cost = time.perf_counter() - t0

    # 正文空 + 撞上 token 上限 + 没发起工具调用 = 预算被思考烧穿，这道题没测到。
    #
    # 内容题的预算既要容纳推理模型的 thinking，也不能无限放宽到失去成本边界。
    # 若 thinking 烧穿预算、正文为空，判分函数给 0 分会把“没测到”误写成“答错”。
    # 所以这种情况记成 graded=False（未测到，维度分会排除），不是 0 分。
    # 预算变化属于题库口径变化，必须升级版本后重新建立标杆。
    if truncated and (not (text or "").strip() or item.get("complete_required")) \
            and not calls:
        return result(
            step, False, reason=TRUNCATED,
            detail=f"输出未完整且撞上 token 上限（{max_tokens}），这道题未测到"
                   + (f"，思考约 {think} token" if think else "")
                   + "；推理模型建议单独评测或调高该维度预算",
            latency=cost, usage=usage,
            extra={**base, "truncated": True, "reasoning_tokens": think})

    injected = detect_injection(text)
    extra = {
        "item": item["id"],
        "level": item.get("level"),
        "item_weight": float(item.get("weight") or 1.0),
        "method_id": item.get("method_id"),
        "tier_item": item.get("tier_item"),
        "specialty_profile": item.get("profile"),
        "evaluation_package": item.get("package"),
        "workload": item.get("workload"),
        "cache_sequence": item.get("cache_sequence"),
        "variant": int(item.get("variant") or 1),
        "input_text": item.get("prompt", ""),
        "injected": injected,
        "reply": _short(text, cfg["key"], 200),
        "reply_len": len(text or ""),
        "actual_model": actual_model,
        "usage_complete": usage["prompt"] >= 0 and usage["completion"] >= 0,
        "turn_count": len(prompts),
        "turn_replies": turn_replies,
        **_response_trace(resp),
    }
    extra["dim"] = dim
    # 截断但有正文：照常判分（可能答案已经写完了才被切），只留个标记。
    # 报告里会汇总截断数 —— 大面积截断说明预算不适配这个模型，分数要打问号。
    if truncated:
        extra["truncated"] = True
    if think:
        extra["reasoning_tokens"] = think
    if item.get("tools"):
        extra["tool_calls"] = [c["name"] for c in calls]
    if actual_model and not protocol.models_compatible(cfg["model"], actual_model):
        extra["model_mismatch"] = True

    evaluated = attach_evaluation(
        result(step, True, detail="回答已完成" + _recovery_note(extra),
               latency=cost, usage=usage, extra=extra),
        item, text=text, tool_calls=calls)
    grade_result = evaluated["extra"]["grade"]
    evaluated["detail"] = (
        f"内容判分 {grade_result['status']}，得分 "
        f"{grade_result['score'] if grade_result['score'] is not None else '—'}"
    )
    return evaluated


async def probe_hard_item(
    cli: httpx.AsyncClient, cfg: dict[str, Any], item: dict[str, Any],
) -> dict[str, Any]:
    """跑一道硬题（编程硬核 / HLE）并判分。0.0 或 1.0，没有部分分。

    与四维核心题的关键区别，都体现在 extra 里：
      - 打 `hard=True` 和 `bank`，**不打 `dim`** —— 所以不会进四维能力总分，
        已有的四维标杆不会因为加了硬题而失效。
      - 报告侧会把 hard 步骤排除在可用性成功率与超时门槛之外。硬题本来就是
        「前沿模型也大面积做错」的题，而且慢；让它拉低成功率或撑爆超时门槛，
        会把一条好渠道判成暂不准入。
    """
    step = f"硬题·{item['id']}"
    t0 = time.perf_counter()
    base = {
        "hard": True, "bank": item["bank"], "item": item["id"],
        "group": item.get("group", ""), "scored": 0.0, "graded": False,
    }
    budget = int(item["max_tokens"])
    try:
        resp = await _chat_post(cli, cfg, item["prompt"], max_tokens=budget,
                                timeout=HARD_HTTP_TIMEOUT)
    except httpx.TimeoutException:
        return result(step, False, reason=TIMEOUT,
                      detail=f"硬题请求超时（上限 {HARD_HTTP_TIMEOUT:.0f}s）",
                      latency=time.perf_counter() - t0, extra=base)
    except httpx.RequestError as exc:
        return result(step, False, reason=CFG,
                      detail=f"请求失败：{_short(str(exc), cfg['key'], 160)}",
                      latency=time.perf_counter() - t0, extra=base)
    cost = time.perf_counter() - t0

    if resp.status_code >= 400:
        return result(step, False, reason=classify(resp.status_code, resp.text),
                      detail=f"HTTP {resp.status_code}：{_short(resp.text, cfg['key'], 200)}",
                      latency=cost, extra=base)
    try:
        data = resp.json()
    except ValueError:
        return result(step, False, reason=PROTO,
                      detail=f"响应非 JSON：{_short(resp.text, cfg['key'], 160)}",
                      latency=cost, extra=base)

    text, actual_model, usage = protocol.parse_reply(cfg["protocol"], data)
    _finish, truncated = protocol.parse_finish(cfg["protocol"], data)
    think = protocol.reasoning_tokens(data)
    got = hardbank.extract_answer(text)

    # 撞上 token 上限且抽不到答案 = 这道题没测到，不是答错。
    #
    # **思考 token 算在预算里**，所以推理模型可能整个 3.2 万都烧在 thinking 上，
    # 正文一个字都没输出。记成答错的话，报告里跟真答错完全一样，
    # 从分数上看不出来 —— 那会系统性压低所有推理模型，标杆直接废掉。
    # graded=False 的题不进正确率分母（见 scoring.hard_scores），
    # 报告里单独列出来，让人看见「这批题需要更高预算」。
    if truncated and not got:
        return result(
            step, False, reason=TRUNCATED,
            detail=f"撞上 token 上限（{budget}）且未给出答案，这道题未测到"
                   + (f"，思考约 {think} token" if think else "")
                   + f"；可用环境变量调高预算（当前 {item['bank']} = {budget}）",
            latency=cost, usage=usage,
            extra={**base, "truncated": True, "reasoning_tokens": think,
                   "budget": budget, "reply_len": len(text or "")})

    try:
        score = float(hardbank.score(item, text))
    except Exception as exc:
        return result(step, False, reason=PLATFORM,
                      detail=f"判分函数异常：{exc}", latency=cost, usage=usage,
                      extra=base)
    extra = {
        **base,
        "scored": score, "graded": True,
        "injected": detect_injection(text),
        "answer": _short(got, cfg["key"], 60),
        "expected": item["expected"][0],
        "reply": _short(text, cfg["key"], 200),
        "reply_len": len(text or ""),
        "actual_model": actual_model,
        "usage_complete": usage["prompt"] >= 0 and usage["completion"] >= 0,
        "budget": budget,
        **_response_trace(resp),
    }
    # 截断但抽到了答案：照常判分（答案写完了才被切），只留个标记
    if truncated:
        extra["truncated"] = True
    if think:
        extra["reasoning_tokens"] = think
    if actual_model and not protocol.models_compatible(cfg["model"], actual_model):
        extra["model_mismatch"] = True

    ok = score >= 0.999
    return result(step, ok, reason="" if ok else MISMATCH,
                  detail=(f"答对（{extra['answer']}）" if ok else
                          f"答错：给出 {extra['answer'] or '（空）'}，"
                          f"正确答案 {item['expected'][0]}") + _recovery_note(extra),
                  latency=cost, usage=usage, extra=extra)


async def probe_capability(
    cli: httpx.AsyncClient, cfg: dict[str, Any], item: dict[str, Any],
) -> dict[str, Any]:
    """Run one monitoring item through the same registered grader contract."""
    result_value = await probe_eval_item(
        cli, cfg, "能力抽检", item, int(item["max_tokens"]))
    result_value["extra"]["probe_name"] = item["name"]
    return result_value
