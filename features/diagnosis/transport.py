import asyncio
import json
import time

import httpx

from .synthetic import request_payload

ROUTES = {"openai": "/chat/completions", "responses": "/responses", "anthropic": "/messages"}
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024


def endpoint_url(base, protocol):
    base = base.rstrip("/")
    for suffix in ROUTES.values():
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    return base + ("" if base.endswith("/v1") else "/v1") + ROUTES[protocol]


def count(value):
    return value if type(value) is int and 0 <= value <= 100_000_000 else None


class StreamEvidence:
    def __init__(self, protocol):
        self.protocol = protocol
        self.chars = 0
        self.input_tokens = None
        self.output_tokens = None
        self.reasoning_tokens = None
        self.done = False
        self.finish = ""
        self.error = ""
        self.refused = False

    def usage(self, data):
        if not isinstance(data, dict):
            return
        for attribute, keys in (("input_tokens", ("input_tokens", "prompt_tokens")),
                                ("output_tokens", ("output_tokens", "completion_tokens"))):
            for key in keys:
                value = count(data.get(key))
                if value is not None:
                    setattr(self, attribute, value)
                    break
        for key in ("completion_tokens_details", "output_tokens_details"):
            details = data.get(key)
            if isinstance(details, dict) and count(details.get("reasoning_tokens")) is not None:
                self.reasoning_tokens = details["reasoning_tokens"]

    def text(self, value):
        if isinstance(value, str):
            self.chars += len(value)

    def ending(self, reason):
        if reason in ("stop", "end_turn", "stop_sequence"):
            self.finish = "stop"
        elif reason in ("length", "max_tokens", "max_output_tokens"):
            self.finish = "output_limit"
        elif reason in ("content_filter", "refusal"):
            self.finish = "refused"
        elif reason in ("tool_calls", "tool_use", "pause_turn"):
            self.finish = "unsupported_output"
        elif reason is not None:
            self.finish = "unknown_finish"

    def response(self, data):
        if not isinstance(data, dict):
            raise ValueError("invalid response")
        self.usage(data.get("usage"))
        if data.get("error"):
            self.error = "upstream_error"
        status = data.get("status")
        if status == "completed":
            self.finish = "stop"
        elif status == "incomplete":
            details = data.get("incomplete_details") or {}
            self.ending(details.get("reason"))
        elif status == "failed":
            self.error = "upstream_error"
        self.done = status in ("completed", "incomplete", "failed")

    def event(self, raw):
        if raw == "[DONE]":
            if self.protocol != "openai":
                raise ValueError("unexpected terminator")
            self.done = True
            return
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("invalid event")
        if data.get("error") or data.get("type") == "error":
            self.error = "upstream_error"
            return
        self.usage(data.get("usage"))
        if self.protocol == "openai":
            choices = data.get("choices", [])
            if not isinstance(choices, list):
                raise ValueError("invalid choices")
            if choices:
                choice = choices[0]
                delta = choice.get("delta", {})
                if not isinstance(delta, dict):
                    raise ValueError("invalid delta")
                self.text(delta.get("content"))
                self.refused = self.refused or bool(delta.get("refusal"))
                if choice.get("finish_reason") is not None:
                    self.ending(choice["finish_reason"])
        elif self.protocol == "responses":
            kind = data.get("type")
            if kind == "response.output_text.delta":
                self.text(data.get("delta"))
            elif kind == "response.refusal.delta":
                self.refused = True
            elif kind in ("response.completed", "response.incomplete", "response.failed"):
                self.response(data.get("response"))
        else:
            kind = data.get("type")
            if kind == "message_start":
                message = data.get("message") or {}
                self.usage(message.get("usage"))
            elif kind == "content_block_delta":
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta":
                    self.text(delta.get("text"))
            elif kind == "message_delta":
                self.ending((data.get("delta") or {}).get("stop_reason"))
            elif kind == "message_stop":
                self.done = True

    def nonstream(self, data):
        if not isinstance(data, dict):
            raise ValueError("invalid response")
        self.usage(data.get("usage"))
        if data.get("error"):
            self.error = "upstream_error"
            return
        if self.protocol == "openai":
            choice = data["choices"][0]
            message = choice.get("message") or {}
            self.text(message.get("content"))
            self.refused = bool(message.get("refusal"))
            self.ending(choice.get("finish_reason"))
            self.done = bool(self.finish)
        elif self.protocol == "responses":
            self.response(data)
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for block in item.get("content", []):
                        if block.get("type") == "output_text":
                            self.text(block.get("text"))
                        elif block.get("type") == "refusal":
                            self.refused = True
        else:
            for block in data.get("content", []):
                if block.get("type") == "text":
                    self.text(block.get("text"))
            self.ending(data.get("stop_reason"))
            self.done = bool(self.finish)

    def outcome(self):
        if self.error:
            return self.error
        if not self.done or not self.finish:
            return "incomplete_response"
        if self.refused or self.finish == "refused":
            return "refused"
        if self.finish in ("unsupported_output", "unknown_finish"):
            return self.finish
        if self.finish == "output_limit":
            return "output_limit"
        if not self.chars:
            return "empty_response"
        return "completed"


