import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from main import (
    CompareRequest,
    EndpointConfig,
    PRESETS,
    QUESTIONS,
    app,
    build_headers,
    build_payload,
    chat_completions_url,
    endpoint_url,
    extract_channel_credentials,
    measure_side,
    parse_stream_line,
)


class QuestionBankTests(unittest.TestCase):
    def test_question_mix_is_fixed(self) -> None:
        self.assertEqual(len(QUESTIONS), 5)
        difficulties = [question["difficulty"] for question in QUESTIONS]
        self.assertEqual(difficulties.count("easy"), 3)
        self.assertEqual(difficulties.count("hard"), 2)
        self.assertEqual(len({question["id"] for question in QUESTIONS}), 5)

    def test_every_request_is_streaming(self) -> None:
        endpoint = EndpointConfig(
            base_url="https://example.com/v1", api_key="secret", model="demo", protocol="openai"
        )
        for question in QUESTIONS:
            payload = build_payload(endpoint, question)
            self.assertIs(payload["stream"], True)
            self.assertEqual(payload["stream_options"], {"include_usage": True})
            self.assertEqual(payload["model"], "demo")
            self.assertGreater(payload["max_tokens"], 0)

    def test_anthropic_request_uses_native_messages_protocol(self) -> None:
        endpoint = EndpointConfig(
            base_url="https://relay.example/v1",
            api_key="secret",
            model="claude-opus-5",
            protocol="anthropic",
        )
        payload = build_payload(endpoint, QUESTIONS[0])
        headers = build_headers(endpoint)
        self.assertEqual(endpoint_url(endpoint), "https://relay.example/v1/messages")
        self.assertEqual(headers["x-api-key"], "secret")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(payload["system"], "请直接完成任务。答案要清楚、紧凑；推理题给出必要推导，不要重复题目。")
        self.assertEqual(payload["messages"], [{"role": "user", "content": QUESTIONS[0]["prompt"]}])
        self.assertIs(payload["stream"], True)
        self.assertNotIn("stream_options", payload)

    def test_compare_contract_uses_candidate_and_reference(self) -> None:
        endpoint = EndpointConfig(
            base_url="https://example.com/v1", api_key="secret", model="demo", protocol="openai"
        )
        request = CompareRequest(candidate=endpoint, reference=endpoint, rounds=3)
        self.assertEqual(request.candidate, endpoint)
        self.assertEqual(request.reference, endpoint)
        self.assertEqual(request.rounds, 3)

    def test_compare_rounds_are_bounded(self) -> None:
        endpoint = EndpointConfig(
            base_url="https://example.com/v1", api_key="secret", model="demo", protocol="openai"
        )
        with self.assertRaises(ValueError):
            CompareRequest(candidate=endpoint, reference=endpoint, rounds=0)
        with self.assertRaises(ValueError):
            CompareRequest(candidate=endpoint, reference=endpoint, rounds=6)

    def test_original_codex_and_claude_families_are_available(self) -> None:
        by_id = {preset["id"]: preset for preset in PRESETS}
        expected = {
            "gpt-5.6-sol": ("Codex", "GPT-5.6 Sol"),
            "gpt-5.6-terra": ("Codex", "GPT-5.6 Terra"),
            "claude-fable-5": ("Claude", "Fable 5"),
            "claude-opus-5": ("Claude", "Opus 5"),
            "claude-sonnet-5": ("Claude", "Sonnet 5"),
        }
        for model_id, (family, label) in expected.items():
            self.assertEqual(by_id[model_id]["provider"], family)
            self.assertEqual(by_id[model_id]["label"], label)
            self.assertEqual(by_id[model_id]["model"], model_id)
            self.assertEqual(by_id[model_id]["official_base_url"], "")
            self.assertEqual(
                by_id[model_id]["protocol"], "anthropic" if family == "Claude" else "openai"
            )


