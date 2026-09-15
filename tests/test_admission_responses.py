"""Synthetic Responses fixtures exercise the real admission request and SSE path."""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from features.admission import main as engine


def event(kind, **values):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **values}) + "\n\n").encode()


def terminal(status="completed", **values):
    return event("response." + status, response={"status": status, **values})


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


class ResponsesRequestTests(unittest.TestCase):
    def test_readonly_inspection_is_sanitized_and_enforces_protocol(self):
        from scripts.inspect_admission import inspect
        result = inspect("https://synthetic.example/custom/v1", "gpt-6-astra", "openai")
        self.assertEqual(result["requests_sent"], 0)
        self.assertEqual(result["protocol"], "responses")
        self.assertEqual(len(result["settings"]), 5)
        self.assertNotIn("synthetic.example", json.dumps(result))
        self.assertNotIn("custom", json.dumps(result))
        self.assertTrue(all(set(row["parameters"]) == {"model", "stream", "store", "max_output_tokens", "reasoning"}
                            for row in result["settings"]))
        for base in ("invalid", "https://synthetic.example/v1?token=synthetic"):
            with self.assertRaises(ValueError):
                inspect(base, "gpt-6-astra", "responses")

    def test_gpt6_forces_responses_with_the_correct_generation_parameters(self):
        for protocol in ("openai", "anthropic", "responses"):
            config = engine.EndpointConfig(base_url="https://synthetic.example/v1", api_key="synthetic",
                                           model=" gpt-6-astra ", protocol=protocol)
            self.assertEqual(config.protocol, "responses")
            for question in engine.QUESTIONS:
                payload = engine.build_payload(config, question)
                self.assertEqual(set(payload), {"model", "input", "instructions", "stream", "store",
                                               "reasoning", "max_output_tokens"})
                self.assertEqual(payload["max_output_tokens"], 4096)
                self.assertEqual(payload["reasoning"], {"effort": "low"})
                self.assertEqual(payload["input"], question["prompt"])
                self.assertEqual(payload["instructions"], engine.SYSTEM_PROMPT)
                self.assertIs(payload["stream"], True)
                self.assertIs(payload["store"], False)
                self.assertEqual(engine.build_headers(config)["Authorization"], "Bearer synthetic")

    def test_root_base_full_endpoint_and_custom_prefix_are_normalized(self):
        for supplied, expected in [
            ("https://synthetic.example", "/v1/responses"),
            ("https://synthetic.example/v1/", "/v1/responses"),
            ("https://synthetic.example/v1/responses", "/v1/responses"),
            ("https://synthetic.example/v1/chat/completions/", "/v1/responses"),
            ("https://synthetic.example/custom/v1/messages", "/custom/v1/responses"),
        ]:
            config = engine.EndpointConfig(base_url=supplied, api_key="synthetic", model="gpt-6-astra", protocol="openai")
            self.assertEqual(engine.endpoint_url(config), "https://synthetic.example" + expected)

    def test_other_models_keep_their_existing_protocol_and_budget(self):
        config = engine.EndpointConfig(base_url="https://synthetic.example/v1", api_key="synthetic",
                                       model="gpt-5.6-sol", protocol="openai")
        payload = engine.build_payload(config, engine.QUESTIONS[0])
        self.assertEqual(config.protocol, "openai")
        self.assertEqual(payload["max_tokens"], 180)
        self.assertNotIn("reasoning", payload)

    def test_gpt6_metadata_exposes_the_enforced_policy(self):
        preset = next(item for item in engine.PRESETS if item["id"] == "gpt-6-astra")
        self.assertEqual(preset["protocol"], "responses")
        self.assertEqual(preset["required_protocol"], "responses")
        self.assertEqual(preset["max_output_tokens"], 4096)
        self.assertEqual(preset["reasoning_effort"], "low")


