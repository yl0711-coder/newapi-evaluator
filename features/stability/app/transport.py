from __future__ import annotations

import json
import math
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .egress import EgressDenied, validate_url
from .security import scrub


INSPECT_VERSION = "ins-v2"
PROBES = (
    {"id": "connect", "name": "连通抽检", "stream": False, "prompt": "用一句话回答：中国的首都是哪里？", "max_tokens": 256},
    {"id": "performance-1", "name": "性能基线 1", "stream": True, "prompt": "请连续写出 80 个从 101 开始的整数，用英文逗号分隔，不要解释。", "max_tokens": 320},
    {"id": "performance-2", "name": "性能基线 2", "stream": True, "prompt": "请连续写出 80 个从 301 开始的整数，用英文逗号分隔，不要解释。", "max_tokens": 320},
    {"id": "performance-3", "name": "性能基线 3", "stream": True, "prompt": "请连续写出 80 个从 501 开始的整数，用英文逗号分隔，不要解释。", "max_tokens": 320},
    {"id": "math", "name": "数学计算", "stream": False, "prompt": "计算 17 乘以 23 等于多少？只回答数字，不要任何其它文字。", "expect": "391", "judge": "contains", "max_tokens": 200},
    {"id": "instruction", "name": "指令遵循", "stream": False, "prompt": "只输出一个单词：OK。不要标点，不要解释。", "expect": "OK", "judge": "exact", "max_tokens": 200},
)


def endpoint_url(base_url: str, protocol: str) -> str:
    value = base_url.strip().rstrip("/")
    parsed = urlsplit(value)
    path = parsed.path.rstrip("/")
    if protocol == "anthropic":
        if not path.endswith("/messages"):
            path = f"{path}/messages" if path.endswith("/v1") else f"{path}/v1/messages"
    elif not path.endswith("/chat/completions"):
        path = f"{path}/chat/completions" if path.endswith("/v1") else f"{path}/v1/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def headers(channel: dict[str, Any]) -> dict[str, str]:
    output = {"Content-Type": "application/json"}
    if channel["protocol"] == "anthropic":
        output["x-api-key"] = channel["api_key"]
        output["anthropic-version"] = "2023-06-01"
    else:
        output["Authorization"] = f"Bearer {channel['api_key']}"
    return output


def payload(channel: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    if channel["protocol"] == "anthropic":
        return {
            "model": channel["model"],
            "messages": [{"role": "user", "content": probe["prompt"]}],
            "max_tokens": probe["max_tokens"],
            "stream": probe["stream"],
        }
    return {
        "model": channel["model"],
        "messages": [{"role": "user", "content": probe["prompt"]}],
        "max_tokens": probe["max_tokens"],
        "stream": probe["stream"],
        **({"stream_options": {"include_usage": True}} if probe["stream"] else {}),
    }


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in value if isinstance(item, (str, dict))
        )
    return ""


def _usage(payload_value: dict[str, Any]) -> int | None:
    usage = payload_value.get("usage") or {}
    for name in ("completion_tokens", "output_tokens"):
        value = usage.get(name)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _usage_complete(data: dict[str, Any]) -> bool:
    usage = data.get("usage") or {}
    has_input = any(isinstance(usage.get(name), int) for name in ("prompt_tokens", "input_tokens"))
    has_output = any(isinstance(usage.get(name), int) for name in ("completion_tokens", "output_tokens"))
    return has_input and has_output


def _nonstream_content(data: dict[str, Any]) -> tuple[str, str, int | None, str, bool]:
    choices = data.get("choices") or []
    if choices:
        choice = choices[0] or {}
        message = choice.get("message") or {}
        return (
            _text(message.get("content") or choice.get("text")),
            str(choice.get("finish_reason") or ""),
            _usage(data),
            str(data.get("model") or ""),
            _usage_complete(data),
        )
    blocks = data.get("content") or []
    if isinstance(blocks, list):
        return (
            _text(blocks), str(data.get("stop_reason") or ""), _usage(data),
            str(data.get("model") or ""), _usage_complete(data),
        )
    return "", "", _usage(data), str(data.get("model") or ""), _usage_complete(data)


def _matches(probe: dict[str, Any], content: str) -> bool:
    expected = str(probe.get("expect") or "")
    if not expected:
        return bool(content.strip())
    if probe.get("judge") == "exact":
        return content.strip().strip(".。!！").casefold() == expected.casefold()
    return expected.casefold() in content.casefold()


def _model_mismatch(requested: str, actual: str) -> bool:
    if not requested or not actual:
        return False
    requested_key = requested.casefold()
    actual_key = actual.casefold()
    return requested_key not in actual_key and actual_key not in requested_key


