"""Independent synthetic fixtures for request fidelity, no business transcripts."""
import asyncio
from copy import deepcopy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx

from features.protocol_admission.catalog import probes
from features.protocol_admission.engine import http_error
from features.protocol_admission.fidelity import run_probe, usage_metrics, summarize, SHORT, WORK_PATH
from features.protocol_admission.models import PlanInput, StartInput
from features.protocol_admission.report import evaluate, html_report
from features.protocol_admission.service import Manager
from features.protocol_admission.storage import Store
from shared.registry import Registry


def plan(**kwargs):
    return PlanInput(models=[{"model": "synthetic-model"}], suite="fidelity", **kwargs)


def response(content=None, **overrides):
    return {"type": "message", "role": "assistant", "model": "synthetic-model", "stop_reason": "end_turn",
            "content": content or [{"type": "text", "text": "READY"}],
            "usage": {"input_tokens": 10, "output_tokens": 20, "output_tokens_details": {"thinking_tokens": 3}}, **overrides}


class FidelityTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, check, responder, memory=None, config=None):
        p = config or plan()
        probe = next(v for v in probes(p) if v.check == check)
        return await run_probe(probe, "https://synthetic.invalid", "synthetic-secret", p, "synthetic-run",
                               httpx.MockTransport(responder), memory if memory is not None else {})

    def test_plan_and_missing_usage(self):
        self.assertEqual(len(probes(plan())), 13)
        self.assertEqual(len(probes(PlanInput(models=[{"model": "synthetic"}]))), 9)
        with self.assertRaises(ValueError):
            plan(reference_channel_id=1)
        empty = usage_metrics({})
        self.assertIsNone(empty["cache_creation_input_tokens"])
        self.assertIsNone(empty["nonthinking_output_tokens"])
        self.assertEqual(usage_metrics({"cache_read_input_tokens": 0})["cache_read_input_tokens"], 0)
        self.assertTrue(usage_metrics({"output_tokens": 4, "output_tokens_details": {"thinking_tokens": 5}})["usage_inconsistent"])

    def test_classify_400_without_retaining_message(self):
        cases = [("thinking blocks cannot be modified", "thinking_block_modified"),
                 ("Invalid signature in thinking block", "thinking_signature_invalid"),
                 ("thinking block is bound to a different conversation", "thinking_prefix_mismatch"),
                 ("adaptive thinking is not supported", "thinking_config_unsupported")]
        for message, expected in cases:
            self.assertEqual(http_error(400, json.dumps({"error": {"message": message}}).encode()), expected)
        self.assertEqual(http_error(400, b'{"error":{"message":5}}'), "request_invalid")
        self.assertEqual(http_error(500, b'{"error":{"message":"Invalid thinking signature"}}'), "upstream_error")
        for message in ("Invalid request signature", "Invalid webhook signature"):
            self.assertEqual(http_error(400, json.dumps({"error": {"message": message}}).encode()), "request_invalid")

    def test_malformed_thinking_usage_is_not_missing(self):
        for value in (-1, True, "3", None):
            self.assertTrue(usage_metrics({"output_tokens": 9, "output_tokens_details": {"thinking_tokens": value}})["usage_inconsistent"])

    async def test_invalid_blocks_and_error_cannot_become_success(self):
        bad = [response(role="user"), response(error={"message": "synthetic failure"}),
               response([{"type": "output_text", "text": "SYSTEM_OK"}])]
        for value in (None, 3):
            bad.append(response([{"type": "thinking", "thinking": value, "signature": "opaque"}, {"type": "text", "text": "READY"}]))
        bad.append(response([{"type": "thinking", "signature": "opaque"}, {"type": "text", "text": "READY"}]))
        for body in bad:
            memory = {}
            result = await self.call("f_thinking_seed", lambda _: httpx.Response(200, json=body), memory)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(memory, {})

    async def test_mismatched_model_seed_cannot_send_followup(self):
        memory, calls = {}, []
        def handler(req):
            calls.append(req)
            return httpx.Response(200, json=response([{"type": "thinking", "thinking": "", "signature": "opaque"},
                                                      {"type": "text", "text": "READY"}], model="other-model"))
        result = await self.call("f_thinking_seed", handler, memory)
        self.assertEqual(result["error_class"], "model_mapping_unconfirmed")
        result = await self.call("f_thinking_replay", handler, memory)
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(len(calls), 1)

    async def test_model_mismatch_preserves_observed_failures(self):
        cases = [("f_limit", response(model="other", usage={"input_tokens": 10, "output_tokens": 100}), "output_limit_exceeded"),
                 ("f_tools_none", response([{"type": "tool_use", "id": "t", "name": "fidelity_probe", "input": {"path": WORK_PATH}}], model="other", stop_reason="tool_use"), "unexpected_tool_use"),
                 ("f_environment", response([{"type": "tool_use", "id": "t", "name": "fidelity_probe", "input": {"path": "/synthetic/other"}}], model="other", stop_reason="tool_use"), "environment_deviation")]
        for check, body, error in cases:
            result = await self.call(check, lambda _: httpx.Response(200, json=body))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error_class"], error)
            self.assertEqual(result["model_match_status"], "mismatch")

    async def test_malformed_fields_do_not_abort_batch(self):
        from pathlib import Path
        from features.protocol_admission.fidelity import mock_response
        for bad in (response(stop_reason={}), response([{"type": [], "text": "READY"}])):
            calls = []
            def responder(req):
                calls.append(req)
                return httpx.Response(200, json=bad) if len(calls) == 1 else mock_response(req)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manager = Manager(Store(root / "reports"), Registry(root / "registry"))
                with patch("features.protocol_admission.fidelity.mock_response", responder):
                    p = plan()
                    preview = manager.preview(p)
                    started = await manager.start(StartInput(**p.model_dump(), preview_fingerprint=preview["fingerprint"]))
                    await manager.task
                report = manager.report(started["id"])
                self.assertEqual(report["state"], "completed")
                self.assertEqual(report["probes"][0]["status"], "failed")
                self.assertEqual(report["probes"][1]["status"], "passed")
                self.assertEqual(len(calls), 13)

    async def test_replay_preserves_all_blocks_and_prefix(self):
        content = [{"type": "thinking", "thinking": "", "signature": "opaque-synthetic"},
                   {"type": "redacted_thinking", "data": "opaque-redacted"}, {"type": "text", "text": "READY"}]
        requests, memory = [], {}
        def handler(req):
            requests.append(json.loads(req.content))
            return httpx.Response(200, json=response(deepcopy(content)))
        seed = await self.call("f_thinking_seed", handler, memory)
        follow = await self.call("f_thinking_replay", handler, memory)
        self.assertEqual((seed["status"], follow["status"]), ("passed", "passed"))
        self.assertEqual(requests[1]["messages"][-2]["content"], content)
        self.assertEqual(requests[1]["messages"][:-2], requests[0]["messages"])
        self.assertEqual(requests[1]["system"], requests[0]["system"])
        self.assertTrue(follow["fidelity"]["replay_exact"])
        self.assertNotIn("opaque-synthetic", json.dumps(follow))
        self.assertNotIn("opaque-redacted", json.dumps(follow))

    async def test_dependency_not_sent_and_missing_thinking_not_failure(self):
        count = 0
        def handler(req):
            nonlocal count
            count += 1
            return httpx.Response(200, json=response())
        memory = {}
        seed = await self.call("f_thinking_seed", handler, memory)
        follow = await self.call("f_thinking_replay", handler, memory)
        self.assertEqual(seed["error_class"], "thinking_not_observed")
        self.assertEqual(follow["attempts"], 0)
        self.assertEqual(count, 1)

    async def test_tool_round_trip_and_400_not_retried(self):
        content = [{"type": "thinking", "thinking": "Synthetic", "signature": "opaque"},
                   {"type": "tool_use", "id": "tool-fixture", "name": "fidelity_probe", "input": {"path": WORK_PATH}}]
        requests, memory = [], {}
        def handler(req):
            body = json.loads(req.content); requests.append(body)
            if len(requests) == 1:
                self.assertEqual(body["tool_choice"], {"type": "auto"})
                return httpx.Response(200, json=response(content, stop_reason="tool_use"))
            self.assertEqual(body["messages"][-1]["content"][0]["tool_use_id"], "tool-fixture")
            self.assertEqual(body["messages"][-2]["content"], content)
            return httpx.Response(400, json={"error": {"message": "thinking blocks cannot be modified synthetic-secret"}})
        await self.call("f_tool_seed", handler, memory)
        result = await self.call("f_tool_replay", handler, memory)
        self.assertEqual(result["error_class"], "thinking_block_modified")
        self.assertEqual(len(requests), 2)
        self.assertFalse(result["fidelity"]["downgrade_used"])
        self.assertNotIn("synthetic-secret", json.dumps(result))

    async def test_real_local_socket_roundtrip(self):
        requests, memory = [], {}
        blocks = [{"type": "thinking", "thinking": "", "signature": "socket-synthetic"}, {"type": "text", "text": "READY"}]
        async def serve(reader, writer):
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n") if line.lower().startswith(b"content-length:"))
                requests.append(json.loads(await reader.readexactly(length)))
                payload = json.dumps(response(deepcopy(blocks))).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
                await writer.drain()
            finally:
                writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        try:
            base = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
            p = plan()
            with patch.dict(os.environ, {"PLATFORM_EGRESS_ALLOWLIST": "127.0.0.1"}):
                for check in ("f_thinking_seed", "f_thinking_replay"):
                    probe = next(v for v in probes(p) if v.check == check)
                    r = await run_probe(probe, base, "synthetic-socket-key", p, "socket-run", None, memory)
                    self.assertEqual(r["status"], "passed")
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[1]["messages"][-2]["content"], blocks)
        finally:
            server.close(); await server.wait_closed()

    def test_cache_denominators_exclude_incomplete_usage(self):
        rows = []
        for check, read, write in [("f_cache_cold", 0, 90), ("f_cache_warm", 90, 0), ("f_cache_changed", 0, None)]:
            rows.append({"check": check, "model": "synthetic", "status": "passed", "fidelity": {
                "input_tokens": 10, "cache_read_input_tokens": read, "cache_creation_input_tokens": write}})
        result = summarize({"config": {}, "probes": rows})["cache"][0]
        self.assertEqual(result["complete_usage_samples"], 2)
        self.assertEqual(result["token_hit_rate"], .45)
        self.assertEqual(result["request_reuse_rate"], .5)
        self.assertEqual(result["write_ratio"], .45)

    async def test_exact_echo_limit_disabled_tool_and_path(self):
        cases = [("f_echo_short", response([{ "type": "text", "text": SHORT}]), ""),
                 ("f_echo_short", response([{ "type": "text", "text": SHORT + "\n"}]), "instruction_deviation"),
                 ("f_limit", response(usage={"input_tokens": 1, "output_tokens": 65}), "output_limit_exceeded"),
                 ("f_tools_none", response([{ "type": "tool_use", "id": "x", "name": "fidelity_probe", "input": {}}], stop_reason="tool_use"), "unexpected_tool_use"),
                 ("f_environment", response([{ "type": "tool_use", "id": "x", "name": "fidelity_probe", "input": {"path": "/other/synthetic.py"}}], stop_reason="tool_use"), "environment_deviation")]
        for check, body, error in cases:
            result = await self.call(check, lambda _: httpx.Response(200, json=body))
            self.assertEqual(result["error_class"], error)

    async def test_cache_prefix_fixed_then_changed(self):
        requests = []
        def handler(req):
            requests.append(json.loads(req.content))
            return httpx.Response(200, json=response())
        for check in ("f_cache_cold", "f_cache_warm", "f_cache_changed"):
            result = await self.call(check, handler)
            self.assertEqual(result["error_class"], "cache_usage_missing")
        self.assertEqual(requests[0], requests[1])
        self.assertNotEqual(requests[1]["system"], requests[2]["system"])

    def test_comparison_requires_exact_successful_same_text_and_usage(self):
        a = {"check": "f_echo_short", "model": "synthetic", "channel_id": 1, "status": "passed",
             "fidelity": {"exact_match": True, "text_sha256": "same", "nonthinking_output_tokens": 100}}
        b = deepcopy(a); b["channel_id"] = 2; b["fidelity"]["nonthinking_output_tokens"] = 200
        report = {"config": {"reference_channel_id": 1}, "probes": [a, b]}
        self.assertEqual(summarize(report)["comparisons"][0]["ratio"], 2)
        b["fidelity"]["exact_match"] = False
        self.assertIsNone(summarize(report)["comparisons"][0]["ratio"])
        b["fidelity"]["exact_match"] = True; b["fidelity"]["nonthinking_output_tokens"] = None
        self.assertIsNone(summarize(report)["comparisons"][0]["ratio"])
        report["config"]["reference_channel_id"] = None
        self.assertEqual(len(summarize(report)["comparisons"]), 2)

    async def test_manager_mock_history_and_export_no_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            root = Path(tmp)
            manager = Manager(Store(root / "reports"), Registry(root / "registry"))
            p = plan()
            preview = manager.preview(p)
            self.assertEqual(preview["request_count"], 13)
            start = StartInput(**p.model_dump(), preview_fingerprint=preview["fingerprint"])
            result = await manager.start(start)
            await manager.task
            report = manager.report(result["id"])
            self.assertEqual(report["state"], "completed")
            self.assertTrue(all(row["status"] == "passed" for row in report["probes"]))
            self.assertEqual(report["fidelity_summary"]["replay_passed"], 2)
            self.assertEqual(report["fidelity_summary"]["replay_400_rate"], 0)
            serialized = json.dumps(report)
            self.assertNotIn("synthetic-signature", serialized)
            self.assertNotIn(SHORT, serialized)
            self.assertIn("fidelity_summary", html_report(report))
            self.assertIn("Token 命中率", html_report(report))
            self.assertNotIn("支持表示本次至少一种普通调用方式通过", html_report(report))
