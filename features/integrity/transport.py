"""Bounded integrity text transport. Answers are returned in memory only for projection."""
from __future__ import annotations

from contextlib import asynccontextmanager

import asyncio
import json
from time import perf_counter

import httpx

from features.admission.main import parse_stream_line
from features.stability.app.egress import EgressDenied, validate_url
from features.stability.app.transport import endpoint_url, headers, _http_status, _reported_usage, _safe_model
from shared.network import SocketEgressDenied, guarded_transport, socket_egress_denied

MAX_RESPONSE_BYTES = 1_048_576
_slots: asyncio.Semaphore | None = None
_slot_loop = None


def _reasoning_usage(data):
    usage = data.get("usage") or (data.get("message") or {}).get("usage") or {}
    details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    value = details.get("reasoning_tokens")
    return value if type(value) is int and value >= 0 else None


def integrity_semaphore():
    """The new low-traffic plan's slot does not change legacy/pressure-test concurrency."""
    global _slots, _slot_loop
    loop = asyncio.get_running_loop()
    if _slot_loop is not loop:
        _slots, _slot_loop = asyncio.Semaphore(1), loop
    return _slots


def payload(channel, probe):
    protocol = channel["protocol"]
    if protocol not in {"openai", "anthropic", "responses"}:
        raise ValueError("unsupported protocol")
    cap = probe["max_tokens"]
    if type(cap) is not int or not 1 <= cap <= 100_000:
        raise ValueError("invalid output cap")
    body = {"model": channel["model"], "stream": bool(probe.get("stream", False))}
    prompt, system = probe["prompt"], probe.get("system_prompt", "")
    if not isinstance(prompt, str) or not isinstance(system, str):
        raise ValueError("invalid prompt")
    if protocol == "responses":
        body.update(input=prompt, max_output_tokens=cap, store=False)
        if system:
            body["instructions"] = system
        if probe.get("reasoning_effort") is not None:
            body["reasoning"] = {"effort": probe["reasoning_effort"]}
    else:
        body.update(messages=[{"role": "user", "content": prompt}], max_tokens=cap)
        if system:
            if protocol == "anthropic":
                body["system"] = system
            else:
                body["messages"].insert(0, {"role": "system", "content": system})
        if probe.get("reasoning_effort") is not None:
            if protocol == "anthropic":
                raise ValueError("reasoning effort is unsupported for this Messages adapter")
            body["reasoning_effort"] = probe["reasoning_effort"]
        if protocol == "openai" and body["stream"]:
            body["stream_options"] = {"include_usage": True}
    for field in ("temperature", "top_p", "seed"):
        if probe.get(field) is not None:
            body[field] = probe[field]
    thinking = probe.get("thinking")
    if thinking is not None:
        if thinking not in {"disabled", "none"}:
            raise ValueError("unsupported thinking policy")
        if protocol == "anthropic":
            body["thinking"] = {"type": "disabled"}
    provider = probe.get("provider")
    if provider is not None:
        if not isinstance(provider, dict) or set(provider) != {"order", "allow_fallbacks"} or provider["allow_fallbacks"] is not False:
            raise ValueError("provider must be pinned without fallback")
        body["provider"] = provider
    return body


def _tool_call(data):
    for choice in data.get("choices") or []:
        content = choice.get("message") or choice.get("delta") or {}
        if content.get("tool_calls") or content.get("function_call"):
            return True
    blocks = data.get("content") or data.get("output") or []
    if any(isinstance(block, dict) and block.get("type") in {"tool_use", "function_call", "computer_call", "tool_call"} for block in blocks):
        return True
    if (data.get("content_block") or {}).get("type") == "tool_use":
        return True
    delta = data.get("delta")
    return isinstance(delta, dict) and delta.get("type") == "input_json_delta"


def _nonstream(data, protocol):
    if protocol == "openai":
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("invalid choices")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ValueError("invalid chat content")
        return message["content"], str(choices[0].get("finish_reason") or ""), ""
    if protocol == "anthropic":
        blocks = data.get("content")
        if not isinstance(blocks, list) or any(not isinstance(b, dict) for b in blocks):
            raise ValueError("invalid messages content")
        text = "".join(b["text"] for b in blocks if b.get("type") == "text")
        return text, str(data.get("stop_reason") or ""), ""
    state = data.get("status")
    if state not in {"completed", "incomplete", "failed"}:
        raise ValueError("invalid response status")
    event = parse_stream_line(json.dumps({"type": "response." + state, "response": data}))
    if event["protocol_error"] and event["protocol_error"] != "upstream_error":
        raise ValueError("invalid Responses content")
    return event["final_content"] or "", event["finish_reason"], state