class ResponsesMeasurementTests(unittest.IsolatedAsyncioTestCase):
    async def measure(self, *chunks, status=200):
        stream = FixtureStream(chunks)
        def handler(request):
            self.assertEqual(request.url.path, "/v1/responses")
            self.assertNotIn("max_completion_tokens", json.loads(request.content))
            return httpx.Response(status, headers={"content-type": "text/event-stream"}, stream=stream)
        config = engine.EndpointConfig(base_url="https://synthetic.example/v1", api_key="synthetic",
                                       model="gpt-6-astra", protocol="openai")
        queue = asyncio.Queue()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await engine.measure_side("candidate", config, engine.QUESTIONS[0], queue, client)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        self.assertTrue(stream.closed)
        result = next(value for value in events if value["type"] == "side_finished")
        return result, events

    async def test_complete_lifecycle_reports_usage_without_duplicating_final_text(self):
        output = [{"type": "reasoning", "summary": []},
                  {"type": "message", "content": [{"type": "output_text", "text": "AB"}]}]
        result, events = await self.measure(
            event("response.created", response={"model": "gpt-6-astra", "id": "synthetic-response"}),
            event("response.in_progress"), event("response.output_item.added"),
            event("response.output_text.delta", delta="A"),
            event("response.output_text.delta", delta="B"),
            event("response.output_text.done", text="AB"),
            terminal(output=output, model="gpt-6-astra", usage={"input_tokens": 20, "output_tokens": 42,
                     "output_tokens_details": {"reasoning_tokens": 40}}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["response_status"], "completed")
        self.assertEqual((result["input_tokens"], result["output_tokens"], result["reasoning_tokens"]), (20, 42, 40))
        self.assertEqual("".join(e.get("content", "") for e in events), "AB")
        self.assertFalse(result["speed_data_valid"])
        self.assertIsNone(result["tokens_per_second"])
        self.assertIsNotNone(result["first_answer_ms"])

    async def test_failed_and_error_events_after_text_are_not_success(self):
        endings = [terminal("failed", error={"code": "server_error"}), event("error", code="server_error")]
        for ending in endings:
            result, _ = await self.measure(event("response.output_text.delta", delta="partial"), ending)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "upstream_error")

    async def test_eof_or_done_without_terminal_is_incomplete(self):
        for ending in (b"", b": keepalive\n\n", b"data: [DONE]\n\n"):
            result, _ = await self.measure(event("response.output_text.delta", delta="partial"), ending)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "incomplete_stream")

    async def test_incomplete_reason_refusal_and_filter_are_distinct(self):
        for reason, expected in [("max_output_tokens", "truncated"), ("content_filter", "refused"),
                                 ("other", "incomplete_response")]:
            result, _ = await self.measure(event("response.output_text.delta", delta="partial"),
                                          terminal("incomplete", incomplete_details={"reason": reason}))
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], expected)
        result, _ = await self.measure(event("response.refusal.delta", delta="synthetic refusal"), terminal())
        self.assertEqual(result["status"], "refused")

    async def test_empty_or_reasoning_only_is_not_a_completed_answer(self):
        for first in (event("response.created"), event("response.reasoning_summary_text.delta", delta="summary")):
            result, _ = await self.measure(first, terminal())
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "empty")
            self.assertIsNone(result["first_answer_ms"])

    async def test_terminal_only_text_is_visible_without_claiming_stream_speed(self):
        result, events = await self.measure(terminal(output=[{"type": "message", "content": [
            {"type": "output_text", "text": "synthetic final"}]}], usage={"output_tokens": 2,
            "output_tokens_details": {"reasoning_tokens": 0}}))
        self.assertTrue(result["ok"])
        self.assertEqual("".join(e.get("content", "") for e in events), "synthetic final")
        self.assertFalse(result["speed_data_valid"])

    async def test_completion_recovers_missing_suffix_and_marks_speed_invalid(self):
        result, events = await self.measure(event("response.output_text.delta", delta="A"),
            terminal(output=[{"type": "message", "content": [{"type": "output_text", "text": "AB"}]}]))
        self.assertTrue(result["ok"])
        self.assertEqual("".join(e.get("content", "") for e in events), "AB")
        self.assertTrue(result["text_recovered_from_completion"])
        self.assertFalse(result["speed_data_valid"])

    async def test_done_recovers_each_block_without_duplicating_terminal_text(self):
        result, events = await self.measure(
            event("response.output_text.delta", output_index=1, content_index=0, delta="A"),
            event("response.output_text.done", output_index=1, content_index=0, text="AB"),
            event("response.output_text.delta", output_index=1, content_index=1, delta="C"),
            event("response.output_text.done", output_index=1, content_index=1, text="CD"),
            event("response.output_text.done", output_index=2, content_index=0, text="EF"),
            terminal(output=[{"type": "reasoning"}, {"type": "message", "content": [
                {"type": "output_text", "text": "AB"}, {"type": "output_text", "text": "CD"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "EF"}]}]))
        self.assertTrue(result["ok"])
        self.assertEqual("".join(e.get("content", "") for e in events), "ABCDEF")
        self.assertTrue(result["text_recovered_from_completion"])

    async def test_conflicting_text_is_not_silently_accepted(self):
        for ending in [event("response.output_text.done", text="different") + terminal(),
                       terminal(output=[{"type": "message", "content": [{"type": "output_text", "text": "different"}]}])]:
            result, _ = await self.measure(event("response.output_text.delta", delta="A"), ending)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "protocol_error")

    async def test_text_done_after_terminal_is_rejected(self):
        result, _ = await self.measure(event("response.output_text.delta", delta="A"), terminal(),
                                       event("response.output_text.done", text="AB"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "protocol_error")

    async def test_malformed_delta_after_valid_text_is_rejected(self):
        for kind in ["output_text", "refusal", "reasoning_text", "reasoning_summary_text"]:
            for value in [None, 123, {}, []]:
                result, _ = await self.measure(event("response.output_text.delta", delta="valid"),
                                               event(f"response.{kind}.delta", delta=value), terminal())
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "protocol_error")

    async def test_final_only_refusal_remains_visible_and_blank_is_empty(self):
        result, events = await self.measure(terminal(output=[{"type": "message", "content": [
            {"type": "refusal", "refusal": "synthetic refusal"}]}]))
        self.assertEqual(result["status"], "refused")
        self.assertEqual("".join(e.get("content", "") for e in events), "synthetic refusal")
        result, _ = await self.measure(event("response.output_text.delta", delta=" \n\t"), terminal())
        self.assertEqual(result["status"], "empty")

    async def test_malformed_wrong_protocol_and_conflicting_terminal_fail_closed(self):
        malformed = [b"data: {bad}\n\n", b'data: {"choices": [{"delta": {"content": "wrong"}}]}\n\n',
                     event("response.completed", response={"status": "failed"}),
                     event("response.completed", response={"status": "completed", "usage": []}),
                     terminal() + event("response.output_text.delta", delta="too late")]
        for bad in malformed:
            result, _ = await self.measure(event("response.output_text.delta", delta="partial"), bad, terminal())
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "protocol_error")

    async def test_multiline_sse_and_arbitrary_byte_boundaries(self):
        data = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta",\ndata: "delta":"AB"}\n\n'
        result, events = await self.measure(*[data[i:i+3] for i in range(0, len(data), 3)], terminal())
        self.assertTrue(result["ok"])
        self.assertEqual("".join(e.get("content", "") for e in events), "AB")

    async def test_missing_usage_remains_unknown(self):
        result, _ = await self.measure(event("response.output_text.delta", delta="answer"), terminal())
        self.assertTrue(result["ok"])
        self.assertIsNone(result["output_tokens"])
        self.assertIsNone(result["reasoning_tokens"])
        self.assertFalse(result["speed_data_valid"])

    async def test_http_failure_and_read_timeout_preserve_request_policy(self):
        for code in (429, 500):
            result, _ = await self.measure(b"synthetic upstream error", status=code)
            self.assertFalse(result["ok"])
            self.assertEqual(result["max_output_tokens"], 4096)
        result, _ = await self.measure(httpx.ReadTimeout("synthetic timeout"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_category"], "read_timeout")
        result, _ = await self.measure(event("response.output_text.delta", delta="partial"),
                                      httpx.ReadError("synthetic disconnect"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_category"], "stream_interrupted")

    async def test_total_deadline_stops_heartbeats_and_closes_the_response(self):
        closed = asyncio.Event()
        class Heartbeats(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    yield b": heartbeat\n\n"
                    await asyncio.sleep(.005)
            async def aclose(self):
                closed.set()
        config = engine.EndpointConfig(base_url="https://synthetic.example/v1", api_key="synthetic",
                                       model="gpt-6-astra", protocol="responses")
        queue = asyncio.Queue()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Heartbeats()))) as client:
            with patch.object(engine, "RESPONSES_TOTAL_TIMEOUT_SECONDS", .02):
                await engine.measure_side("candidate", config, engine.QUESTIONS[0], queue, client)
        self.assertTrue(closed.is_set())
        self.assertEqual(queue.get_nowait()["error_category"], "total_timeout")


if __name__ == "__main__":
    unittest.main()
