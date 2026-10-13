"""Concurrent paired HTTP with no retry, redirect, proxy or cumulative-usage sum."""
from __future__ import annotations

import asyncio
import json
import weakref
import zlib

import httpx

from features.admission.main import parse_stream_line
from features.integrity.transport import _nonstream, _tool_call, payload
from features.stability.app.egress import EgressDenied, validate_url
from features.stability.app.transport import endpoint_url, headers as _protocol_headers
from shared.network import SocketEgressDenied, guarded_transport, socket_egress_denied
from .domain import ASTRA_PARAMETERS, FIXED_INPUT, PARAMETERS, classify_pair, empty_result, normalize_usage, observation, protocol

_capacities = weakref.WeakKeyDictionary()
MAX_RESPONSE_BYTES = 1_048_576
READ_CHUNK_BYTES = 8192
NORMAL_FINISH = {"openai": {"stop"}, "anthropic": {"end_turn", "stop_sequence"}}


def headers(endpoint):
    """Expose the complete Token request headers for frozen send contracts."""
    return {**_protocol_headers(endpoint), "Accept-Encoding": "identity"}


def _invalid_payload(data, kind):
    body = data.get("response", data) if kind == "responses" else data
    if not isinstance(body, dict):
        return True
    if body.get("error") is not None or _tool_call(body):
        return True
    if kind == "responses" and body.get("status") == "completed" and body.get("incomplete_details") is not None:
        return True
    if kind == "openai":
        return any((choice.get("message") or choice.get("delta") or {}).get("refusal") for choice in body.get("choices") or [])
    return False


async def _bounded_chunks(response):
    """Bound wire bytes and expansion before SSE, bypassing HTTPX's decoder.

    aiter_bytes(chunk_size=...) still expands one compressed raw chunk in full
    before splitting it. zlib's max_length bounds each expansion here instead.
    """
    size = wire_size = 0
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"", "identity", "gzip", "deflate"}:
        raise ValueError("unsupported_content_encoding")
    # MockTransport can supply a preloaded response. Its content has already
    # been decoded by its creator; the live streaming path always uses raw.
    if response.is_stream_consumed:
        content = response.content
        if len(content) > MAX_RESPONSE_BYTES:
            raise ValueError("response_too_large")
        for index in range(0, len(content), READ_CHUNK_BYTES):
            yield content[index:index + READ_CHUNK_BYTES]
        return
    decoder = zlib.decompressobj(zlib.MAX_WBITS | 16) if encoding == "gzip" else None
    prefix = bytearray()
    # Check the supplied raw chunk before HTTPX's ByteChunker can copy it.
    async for raw in response.aiter_raw():
        wire_size += len(raw)
        if wire_size > MAX_RESPONSE_BYTES:
            raise ValueError("response_too_large")
        if encoding == "deflate" and decoder is None:
            prefix.extend(raw)
            if len(prefix) < 2:
                continue
            # HTTP deflate implementations use either a zlib wrapper or raw
            # DEFLATE. Detect the two-byte zlib header without a large retry.
            wrapped = prefix[0] & 15 == 8 and prefix[0] >> 4 <= 7 and (prefix[0] * 256 + prefix[1]) % 31 == 0
            decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
            raw = bytes(prefix)
            prefix.clear()
        if decoder is None:
            size += len(raw)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("response_too_large")
            for index in range(0, len(raw), READ_CHUNK_BYTES):
                yield raw[index:index + READ_CHUNK_BYTES]
            continue
        if decoder.eof:
            raise ValueError("compressed_data_after_eof")
        while True:
            limit = min(READ_CHUNK_BYTES, MAX_RESPONSE_BYTES - size + 1)
            try:
                chunk = decoder.decompress(raw, max_length=limit)
            except zlib.error as exc:
                raise ValueError("invalid_compressed_response") from exc
            raw = decoder.unconsumed_tail
            if decoder.unused_data:
                raise ValueError("compressed_data_after_eof")
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("response_too_large")
            if chunk:
                yield chunk
            if not raw and len(chunk) < limit:
                break
    if prefix or (decoder is not None and not decoder.eof):
        raise ValueError("incomplete_compressed_response")