async def run_probe(client, channel, probe, *, before_send=None):
    started = perf_counter()
    base = {"status": "invalid_response", "valid": False, "text": "", "finish_reason": "",
            "input_tokens_reported": None, "output_tokens_reported": None, "usage_complete": False,
            "reasoning_tokens_reported": None,
            "tool_calls": False, "truncated": False, "latency_ms": None, "actual_model": ""}

    async def measure():
        protocol = channel["protocol"]
        url = endpoint_url(channel["base_url"], protocol)
        body = payload(channel, probe)
        await validate_url(url)
        if before_send is not None:
            await before_send()
        async with client.stream("POST", url, headers=headers(channel), json=body) as response:
            if response.status_code != 200:
                return {**base, "status": _http_status(response.status_code)}
            answer, finish, terminal = "", "", ""
            tool, refused, protocol_error = False, False, False
            input_count = output_count = None
            reasoning_count, done = None, False
            actual = ""
            if not body["stream"]:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise ValueError("response_too_large")
                data = json.loads(content)
                if not isinstance(data, dict):
                    raise ValueError("invalid response")
                tool = _tool_call(data)
                answer, finish, terminal = _nonstream(data, protocol)
                input_count, output_count = _reported_usage(data)
                reasoning_count = _reasoning_usage(data)
                actual = _safe_model(data.get("model"), channel["api_key"])
                refused = bool(data.get("refusal"))
            else:
                size, blocks = 0, {}
                async for line in response.aiter_lines():
                    size += len(line.encode()) + 1
                    if size > MAX_RESPONSE_BYTES:
                        raise ValueError("response_too_large")
                    line = line.strip()
                    if not line or line.startswith(":") or line.startswith("event:"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        if protocol != "openai":
                            protocol_error = True
                        done = True
                        continue
                    if done:
                        protocol_error = True
                    data = json.loads(line)
                    if not isinstance(data, dict):
                        raise ValueError("invalid event")
                    tool = tool or _tool_call(data)
                    if data.get("error") or data.get("type") == "error":
                        return {**base, "status": "upstream_error"}
                    event = parse_stream_line(line)
                    expected_formats = {"responses"} if protocol == "responses" else {"anthropic", "usage"} if protocol == "anthropic" else {"chat_delta", "usage"}
                    if not event or not event["recognized"] or event["format"] not in expected_formats:
                        protocol_error = True
                        continue
                    if terminal and (event["content"] or event["response_status"] or event["block_content"] is not None):
                        protocol_error = True
                    protocol_error = protocol_error or bool(event["protocol_error"] and event["protocol_error"] != "upstream_error")
                    if event["protocol_error"] == "upstream_error":
                        return {**base, "status": "upstream_error"}
                    new_input, new_output = _reported_usage(data.get("response") or data)
                    new_reasoning = _reasoning_usage(data.get("response") or data)
                    reasoning_count = new_reasoning if new_reasoning is not None else reasoning_count
                    input_count = new_input if new_input is not None else input_count
                    output_count = new_output if new_output is not None else output_count
                    actual = _safe_model(event["actual_model"] or actual, channel["api_key"])
                    refused = refused or event["refused"]
                    delta = event["content"]
                    if event["block_key"] is not None:
                        key, previous = event["block_key"], blocks.get(event["block_key"], "")
                        if event["block_content"] is not None:
                            complete = event["block_content"]
                            if not complete.startswith(previous):
                                protocol_error, delta = True, ""
                            else:
                                delta = complete[len(previous):]
                        blocks[key] = previous + delta
                    if event["final_content"] is not None:
                        complete = event["final_content"]
                        if not complete.startswith(answer):
                            protocol_error, delta = True, ""
                        else:
                            delta = complete[len(answer):]
                        tool = tool or _tool_call(data.get("response") or {})
                    answer += delta
                    if protocol == "responses":
                        terminal = event["response_status"] or terminal
                        finish = event["finish_reason"] or finish
                    elif protocol == "anthropic":
                        if data.get("type") == "message_delta":
                            finish = event["finish_reason"] or finish
                        if data.get("type") == "message_stop":
                            terminal = "completed"
                    else:
                        finish = event["finish_reason"] or finish
                        if finish:
                            terminal = "completed"
            if protocol_error:
                state = "invalid_response"
            elif tool:
                state = "tool_calls"
            elif refused or finish == "content_filter":
                state = "refused"
            elif terminal == "failed":
                state = "upstream_error"
            elif finish in {"length", "max_tokens", "max_output_tokens"} or terminal == "incomplete":
                state = "truncated"
            elif body["stream"] and terminal != "completed":
                state = "stream_break"
            elif body["stream"] and protocol == "openai" and not done:
                state = "stream_break"
            elif protocol == "responses" and terminal != "completed":
                state = "invalid_response"
            elif protocol != "responses" and finish not in {"stop", "end_turn", "stop_sequence"}:
                state = "invalid_response"
            elif not answer.strip():
                state = "empty_response"
            else:
                state = "completed"
            return {**base, "status": state, "valid": state == "completed", "text": answer if state == "completed" else "",
                    "finish_reason": finish, "tool_calls": tool, "truncated": state == "truncated",
                    "input_tokens_reported": input_count, "output_tokens_reported": output_count,
                    "reasoning_tokens_reported": reasoning_count,
                    "usage_complete": input_count is not None and output_count is not None, "actual_model": actual}
    try:
        timeout = probe.get("request_timeout_seconds", 60)
        if type(timeout) not in {int, float} or not 0 < timeout <= 240:
            raise ValueError("invalid request timeout")
        result = await asyncio.wait_for(measure(), timeout)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        result = {**base, "status": "timeout"}
    except (EgressDenied, SocketEgressDenied):
        result = {**base, "status": "egress_denied"}
    except httpx.HTTPError as exc:
        result = {**base, "status": "egress_denied" if socket_egress_denied(exc) else "network_error"}
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        result = base
    return {**result, "latency_ms": round((perf_counter() - started) * 1000)}


@asynccontextmanager
async def send_capacity():
    from features.stability.app.scheduler import probe_semaphore
    async with integrity_semaphore(), probe_semaphore():
        yield


async def send_probe_with_capacity(channel, probe, *, before_send=None):
    """Internal sender; caller must already hold both shared capacity guards."""
    async with httpx.AsyncClient(transport=guarded_transport(), trust_env=False, follow_redirects=False,
                                 timeout=httpx.Timeout(connect=20, read=180, write=20, pool=20)) as client:
        return await run_probe(client, channel, probe, before_send=before_send)


async def send_probe(channel, probe, *, before_send=None):
    async with send_capacity():
        return await send_probe_with_capacity(channel, probe, before_send=before_send)
