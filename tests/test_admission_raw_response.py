"""Synthetic upstream bytes exercise admission raw evidence and persistence."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import ValidationError

from features.admission import api, main as engine, storage
from features.admission.jobs import AdmissionManager
from shared.registry import Registry


def sse(payload):
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


def raw_text(events):
    value, truncated = "", False
    for event in events:
        if event["type"] == "raw_response":
            if event.get("raw_response_replace"):
                value = ""
            value += event["raw_response"]
            truncated = event["raw_response_truncated"]
    return value, truncated


def logical_raw_text(value):
    """Independently decode text fields as an ordinary report reader can."""
    content, reasoning = [], []
    candidates, data = [], []
    # SSE recognizes CR/LF only. Unicode line separators are JSON text.
    for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith("data:"):
            data.append(line[5:])
        elif not line and data:
            candidates.append("\n".join(data))
            data = []
    if data:
        candidates.append("\n".join(data))
    for line in candidates:
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        choices = payload.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            content.append(delta.get("content") or "")
            reasoning.append(delta.get("reasoning_content") or "")
        kind = payload.get("type") or ""
        if kind in {"response.output_text.delta", "response.refusal.delta"}:
            content.append(payload.get("delta") or "")
        if kind in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
            reasoning.append(payload.get("delta") or "")
        if kind == "content_block_delta":
            delta = payload.get("delta") or {}
            content.append(delta.get("text") or "")
            reasoning.append(delta.get("thinking") or "")
    return "".join(content), "".join(reasoning)


def text_delta(protocol, field, value):
    if protocol == "openai":
        return {"choices": [{"delta": {"content" if field == "content" else "reasoning_content": value}}]}
    if protocol == "responses":
        return {"type": "response.output_text.delta" if field == "content" else "response.reasoning_text.delta", "delta": value}
    return {"type": "content_block_delta", "delta": {"text" if field == "content" else "thinking": value}}


class FixtureStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def aclose(self):
        self.closed = True


class RawResponseTests(unittest.IsolatedAsyncioTestCase):
    async def measure(self, *chunks, protocol="openai", status=200, key="synthetic-key", secrets=()):
        stream = FixtureStream(chunks)
        config = engine.EndpointConfig(base_url="https://synthetic.example/v1", api_key=key,
                                       model="synthetic-model", protocol=protocol)
        queue = asyncio.Queue()
        transport = httpx.MockTransport(lambda request: httpx.Response(status, stream=stream))
        async with httpx.AsyncClient(transport=transport) as client:
            await engine.measure_side("candidate", config, engine.QUESTIONS[0], queue, client, secrets=secrets)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        self.assertTrue(stream.closed)
        return next(event for event in events if event["type"] == "side_finished"), events

    async def test_input_only_cache_usage_preserves_raw_response_and_answer(self):
        body = (sse({"choices": [{"delta": {"content": "synthetic answer"}, "finish_reason": "stop"}]})
                + sse({"choices": [], "usage": {"prompt_tokens": 100,
                      "prompt_tokens_details": {"cached_tokens": 40}}}) + b"data: [DONE]\n\n")
        result, events = await self.measure(body[:31], body[31:])
        self.assertTrue(result["ok"])
        self.assertEqual(raw_text(events), (body.decode(), False))
        self.assertEqual("".join(event.get("content", "") for event in events), "synthetic answer")
        self.assertEqual(result["cache_hit_rate"], 0.4)
        self.assertTrue(result["cache_request_hit"])
        self.assertIsNone(result["cache_write_input_tokens"])
        self.assertIsNone(result["cache_write_rate"])

    async def test_three_protocol_cache_and_raw_survive_display_budget(self):
        for protocol in ("openai", "responses", "anthropic"):
            for oversized in (False, True):
                with self.subTest(protocol=protocol, oversized=oversized):
                    padding = b": " + b"x" * (engine.RAW_RESPONSE_MAX_BYTES * 2) + b"\n\n" if oversized else b""
                    if protocol == "openai":
                        body = padding + sse({"choices": [{"delta": {"content": "READY"}, "finish_reason": "stop"}]})
                        body += sse({"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 40}}}) + b"data: [DONE]\n\n"
                    elif protocol == "responses":
                        terminal = {"type": "response.completed", "response": {"status": "completed",
                                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "READY"}]}],
                                    "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 40}, "output_tokens": 2}}}
                        multiline = "\n".join("data: " + line for line in json.dumps(terminal, indent=2).splitlines()) + "\n\n"
                        body = padding + sse({"type": "response.output_text.delta", "delta": "READY"}) + multiline.encode()
                    else:
                        body = padding + sse({"type": "message_start", "message": {"usage": {
                            "input_tokens": 50, "cache_read_input_tokens": 40, "cache_creation_input_tokens": 10}}})
                        body += sse({"type": "content_block_delta", "delta": {"text": "READY"}})
                        body += sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}})
                        body += sse({"type": "message_stop"})
                    result, events = await self.measure(body[:23], body[23:], protocol=protocol)
                    captured, truncated = raw_text(events)
                    self.assertTrue(result["ok"])
                    self.assertEqual("".join(e.get("content", "") for e in events), "READY")
                    self.assertEqual((result["cache_hit_input_tokens"], result["cache_total_input_tokens"], result["cache_hit_rate"]), (40, 100, .4))
                    self.assertTrue(result["cache_request_hit"])
                    self.assertEqual(result["cache_write_rate"], .1 if protocol == "anthropic" else None)
                    self.assertEqual(truncated, oversized)
                    self.assertLessEqual(len(captured.encode()), engine.RAW_RESPONSE_MAX_BYTES)
                    if not oversized:
                        self.assertEqual(captured, body.decode())

    async def test_repeated_cache_usage_replaces_counts_and_missing_does_not_erase(self):
        def usage(hit):
            return sse({"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": hit}}})
        for sequence, expected_status, expected_hit in (
            ((40, 20), "reported", 20), ((101, 20), "reported", 20), ((40, 101), "invalid", None),
        ):
            with self.subTest(sequence=sequence):
                body = b"".join(usage(hit) for hit in sequence)
                body += sse({"usage": {"prompt_tokens": 100}})
                body += sse({"choices": [{"delta": {"content": "READY"}, "finish_reason": "stop"}]}) + b"data: [DONE]\n\n"
                result, events = await self.measure(body)
                self.assertTrue(result["ok"])
                self.assertEqual(result["cache_status"], expected_status)
                self.assertEqual(result["cache_hit_input_tokens"], expected_hit)
                self.assertEqual(raw_text(events), (body.decode(), False))

    async def test_usage_before_stream_error_stays_evidence_not_success(self):
        usage = sse({"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 40}}})
        error = sse({"error": {"code": "synthetic_failure", "message": "synthetic upstream failure"}})
        for ending in (error, httpx.ReadError("synthetic interrupted")):
            with self.subTest(ending=type(ending).__name__):
                result, events = await self.measure(usage, ending)
                self.assertFalse(result["ok"])
                self.assertEqual(result["cache_hit_rate"], .4)
                self.assertIsNone(result["cache_write_rate"])
                self.assertFalse(any(e["type"] == "chunk" for e in events))
                self.assertEqual(raw_text(events)[0], (usage + (ending if isinstance(ending, bytes) else b"")).decode())
        result, events = await self.measure(usage + error, status=429)
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 429)
        self.assertIsNone(result["cache_hit_rate"])
        self.assertEqual(raw_text(events)[0], (usage + error).decode())

    async def test_http_failures_keep_actual_body_status_and_code_out_of_answers(self):
        for status in (401, 429, 502):
            with self.subTest(status=status):
                body = ' {\n "error": {"code":"synthetic_code","message":"synthetic upstream <error>"}\n}\r\n'
                result, events = await self.measure(body[:11].encode(), body[11:].encode(), status=status)
                self.assertEqual(raw_text(events), (body, False))
                self.assertEqual(result["http_status"], status)
                self.assertEqual(result["upstream_error_code"], "synthetic_code")
                self.assertEqual(result["upstream_error_message"], "synthetic upstream <error>")
                self.assertFalse(any(event["type"] == "chunk" for event in events))

    async def test_three_protocol_error_events_preserve_partial_answer_and_original_error(self):
        for protocol in ("openai", "responses", "anthropic"):
            with self.subTest(protocol=protocol):
                partial = {"openai": {"choices": [{"delta": {"content": "synthetic answer"}}]},
                           "responses": {"type": "response.output_text.delta", "delta": "synthetic answer"},
                           "anthropic": {"type": "content_block_delta", "delta": {"text": "synthetic answer"}}}[protocol]
                error = {"type": "error", "error": {"code": "synthetic_overload", "message": "synthetic provider failure"}}
                body = sse(partial) + sse(error)
                result, events = await self.measure(body, protocol=protocol)
                self.assertEqual(raw_text(events), (body.decode(), False))
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "upstream_error")
                self.assertEqual(result["upstream_error_code"], "synthetic_overload")
                self.assertEqual(result["upstream_error_message"], "synthetic provider failure")
                self.assertEqual("".join(event.get("content", "") for event in events), "synthetic answer")
                self.assertEqual(result["chars"], len("synthetic answer"))

    async def test_logical_content_and_reasoning_hide_both_credentials_across_protocol_frames(self):
        own, other = "synthetic-candidate-credential-unique", "synthetic-reference-credential-unique"
        for protocol in ("openai", "responses", "anthropic"):
            for field in ("content", "reasoning"):
                with self.subTest(protocol=protocol, field=field):
                    fragments = (own[:12], own[12:], "|", other[:12], other[12:])
                    body = b"".join(sse(text_delta(protocol, field, part)) for part in fragments)
                    _, events = await self.measure(*(body[index:index + 11] for index in range(0, len(body), 11)),
                                                   protocol=protocol, key=own, secrets=(other,))
                    for index in range(1, len(events) + 1):
                        captured, _ = raw_text(events[:index])
                        for decoded in logical_raw_text(captured):
                            self.assertNotIn(own, decoded)
                            self.assertNotIn(other, decoded)
                    decoded = logical_raw_text(raw_text(events)[0])[field == "reasoning"]
                    self.assertIn("[REDACTED]", decoded)
                    self.assertIn("|", decoded)

    async def test_multiline_responses_events_hide_split_credentials_without_changing_other_frames(self):
        own = "synthetic-candidate-credential-unique"
        frames = []
        for part in (own[:12], own[12:]):
            payload = json.dumps(text_delta("responses", "content", part), indent=2)
            frames.append(("event: response.output_text.delta\r\ndata: " + payload.replace("\n", "\r\ndata: ") + "\r\n\r\n").encode())
        _, events = await self.measure(*frames, protocol="responses", key=own)
        captured, _ = raw_text(events)
        self.assertNotIn(own, logical_raw_text(captured)[0])
        self.assertIn("[REDACTED]", logical_raw_text(captured)[0])
        safe = b'event: response.output_text.delta\r\ndata: {\r\ndata:   "type": "response.output_text.delta",\r\ndata:   "delta": "synthetic harmless"\r\ndata: }\r\n\r\n'
        _, events = await self.measure(safe, protocol="responses", key=own)
        self.assertEqual(raw_text(events), (safe.decode(), False))

    async def test_line_based_chat_and_messages_compatibility_cannot_reveal_split_keys(self):
        own = "synthetic-candidate-credential-unique"
        for protocol in ("openai", "anthropic"):
            body = b"".join(sse(text_delta(protocol, "content", part)).replace(b"\n\n", b"\n") for part in (own[:12], own[12:]))
            _, events = await self.measure(body, protocol=protocol, key=own)
            captured, _ = raw_text(events)
            parts = []
            for line in captured.splitlines():
                payload = json.loads(line.removeprefix("data:"))
                parts.append(payload["choices"][0]["delta"]["content"] if protocol == "openai" else payload["delta"]["text"])
            self.assertNotIn(own, "".join(parts))
            self.assertIn("[REDACTED]", "".join(parts))

    async def test_text_arrays_track_only_rendered_text_and_hide_split_credentials(self):
        own = "synthetic-candidate-credential-unique"
        for protocol in ("openai", "anthropic"):
            for field in ("content", "reasoning"):
                payload = text_delta(protocol, field, "unused")
                array = [{"type": "text", "text": own[:12]}, own[12:]]
                if protocol == "openai":
                    payload["choices"][0]["delta"]["content" if field == "content" else "reasoning_content"] = array
                else:
                    payload["delta"]["text" if field == "content" else "thinking"] = array
                _, events = await self.measure(sse(payload), protocol=protocol, key=own)
                line = next(line for line in raw_text(events)[0].splitlines() if line.startswith("data:"))
                decoded = json.loads(line[5:])
                if protocol == "openai":
                    values = decoded["choices"][0]["delta"]["content" if field == "content" else "reasoning_content"]
                else:
                    values = decoded["delta"]["text" if field == "content" else "thinking"]
                rendered = "".join(item if isinstance(item, str) else item["text"] for item in values)
                self.assertNotIn(own, rendered)
                self.assertIn("[REDACTED]", rendered)
                self.assertEqual(values[0]["type"], "text")

    async def test_json_unicode_and_slash_escapes_cannot_restore_credentials(self):
        own, other = "synthetic-candidate/key-unique", "synthetic-reference/key-unique"
        for protocol in ("openai", "responses", "anthropic"):
            with self.subTest(protocol=protocol):
                body = b"".join(sse(text_delta(protocol, "content", part)) for part in (own[:12], own[12:], "|", other))
                body = body.decode().replace("-", "\\u002d").replace("/", "\\/").encode()
                _, events = await self.measure(*(body[index:index + 11] for index in range(0, len(body), 11)),
                                               protocol=protocol, key=own, secrets=(other,))
                content, _ = logical_raw_text(raw_text(events)[0])
                self.assertNotIn(own, content)
                self.assertNotIn(other, content)
                self.assertIn("[REDACTED]", content)
        error = json.dumps({"error": {"message": own + " " + other}}, indent=2).replace("-", "\\u002d").replace("/", "\\/")
        _, events = await self.measure(error.encode(), status=401, key=own, secrets=(other,))
        captured = json.loads(raw_text(events)[0])
        self.assertEqual(captured["error"]["message"], "[REDACTED] [REDACTED]")

    async def test_unclosed_json_string_is_safe_on_eof_interruption_and_cancellation(self):
        own = "synthetic-private-credential-unique"
        encoded = own.replace("-", "\\u002d")
        body = ('{"error":{"message":"' + encoded + " \\u00").encode()
        for ending in ((), (httpx.ReadError("synthetic interrupted"),)):
            _, events = await self.measure(body, *ending, key=own)
            captured, _ = raw_text(events)
            self.assertEqual(captured, '{"error":{"message":"[REDACTED] \\u00')
        started = asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield body
                started.set()
                await asyncio.Event().wait()
            async def aclose(self):
                pass
        config = engine.EndpointConfig(base_url="https://synthetic.example/v1", api_key=own,
                                       model="synthetic-model", protocol="openai")
        queue = asyncio.Queue()
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
        async with httpx.AsyncClient(transport=transport) as client:
            task = asyncio.create_task(engine.measure_side("candidate", config, engine.QUESTIONS[0], queue, client))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            result = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(result[0], asyncio.CancelledError)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        self.assertEqual(raw_text(events)[0], '{"error":{"message":"[REDACTED] \\u00')

    async def test_unicode_separators_in_json_strings_preserve_text_without_exposing_keys(self):
        own = "synthetic-candidate-credential-unique"
        for protocol in ("openai", "responses", "anthropic"):
            for field in ("content", "reasoning"):
                for separator in ("\u2028", "\u2029", "\u0085"):
                    with self.subTest(protocol=protocol, field=field, separator=ord(separator)):
                        payload = text_delta(protocol, field, "safe" + separator + own)
                        body = sse(payload).decode().replace("-", "\\u002d").encode()
                        _, events = await self.measure(*(body[index:index + 11] for index in range(0, len(body), 11)),
                                                       protocol=protocol, key=own)
                        decoded = logical_raw_text(raw_text(events)[0])[field == "reasoning"]
                        self.assertEqual(decoded, "safe" + separator + "[REDACTED]")
                        harmless = sse(text_delta(protocol, field, "safe" + separator + "ordinary"))
                        _, safe_events = await self.measure(harmless, protocol=protocol, key=own)
                        self.assertEqual(raw_text(safe_events), (harmless.decode(), False))

    async def test_split_key_followed_by_unclosed_json_is_safe_on_all_stream_endings(self):
        own = "synthetic-candidate-credential-unique"
        for protocol in ("openai", "responses", "anthropic"):
            for field in ("content", "reasoning"):
                encoded_suffix = json.dumps(own[12:])[1:-1].replace("-", "\\u002d")
                second = sse(text_delta(protocol, field, own[12:])).decode().replace("-", "\\u002d")
                second = second[:second.index(encoded_suffix) + len(encoded_suffix)]
                body = sse(text_delta(protocol, field, own[:12])) + second.encode()
                for ending in ("eof", "read_error", "cancel"):
                    with self.subTest(protocol=protocol, field=field, ending=ending):
                        if ending != "cancel":
                            tail = (httpx.ReadError("synthetic interrupted"),) if ending == "read_error" else ()
                            _, events = await self.measure(body, *tail, protocol=protocol, key=own)
                        else:
                            started = asyncio.Event()
                            class Stream(httpx.AsyncByteStream):
                                async def __aiter__(self):
                                    yield body
                                    started.set()
                                    await asyncio.Event().wait()
                                async def aclose(self):
                                    pass
                            config = engine.EndpointConfig(base_url="https://synthetic.example/v1", api_key=own,
                                                           model="synthetic-model", protocol=protocol)
                            queue = asyncio.Queue()
                            transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
                            async with httpx.AsyncClient(transport=transport) as client:
                                task = asyncio.create_task(engine.measure_side("candidate", config, engine.QUESTIONS[0], queue, client))
                                await asyncio.wait_for(started.wait(), 2)
                                task.cancel()
                                result = await asyncio.gather(task, return_exceptions=True)
                            self.assertIsInstance(result[0], asyncio.CancelledError)
                            events = []
                            while not queue.empty():
                                events.append(queue.get_nowait())
                        captured, _ = raw_text(events)
                        complete = logical_raw_text(captured)[field == "reasoning"]
                        # Independently recover the final unclosed string by
                        # closing only its JSON quote, as a report reader can.
                        fragment = captured.rsplit('\": \"', 1)[1]
                        incomplete = json.loads('"' + fragment + '"')
                        self.assertNotIn(own, complete + incomplete)
                        self.assertIn("[REDACTED]", complete)
                        self.assertFalse(captured.endswith('"'))

    async def test_redaction_preserves_surrogate_escapes_and_other_wire_spelling(self):
        own = "synthetic-candidate-credential-unique"
        encoded = own.replace("-", "\\u002d")
        body = ' { "error" : { "code" : "synthetic_code", "message" : "\\ud800 safe\\/\\u0041 ' + encoded + ' \\udfff" } }\r\n'
        result, events = await self.measure(body.encode(), status=401, key=own)
        self.assertEqual(raw_text(events), (body.replace(encoded, "[REDACTED]"), False))
        self.assertEqual(result["http_status"], 401)
        self.assertEqual(result["error_category"], "http_401")
        self.assertEqual(result["upstream_error_code"], "synthetic_code")
        self.assertEqual(result["upstream_error_message"], "\\ud800 safe/A [REDACTED] \\udfff")
        self.assertFalse(result["upstream_error_truncated"])
        json.dumps(result, ensure_ascii=False).encode("utf-8")

    async def test_unmatched_key_prefix_keeps_exact_original_protocol_bytes(self):
        body = b'data: { "choices" : [{"delta":{"content":"synthe"}}] }\r\n\r\n' + sse(text_delta("openai", "content", "tic ordinary text"))
        _, events = await self.measure(body, key="synthetic-private-credential")
        self.assertEqual(raw_text(events), (body.decode(), False))

    async def test_pending_prefix_queue_is_bounded_when_metadata_interrupts_text(self):
        own = "synthetic-candidate-credential-unique"
        queue = asyncio.Queue()
        capture = engine._RawResponseCapture(queue, "synthetic-question", "candidate", (own,))
        await capture.feed(sse(text_delta("openai", "content", own[:12])).decode())
        for index in range(3000):
            await capture.feed(f"id: synthetic-{index}\n")
            self.assertLessEqual(capture.protocol_redactor.buffered_bytes, engine.RAW_RESPONSE_MAX_BYTES)
        await capture.feed(sse(text_delta("openai", "content", own[12:])).decode())
        await capture.finish()
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        captured, truncated = raw_text(events)
        self.assertTrue(truncated)
        self.assertNotIn(own, logical_raw_text(captured)[0])
        self.assertLessEqual(len(json.dumps(captured).encode()), engine.RAW_RESPONSE_MAX_BYTES + 2)

    async def test_legal_sse_metadata_does_not_hide_malformed_body_or_change_success(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol):
                body = b"id: synthetic-event\nretry: 1000\n" + sse(text_delta(protocol, "content", "synthetic answer"))
                result, events = await self.measure(body, protocol=protocol)
                self.assertTrue(result["ok"])
                self.assertEqual(raw_text(events), (body.decode(), False))
                malformed = body + b"synthetic malformed body\n"
                result, events = await self.measure(malformed, protocol=protocol)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "unrecognized")
                self.assertEqual(raw_text(events), (malformed.decode(), False))

    async def test_generic_upstream_error_is_labeled_with_actual_protocol(self):
        for protocol, expected in (("openai", "chat_delta"), ("anthropic", "anthropic"), ("responses", "responses")):
            with self.subTest(protocol=protocol):
                result, _ = await self.measure(sse({"type": "error", "error": {"code": "synthetic_failure"}}), protocol=protocol)
                self.assertEqual(result["response_format"], expected)

    async def test_chat_error_envelope_and_anthropic_error_type_are_real_upstream_codes(self):
        for protocol, payload, code in (
            ("openai", {"error": {"code": 4801, "message": "synthetic chat failure"}}, "4801"),
            ("anthropic", {"type": "error", "error": {"type": "synthetic_overloaded", "message": "synthetic native failure"}}, "synthetic_overloaded"),
            ("responses", {"type": "error", "code": "synthetic_flat", "message": "synthetic flat failure"}, "synthetic_flat"),
        ):
            with self.subTest(protocol=protocol):
                body = sse(payload)
                result, events = await self.measure(body, protocol=protocol)
                self.assertEqual(result["upstream_error_code"], code)
                self.assertEqual(result["status"], "upstream_error")
                self.assertEqual(result["chars"], 0)
                self.assertIsNone(result["ttft_ms"])
                self.assertEqual(raw_text(events), (body.decode(), False))

    async def test_responses_failed_event_exposes_nested_error_and_ignores_success_message(self):
        body = sse({"type": "response.failed", "response": {"status": "failed", "output": [],
                    "error": {"code": "synthetic_failed", "message": "synthetic original failure"}}})
        result, events = await self.measure(body, protocol="responses")
        self.assertEqual(result["upstream_error_code"], "synthetic_failed")
        self.assertEqual(result["upstream_error_message"], "synthetic original failure")
        self.assertEqual(raw_text(events), (body.decode(), False))
        parsed = engine.parse_stream_line(json.dumps({"type": "response.completed", "message": "synthetic metadata",
                                            "response": {"status": "completed", "output": []}}))
        self.assertEqual(parsed["upstream_error_message"], "")

    async def test_empty_unknown_and_malformed_bodies_keep_exact_original_without_answer_fallback(self):
        for protocol in ("openai", "responses", "anthropic"):
            for body in ("", "data: [DONE]\r\n\r\n", " {synthetic-invalid-json}\r\n", "data: {\"synthetic_unknown\":true}\n\n"):
                with self.subTest(protocol=protocol, body_size=len(body)):
                    result, events = await self.measure(body.encode(), protocol=protocol)
                    self.assertEqual(raw_text(events), (body, False))
                    self.assertFalse(any(event["type"] == "chunk" for event in events))
                    self.assertEqual(result["chars"], 0)
                    self.assertIsNone(result["ttft_ms"])
                    self.assertEqual(result["http_status"], 200)

    async def test_transport_failure_and_interruption_do_not_invent_http_or_upstream_errors(self):
        def failed(request):
            raise httpx.ConnectError("synthetic connection error")
        queue = asyncio.Queue()
        config = engine.EndpointConfig(base_url="https://synthetic.example/v1", api_key="synthetic-key",
                                       model="synthetic-model", protocol="openai")
        async with httpx.AsyncClient(transport=httpx.MockTransport(failed)) as client:
            await engine.measure_side("candidate", config, engine.QUESTIONS[0], queue, client)
        result = queue.get_nowait()
        self.assertEqual(result["type"], "side_finished")
        self.assertIsNone(result["http_status"])
        self.assertEqual(result["upstream_error_code"], "")
        partial = b'data: {"choices":[{"delta":{"content":"synthetic partial"}}]}\n\n'
        result, events = await self.measure(partial, httpx.ReadError("synthetic interrupted"))
        self.assertEqual(raw_text(events), (partial.decode(), False))
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["error_category"], "stream_interrupted")

    async def test_raw_capture_redacts_both_credentials_and_json_escaped_forms_across_byte_chunks(self):
        own, other = 'synthetic-"quoted\\credential', "synthetic-other-credential"
        body = sse({"error": {"code": "synthetic_code", "message": own + " " + other}})
        result, events = await self.measure(*(body[index:index + 1] for index in range(len(body))), key=own, secrets=(other,))
        captured, truncated = raw_text(events)
        expected = body.decode().replace(json.dumps(own, ensure_ascii=False)[1:-1], "[REDACTED]").replace(other, "[REDACTED]")
        # json.dumps(..., ensure_ascii=False) still escapes quotes/backslashes;
        # the capture also protects the usual ASCII-escaped representation.
        self.assertEqual(captured, expected)
        self.assertFalse(truncated)
        for secret in (own, other, json.dumps(own)[1:-1]):
            self.assertNotIn(secret, json.dumps(events, ensure_ascii=False))
        self.assertEqual(result["upstream_error_message"], "[REDACTED] [REDACTED]")

    async def test_long_stream_keeps_bounded_utf8_head_tail_and_late_error_code(self):
        head = ": synthetic head\n\n"
        middle = ": " + "界" * 7000 + "\n\n"
        tail = sse({"type": "error", "code": "synthetic_late", "message": "synthetic final error"}).decode()
        body = head + middle + tail
        result, events = await self.measure(head.encode(), middle.encode(), tail.encode(), protocol="responses")
        captured, truncated = raw_text(events)
        self.assertTrue(truncated)
        self.assertLessEqual(len(captured.encode()), engine.RAW_RESPONSE_MAX_BYTES)
        self.assertTrue(captured.startswith(head))
        self.assertTrue(captured.endswith(tail))
        self.assertNotEqual(captured, body)
        self.assertEqual(result["upstream_error_code"], "synthetic_late")
        self.assertEqual(result["upstream_error_message"], "synthetic final error")
        self.assertLessEqual(sum(len(event.get("raw_response", "").encode()) for event in events), engine.RAW_RESPONSE_MAX_BYTES * 2)

    async def test_head_tail_join_cannot_reconstruct_a_credential_or_exceed_budget(self):
        half = engine.RAW_RESPONSE_MAX_BYTES // 2
        body = "H" * (half - 1) + "x" + "M" * 20_000 + "Yz" + "T" * (half - 2)
        _, events = await self.measure(body.encode(), key="xYz")
        captured, truncated = raw_text(events)
        self.assertTrue(truncated)
        self.assertNotIn("xYz", captured)
        self.assertIn("[REDACTED]", captured)
        self.assertLessEqual(len(json.dumps(captured).encode()), engine.RAW_RESPONSE_MAX_BYTES + 2)

    async def test_head_tail_join_redacts_json_decoded_keys_even_inside_a_unicode_escape(self):
        own = "synthetic-private-credential-unique"
        header, footer = '{"error":{"message":"', '"}}'
        half = engine.RAW_RESPONSE_MAX_BYTES // 2

        def budget(value):
            return len(json.dumps(value, ensure_ascii=False)[1:-1].encode("utf-8"))

        cases = (
            (own[:12].replace("-", "\\u002d"), "", own[12:].replace("-", "\\u002d")),
            ("\\u00", "41", "73" + own[1:].replace("-", "\\u002d")),
        )
        for prefix, escape_end, suffix in cases:
            with self.subTest(inside_escape=bool(escape_end)):
                head = "H" * (half - budget(header + prefix))
                tail = "T" * (half - budget(suffix + footer))
                body = header + head + prefix + escape_end + "M" * 20_000 + suffix + tail + footer
                self.assertNotIn(own, json.loads(body)["error"]["message"])
                _, events = await self.measure(body.encode(), status=401, key=own)
                captured, truncated = raw_text(events)
                self.assertTrue(truncated)
                self.assertNotIn(own, json.loads(captured)["error"]["message"])
                self.assertEqual(captured, header + head + "[REDACTED]" + tail + footer)
                self.assertLessEqual(budget(captured), engine.RAW_RESPONSE_MAX_BYTES)

    async def test_large_http_json_extracts_code_from_untruncated_parse_buffer(self):
        body = json.dumps({"synthetic_padding": "x" * 20_000, "error": {"code": "synthetic_after_limit", "message": "synthetic failure"}})
        result, events = await self.measure(body.encode(), status=429)
        self.assertEqual(result["upstream_error_code"], "synthetic_after_limit")
        self.assertEqual(result["upstream_error_message"], "synthetic failure")
        captured, truncated = raw_text(events)
        self.assertTrue(truncated)
        self.assertLessEqual(len(captured.encode()), engine.RAW_RESPONSE_MAX_BYTES)
        self.assertTrue(captured.endswith('"message": "synthetic failure"}}'))

    async def test_sixty_responses_with_controls_multibyte_and_long_errors_fit_report_budget(self):
        body = json.dumps({"error": {"code": "\0" * 10_000, "message": "界" * 10_000}})
        result, events = await self.measure(body.encode(), status=429)
        captured, truncated = raw_text(events)
        self.assertTrue(truncated)
        self.assertTrue(result["upstream_error_truncated"])
        self.assertLessEqual(len(json.dumps(result["upstream_error_code"]).encode()), engine.RAW_RESPONSE_MAX_BYTES + 2)
        self.assertLessEqual(len(json.dumps(result["upstream_error_message"], ensure_ascii=False).encode()), engine.RAW_RESPONSE_MAX_BYTES + 2)
        # Exercise raw control-character expansion independently of a valid
        # JSON envelope, then save the full 5 rounds x 6 questions x 2 sides.
        _, control_events = await self.measure(("\0界" * 10_000).encode())
        control_raw, control_truncated = raw_text(control_events)
        self.assertTrue(control_truncated)
        report = {"version": 2, "status": "completed", "rounds": 5, "questions": [], "responses": [], "measurements": []}
        for index in range(60):
            identity = {"round": index // 12 + 1, "question_id": f"synthetic-{index % 12 // 2}",
                        "side": "candidate" if index % 2 == 0 else "reference"}
            report["responses"].append({**identity, "raw_response": control_raw if index % 2 else captured,
                                        "raw_response_truncated": True, "content": "synthetic answer", "reasoning": ""})
            report["measurements"].append({**result, **identity})
        encoded = storage.JobStore._encode(report)
        self.assertLess(len(encoded.encode()), storage.MAX_REPORT_BYTES)
        self.assertEqual(len(json.loads(encoded)["responses"]), 60)

    async def test_oversized_unterminated_line_preserves_bounded_raw_and_stops_reading(self):
        result, events = await self.measure(b"x" * 1_048_577)
        captured, truncated = raw_text(events)
        self.assertTrue(truncated)
        self.assertEqual(len(captured.encode()), engine.RAW_RESPONSE_MAX_BYTES)
        self.assertEqual(result["status"], "error")

    async def test_responses_total_timeout_preserves_received_status_and_raw(self):
        first = b": synthetic timeout evidence\n\n"
        started = asyncio.Event()
        closed = asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield first
                started.set()
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        config = engine.EndpointConfig(base_url="https://synthetic.example/v1", api_key="synthetic-key",
                                       model="synthetic-model", protocol="responses")
        queue = asyncio.Queue()
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
        with patch.object(engine, "RESPONSES_TOTAL_TIMEOUT_SECONDS", .05):
            async with httpx.AsyncClient(transport=transport) as client:
                await engine.measure_side("candidate", config, engine.QUESTIONS[0], queue, client)
        self.assertTrue(started.is_set())
        self.assertTrue(closed.is_set())
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        self.assertEqual(raw_text(events), (first.decode(), False))
        result = next(event for event in events if event["type"] == "side_finished")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["error_category"], "total_timeout")


class RawResponsePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(os.environ.get("TMPDIR") or os.environ.get("EVAL_TEST_ARTIFACT_ROOT") or
                    "/Users/lmurder/Desktop/api中转站/中转站极限测试数据/workbench-tests/tmp")
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="raw-response-", dir=root)
        self.root = Path(self.temp.name)
        self.registry = Registry(self.root / "registry")
        self.own, self.other = "synthetic-candidate-credential", "synthetic-reference-credential"
        self.reference = self.registry.save({"base_url": "https://reference.example/v1", "api_key": self.other, "multiplier": 1})
        self.release, self.started = asyncio.Event(), asyncio.Event()
        self.body = api.CompareInput(candidate={"base_url": "https://candidate.example/v1", "api_key": self.own,
                                      "model": "synthetic-model", "protocol": "openai"},
                                    reference={"channel_id": self.reference["id"], "model": "synthetic-model", "protocol": "openai"}, rounds=1)
        self.raw = sse({"error": {"code": "synthetic_persisted", "message": self.own + " " + self.other + " <synthetic-tag>"}})
        owner = self
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for index in range(0, len(owner.raw), 7):
                    yield owner.raw[index:index + 7]
                owner.started.set()
                await owner.release.wait()
            async def aclose(self):
                pass
        self.transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
        self.manager = AdmissionManager(self.root / "admission" / "reports.db", self.registry, transport=self.transport)
        self.delay = patch.object(engine, "wait_between_questions", new=AsyncMock())
        self.delay.start()
        await self.manager.start()

    async def asyncTearDown(self):
        self.release.set()
        await self.manager.stop()
        self.delay.stop()
        storage.close()
        self.temp.cleanup()

    def assert_safe(self, value):
        serialized = json.dumps(value, ensure_ascii=False)
        self.assertNotIn(self.own, serialized)
        self.assertNotIn(self.other, serialized)

    async def test_live_snapshot_history_and_events_keep_same_safe_raw(self):
        self.raw = sse({"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 40}}}) + self.raw
        job = await self.manager.enqueue(self.body)
        await asyncio.wait_for(self.started.wait(), 2)
        async with asyncio.timeout(2):
            while True:
                snapshot = self.manager.public(job["id"])
                if all(row["raw_response"] for row in snapshot["report"]["responses"]):
                    break
                await asyncio.sleep(.005)
        self.assertTrue(all(row["raw_response"] for row in snapshot["report"]["responses"]))
        self.assert_safe(snapshot)
        self.assert_safe(self.manager.store.events(job["id"]))
        self.release.set()
        async with asyncio.timeout(5):
            while self.manager.public(job["id"])["state"] not in {"succeeded", "failed"}:
                await asyncio.sleep(.005)
        final = self.manager.public(job["id"])
        expected = self.raw.decode().replace(self.own, "[REDACTED]").replace(self.other, "[REDACTED]")
        self.assertEqual(final["state"], "succeeded")
        self.assertTrue(all(row["raw_response"] == expected and row["content"] == "" for row in final["report"]["responses"]))
        self.assertTrue(all(row["upstream_error_code"] == "synthetic_persisted" for row in final["report"]["measurements"]))
        for row in final["report"]["measurements"]:
            self.assertFalse(row["ok"])
            self.assertEqual(row["cache_hit_rate"], .4)
            self.assertTrue(row["cache_request_hit"])
            self.assertIsNone(row["cache_write_rate"])
        finished = [e for e in self.manager.store.events(job["id"], limit=500)["events"] if e["type"] == "side_finished"]
        self.assertEqual(len(finished), len(final["report"]["measurements"]))
        self.assertTrue(all(e["cache_hit_rate"] == .4 for e in finished))
        with patch.object(storage, "DB_PATH", self.manager.store.path):
            storage.close()
            historical = storage.get_report(final["report_id"])
            self.assertEqual(historical["responses"], final["report"]["responses"])
            self.assertEqual(historical["measurements"], final["report"]["measurements"])
            exported = json.loads(json.dumps(historical))
            self.assertEqual(exported["summary"]["cache"], final["report"]["summary"]["cache"])
            self.assert_safe(historical)
            storage.close()
        for path in self.manager.store.path.parent.glob("reports.db*"):
            for secret in (self.own, self.other):
                self.assertNotIn(secret.encode(), path.read_bytes())

    async def test_cancel_flushes_received_raw_tail_and_keeps_unknown_steps(self):
        self.raw = sse({"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 40}}}) + self.raw
        job = await self.manager.enqueue(self.body)
        await asyncio.wait_for(self.started.wait(), 2)
        final = await self.manager.cancel(job["id"])
        expected = self.raw.decode().replace(self.own, "[REDACTED]").replace(self.other, "[REDACTED]")
        self.assertEqual(final["state"], "cancelled")
        self.assertEqual(final["progress"]["unknown"], 2)
        self.assertTrue(all(row["raw_response"] == expected for row in final["report"]["responses"]))
        self.assert_safe(final)
        self.assert_safe(self.manager.store.events(job["id"]))
        for row in final["report"]["measurements"]:
            self.assertEqual(row["status"], "unknown")
            self.assertIsNone(row.get("cache_hit_rate"))
        for side in ("candidate", "reference"):
            self.assertIsNone(final["report"]["summary"]["cache"][side]["rate"])
        with patch.object(storage, "DB_PATH", self.manager.store.path):
            storage.close()
            historical = storage.get_report(final["report_id"])
            self.assertEqual(historical["responses"], final["report"]["responses"])
            self.assertEqual(historical["measurements"], final["report"]["measurements"])
            self.assert_safe(historical)
            storage.close()

    async def test_recovered_single_side_receives_both_credential_redaction(self):
        run = engine.CompareRequest(candidate={"base_url": "https://candidate.example/v1", "api_key": self.own,
                                              "model": "synthetic-model", "protocol": "openai"},
                                    reference={"base_url": "https://reference.example/v1", "api_key": self.other,
                                              "model": "synthetic-model", "protocol": "openai"}, rounds=1)
        self.release.set()
        async with httpx.AsyncClient(transport=self.transport) as client:
            events = [event async for event in self.manager._single_side("candidate", run, engine.QUESTIONS[0], client, None)]
        self.assert_safe(events)
        expected = self.raw.decode().replace(self.own, "[REDACTED]").replace(self.other, "[REDACTED]")
        self.assertEqual(raw_text(events), (expected, False))

    async def test_split_protocol_credentials_stay_safe_live_in_events_database_history_and_json(self):
        for protocol in ("openai", "responses", "anthropic"):
            with self.subTest(protocol=protocol):
                started, release = asyncio.Event(), asyncio.Event()
                self.body.candidate.protocol = self.body.reference.protocol = protocol
                prefix = sse(text_delta(protocol, "content", self.own[:12])) + sse(text_delta(protocol, "reasoning", self.other[:12]))
                suffix = sse(text_delta(protocol, "content", self.own[12:])) + sse(text_delta(protocol, "reasoning", self.other[12:]))
                class Stream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        for index in range(0, len(prefix), 11):
                            yield prefix[index:index + 11]
                        started.set()
                        await release.wait()
                        for index in range(0, len(suffix), 11):
                            yield suffix[index:index + 11]
                    async def aclose(self):
                        pass
                self.manager.transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
                job = await self.manager.enqueue(self.body)
                await asyncio.wait_for(started.wait(), 2)
                live = self.manager.public(job["id"])
                self.assertEqual(live["state"], "running")
                self.assert_safe(live)
                for row in live["report"]["responses"]:
                    for decoded in logical_raw_text(row["raw_response"]):
                        self.assertNotIn(self.own, decoded)
                        self.assertNotIn(self.other, decoded)
                release.set()
                async with asyncio.timeout(5):
                    while self.manager.public(job["id"])["state"] not in {"succeeded", "failed"}:
                        await asyncio.sleep(.005)
                final = self.manager.public(job["id"])
                self.assertEqual(final["state"], "succeeded")
                for row in final["report"]["responses"]:
                    for decoded in logical_raw_text(row["raw_response"]):
                        self.assertIn("[REDACTED]", decoded)
                        self.assertNotIn(self.own, decoded)
                        self.assertNotIn(self.other, decoded)
                events = self.manager.store.events(job["id"], limit=500)["events"]
                self.assert_safe(events)
                identities = {(event.get("round"), event.get("question_id"), event.get("side"))
                              for event in events if event["type"] == "raw_response"}
                for identity in identities:
                    matching = [event for event in events if (event.get("round"), event.get("question_id"), event.get("side")) == identity]
                    for end in range(1, len(matching) + 1):
                        for decoded in logical_raw_text(raw_text(matching[:end])[0]):
                            self.assertNotIn(self.own, decoded)
                            self.assertNotIn(self.other, decoded)
                with patch.object(storage, "DB_PATH", self.manager.store.path):
                    storage.close()
                    historical = storage.get_report(final["report_id"])
                    self.assertEqual(historical["responses"], final["report"]["responses"])
                    storage.close()
                # Technical JSON exports this report, so round-trip its exact
                # persisted text rather than checking only contiguous bytes.
                exported = json.loads(json.dumps(historical, ensure_ascii=False))
                for row in exported["responses"]:
                    for decoded in logical_raw_text(row["raw_response"]):
                        self.assertNotIn(self.own, decoded)
                        self.assertNotIn(self.other, decoded)
                with self.manager.store.connect() as conn:
                    stored = json.loads(conn.execute("SELECT report_json FROM admission_jobs WHERE task_id=?", (job["id"],)).fetchone()[0])
                self.assertEqual(stored["responses"], final["report"]["responses"])


class RawResponseInputTests(unittest.TestCase):
    def test_old_reports_default_raw_empty_and_new_raw_enforces_utf8_bytes(self):
        base = {"round": 1, "question_id": "synthetic-question", "side": "candidate"}
        self.assertEqual(api.ReportResponseInput(**base).raw_response, "")
        self.assertFalse(api.ReportResponseInput(**base).raw_response_truncated)
        model = api.ReportResponseInput(**base, raw_response="界" * 5461)
        self.assertEqual(len(model.raw_response.encode()), 16_383)
        with self.assertRaises(ValidationError):
            api.ReportResponseInput(**base, raw_response="界" * 5462)
        with self.assertRaises(ValidationError):
            api.ReportResponseInput(**base, raw_response="\0" * 3000)


if __name__ == "__main__":
    unittest.main()