def _http_status(status_code: int) -> str:
    if status_code in {401, 403}:
        return "auth_error"
    if status_code == 429:
        return "rate_limited"
    if status_code in {408, 504}:
        return "timeout"
    if status_code >= 500:
        return "upstream_5xx"
    return "http_error"


async def run_probe(client: httpx.AsyncClient, channel: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    url = endpoint_url(channel["base_url"], channel["protocol"])
    started = perf_counter()
    base = {
        "probe_id": probe["id"], "probe_name": probe["name"], "ok": False,
        "status": "error", "latency_ms": None, "ttft_ms": None,
        "tokens_per_second": None, "output_tokens": None, "finish_reason": "", "error": "",
        "actual_model": "", "usage_complete": False, "model_mismatch": False,
        "stream_break": False,
    }
    chunks = 0
    try:
        await validate_url(url)
        if not probe["stream"]:
            response = await client.post(url, headers=headers(channel), json=payload(channel, probe))
            elapsed = round((perf_counter() - started) * 1000)
            if response.status_code >= 400:
                return {**base, "status": _http_status(response.status_code), "latency_ms": elapsed,
                        "error": f"HTTP {response.status_code}"}
            try:
                data = response.json()
            except ValueError:
                return {**base, "status": "invalid_response", "latency_ms": elapsed, "error": "响应不是 JSON"}
            if not isinstance(data, dict):
                return {**base, "status": "invalid_response", "latency_ms": elapsed, "error": "响应 JSON 不是对象"}
            content, finish_reason, output_tokens, actual_model, usage_complete = _nonstream_content(data)
            evidence = {
                "actual_model": actual_model,
                "usage_complete": usage_complete,
                "model_mismatch": _model_mismatch(channel["model"], actual_model),
            }
            if not content.strip():
                return {**base, "status": "empty_response", "latency_ms": elapsed,
                        "finish_reason": finish_reason, "output_tokens": output_tokens, **evidence}
            ok = _matches(probe, content)
            expected = str(probe.get("expect") or "")
            return {**base, "ok": ok, "status": "completed" if ok else "content_mismatch",
                    "latency_ms": elapsed, "finish_reason": finish_reason,
                    "output_tokens": output_tokens, **evidence,
                    "error": "" if ok else f"未找到预期结果 {expected}"}

        first_output_at: float | None = None
        last_output_at: float | None = None
        output_tokens: int | None = None
        finish_reason = ""
        chars = 0
        done = False
        actual_model = ""
        has_input_usage = False
        has_output_usage = False
        async with client.stream("POST", url, headers=headers(channel), json=payload(channel, probe)) as response:
            if response.status_code >= 400:
                await response.aread()
                return {**base, "status": _http_status(response.status_code),
                        "latency_ms": round((perf_counter() - started) * 1000),
                        "error": f"HTTP {response.status_code}"}
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":") or line.startswith("event:"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    done = True
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                output_tokens = _usage(data) or output_tokens
                message = data.get("message") or {}
                usage = data.get("usage") or message.get("usage") or {}
                has_input_usage = has_input_usage or any(
                    isinstance(usage.get(name), int) for name in ("prompt_tokens", "input_tokens")
                )
                has_output_usage = has_output_usage or any(
                    isinstance(usage.get(name), int) for name in ("completion_tokens", "output_tokens")
                )
                actual_model = str(data.get("model") or message.get("model") or actual_model)
                content = ""
                choices = data.get("choices") or []
                if choices:
                    choice = choices[0] or {}
                    delta = choice.get("delta") or {}
                    content = _text(delta.get("content")) or _text(delta.get("reasoning_content"))
                    finish_reason = str(choice.get("finish_reason") or finish_reason)
                    done = done or bool(choice.get("finish_reason"))
                event_type = str(data.get("type") or "")
                if event_type == "content_block_delta":
                    delta = data.get("delta") or {}
                    content = _text(delta.get("text")) or _text(delta.get("thinking"))
                elif event_type == "message_delta":
                    finish_reason = str((data.get("delta") or {}).get("stop_reason") or finish_reason)
                elif event_type == "message_stop":
                    done = True
                if content:
                    now = perf_counter()
                    first_output_at = first_output_at or now
                    last_output_at = now
                    chunks += 1
                    chars += len(content)
        ended = perf_counter()
        latency_ms = round((ended - started) * 1000)
        ttft_ms = round((first_output_at - started) * 1000) if first_output_at else None
        usage_complete = has_input_usage and has_output_usage
        if chars == 0:
            return {**base, "status": "empty_response", "latency_ms": latency_ms,
                    "ttft_ms": ttft_ms, "finish_reason": finish_reason, "output_tokens": output_tokens,
                    "actual_model": actual_model, "usage_complete": usage_complete,
                    "model_mismatch": _model_mismatch(channel["model"], actual_model)}
        if not done:
            return {**base, "status": "stream_break", "latency_ms": latency_ms,
                    "ttft_ms": ttft_ms, "finish_reason": finish_reason, "output_tokens": output_tokens,
                    "actual_model": actual_model, "usage_complete": usage_complete,
                    "model_mismatch": _model_mismatch(channel["model"], actual_model),
                    "stream_break": True,
                    "error": "连接结束但未收到完成标记"}
        generation = (last_output_at - first_output_at) if first_output_at and last_output_at else 0
        measured_tokens = output_tokens or max(round(chars / 4), 1)
        speed = round(measured_tokens / generation, 1) if chunks > 1 and generation > 0 else None
        return {**base, "ok": True, "status": "completed", "latency_ms": latency_ms,
                "ttft_ms": ttft_ms, "finish_reason": finish_reason,
                "output_tokens": measured_tokens, "tokens_per_second": speed,
                "actual_model": actual_model, "usage_complete": usage_complete,
                "model_mismatch": _model_mismatch(channel["model"], actual_model)}
    except EgressDenied as exc:
        return {**base, "status": "egress_denied", "latency_ms": round((perf_counter() - started) * 1000),
                "error": scrub(str(exc), channel.get("api_key", ""))}
    except httpx.TimeoutException:
        return {**base, "status": "timeout", "latency_ms": round((perf_counter() - started) * 1000),
                "stream_break": bool(probe["stream"]), "error": "请求超时"}
    except httpx.HTTPError as exc:
        return {**base, "status": "network_error", "latency_ms": round((perf_counter() - started) * 1000),
                "stream_break": bool(probe["stream"]),
                "error": scrub(type(exc).__name__, channel.get("api_key", ""))}
    except ValueError as exc:
        return {**base, "status": "invalid_response", "latency_ms": round((perf_counter() - started) * 1000),
                "error": scrub(str(exc), channel.get("api_key", ""))}


def percentile(values: list[float], quantile: float) -> float | None:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return None
    return clean[max(0, math.ceil(len(clean) * quantile) - 1)]


def summarize(results: list[dict[str, Any]], thresholds: dict[str, Any]) -> dict[str, Any]:
    total = len(results)
    completed = sum(bool(item["ok"]) for item in results)
    streaming = [item for item in results if item["probe_id"].startswith("performance-")]
    timeout_count = sum(item["status"] == "timeout" for item in results)
    stream_break_count = sum(
        item["status"] == "stream_break" or bool(item.get("stream_break")) for item in streaming
    )
    latencies = [float(item["latency_ms"]) for item in results if item.get("latency_ms") is not None]
    successful_latencies = [
        float(item["latency_ms"]) for item in results
        if item.get("ok") and item.get("latency_ms") is not None
    ]
    ttfts = [float(item["ttft_ms"]) for item in streaming if item.get("ttft_ms") is not None]
    speeds = [float(item["tokens_per_second"]) for item in streaming if item.get("tokens_per_second") is not None]
    pass_rate = completed / total if total else 0.0
    timeout_rate = timeout_count / total if total else 0.0
    break_rate = stream_break_count / len(streaming) if streaming else 0.0
    p95 = percentile(latencies, 0.95)
    failures: dict[str, int] = {}
    for item in results:
        if not item["ok"]:
            failures[item["status"]] = failures.get(item["status"], 0) + 1
    reasons = []
    if pass_rate < float(thresholds["min_success_rate"]):
        reasons.append("成功率低于阈值")
    if timeout_rate > float(thresholds["max_timeout_rate"]):
        reasons.append("超时率超过阈值")
    if break_rate > float(thresholds["max_stream_break_rate"]):
        reasons.append("断流率超过阈值")
    if p95 is not None and p95 > float(thresholds["max_p95_ms"]):
        reasons.append("P95 延迟超过阈值")
    return {
        "version": INSPECT_VERSION,
        "total": total,
        "completed": completed,
        "pass_rate": round(pass_rate, 4),
        "timeout_rate": round(timeout_rate, 4),
        "timeout_count": timeout_count,
        "stream_break_rate": round(break_rate, 4),
        "stream_break_count": stream_break_count,
        "streaming_total": len(streaming),
        "p50_latency_ms": percentile(latencies, 0.5),
        "p95_latency_ms": p95,
        "p95_success_latency_ms": percentile(successful_latencies, 0.95),
        "p50_ttft_ms": percentile(ttfts, 0.5),
        "p95_ttft_ms": percentile(ttfts, 0.95),
        "p50_tokens_per_second": percentile(speeds, 0.5),
        "failures": failures,
        "verdict": "pass" if not reasons and total else "fail",
        "reasons": reasons or (["没有测试结果"] if not total else []),
    }