async def _sse_data(response):
    """Parse framed SSE after the decoded byte budget, including split UTF-8."""
    pending = bytearray()
    data = []
    skip_lf = False
    first_line = True

    def line(raw):
        nonlocal first_line
        value = raw.decode("utf-8", errors="strict")
        if first_line:
            value = value.lstrip("\ufeff")
            first_line = False
        if not value:
            event = "\n".join(data) if data else None
            data.clear()
            return event
        if value.startswith(":"):
            return None
        field, separator, value = value.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
        elif field not in {"event", "id", "retry"}:
            raise ValueError("invalid_sse_field")
        return None

    async for chunk in _bounded_chunks(response):
        # A single line is bounded by MAX_RESPONSE_BYTES; no decoder owns an
        # unbounded line and no event is parsed until its framing is complete.
        for byte in chunk:
            if skip_lf:
                skip_lf = False
                if byte == 10:
                    continue
            if byte in (10, 13):
                event = line(bytes(pending))
                pending.clear()
                skip_lf = byte == 13
                if event is not None:
                    yield event
            else:
                pending.append(byte)
    if pending:
        event = line(bytes(pending))
        if event is not None:
            yield event
    if data:
        yield "\n".join(data)


class SendControl(RuntimeError):
    """A caller's durable send guard failed; it is never an upstream result."""


def capacity():
    loop = asyncio.get_running_loop()
    if loop not in _capacities:
        _capacities[loop] = asyncio.Semaphore(2)
    return _capacities[loop]


async def _attempt(client, endpoint, before_send):
    trace = []
    sent = False
    kind = endpoint["protocol"]

    def record(data):
        data = data.get("response") or data
        usage = data.get("usage") or (data.get("message") or {}).get("usage")
        row = normalize_usage(usage, kind)
        if row is not None:
            if len(trace) >= 64:
                raise ValueError("too_many_usage_updates")
            trace.append(row)

    async def send():
        nonlocal sent
        url = endpoint_url(endpoint["base_url"], kind)
        await validate_url(url)
        selected_parameters = ASTRA_PARAMETERS if endpoint.get("canonical_model", endpoint["model"]) == "gpt-6-astra" else PARAMETERS
        probe = {"prompt": FIXED_INPUT, **selected_parameters}
        body = payload(endpoint, probe)
        if before_send:
            try:
                await before_send()
            except Exception as exc:
                raise SendControl("send_not_permitted") from exc
        sent = True
        async with client.stream("POST", url, headers=headers(endpoint), json=body) as response:
            if response.status_code != 200:
                return observation(trace, status="http_error", transport_status=f"http_{response.status_code}")
            terminal, finish, done, answer, bad = "", "", False, False, False
            message_started, finish_seen, response_started = False, False, False
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raw = bytearray()
                async for chunk in _bounded_chunks(response):
                    raw.extend(chunk)
                data = json.loads(raw)
                bad = _invalid_payload(data, kind)
                if kind == "responses":
                    terminal = data.get("status") or ""
                    event = parse_stream_line(json.dumps({"type": "response." + terminal, "response": data}))
                    text, finish = event.get("final_content") or "", event.get("finish_reason") or ""
                    bad = bad or not event.get("recognized") or event.get("format") != "responses" or bool(event.get("protocol_error")) or bool(event.get("refused"))
                else:
                    text, finish, terminal = _nonstream(data, kind)
                answer = bool(text.strip())
                record(data)
                done = True
                if kind != "responses" and finish in NORMAL_FINISH[kind]:
                    terminal = "completed"
            else:
                async for line in _sse_data(response):
                    line = line.strip()
                    if line == "[DONE]":
                        bad = bad or kind != "openai" or done or not finish_seen
                        done = True
                        continue
                    if done or (kind != "openai" and terminal):
                        bad = True
                        continue
                    data = json.loads(line)
                    if not isinstance(data, dict):
                        raise ValueError("invalid_event")
                    if kind == "anthropic" and data.get("type") == "ping":
                        continue  # A heartbeat has no content, finish or usage.
                    record(data)
                    event = parse_stream_line(line)
                    formats = {"openai": {"chat_delta", "usage"}, "responses": {"responses"}, "anthropic": {"anthropic", "usage"}}
                    bad = bad or not event.get("recognized") or event.get("format") not in formats[kind]
                    bad = bad or bool(event.get("protocol_error")) or _invalid_payload(data, kind) or bool(event.get("refused"))
                    answer = answer or bool(event.get("content", "").strip()) or bool((event.get("final_content") or "").strip())
                    if kind == "responses":
                        event_type = data.get("type")
                        if event_type == "response.created":
                            bad = bad or response_started
                            response_started = True
                        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
                            terminal = event.get("response_status") or "invalid"
                            finish = event.get("finish_reason") or ""
                    elif kind == "anthropic":
                        event_type = data.get("type")
                        if event_type == "message_start":
                            bad = bad or message_started
                            message_started = True
                        else:
                            bad = bad or not message_started
                        if event_type == "message_delta" and event.get("finish_reason"):
                            bad = bad or finish_seen
                            finish, finish_seen = event["finish_reason"], True
                        elif event_type == "message_stop":
                            # message_stop provides framing, never a replacement
                            # for message_delta's actual reason (e.g. max_tokens).
                            bad = bad or not finish_seen
                            terminal = "completed"
                        elif finish_seen and event_type.startswith("content_block_"):
                            bad = True
                    else:
                        event_finish = event.get("finish_reason")
                        if finish_seen and (data.get("choices") or event.get("content")):
                            bad = True
                        if event_finish:
                            bad = bad or finish_seen
                            finish, finish_seen, terminal = event_finish, True, "completed"
            complete = terminal == "completed" and answer and not bad
            complete = complete and (kind != "openai" or done) and (kind == "responses" or finish in NORMAL_FINISH[kind])
            return observation(trace, status="completed" if complete else "invalid_response", terminal=bool(complete))
    try:
        return await asyncio.wait_for(send(), 60)
    except (EgressDenied, SocketEgressDenied):
        return observation(trace, status="not_sent", transport_status="egress_denied")
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return observation(trace, status="unknown" if sent else "not_sent", transport_status="timeout")
    except httpx.HTTPError as exc:
        denied = socket_egress_denied(exc)
        return observation(trace, status="not_sent" if denied or not sent else "unknown",
                           transport_status="egress_denied" if denied else "network_error")
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return observation(trace, status="invalid_response")


