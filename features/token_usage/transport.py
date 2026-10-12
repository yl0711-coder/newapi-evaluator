"""Concurrent paired HTTP with no retry, redirect, proxy or cumulative-usage sum."""
from __future__ import annotations

import asyncio
import json
import weakref

import httpx

from features.admission.main import parse_stream_line
from features.integrity.transport import _nonstream, _tool_call, payload
from features.stability.app.egress import EgressDenied, validate_url
from features.stability.app.transport import endpoint_url, headers
from shared.network import SocketEgressDenied, guarded_transport, socket_egress_denied
from .domain import ASTRA_PARAMETERS, FIXED_INPUT, PARAMETERS, classify_pair, empty_result, normalize_usage, observation, protocol

_capacities = weakref.WeakKeyDictionary()
MAX_RESPONSE_BYTES = 1_048_576


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
            size = 0
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ValueError("response_too_large")
                data = json.loads(raw)
                text, finish, terminal = _nonstream(data, kind)
                answer, bad = bool(text.strip()), _tool_call(data)
                record(data)
                done = True
                if kind != "responses" and finish in {"stop", "end_turn", "stop_sequence"}:
                    terminal = "completed"
            else:
                async for line in response.aiter_lines():
                    size += len(line.encode()) + 1
                    if size > MAX_RESPONSE_BYTES:
                        raise ValueError("response_too_large")
                    line = line.strip()
                    if not line or line.startswith((":", "event:")):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        bad = bad or kind != "openai" or done
                        done = True
                        continue
                    if done:
                        bad = True
                    data = json.loads(line)
                    if not isinstance(data, dict):
                        raise ValueError("invalid_event")
                    record(data)
                    event = parse_stream_line(line)
                    formats = {"openai": {"chat_delta", "usage"}, "responses": {"responses"}, "anthropic": {"anthropic", "usage"}}
                    bad = bad or not event.get("recognized") or event.get("format") not in formats[kind]
                    bad = bad or bool(event.get("protocol_error")) or _tool_call(data) or bool(event.get("refused"))
                    answer = answer or bool(event.get("content", "").strip()) or bool((event.get("final_content") or "").strip())
                    finish = event.get("finish_reason") or finish
                    if kind == "responses":
                        terminal = event.get("response_status") or terminal
                    elif kind == "anthropic" and data.get("type") == "message_stop":
                        terminal = "completed"
                    elif kind == "openai" and finish:
                        terminal = "completed"
            complete = terminal == "completed" and answer and not bad
            complete = complete and (kind != "openai" or done) and (kind == "responses" or finish in {"stop", "end_turn", "stop_sequence"})
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
