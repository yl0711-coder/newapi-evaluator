"""One bounded HTTP attempt per probe; only derived evidence leaves this module."""
import asyncio
import json
import re
import time
from urllib.parse import urlsplit

import httpx

from shared.network import guarded_transport
from shared.channel_protocol import safe_text
from .catalog import request_body

MAX_BODY = 1_048_576


def require(condition):
    if not condition:
        raise ValueError("invalid protocol structure")


def endpoint(base, path):
    parts = urlsplit(base)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Base URL 必须是无凭据和查询参数的 HTTP(S) 接口根地址")
    try:
        parts.port
    except ValueError:
        raise ValueError("Base URL 端口无效") from None
    root = base.rstrip("/")
    if any(root.endswith(s) for s in ("/responses", "/messages", "/chat/completions", "/alpha/search")):
        raise ValueError("请填写接口根地址，不要填写具体端点")
    return root + ("" if root.endswith("/v1") else "/v1") + path


def valid_usage(value, protocol):
    keys = ("prompt_tokens", "completion_tokens") if protocol == "openai" else ("input_tokens", "output_tokens")
    return isinstance(value, dict) and all(type(value.get(k)) is int and value[k] >= 0 for k in keys)


def text_blocks(items):
    return "".join(item.get("text", "") for item in items
                   if isinstance(item, dict) and item.get("type") in {"text", "output_text"} and isinstance(item.get("text"), str))


def tool_valid(item, protocol):
    if not isinstance(item, dict) or item.get("name") != "protocol_probe":
        return False
    if protocol == "responses":
        if item.get("type") != "function_call" or not isinstance(item.get("call_id"), str) or not item["call_id"]:
            return False
        try:
            arguments = json.loads(item.get("arguments", ""))
        except (TypeError, ValueError):
            return False
    else:
        if item.get("type") != "tool_use" or not isinstance(item.get("id"), str) or not item["id"]:
            return False
        arguments = item.get("input")
    return arguments == {"marker": "ready"}


def search_evidence(body):
    def target_source(value, content):
        if not isinstance(value, str) or not re.search(r"RFC\s*9110", content, re.I):
            return False
        try:
            parts = urlsplit(value)
            return parts.scheme in {"http", "https"} and parts.hostname in {"rfc-editor.org", "www.rfc-editor.org"} and parts.path.rstrip("/.") in {"/rfc/rfc9110", "/rfc/rfc9110.html", "/info/rfc9110"}
        except ValueError:
            return False

    for item in body.get("results") or []:
        if isinstance(item, dict) and item.get("type") == "text_result":
            content = " ".join(item[key] for key in ("title", "snippet") if isinstance(item.get(key), str))
            if target_source(item.get("url"), content):
                return True
    # Recognize a source heading followed by a search citation, not arbitrary echoed URLs.
    citations = re.finditer(r"(?m)^([^\r\n]+?)\s*\((https?://[^\s()]+)\)[ \t]*\r?\n[ \t]*【([A-Za-z0-9_-]*(?:search|view)[A-Za-z0-9_-]*)】", body["output"])
    return any(target_source(match[2], match[1]) for match in citations)


def search_failed(body):
    if body.get("error"):
        return True
    if any(isinstance(item, dict) and (item.get("error") or item.get("type") == "error") for item in body.get("results") or []):
        return True
    return re.match(r"\s*(?:error\s*[:：]|error\s+(?:searching|fetching)\b|search\s+failed\b|failed\s+to\s+(?:search|fetch|retrieve)\b|搜索失败|检索失败|错误\s*[:：])", body["output"], re.I) is not None