async def run_paired_check(candidate: dict, reference: dict, *, previous=None, on_result=None,
                           before_send=None, on_attempt=None, upstream_transport=None, client=None,
                           resume_attempts=None):
    """Callbacks are awaited. A cancelled sending permit stays unknown for its owner."""
    candidate, reference = dict(candidate), dict(reference)
    try:
        candidate["protocol"], reference["protocol"] = protocol(candidate["protocol"]), protocol(reference["protocol"])
        if (candidate["protocol"] != reference["protocol"]
                or candidate["model"] != reference["model"]
                or candidate.get("canonical_model", candidate["model"]) != reference.get("canonical_model", reference["model"])
                or (candidate.get("canonical_model", candidate["model"]) == "gpt-6-astra" and candidate["protocol"] != "responses")):
            return empty_result("conditions_mismatch")
    except (ValueError, KeyError, TypeError):
        return empty_result("invalid_endpoint")
    owned = client is None
    if owned:
        client = httpx.AsyncClient(transport=upstream_transport or guarded_transport(), trust_env=False,
                                  follow_redirects=False, timeout=httpx.Timeout(60, connect=10))
    elif client.follow_redirects:
        raise ValueError("redirects_not_permitted")
    pairs, requests_sent = [], 0
    restored = resume_attempts or {}

    async def side_attempt(side, index, endpoint):
        nonlocal requests_sent
        cached = restored.get(f"token:{side}:{index}")
        if cached and cached.get("status") != "not_sent":
            return cached
        from features.stability.app.scheduler import probe_semaphore
        async with capacity(), probe_semaphore():
            async def permit():
                nonlocal requests_sent
                if before_send:
                    await before_send(side, index)
                requests_sent += 1
            result = await _attempt(client, endpoint, permit)
            if on_attempt:
                await on_attempt(side, index, result)
            return result

    async def pair(index):
        tasks = [asyncio.create_task(side_attempt(side, index, endpoint))
                 for side, endpoint in (("candidate", candidate), ("reference", reference))]
        try:
            c, r = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        pairs.append({"pair_index": index, "candidate": c, "reference": r})
        return classify_pair(c, r, previous=previous, threshold_config=reference.get("_token_thresholds"))

    try:
        result = await pair(0)
        historical = sum(bool(v and v.get("status") not in {"not_sent"}) for v in restored.values())
        result.update(pairs=list(pairs), requests_sent=requests_sent + historical, new_requests_sent=requests_sent)
        if result["severity"] == "high_risk":
            result["verification_status"] = "pending"
            if on_result:
                await on_result(result)
            verification = await pair(1)
            result.update(verification_status=verification["conclusion"], pairs=list(pairs), requests_sent=requests_sent + historical, new_requests_sent=requests_sent)
        if on_result:
            await on_result(result)
        return result
    finally:
        if owned:
            await client.aclose()
