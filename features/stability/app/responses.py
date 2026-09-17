"""Responses measurements reuse the admission SSE parser and preserve token semantics."""
import asyncio
import json
from time import perf_counter

import httpx

from features.admission.main import (GPT6_OUTPUT_LIMIT, GPT6_REASONING_EFFORT,
                                     RESPONSES_TOTAL_TIMEOUT_SECONDS, parse_stream_line, stream_events)
from .egress import EgressDenied, validate_url
from .security import scrub
from .transport import _http_status, _matches, _model_mismatch, endpoint_url, headers


async def run_probe(client, channel, probe):
    started = perf_counter()
    base = {"probe_id": probe["id"], "probe_name": probe["name"], "ok": False,
            "status": "error", "latency_ms": None, "ttft_ms": None, "tokens_per_second": None,
            "output_tokens": None, "finish_reason": "", "error": "", "actual_model": "",
            "usage_complete": False, "model_mismatch": False, "stream_break": False}

    async def measure():
        url = endpoint_url(channel["base_url"], "responses")
        await validate_url(url)
        body = {"model": channel["model"], "input": probe["prompt"], "stream": probe["stream"],
                "store": False, "max_output_tokens": max(GPT6_OUTPUT_LIMIT, probe["max_tokens"]),
                "reasoning": {"effort": GPT6_REASONING_EFFORT}}
        answer = ""
        blocks = {}
        terminal = ""
        protocol_error = False
        refused = False
        first = last = None
        count = 0
        recovered = False
        input_tokens = output_tokens = reasoning_tokens = None
        actual = finish = ""
        async with client.stream("POST", url, headers=headers(channel), json=body) as response:
            if response.status_code != 200:
                return {**base, "status": _http_status(response.status_code), "error": f"HTTP {response.status_code}"}

            async def events():
                if probe["stream"]:
                    async for event in stream_events(response, "responses"):
                        yield event
                else:
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 1_048_576:
                            raise ValueError("response_too_large")
                    data = json.loads(content)
                    if not isinstance(data, dict) or data.get("status") not in {"completed", "incomplete", "failed"}:
                        raise ValueError("invalid_response")
                    yield parse_stream_line(json.dumps({"type": "response." + data["status"], "response": data}))

            async for event in events():
                if not event or not event["recognized"] or event["format"] != "responses":
                    protocol_error = True
                    continue
                if terminal and (event["response_status"] or event["content"] or event["reasoning"] or event["block_content"] is not None):
                    protocol_error = True
                protocol_error = protocol_error or bool(event["protocol_error"])
                terminal = event["response_status"] or terminal
                refused = refused or event["refused"]
                actual = scrub(event["actual_model"] or actual, channel["api_key"])[:160]
                finish = event["finish_reason"] or finish
                if event["input_tokens"] is not None:
                    input_tokens = event["input_tokens"]
                if event["output_tokens"] is not None:
                    output_tokens = event["output_tokens"]
                if event["reasoning_tokens"] is not None:
                    reasoning_tokens = event["reasoning_tokens"]
                delta = event["content"]
                if event["block_key"] is not None:
                    key = event["block_key"]
                    previous = blocks.get(key, "")
                    if event["block_content"] is not None:
                        complete = event["block_content"]
                        if not complete.startswith(previous):
                            protocol_error = True
                            delta = ""
                        else:
                            delta = complete[len(previous):]
                            recovered = recovered or bool(delta)
                    blocks[key] = previous + delta
                if event["final_content"] is not None:
                    complete = event["final_content"]
                    if not complete.startswith(answer):
                        protocol_error = True
                        delta = ""
                    else:
                        delta = complete[len(answer):]
                        recovered = recovered or bool(delta)
                if delta:
                    now = perf_counter()
                    first = first if first is not None else now
                    last = now
                    count += 1
                    answer += delta
                    if len(answer) > 1_048_576:
                        raise ValueError("response_too_large")
        status = ("invalid_response" if protocol_error else "upstream_5xx" if terminal == "failed"
                  else "stream_break" if not terminal and probe["stream"] else "truncated" if terminal != "completed"
                  else "refused" if refused else "empty_response" if not answer.strip()
                  else "completed" if _matches(probe, answer) else "content_mismatch")
        speed = None
        if probe["stream"] and not recovered and reasoning_tokens == 0 and output_tokens is not None and count > 1 and last > first:
            speed = round(output_tokens / (last - first), 1)
        return {**base, "ok": status == "completed", "status": status,
                "ttft_ms": round((first - started) * 1000) if first is not None and probe["stream"] else None,
                "output_tokens": output_tokens, "tokens_per_second": speed, "finish_reason": finish,
                "actual_model": actual, "usage_complete": input_tokens is not None and output_tokens is not None,
                "model_mismatch": _model_mismatch(channel["model"], actual), "stream_break": status == "stream_break"}
    try:
        result = await asyncio.wait_for(measure(), RESPONSES_TOTAL_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        result = {**base, "status": "timeout", "stream_break": bool(probe["stream"]), "error": "请求超时"}
    except EgressDenied:
        result = {**base, "status": "egress_denied", "error": "上游地址不在允许范围"}
    except httpx.HTTPError:
        result = {**base, "status": "network_error", "stream_break": bool(probe["stream"]), "error": "上游连接失败"}
    except (ValueError, TypeError, AttributeError, KeyError, IndexError):
        result = {**base, "status": "invalid_response", "error": "Responses 响应格式无效"}
    return {**result, "latency_ms": round((perf_counter() - started) * 1000)}