async def measure(client, base_url, api_key, target, attempt, config, progress=None):
    started = time.monotonic()
    first = last = None
    evidence = StreamEvidence(target["protocol"])
    result = {**attempt, "started_at": time.time(), "outcome": "running", "http_status": None,
              "headers_ms": None, "ttft_ms": None, "latency_ms": None, "max_content_gap_ms": None,
              "output_chars": 0, "usage_input_tokens": None, "usage_output_tokens": None,
              "usage_reasoning_tokens": None, "usage_source": "missing", "transport_complete": False,
              "phase": "waiting_headers", "request_ok": False}
    deadline = started + config["timeout_seconds"]
    headers = {"content-type": "application/json"}
    if api_key:
        if target["protocol"] == "anthropic":
            headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        else:
            headers["authorization"] = "Bearer " + api_key
    max_gap = 0.0
    response = None

    async def wait_next(awaitable):
        now = time.monotonic()
        activity = ((last + config["idle_timeout_seconds"]) if last is not None else
                    (started + config["first_content_timeout_seconds"])) if attempt["stream"] else deadline
        expires = min(deadline, activity)
        try:
            return await asyncio.wait_for(awaitable, max(0.0001, expires - now))
        except asyncio.TimeoutError:
            result["outcome"] = ("total_timeout" if deadline <= activity else
                                 "idle_timeout" if last is not None else "first_content_timeout")
            raise

    try:
        request = client.build_request("POST", endpoint_url(base_url, target["protocol"]), headers=headers,
                                       json=request_payload(target["protocol"], target["model"], attempt))
        response = await wait_next(client.send(request, stream=True))
        result.update(http_status=response.status_code, headers_ms=(time.monotonic() - started) * 1000,
                      phase="waiting_content")
        if progress:
            await progress(dict(result))
        if response.status_code != 200:
            result["outcome"] = ({401: "authentication_error", 403: "permission_error", 429: "rate_limited"}.get(
                response.status_code, "upstream_http_error" if response.status_code >= 500 else "request_rejected"))
        elif attempt["stream"] and "text/event-stream" not in response.headers.get("content-type", "").lower():
            result["outcome"] = "invalid_content_type"
        else:
            iterator = response.aiter_bytes().__aiter__()
            pending = b""
            total = 0
            event_lines = []
            event_size = 0
            while True:
                try:
                    chunk = await wait_next(iterator.__anext__())
                except StopAsyncIteration:
                    break
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    result["outcome"] = "response_too_large"
                    break
                pending += chunk
                if not attempt["stream"]:
                    continue
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if len(line) > MAX_EVENT_BYTES:
                        raise ValueError("oversized event")
                    line = line.rstrip(b"\r").decode("utf-8")
                    if line.startswith("data:"):
                        value = line[5:].removeprefix(" ")
                        event_size += len(value.encode())
                        if event_size > MAX_EVENT_BYTES:
                            raise ValueError("oversized event")
                        event_lines.append(value)
                    elif not line and event_lines:
                        before = evidence.chars
                        evidence.event("\n".join(event_lines))
                        event_lines, event_size = [], 0
                        if evidence.chars > before:
                            now = time.monotonic()
                            if last is not None:
                                max_gap = max(max_gap, now - last)
                            last = now
                            if first is None:
                                first = now
                                result.update(ttft_ms=(now - started) * 1000, phase="receiving")
                                if progress:
                                    await progress(dict(result))
                    elif line and not line.startswith((":", "event:", "id:", "retry:")):
                        raise ValueError("invalid SSE field")
                if len(pending) > MAX_EVENT_BYTES:
                    raise ValueError("oversized event")
                if evidence.error or evidence.done:
                    break
            if result["outcome"] == "running":
                if not attempt["stream"]:
                    evidence.nonstream(json.loads(pending))
                    if evidence.chars:
                        first = time.monotonic()
                        result["ttft_ms"] = (first - started) * 1000
                result["outcome"] = evidence.outcome()
    except asyncio.CancelledError:
        result["outcome"] = "cancelled"
    except httpx.TimeoutException:
        result["outcome"] = "transport_timeout"
    except asyncio.TimeoutError:
        if result["outcome"] == "running":
            result["outcome"] = "total_timeout"
    except httpx.HTTPError:
        result["outcome"] = "connection_error" if response is None else "stream_disconnect"
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        result["outcome"] = "invalid_response"
    finally:
        if response is not None:
            try:
                await asyncio.wait_for(response.aclose(), 2)
            except (httpx.HTTPError, asyncio.TimeoutError):
                result["outcome"] = "connection_close_error"
        elapsed = (time.monotonic() - started) * 1000
        result.update(latency_ms=elapsed, ended_at=time.time(), phase="finished", output_chars=evidence.chars,
                      finish_reason=evidence.finish or None,
                      max_content_gap_ms=max_gap * 1000 if first is not None else None,
                      usage_input_tokens=evidence.input_tokens, usage_output_tokens=evidence.output_tokens,
                      usage_reasoning_tokens=evidence.reasoning_tokens,
                      transport_complete=evidence.done and bool(evidence.finish) and not evidence.error,
                      request_ok=result["outcome"] in ("completed", "output_limit"))
        if evidence.input_tokens is not None or evidence.output_tokens is not None:
            result["usage_source"] = "mock" if target["mode"] == "mock" else "provider"
        result["usage_output_tokens_per_second_total"] = (
            evidence.output_tokens / (elapsed / 1000) if evidence.output_tokens is not None and elapsed > 0 else None)
    return result
