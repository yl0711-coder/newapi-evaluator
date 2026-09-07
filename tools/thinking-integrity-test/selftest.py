"""Run deterministic single-channel checks without network access or open ports."""

import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

import main as app_module


API_KEY = "sk-selftest-secret"


def openai_response(answer="42", reasoning="Analyze the constraints and derive 42.", finish_reason="stop"):
    message = {"role": "assistant", "content": answer}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def anthropic_response(answer="42", thinking="The constraints imply 42.", stop_reason="end_turn"):
    blocks = []
    if thinking is not None:
        blocks.append({"type": "thinking", "thinking": thinking, "signature": "test-signature"})
    if answer is not None:
        blocks.append({"type": "text", "text": answer})
    return {"type": "message", "content": blocks, "stop_reason": stop_reason}


def endpoint(protocol="openai", **kwargs):
    values = {
        "base_url": "https://channel.test",
        "api_key": API_KEY,
        "model": "test-model",
        "protocol": protocol,
    }
    values.update(kwargs)
    return app_module.EndpointConfig(**values)


def question(reasoning_required=True):
    return app_module.Question(
        id="unit-question", title="Deterministic reasoning question", difficulty="hard",
        prompt="What is six times seven?", reasoning_required=reasoning_required,
    )


class RequestTests(unittest.TestCase):
    def test_endpoint_roots_and_complete_paths(self):
        cases = [
            ("https://api.deepseek.com", "openai", "https://api.deepseek.com/v1/chat/completions"),
            ("https://channel.test/v1/", "openai", "https://channel.test/v1/chat/completions"),
            ("https://channel.test/custom/chat/completions", "openai", "https://channel.test/custom/chat/completions"),
            ("https://api.anthropic.com", "anthropic", "https://api.anthropic.com/v1/messages"),
            ("https://channel.test/v1/messages", "anthropic", "https://channel.test/v1/messages"),
        ]
        for value, protocol, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(app_module.normalize_url(value, protocol), expected)

    def test_openai_request_is_single_nonstream_chat(self):
        url, headers, body = app_module.build_request(endpoint(), question(), 8192)
        self.assertTrue(url.endswith("/v1/chat/completions"))
        self.assertEqual(headers["Authorization"], f"Bearer {API_KEY}")
        self.assertIs(body["stream"], False)
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["max_tokens"], 8192)
        self.assertIn(question().prompt, json.dumps(body["messages"]))

    def test_anthropic_explicit_thinking_budget(self):
        url, headers, body = app_module.build_request(endpoint("anthropic"), question(), 8192)
        self.assertTrue(url.endswith("/v1/messages"))
        self.assertEqual(headers["x-api-key"], API_KEY)
        self.assertIs(body["stream"], False)
        self.assertEqual(body["thinking"], {"type": "enabled", "budget_tokens": 1024})
        self.assertLess(body["thinking"]["budget_tokens"], body["max_tokens"])

    def test_anthropic_adaptive_thinking(self):
        _, _, body = app_module.build_request(endpoint("anthropic", thinking_mode="adaptive"), question(), 8192)
        self.assertEqual(body["thinking"], {"type": "adaptive"})

    def test_anthropic_disabled_thinking(self):
        _, _, body = app_module.build_request(endpoint("anthropic", thinking_mode="disabled"), question(), 8192)
        self.assertEqual(body["thinking"], {"type": "disabled"})


