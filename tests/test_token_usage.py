"""Independent synthetic paired-usage expectations, with real loopback HTTP."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

import httpx

from features.token_usage.domain import MAX_REPORTED_TOKENS, classify_pair, condition_hash, normalize_usage, observation
from features.token_usage.transport import SendControl, run_paired_check


def valid(value):
    return observation([normalize_usage({"input_tokens": value}, "responses")], status="completed", terminal=True)


class DomainTests(unittest.TestCase):
    def test_major_both_boundaries_and_single_minor(self):
        self.assertEqual(classify_pair(valid(2048), valid(1024))["conclusion"], "high_risk")
        self.assertEqual(classify_pair(valid(2000), valid(1000))["conclusion"], "pending_confirmation")
        self.assertEqual(classify_pair(valid(2200), valid(2000))["conclusion"], "pending_confirmation")
        self.assertEqual(classify_pair(valid(1408), valid(1280))["conclusion"], "pending_confirmation")
        self.assertEqual(classify_pair(valid(2127), valid(2000))["conclusion"], "normal")

    def test_minor_decimal_boundary_comparison_is_exact(self):
        self.assertEqual(classify_pair(valid(2816), valid(2560))["conclusion"], "pending_confirmation")
        self.assertEqual(classify_pair(valid(2815), valid(2560))["conclusion"], "normal")
        configured = {"minor_ratio": 1.17, "minor_difference": 128}
        self.assertEqual(classify_pair(valid(14976), valid(12800), threshold_config=configured)["conclusion"], "pending_confirmation")
        self.assertEqual(classify_pair(valid(14975), valid(12800), threshold_config=configured)["conclusion"], "normal")
        # A displayed float ratio can round to the threshold while integer evidence remains below it.
        self.assertEqual(classify_pair(valid(1100000000000000000 - 1), valid(1000000000000000000))["conclusion"], "normal")

    def test_cache_normalization_and_missing_semantics(self):
        self.assertEqual(normalize_usage({"prompt_tokens": 5000, "prompt_tokens_details": {"cached_tokens": 4500}}, "openai")["normalized_input_tokens"], 5000)
        self.assertEqual(normalize_usage({"input_tokens": 5000, "input_tokens_details": {"cached_tokens": 4500}}, "responses")["normalized_input_tokens"], 5000)
        value = normalize_usage({"input_tokens": 100, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 4700}, "messages")
        self.assertEqual(value["normalized_input_tokens"], 5000)
        self.assertIsNone(normalize_usage({}, "openai"))
        self.assertIsNone(normalize_usage({"input_tokens": 100, "cache_read_input_tokens": 200}, "messages")["normalized_input_tokens"])
        self.assertIsNone(normalize_usage({"input_tokens": True}, "responses")["normalized_input_tokens"])
        self.assertIsNone(normalize_usage({"prompt_tokens": 20, "prompt_tokens_details": {"cached_tokens": 21}}, "chat")["normalized_input_tokens"])

    def test_reported_count_upper_bound_and_cache_sum_are_invalid_without_clamping(self):
        limit = MAX_REPORTED_TOKENS
        self.assertTrue(valid(limit)["valid"])
        for count in (limit + 1, 10**400):
            value = normalize_usage({"input_tokens": count}, "responses")
            self.assertTrue(value["invalid"])
            self.assertIsNone(value["input_tokens"])
            self.assertIsNone(value["normalized_input_tokens"])
            self.assertEqual(classify_pair(valid(count), valid(2000))["reason"], "candidate_usage_invalid")
        exact = normalize_usage({"input_tokens": limit - 2, "cache_creation_input_tokens": 1, "cache_read_input_tokens": 1}, "messages")
        self.assertEqual(exact["normalized_input_tokens"], limit)
        self.assertFalse(exact["invalid"])
        for value in ({"input_tokens": limit, "cache_creation_input_tokens": 1, "cache_read_input_tokens": 0},
                      {"input_tokens": 1, "cache_creation_input_tokens": limit + 1, "cache_read_input_tokens": 0},
                      {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": limit + 1}):
            row = normalize_usage(value, "messages")
            self.assertTrue(row["invalid"])
            self.assertIsNone(row["normalized_input_tokens"])
        cached = normalize_usage({"prompt_tokens": limit, "prompt_tokens_details": {"cached_tokens": limit + 1}}, "openai")
        self.assertTrue(cached["invalid"])
        restored = {**valid(2000), "normalized_input_tokens": 10**400}
        self.assertEqual(classify_pair(restored, valid(2000))["conclusion"], "unable_to_determine")

    def test_invalid_zero_missing_and_first_final_conflict(self):
        for value in (valid(0), observation([], status="completed", terminal=True),
                      observation([normalize_usage({"input_tokens": -1}, "responses")], status="completed", terminal=True)):
            self.assertEqual(classify_pair(value, valid(1000))["conclusion"], "unable_to_determine")
        traced = observation([normalize_usage({"input_tokens": 1000}, "responses"), normalize_usage({"input_tokens": 2000}, "responses")], status="completed", terminal=True)
        self.assertTrue(traced["usage_conflict"])
        self.assertEqual(traced["first_usage"]["input_tokens"], 1000)
        self.assertEqual(traced["final_usage"]["input_tokens"], 2000)
        self.assertEqual(classify_pair(traced, valid(1000))["conclusion"], "unable_to_determine")

    def test_only_successive_scheduled_minor_and_log_evidence_separate(self):
        first = classify_pair(valid(2300), valid(2000), source="scheduled", frozen_hash="f", occurrence=1800)
        second = classify_pair(valid(2300), valid(2000), previous=first, source="scheduled", frozen_hash="f", occurrence=3600)
        self.assertEqual(second["conclusion"], "minor_anomaly")
        for source, frozen, occurrence in (("fixed_retest", "f", 3600), ("scheduled", "changed", 3600), ("scheduled", "f", 5400)):
            self.assertEqual(classify_pair(valid(2300), valid(2000), previous=first, source=source, frozen_hash=frozen, occurrence=occurrence)["conclusion"], "pending_confirmation")
        retest = classify_pair(valid(2000), valid(2000), log_evidence={"event_id": "synthetic-event"})
        self.assertEqual(retest["conclusion"], "not_reproduced")
        self.assertEqual(retest["billing_status"], "unverified")

    def test_configurable_minor_is_frozen_and_usage_change_reason_is_explicit(self):
        configured = {"minor_ratio": 1.25, "minor_difference": 200}
        first = classify_pair(valid(2300), valid(2000), threshold_config=configured)
        self.assertEqual(first["conclusion"], "normal")
        self.assertNotEqual(first["condition_hash"], condition_hash())
        traced = observation([normalize_usage({"input_tokens": 9000}, "responses"), normalize_usage({"input_tokens": 126000}, "responses")], status="completed", terminal=True)
        result = classify_pair(traced, valid(9000))
        self.assertEqual(result["conclusion"], "unable_to_determine")
        self.assertEqual(result["reason"], "input_usage_changed_within_request")
        self.assertIn("同一请求", result["label"])
        self.assertEqual(traced["input_usage_delta"], 117000)


class Loopback:
    def __init__(self, counts=None, *, streaming=False, conflict=False):
        self.counts = counts or [2000, 2000]
        self.requests = []
        self.streaming, self.conflict = streaming, conflict
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["content-length"])))
                fixture.requests.append({"path": self.path, "input": data.get("input") or data.get("messages"), "model": data["model"]})
                if isinstance(fixture.counts, dict):
                    count = fixture.counts[self.headers.get("Authorization", "")]
                else:
                    count = fixture.counts[min(len(fixture.requests) - 1, len(fixture.counts) - 1)]
                if self.path.endswith("/responses"):
                    payload = {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}], "usage": {"input_tokens": count, "output_tokens": 1}}
                    events = [{"type": "response.created", "response": {"status": "in_progress", "usage": {"input_tokens": count - 1 if fixture.conflict else count, "output_tokens": 0}}}, {"type": "response.completed", "response": payload}]
                elif self.path.endswith("/messages"):
                    payload = {"content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn", "usage": {"input_tokens": count - 100, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 50, "output_tokens": 1}}
                    events = [{"type": "message_start", "message": {"usage": payload["usage"]}}, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}}, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}, {"type": "message_stop"}]
                else:
                    payload = {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": count, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 500}}}
                    events = [{"choices": [{"delta": {"content": "OK"}, "finish_reason": None}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": payload["usage"]}]
                raw = ("\n\n".join("data: " + json.dumps(e) for e in events) + ("\n\ndata: [DONE]" if self.path.endswith("/chat/completions") else "") + "\n\n").encode() if fixture.streaming else json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if fixture.streaming else "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_guard_exception_is_never_published_as_upstream_evidence(self):
        calls=[]
        async def upstream(request):
            calls.append(1)
            return httpx.Response(200)
        async def denied(side,index):
            raise ValueError("synthetic_guard_rejection")
        endpoint={"base_url":"http://127.0.0.1","api_key":"fixture_key","model":"synthetic-model","protocol":"openai"}
        with patch("features.stability.app.egress.EGRESS_ALLOWLIST", ("127.0.0.1",)):
            with self.assertRaises(SendControl):
                await run_paired_check(endpoint,endpoint,upstream_transport=httpx.MockTransport(upstream),before_send=denied)
        self.assertEqual(calls,[])

    async def test_actual_http_all_protocols_cache_and_cumulative_not_summed(self):
        for wire in ("openai", "responses", "anthropic"):
            fixture = Loopback(streaming=True)
            try:
                endpoint = {"base_url": fixture.url, "api_key": "fixture_key", "model": "synthetic-model", "protocol": wire}
                with patch("features.stability.app.egress.EGRESS_ALLOWLIST", ("127.0.0.1",)), patch.dict(os.environ, {"PLATFORM_EGRESS_ALLOWLIST": "127.0.0.1"}):
                    result = await run_paired_check(endpoint, endpoint)
                self.assertEqual(result["conclusion"], "normal", result)
                self.assertEqual(result["candidate_input_tokens"], 2000)
                self.assertEqual(result["requests_sent"], 2)
                self.assertEqual(fixture.requests[0]["input"], fixture.requests[1]["input"])
                self.assertNotIn("fixture_key", json.dumps(result))
                self.assertNotIn("record-0000", json.dumps(result))
            finally:
                fixture.close()

    async def test_early_major_and_healthy_verification_cannot_erase(self):
        calls, published = [], []
        async def upstream(request):
            calls.append(request.headers["authorization"])
            side = "candidate" if request.headers["authorization"] == "Bearer fixture_candidate" else "reference"
            count = 4096 if side == "candidate" and len(calls) <= 2 else 2048
            if len(calls) > 2:
                self.assertEqual(published[0]["conclusion"], "high_risk")
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": count}})
        async def publish(result):
            published.append(json.loads(json.dumps(result)))
        endpoint = {"base_url": "http://127.0.0.1", "api_key": "fixture_reference", "model": "synthetic-model", "protocol": "openai"}
        with patch("features.stability.app.egress.EGRESS_ALLOWLIST", ("127.0.0.1",)):
            result = await run_paired_check({**endpoint, "api_key": "fixture_candidate"}, endpoint, upstream_transport=httpx.MockTransport(upstream), on_result=publish)
        self.assertEqual(len(calls), 4)
        self.assertEqual(result["conclusion"], "high_risk")
        self.assertEqual(result["verification_status"], "normal")
        self.assertEqual(len(result["pairs"]), 2)

    async def test_mismatch_zero_send_and_unknown_is_not_replayed(self):
        calls = []
        async def upstream(request):
            calls.append(1)
            return httpx.Response(503)
        endpoint = {"base_url": "http://127.0.0.1", "api_key": "fixture_key", "model": "synthetic-model", "protocol": "openai"}
        transport = httpx.MockTransport(upstream)
        result = await run_paired_check(endpoint, {**endpoint, "protocol": "responses"}, upstream_transport=transport)
        self.assertEqual(result["reason"], "conditions_mismatch")
        self.assertEqual(calls, [])
        astra = {**endpoint, "protocol": "responses", "canonical_model": "gpt-6-astra", "model": "synthetic-astra-alias"}
        result = await run_paired_check(astra, {**astra, "model": "synthetic-other-alias"}, upstream_transport=transport)
        self.assertEqual(result["reason"], "conditions_mismatch")
        self.assertEqual(calls, [])
        with patch("features.stability.app.egress.EGRESS_ALLOWLIST", ("127.0.0.1",)):
            result = await run_paired_check(endpoint, endpoint, upstream_transport=transport, resume_attempts={"token:candidate:0": observation([], status="unknown")})
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["conclusion"], "unable_to_determine")

    async def test_stream_usage_conflict_is_retained(self):
        fixture = Loopback(streaming=True, conflict=True)
        try:
            endpoint = {"base_url": fixture.url, "api_key": "fixture_key", "model": "synthetic-model", "protocol": "responses"}
            with patch("features.stability.app.egress.EGRESS_ALLOWLIST", ("127.0.0.1",)), patch.dict(os.environ, {"PLATFORM_EGRESS_ALLOWLIST": "127.0.0.1"}):
                result = await run_paired_check(endpoint, endpoint)
            self.assertEqual(result["conclusion"], "unable_to_determine")
            self.assertTrue(result["pairs"][0]["candidate"]["usage_conflict"])
        finally:
            fixture.close()