class StreamProtocolTests(unittest.TestCase):
    def test_openai_sse_content_and_reasoning(self) -> None:
        line = 'data: {"choices":[{"delta":{"content":"答案","reasoning_content":"思考"}}]}'
        event = parse_stream_line(line)
        self.assertEqual(event["content"], "答案")
        self.assertEqual(event["reasoning"], "思考")
        self.assertTrue(event["recognized"])
        self.assertIsNone(parse_stream_line("data: [DONE]"))

    def test_plain_ndjson_chunk(self) -> None:
        line = '{"choices":[{"delta":{"content":"A"}}]}'
        event = parse_stream_line(line)
        self.assertEqual(event["content"], "A")
        self.assertEqual(event["reasoning"], "")

    def test_openai_buffered_message_is_recognized(self) -> None:
        event = parse_stream_line('{"choices":[{"message":{"content":"完整回答"},"finish_reason":"stop"}]}')
        self.assertEqual(event["content"], "完整回答")
        self.assertEqual(event["finish_reason"], "stop")
        self.assertEqual(event["format"], "chat_message")

    def test_openai_responses_delta_is_recognized(self) -> None:
        event = parse_stream_line('data: {"type":"response.output_text.delta","delta":"片段"}')
        self.assertEqual(event["content"], "片段")
        self.assertEqual(event["format"], "responses")

    def test_anthropic_delta_is_recognized(self) -> None:
        event = parse_stream_line(
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"文本"}}'
        )
        self.assertEqual(event["content"], "文本")
        self.assertEqual(event["format"], "anthropic")

    def test_finish_reason_is_preserved(self) -> None:
        event = parse_stream_line('data: {"choices":[{"delta":{},"finish_reason":"length"}]}')
        self.assertEqual(event["finish_reason"], "length")

    def test_openai_usage_is_preserved(self) -> None:
        event = parse_stream_line(
            'data: {"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":12,"total_tokens":32}}'
        )
        self.assertTrue(event["recognized"])
        self.assertEqual(event["output_tokens"], 12)

    def test_responses_usage_is_preserved(self) -> None:
        event = parse_stream_line(
            'data: {"type":"response.completed","response":{"status":"completed","usage":{"output_tokens":18}}}'
        )
        self.assertEqual(event["output_tokens"], 18)

    def test_chat_completion_url_is_normalized(self) -> None:
        self.assertEqual(
            chat_completions_url("https://api.example.com/v1/"),
            "https://api.example.com/v1/chat/completions",
        )
        self.assertEqual(
            chat_completions_url("https://api.example.com"),
            "https://api.example.com/v1/chat/completions",
        )
        self.assertEqual(
            chat_completions_url("https://api.deepseek.com"),
            "https://api.deepseek.com/chat/completions",
        )
        self.assertEqual(
            chat_completions_url("https://open.bigmodel.cn"),
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        )
        full = "https://api.example.com/v1/chat/completions"
        self.assertEqual(chat_completions_url(full), full)
        with self.assertRaises(ValueError):
            chat_completions_url("api.example.com/v1")


class ChannelExtractionTests(unittest.TestCase):
    def test_extracts_json_credentials(self) -> None:
        extracted = extract_channel_credentials(
            '{"channel":{"base_url":"https://relay.example/v1","api_key":"sk-json-123"}}'
        )
        self.assertEqual(extracted["base_url"], "https://relay.example/v1")
        self.assertEqual(extracted["api_key"], "sk-json-123")

    def test_extracts_curl_credentials(self) -> None:
        extracted = extract_channel_credentials(
            "curl https://relay.example/v1/chat/completions -H 'Authorization: Bearer token-curl-456'"
        )
        self.assertEqual(extracted["base_url"], "https://relay.example/v1")
        self.assertEqual(extracted["api_key"], "token-curl-456")

    def test_extracts_environment_credentials(self) -> None:
        extracted = extract_channel_credentials("BASE_URL=relay.example/v1\nAPI_KEY=plain-key-789")
        self.assertEqual(extracted["base_url"], "https://relay.example/v1")
        self.assertEqual(extracted["api_key"], "plain-key-789")


class MockStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"reasoning_content":"R"}}]}\n\n'
        await asyncio.sleep(0.01)
        yield b'data: {"choices":[{"delta":{"content":"O"}}]}\n\n'
        await asyncio.sleep(0.01)
        yield b'data: {"choices":[{"delta":{"content":"K"}}]}\n\n'
        yield b'data: {"choices":[],"usage":{"completion_tokens":12}}\n\ndata: [DONE]\n\n'


class AnthropicStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"output_tokens":1}}}\n\n'
        yield b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"OK"}}\n\n'
        yield b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}\n\n'
        yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


class StaticStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class StreamingMeasurementTests(unittest.IsolatedAsyncioTestCase):
    async def measure_events(self, stream: httpx.AsyncByteStream) -> list[dict]:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

        queue: asyncio.Queue = asyncio.Queue()
        endpoint = EndpointConfig(
            base_url="https://mock.example/v1", api_key="secret", model="demo", protocol="openai"
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await measure_side("candidate", endpoint, QUESTIONS[0], queue, client)

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        return events

    async def measure(self, stream: httpx.AsyncByteStream) -> dict:
        events = await self.measure_events(stream)
        return next(event for event in events if event["type"] == "side_finished")

    async def test_measurement_emits_chunks_and_metrics(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertIs(payload["stream"], True)
            self.assertEqual(request.headers["authorization"], "Bearer secret")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=MockStream())

        queue: asyncio.Queue = asyncio.Queue()
        endpoint = EndpointConfig(
            base_url="https://mock.example/v1", api_key="secret", model="demo", protocol="openai"
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await measure_side("candidate", endpoint, QUESTIONS[0], queue, client)

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        chunks = [event for event in events if event["type"] == "chunk"]
        finished = [event for event in events if event["type"] == "side_finished"]
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0]["reasoning"], "R")
        self.assertEqual(chunks[1]["content"], "O")
        self.assertEqual(chunks[2]["content"], "K")
        self.assertEqual(len(finished), 1)
        self.assertIs(finished[0]["ok"], True)
        self.assertEqual(finished[0]["status"], "completed")
        self.assertEqual(finished[0]["chars"], 3)
        self.assertEqual(finished[0]["output_tokens"], 12)
        self.assertGreaterEqual(finished[0]["first_answer_ms"], finished[0]["ttft_ms"])
        self.assertGreater(finished[0]["tokens_per_second"], 0)

    async def test_anthropic_measurement_posts_native_messages_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertEqual(str(request.url), "https://mock.example/v1/messages")
            self.assertEqual(request.headers["x-api-key"], "secret")
            self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("stream_options", payload)
            self.assertEqual(payload["messages"], [{"role": "user", "content": QUESTIONS[0]["prompt"]}])
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=AnthropicStream())

        queue: asyncio.Queue = asyncio.Queue()
        endpoint = EndpointConfig(
            base_url="https://mock.example/v1",
            api_key="secret",
            model="claude-opus-5",
            protocol="anthropic",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await measure_side("candidate", endpoint, QUESTIONS[0], queue, client)

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        finished = next(event for event in events if event["type"] == "side_finished")
        self.assertTrue(finished["ok"])
        self.assertEqual(finished["output_tokens"], 7)
        self.assertEqual(finished["response_format"], "anthropic")

    async def test_buffered_response_does_not_claim_token_speed(self) -> None:
        finished = await self.measure(
            StaticStream(
                b'{"choices":[{"message":{"content":"complete"},"finish_reason":"stop"}],'
                b'"usage":{"completion_tokens":5}}\n'
            )
        )
        self.assertEqual(finished["output_tokens"], 5)
        self.assertIsNone(finished["tokens_per_second"])

    async def test_length_finish_is_reported_as_truncated(self) -> None:
        finished = await self.measure(
            StaticStream(
                b'data: {"choices":[{"delta":{"content":"unfinished"}}]}\n\n',
                b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n',
            )
        )
        self.assertFalse(finished["ok"])
        self.assertEqual(finished["status"], "truncated")
        self.assertEqual(finished["finish_reason"], "length")

    async def test_recognized_empty_response_is_not_success(self) -> None:
        finished = await self.measure(
            StaticStream(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
        )
        self.assertFalse(finished["ok"])
        self.assertEqual(finished["status"], "empty")
        self.assertIsNone(finished["ttft_ms"])

    async def test_unknown_stream_format_is_not_success(self) -> None:
        events = await self.measure_events(
            StaticStream(b'data: {"unexpected":{"text":"visible upstream answer"}}\n\n')
        )
        chunks = [event for event in events if event["type"] == "chunk"]
        finished = next(event for event in events if event["type"] == "side_finished")
        self.assertEqual(len(chunks), 1)
        self.assertIn("visible upstream answer", chunks[0]["content"])
        self.assertFalse(finished["ok"])
        self.assertEqual(finished["status"], "unrecognized")
        self.assertGreater(finished["chars"], 0)
        self.assertIsNotNone(finished["ttft_ms"])

    async def test_root_url_uses_the_v1_chat_completions_endpoint_without_a_probe(self) -> None:
        requested_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=MockStream(),
            )

        queue: asyncio.Queue = asyncio.Queue()
        endpoint = EndpointConfig(
            base_url="https://mock.example", api_key="secret", model="demo", protocol="openai"
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await measure_side("candidate", endpoint, QUESTIONS[0], queue, client)

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        finished = next(event for event in events if event["type"] == "side_finished")
        self.assertEqual(requested_urls, ["https://mock.example/v1/chat/completions"])
        self.assertTrue(finished["ok"])
        self.assertEqual(finished["status"], "completed")


class FrontendSafetyTests(unittest.TestCase):
    def test_frontend_persists_only_explicit_reference_credentials(self) -> None:
        source = (Path(__file__).parent / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("sessionStorage", source)
        self.assertNotIn("innerHTML", source)
        self.assertIn("OFFICIAL_KEYS_STORAGE_KEY", source)
        self.assertIn("ONLINE_CHANNELS_STORAGE_KEY", source)
        save_function = source[source.index("function saveOfficialKey"):source.index("function clearOfficialKey")]
        self.assertNotIn("channelApiKey", save_function)
        library_writer = source[source.index("function writeOnlineChannels"):source.index("function findOnlineChannel")]
        self.assertNotIn("fields.channelApiKey", library_writer)

    def test_frontend_exposes_reference_selection_and_library(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        source = (Path(__file__).parent / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="reference-source"', html)
        self.assertIn('id="online-channel-dialog"', html)
        self.assertIn('data-side="reference"', html)
        self.assertIn("candidate:", source)
        self.assertIn("reference:", source)
        self.assertIn("protocol: preset.protocol", source)
        self.assertIn("此家族没有预置官方端", source)

    def test_question_file_is_valid_json(self) -> None:
        question_path = Path(__file__).parent / "questions.json"
        self.assertEqual(len(json.loads(question_path.read_text(encoding="utf-8"))), 5)

    def test_frontend_exposes_token_metrics(self) -> None:
        source = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-metric="first-answer"', source)
        self.assertIn('data-metric="output-tokens"', source)
        self.assertIn('data-metric="tokens-per-second"', source)

    def test_frontend_reports_are_key_free_and_support_rounds(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        source = (Path(__file__).parent / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="rounds"', html)
        self.assertIn('id="report-history"', html)
        self.assertIn("REPORTS_STORAGE_KEY", source)
        self.assertIn("function buildSafeReport", source)
        report_builder = source[source.index("function buildSafeReport"):source.index("function saveReport")]
        self.assertNotIn("api_key", report_builder)
        self.assertNotIn("ApiKey", report_builder)


class MultiRoundRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_compare_stream_labels_every_round(self) -> None:
        endpoint = EndpointConfig(
            base_url="https://example.com/v1", api_key="secret", model="demo", protocol="openai"
        )

        async def fake_run_question(body, question, client):
            for side in ("candidate", "reference"):
                yield {
                    "type": "side_finished",
                    "question_id": question["id"],
                    "side": side,
                    "ok": True,
                    "status": "completed",
                    "ttft_ms": 100,
                    "first_answer_ms": 120,
                    "total_ms": 500,
                }

        transport = httpx.ASGITransport(app=app)
        with patch("main.run_question", fake_run_question), patch(
            "main.wait_between_questions", new_callable=AsyncMock
        ) as wait_between_questions:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/compare",
                    json={
                        "candidate": endpoint.model_dump(),
                        "reference": endpoint.model_dump(),
                        "rounds": 2,
                    },
                )

        self.assertEqual(response.status_code, 200)
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(events[0]["rounds"], 2)
        self.assertEqual(events[0]["total_question_runs"], 10)
        self.assertEqual([event["round"] for event in events if event["type"] == "round_started"], [1, 2])
        finished = [event for event in events if event["type"] == "side_finished"]
        self.assertEqual(len(finished), 20)
        self.assertEqual({event["round"] for event in finished}, {1, 2})
        cooldowns = [event for event in events if event["type"] == "question_cooldown"]
        self.assertEqual(len(cooldowns), 9)
        self.assertTrue(all(event["seconds"] == 3 for event in cooldowns))
        self.assertEqual(wait_between_questions.await_count, 9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