def analyze_json(body, probe):
    result = {"schema_status": "failed", "result_status": "failed", "error_class": "invalid_schema",
              "usage_present": False, "protocol_completed": False, "observed_model": None}
    if not isinstance(body, dict):
        return result
    if probe.protocol == "alpha_search":
        if not isinstance(body.get("output"), str) or (body.get("results") is not None and not isinstance(body["results"], list)) or (body.get("encrypted_output") is not None and not isinstance(body["encrypted_output"], str)):
            return result
        result.update(schema_status="passed", protocol_completed=True, error_class="", result_status="unconfirmed")
        if search_failed(body):
            result.update(result_status="failed", error_class="search_failed")
        elif not body["output"].strip():
            result.update(error_class="empty_output")
        elif re.match(r"\s*(?:no\s+(?:search\s+)?results\b|未找到(?:搜索|检索)?结果|没有(?:搜索|检索)?结果)", body["output"], re.I):
            result.update(error_class="search_no_results")
        elif search_evidence(body):
            result.update(result_status="passed")
        else:
            result.update(error_class="search_result_unconfirmed")
        return result
    try:
        if body.get("error"):
            result["error_class"] = "upstream_error"
            return result
        if probe.protocol == "responses":
            require(isinstance(body["output"], list) and body["status"] in {"completed", "incomplete", "failed"})
            finish = body["status"]
            content = "".join(text_blocks(item.get("content", [])) for item in body["output"] if isinstance(item, dict) and item.get("type") == "message")
            tool = any(tool_valid(item, probe.protocol) for item in body["output"])
            complete = finish == "completed"
        elif probe.protocol == "anthropic":
            require(body.get("type") == "message" and isinstance(body["content"], list))
            finish = body["stop_reason"]
            require(finish in {"end_turn", "tool_use", "max_tokens", "stop_sequence", "pause_turn", "refusal", "model_context_window_exceeded"})
            content = text_blocks(body["content"])
            tool = any(tool_valid(item, probe.protocol) for item in body["content"])
            complete = finish == "tool_use" if probe.purpose == "tool" else finish in {"end_turn", "stop_sequence"}
        else:
            require(isinstance(body["choices"], list) and len(body["choices"]) == 1)
            choice = body["choices"][0]
            require(isinstance(choice["message"], dict))
            finish = choice["finish_reason"]
            require(finish in {"stop", "length", "tool_calls", "content_filter", "function_call"})
            content = choice["message"].get("content") or ""
            require(isinstance(content, str))
            tool = False
            complete = finish == "stop"
        usage = valid_usage(body.get("usage"), probe.protocol)
        result.update(schema_status="passed", usage_present=usage, protocol_completed=True,
                      observed_model=body.get("model") if isinstance(body.get("model"), str) else None)
        error = ""
        if not complete:
            error = "generation_incomplete"
        elif probe.purpose == "tool" and not tool:
            error = "invalid_tool_call"
        elif probe.purpose != "tool" and not content.strip():
            error = "empty_output"
        elif not usage:
            error = "usage_missing"
        result.update(error_class=error, result_status="failed" if error else "passed")
    except (AssertionError, KeyError, TypeError, ValueError):
        pass
    return result