class ChannelExtractionTests(unittest.TestCase):
    def test_extracts_nested_json_channel_information(self):
        extracted = app_module.extract_channel_information(json.dumps({
            "channel": {
                "base_url": "https://relay.example/v1/chat/completions",
                "api_key": "sk-json-123",
                "model": "gpt-5.5",
                "protocol": "openai",
            }
        }))
        self.assertEqual(extracted["base_url"], "https://relay.example/v1")
        self.assertEqual(extracted["api_key"], "sk-json-123")
        self.assertEqual(extracted["model"], "gpt-5.5")
        self.assertEqual(extracted["protocol"], "openai")

    def test_extracts_curl_channel_information(self):
        extracted = app_module.extract_channel_information(
            "curl https://relay.example/v1/messages "
            "-H 'x-api-key: token-curl-456' -H 'anthropic-version: 2023-06-01' "
            "-d '{\"model\":\"claude-test\"}'"
        )
        self.assertEqual(extracted["base_url"], "https://relay.example/v1")
        self.assertEqual(extracted["api_key"], "token-curl-456")
        self.assertEqual(extracted["model"], "claude-test")
        self.assertEqual(extracted["protocol"], "anthropic")

    def test_extracts_environment_channel_information(self):
        extracted = app_module.extract_channel_information(
            "OPENAI_BASE_URL=relay.example/v1\nOPENAI_API_KEY=plain-key-789\nOPENAI_MODEL=gpt-test"
        )
        self.assertEqual(extracted["base_url"], "https://relay.example/v1")
        self.assertEqual(extracted["api_key"], "plain-key-789")
        self.assertEqual(extracted["model"], "gpt-test")


class ResponseTests(unittest.TestCase):
    def inspect(self, body, protocol="openai", required=True):
        return app_module.inspect_response(body, protocol, required)

    def test_numbered_steps_without_blank_lines_are_returned(self):
        text = "1. Read conditions.\n2. Derive the value.\n3. Conclude 42."
        completion, reasoning, answer, returned = self.inspect(openai_response(reasoning=text))
        self.assertEqual(completion.status, "complete")
        self.assertEqual(reasoning.status, "returned")
        self.assertEqual(reasoning.characters, len(text))
        self.assertEqual(answer, "42")
        self.assertEqual(returned, text)

    def test_reasoning_without_answer_is_not_complete(self):
        completion, reasoning, answer, _ = self.inspect(openai_response(answer="", reasoning="First.\n\nSecond.\n\nThird."))
        self.assertEqual(completion.status, "empty")
        self.assertEqual(reasoning.status, "returned")
        self.assertFalse(answer)

    def test_missing_reasoning_does_not_make_answer_incomplete(self):
        completion, reasoning, _, _ = self.inspect(openai_response(reasoning=None))
        self.assertEqual(completion.status, "complete")
        self.assertEqual(reasoning.status, "missing")
        self.assertFalse(reasoning.field_present)

    def test_reasoning_tokens_are_reported_without_claiming_visible_reasoning(self):
        body = openai_response(answer="The answer contains a written derivation.", reasoning=None)
        body["usage"]["completion_tokens_details"] = {"reasoning_tokens": 147}
        completion, reasoning, answer, _ = self.inspect(body)
        self.assertEqual(completion.status, "complete")
        self.assertEqual(reasoning.status, "missing")
        self.assertEqual(reasoning.reported_tokens, 147)
        self.assertIn("written derivation", answer)
        self.assertIn("147", reasoning.detail)

    def test_empty_reasoning_field_is_present_but_missing(self):
        _, reasoning, _, _ = self.inspect(openai_response(reasoning="   "))
        self.assertEqual(reasoning.status, "missing")
        self.assertTrue(reasoning.field_present)

    def test_baseline_does_not_require_reasoning(self):
        _, reasoning, _, _ = self.inspect(openai_response(reasoning=None), required=False)
        self.assertEqual(reasoning.status, "not_required")

    def test_baseline_can_still_report_returned_reasoning(self):
        _, reasoning, _, _ = self.inspect(openai_response(), required=False)
        self.assertEqual(reasoning.status, "returned")

    def test_explicit_token_exhaustion_is_truncated(self):
        for reason in ("length", "max_tokens", "model_context_window_exceeded"):
            with self.subTest(reason=reason):
                completion, _, _, _ = self.inspect(openai_response(finish_reason=reason))
                self.assertEqual(completion.status, "truncated")

    def test_unknown_filter_and_tool_stops_are_not_complete(self):
        for reason in (None, "content_filter", "tool_calls", "refusal", "custom_stop"):
            with self.subTest(reason=reason):
                completion, _, _, _ = self.inspect(openai_response(finish_reason=reason))
                self.assertEqual(completion.status, "unknown")

    def test_incomplete_flag_overrides_normal_stop(self):
        for location in ("root", "choice", "message"):
            with self.subTest(location=location):
                body = openai_response()
                target = body if location == "root" else body["choices"][0]
                if location == "message":
                    target = target["message"]
                target["incomplete"] = True
                completion, _, _, _ = self.inspect(body)
                self.assertEqual(completion.status, "truncated")

    def test_anthropic_one_thinking_block_is_visible_reasoning(self):
        completion, reasoning, answer, text = self.inspect(anthropic_response(), "anthropic")
        self.assertEqual(completion.status, "complete")
        self.assertEqual(reasoning.status, "returned")
        self.assertEqual(answer, "42")
        self.assertEqual(text, "The constraints imply 42.")

    def test_anthropic_redacted_block_does_not_count_as_fully_returned(self):
        for visible in (None, "Visible part"):
            with self.subTest(visible=visible):
                body = anthropic_response(thinking=visible)
                body["content"].insert(0, {"type": "redacted_thinking", "data": "opaque"})
                completion, reasoning, _, _ = self.inspect(body, "anthropic")
                self.assertEqual(completion.status, "complete")
                self.assertEqual(reasoning.status, "redacted")
                self.assertEqual(reasoning.redacted_blocks, 1)

    def test_malformed_response_is_rejected(self):
        cases = (
            ("openai", {}),
            ("openai", {"choices": []}),
            ("openai", {"choices": [None]}),
            ("openai", {"choices": [{"message": "invalid"}]}),
            ("openai", openai_response(answer=7)),
            ("openai", openai_response(reasoning={"invalid": "object"})),
            ("openai", openai_response(finish_reason=["stop"])),
            ("anthropic", {}),
            ("anthropic", {"content": ["invalid"], "stop_reason": "end_turn"}),
        )
        for protocol, body in cases:
            with self.subTest(protocol=protocol, body=body):
                with self.assertRaises(ValueError):
                    self.inspect(body, protocol)


class ChannelTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, handler, protocol="openai", required=True, timeout=30):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            return await app_module.call_non_stream(client, endpoint(protocol), question(required), 8192, timeout)

    async def test_success_preserves_response_and_timings(self):
        result = await self.call(lambda request: httpx.Response(200, json=openai_response()))
        self.assertIsNone(result.error)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.completion.status, "complete")
        self.assertEqual(result.reasoning.status, "returned")
        self.assertEqual(json.loads(result.raw_response), openai_response())
        self.assertEqual(result.usage["total_tokens"], 30)
        self.assertGreater(result.response_bytes, 0)
        self.assertGreaterEqual(result.total_time_ms, result.headers_time_ms)
        self.assertGreaterEqual(result.total_time_ms, result.first_byte_ms)

    async def test_key_free_response_preserves_original_json_format(self):
        raw = json.dumps(openai_response(answer="答案 42"), indent=4, ensure_ascii=True) + "\n"
        result = await self.call(lambda request: httpx.Response(200, content=raw.encode("utf-8")))
        self.assertEqual(result.completion.status, "complete")
        self.assertEqual(result.raw_response, raw)

    async def test_http_failure_is_a_counted_result(self):
        result = await self.call(lambda request: httpx.Response(401, text="Rejected"))
        self.assertEqual(result.http_status, 401)
        self.assertEqual(result.completion.status, "error")
        self.assertTrue(result.error)

    async def test_invalid_json_is_reported_without_crashing(self):
        result = await self.call(lambda request: httpx.Response(200, text='{ "choices":'))
        self.assertEqual(result.completion.status, "error")
        self.assertTrue(result.error)

    async def test_invalid_response_shape_is_reported(self):
        result = await self.call(lambda request: httpx.Response(200, json={"unexpected": "shape"}))
        self.assertEqual(result.completion.status, "error")
        self.assertTrue(result.error)

    async def test_partial_read_preserves_prefix_and_closes_stream(self):
        class InterruptedStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b'{"choices":['
                raise httpx.ReadError("Connection dropped")

            async def aclose(self):
                self.closed = True

        stream = InterruptedStream()
        result = await self.call(lambda request: httpx.Response(200, stream=stream))
        self.assertEqual(result.raw_response, '{"choices":[')
        self.assertEqual(result.completion.status, "error")
        self.assertTrue(result.error)
        self.assertTrue(stream.closed)
        self.assertIsNotNone(result.first_byte_ms)

    async def test_total_deadline_cancels_slow_body_and_preserves_prefix(self):
        class SlowStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b'{"choices":['
                await asyncio.sleep(1)
                yield b"]}"

            async def aclose(self):
                self.closed = True

        stream = SlowStream()
        result = await self.call(lambda request: httpx.Response(200, stream=stream), timeout=0.03)
        self.assertEqual(result.raw_response, '{"choices":[')
        self.assertEqual(result.completion.status, "error")
        self.assertTrue(result.error)
        self.assertTrue(stream.closed)
        self.assertLess(result.total_time_ms, 1000)

    async def test_local_size_limit_preserves_prefix_and_marks_error(self):
        response_bytes = json.dumps(openai_response()).encode("utf-8")
        limit = 23
        with patch.object(app_module, "MAX_RESPONSE_BYTES", limit):
            result = await self.call(lambda request: httpx.Response(200, content=response_bytes))
        self.assertEqual(result.completion.status, "error")
        self.assertTrue(result.error)
        self.assertEqual(result.response_bytes, limit)
        self.assertEqual(result.raw_response.encode("utf-8"), response_bytes[:limit])
        self.assertEqual(app_module.summarize([result])["complete_count"], 0)

    async def test_error_results_remain_in_complete_rate_denominator(self):
        success = await self.call(lambda request: httpx.Response(200, json=openai_response()))
        failure = await self.call(lambda request: httpx.Response(503, text="Unavailable"))
        summary = app_module.summarize([success, failure, failure, failure, failure])
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["success_count"], 1)
        self.assertEqual(summary["error_count"], 4)
        self.assertEqual(summary["complete_rate"], 0.2)
        self.assertEqual(summary["reasoning_return_rate"], 0.2)
        self.assertEqual(summary["reasoning_complete_rate"], 0.2)

    async def test_baseline_is_excluded_from_reasoning_denominator(self):
        hard = await self.call(lambda request: httpx.Response(200, json=openai_response()))
        baseline = await self.call(lambda request: httpx.Response(200, json=openai_response(reasoning=None)), required=False)
        summary = app_module.summarize([hard, baseline])
        self.assertEqual(summary["reasoning_expected"], 1)
        self.assertEqual(summary["reasoning_returned"], 1)
        self.assertEqual(summary["reasoning_return_rate"], 1)

    async def test_truncated_reasoning_is_returned_but_not_complete(self):
        result = await self.call(lambda request: httpx.Response(200, json=openai_response(finish_reason="length")))
        summary = app_module.summarize([result])
        self.assertEqual(summary["reasoning_returned"], 1)
        self.assertEqual(summary["reasoning_complete_count"], 0)
        self.assertEqual(summary["truncated_count"], 1)


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_transport = app_module.app.state.upstream_transport
        self.requests = []

        def upstream(request):
            self.requests.append(request)
            return httpx.Response(200, json=openai_response())

        app_module.app.state.upstream_transport = httpx.MockTransport(upstream)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://testserver", trust_env=False)

    async def asyncTearDown(self):
        await self.client.aclose()
        app_module.app.state.upstream_transport = self.previous_transport

    def payload(self):
        config = endpoint().model_dump(exclude={"api_key"})
        config["api_key"] = API_KEY
        return {"endpoint": config, "max_tokens": 8192, "timeout_seconds": 30}

    async def test_extract_channel_endpoint_does_not_contact_upstream(self):
        response = await self.client.post("/api/extract-channel", json={"text": (
            "curl https://relay.example/v1/chat/completions "
            "-H 'Authorization: Bearer sk-import-123' -d '{\"model\":\"gpt-test\"}'"
        )})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.requests, [])
        self.assertEqual(response.json(), {
            "base_url": "https://relay.example/v1",
            "api_key": "sk-import-123",
            "model": "gpt-test",
            "protocol": "openai",
            "has_url": True,
            "has_key": True,
            "has_model": True,
            "has_protocol": True,
        })

    async def test_one_channel_five_questions_exactly_five_requests(self):
        question_response = await self.client.get("/api/questions")
        self.assertEqual(question_response.status_code, 200)
        questions = question_response.json()
        self.assertEqual(len(questions), 5)
        self.assertEqual(len({item["id"] for item in questions}), 5)
        self.assertEqual(sum(item["reasoning_required"] for item in questions), 4)
        response = await self.client.post("/api/test", json=self.payload())
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(len(self.requests), 5)
        self.assertEqual(len(data["results"]), 5)
        self.assertEqual(data["summary"]["total"], 5)
        self.assertEqual(data["summary"]["complete_count"], 5)
        self.assertEqual(data["summary"]["reasoning_expected"], 4)
        self.assertEqual(data["summary"]["reasoning_complete_count"], 4)
        self.assertNotIn("candidate", data)
        self.assertNotIn("reference", data)
        self.assertNotIn(API_KEY, response.text)
        self.assertNotIn("api_key", data["endpoint"])
        for request in self.requests:
            self.assertIs(json.loads(request.content)["stream"], False)
            self.assertEqual(request.headers["Authorization"], f"Bearer {API_KEY}")

    async def test_channel_failure_does_not_abort_later_questions(self):
        calls = []

        def upstream(request):
            calls.append(request)
            return httpx.Response(503, text="Unavailable") if len(calls) < 5 else httpx.Response(200, json=openai_response())

        app_module.app.state.upstream_transport = httpx.MockTransport(upstream)
        response = await self.client.post("/api/test", json=self.payload())
        self.assertEqual(response.status_code, 200, response.text)
        summary = response.json()["summary"]
        self.assertEqual(len(calls), 5)
        self.assertEqual(summary["error_count"], 4)
        self.assertEqual(summary["complete_rate"], 0.2)

    async def test_reports_redact_echoed_keys_in_success_and_error_bodies(self):
        for status in (200, 401):
            with self.subTest(status=status):
                body = openai_response(answer=API_KEY, reasoning=API_KEY) if status == 200 else {"error": API_KEY}
                app_module.app.state.upstream_transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
                response = await self.client.post("/api/test", json=self.payload())
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(API_KEY, response.text)

    async def test_unicode_escaped_keys_are_redacted_after_json_decoding(self):
        for status in (200, 401):
            with self.subTest(status=status):
                body = openai_response(answer=API_KEY, reasoning=API_KEY) if status == 200 else {"error": API_KEY}
                raw = json.dumps(body, indent=2).replace(API_KEY, "\\u0073" + API_KEY[1:])
                self.assertNotIn(API_KEY, raw)
                self.assertIn(API_KEY, json.dumps(json.loads(raw)))
                app_module.app.state.upstream_transport = httpx.MockTransport(
                    lambda request: httpx.Response(status, content=raw.encode("utf-8"))
                )
                response = await self.client.post("/api/test", json=self.payload())
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(API_KEY, response.text)
                results = response.json()["results"]
                self.assertEqual(len(results), 5)
                for result in results:
                    decoded_raw = json.loads(result["raw_response"])
                    self.assertNotIn(API_KEY, json.dumps(decoded_raw))
                    self.assertIn("[REDACTED]", result["raw_response"])
                    self.assertEqual(result["completion"]["status"], "complete" if status == 200 else "error")

    async def test_invalid_budget_rejected_before_network_without_echoing_key(self):
        payload = self.payload()
        payload["endpoint"]["protocol"] = "anthropic"
        payload["endpoint"]["thinking_budget_tokens"] = 8192
        response = await self.client.post("/api/test", json=payload)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.requests, [])
        self.assertNotIn(API_KEY, response.text)

    async def test_unserializable_upstream_json_preserves_all_five_errors(self):
        numeric_body = openai_response()
        numeric_body["usage"]["invalid_number"] = "NUMBER_PLACEHOLDER"
        numeric_template = json.dumps(numeric_body)
        cases = {
            value: numeric_template.replace('"NUMBER_PLACEHOLDER"', value).encode("utf-8")
            for value in ("NaN", "Infinity", "1e999")
        }
        cases["lone_surrogate"] = json.dumps(openai_response(answer="\ud800")).encode("utf-8")
        cases["deep_nesting"] = b'{"nested":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
        nested_usage = 0
        for _ in range(100):
            nested_usage = {"nested": nested_usage}
        usage_body = openai_response()
        usage_body["usage"] = nested_usage
        cases["usage_100_levels"] = json.dumps(usage_body).encode("utf-8")

        for name, raw in cases.items():
            with self.subTest(response=name):
                calls = []

                def upstream(request):
                    calls.append(request)
                    return httpx.Response(200, content=raw)

                app_module.app.state.upstream_transport = httpx.MockTransport(upstream)
                response = await self.client.post("/api/test", json=self.payload())
                self.assertEqual(response.status_code, 200, response.text)
                report = response.json()
                self.assertEqual(len(calls), 5)
                self.assertEqual(len(report["results"]), 5)
                self.assertEqual(report["summary"]["error_count"], 5)
                self.assertEqual(report["summary"]["complete_count"], 0)
                for result in report["results"]:
                    self.assertEqual(result["completion"]["status"], "error")
                    self.assertTrue(result["error"])
                    self.assertEqual(result["raw_response"].encode("utf-8"), raw)

    async def test_short_keys_do_not_change_report_schema_or_statuses(self):
        report_fields = {"endpoint", "settings", "summary", "results", "notes"}
        result_fields = set(app_module.TestResult.model_fields)
        for key in ("e", "complete"):
            with self.subTest(key=key):
                payload = self.payload()
                payload["endpoint"]["api_key"] = key
                body = openai_response(answer=key, reasoning=key)
                body["usage"] = {key: {"echo": key}}
                app_module.app.state.upstream_transport = httpx.MockTransport(
                    lambda request: httpx.Response(200, json=body)
                )
                response = await self.client.post("/api/test", json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                report = response.json()
                self.assertEqual(set(report), report_fields)
                self.assertEqual(report["summary"]["complete_count"], 5)
                self.assertEqual(report["summary"]["reasoning_complete_count"], 4)
                for result in report["results"]:
                    self.assertEqual(set(result), result_fields)
                    self.assertEqual(result["completion"]["status"], "complete")
                    self.assertEqual(result["reasoning"]["status"], "returned")
                    self.assertEqual(result["answer"], "[REDACTED]")
                    self.assertEqual(result["reasoning_text"], "[REDACTED]")
                    self.assertNotIn(key, result["raw_response"])
                    self.assertNotIn(key, json.dumps(result["usage"]))
                    self.assertIn("[REDACTED]", result["usage"])

    async def test_ipvfuture_url_is_rejected_before_network(self):
        payload = self.payload()
        payload["endpoint"]["base_url"] = "http://[v1.test]"
        response = await self.client.post("/api/test", json=payload)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.requests, [])
        self.assertNotIn(API_KEY, response.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