class StreamState:
    def __init__(self, probe):
        self.probe = probe
        self.buffer = b""
        self.lines = []
        self.events = []
        self.ttft = None
        self.at_start = True
        self.skip_lf = False

    def feed(self, chunk, elapsed):
        self.buffer += chunk
        if self.at_start:
            bom = b"\xef\xbb\xbf"
            if len(self.buffer) < 3 and bom.startswith(self.buffer):
                return
            if self.buffer.startswith(bom):
                self.buffer = self.buffer[3:]
            self.at_start = False
        while self.buffer:
            if self.skip_lf:
                if self.buffer.startswith(b"\n"):
                    self.buffer = self.buffer[1:]
                self.skip_lf = False
            boundaries = [index for index in (self.buffer.find(b"\n"), self.buffer.find(b"\r")) if index >= 0]
            if not boundaries:
                return
            index = min(boundaries)
            line, separator = self.buffer[:index], self.buffer[index:index + 1]
            self.buffer = self.buffer[index + 1:]
            self.skip_lf = separator == b"\r"
            if not line:
                if self.lines:
                    self.event(b"\n".join(self.lines).decode("utf-8", errors="replace"), elapsed)
                    self.lines = []
            elif line.startswith(b"data:"):
                value = line[5:]
                self.lines.append(value[1:] if value.startswith(b" ") else value)

    def event(self, data, elapsed):
        if data == "[DONE]":
            self.events.append(data)
            return
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("invalid SSE object")
        self.events.append(value)
        visible = False
        if self.probe.protocol == "responses":
            visible = value.get("type") == "response.output_text.delta" and isinstance(value.get("delta"), str) and bool(value["delta"])
        elif self.probe.protocol == "anthropic":
            delta = value.get("delta", {})
            visible = isinstance(delta, dict) and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str) and bool(delta["text"])
        else:
            for choice in value.get("choices", []):
                delta = choice.get("delta", {})
                visible = visible or isinstance(delta.get("content"), str) and bool(delta["content"])
        if visible and self.ttft is None:
            self.ttft = elapsed

    def analyze(self):
        fail = {"schema_status": "failed", "result_status": "failed", "error_class": "stream_incomplete",
                "usage_present": False, "protocol_completed": False, "observed_model": None}
        if self.buffer.strip() or self.lines or not self.events:
            return fail
        try:
            if self.probe.protocol == "responses":
                terminal = [i for i, v in enumerate(self.events) if isinstance(v, dict) and v.get("type") in {"response.completed", "response.incomplete", "response.failed"}]
                require(len(terminal) == 1 and terminal[0] == len(self.events) - 1)
                require(all(isinstance(v, dict) and v.get("type") not in {"error", "response.error"} for v in self.events))
                last = self.events[-1]
                body = last["response"]
                require(body["status"] == last["type"].removeprefix("response."))
                text = "".join(v.get("delta", "") for v in self.events if v.get("type") == "response.output_text.delta")
                body = {**body, "output": body.get("output") or [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
            elif self.probe.protocol == "openai":
                require(self.events[-1] == "[DONE]" and self.events.count("[DONE]") == 1)
                text, finish, usage, model = "", None, None, None
                for value in self.events[:-1]:
                    require(isinstance(value, dict) and not value.get("error") and isinstance(value["choices"], list))
                    model = value.get("model") or model
                    usage = value.get("usage") or usage
                    for choice in value["choices"]:
                        require(choice.get("index", 0) == 0 and isinstance(choice["delta"], dict))
                        content = choice["delta"].get("content") or ""
                        require(isinstance(content, str) and not (finish and content))
                        text += content
                        if choice.get("finish_reason") is not None:
                            require(finish is None)
                            finish = choice["finish_reason"]
                require(finish is not None)
                body = {"choices": [{"message": {"content": text}, "finish_reason": finish}], "usage": usage, "model": model}
            else:
                require(all(isinstance(v, dict) for v in self.events))
                require(self.events[0]["type"] == "message_start" and self.events[-1]["type"] == "message_stop")
                require(sum(v.get("type") == "message_stop" for v in self.events) == 1)
                body = dict(self.events[0]["message"])
                usage = dict(body.get("usage", {}))
                content, blocks, finish = "", set(), None
                for v in self.events[1:-1]:
                    kind = v["type"]
                    require(kind not in {"error", "message_start", "message_stop"})
                    if kind == "content_block_start":
                        require(finish is None and v["index"] not in blocks)
                        blocks.add(v["index"])
                        content += text_blocks([v["content_block"]])
                    elif kind == "content_block_delta":
                        require(finish is None and v["index"] in blocks)
                        if v["delta"]["type"] == "text_delta":
                            content += v["delta"]["text"]
                    elif kind == "content_block_stop":
                        require(v["index"] in blocks)
                        blocks.remove(v["index"])
                    elif kind == "message_delta":
                        require(not blocks and finish is None)
                        finish = v["delta"]["stop_reason"]
                        usage.update(v.get("usage", {}))
                require(not blocks and finish is not None)
                body.update(content=[{"type": "text", "text": content}], usage=usage, stop_reason=finish)
            return analyze_json(body, self.probe)
        except (AssertionError, KeyError, TypeError, ValueError, AttributeError):
            fail["error_class"] = "stream_protocol_error"
            return fail


def http_error(status, body):
    if b"channel does not support /v1/alpha/search" in body.lower():
        return "local_protocol_unsupported"
    try:
        error = json.loads(body).get("error", {})
        if isinstance(error, dict) and error.get("code") in {"endpoint_unsupported", "unsupported_endpoint", "unsupported_path"}:
            return "upstream_protocol_unsupported"
    except (ValueError, AttributeError):
        pass
    if status in {401, 403}:
        return "authentication_failed"
    if status in {404, 405}:
        return "endpoint_unconfirmed"
    if status in {400, 422}:
        return "request_invalid"
    if status == 429:
        return "rate_limited"
    if status == 408:
        return "request_timeout"
    if status in {504, 524}:
        return "upstream_timeout"
    return "upstream_error" if status >= 500 else "http_error"


async def execute(probe, base, key, plan, session_id, transport=None):
    started = time.monotonic()
    result = {"probe_id": probe.id, "check": probe.check, "model": probe.model, "upstream_model": probe.upstream_model,
              "protocol": probe.protocol, "method": "POST", "path": "/v1" + probe.path, "stream": probe.stream, "label": probe.label,
              "attempts": 1, "gateway_retries": None, "request_id": None, "http_status": None,
              "transport_status": "failed", "schema_status": "not_tested", "result_status": "unconfirmed",
              "protocol_completed": False, "usage_present": False, "error_class": "", "ttft_ms": None,
              "header_ms": None, "total_ms": None, "model_match_status": "unconfirmed",
              "channel_attribution_status": "unverified", "billing_status": "unverified"}
    stage = "headers"
    stream = StreamState(probe)
    chunks = bytearray()
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if probe.stream else "application/json"}
    if probe.protocol == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = "Bearer " + key
    try:
        async def attempt():
            nonlocal stage
            timeout = httpx.Timeout(plan.idle_timeout, connect=plan.first_byte_timeout, pool=plan.first_byte_timeout, write=plan.first_byte_timeout)
            async with httpx.AsyncClient(transport=transport or guarded_transport(), trust_env=False, follow_redirects=False, timeout=timeout) as client:
                request = client.build_request("POST", endpoint(base, probe.path), headers=headers, json=request_body(probe, session_id))
                response = await asyncio.wait_for(client.send(request, stream=True), plan.first_byte_timeout)
                try:
                    result["header_ms"] = round((time.monotonic() - started) * 1000, 2)
                    result["http_status"] = response.status_code
                    request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                    # Preserve correlation without accepting arbitrary upstream text into evidence.
                    if request_id and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,160}", request_id) and key not in request_id:
                        try:
                            result["request_id"] = safe_text(request_id)
                        except ValueError:
                            pass
                    result["content_type_json"] = "json" in response.headers.get("content-type", "").lower()
                    iterator = response.aiter_bytes().__aiter__()
                    while True:
                        stage = "first_byte" if not chunks else "idle"
                        try:
                            chunk = await asyncio.wait_for(iterator.__anext__(), plan.first_byte_timeout if not chunks else plan.idle_timeout)
                        except StopAsyncIteration:
                            break
                        if len(chunks) + len(chunk) > MAX_BODY:
                            raise OverflowError
                        chunks.extend(chunk)
                        if probe.stream and 200 <= response.status_code < 300:
                            stream.feed(chunk, round((time.monotonic() - started) * 1000, 2))
                    result["transport_status"] = "passed" if 200 <= response.status_code < 300 else "failed"
                    if result["transport_status"] == "failed":
                        result["error_class"] = http_error(response.status_code, bytes(chunks))
                    else:
                        result.update(stream.analyze() if probe.stream else analyze_json(json.loads(chunks), probe))
                finally:
                    await response.aclose()
        await asyncio.wait_for(attempt(), plan.total_timeout)
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - started
        result["error_class"] = "total_timeout" if elapsed >= plan.total_timeout * .98 else stage + "_timeout"
    except httpx.TimeoutException:
        result["error_class"] = stage + "_timeout"
    except (httpx.TransportError, OSError):
        result["error_class"] = "connection_error"
    except OverflowError:
        result["error_class"] = "body_too_large"
    except (ValueError, TypeError, KeyError, AttributeError):
        result.update(schema_status="failed", result_status="failed", error_class="invalid_json_or_event")
    result["ttft_ms"] = stream.ttft if probe.stream else None
    result["total_ms"] = round((time.monotonic() - started) * 1000, 2)
    result["body_bytes"] = len(chunks)
    observed = result.pop("observed_model", None)
    if observed == probe.upstream_model:
        result["model_match_status"] = "matched"
    elif observed is not None:
        result["model_match_status"] = "mismatch"
        if result["result_status"] == "passed":
            result.update(result_status="unconfirmed", error_class="model_mapping_unconfirmed")
    result["status"] = "passed" if result["transport_status"] == result["schema_status"] == result["result_status"] == "passed" else "unconfirmed" if result["result_status"] == "unconfirmed" else "failed"
    return result
