"""Bounded synthetic regressions for the v1.8.4 acceptance edge cases."""
from __future__ import annotations

import asyncio
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tracemalloc
import unittest
import zlib
from unittest.mock import patch

import httpx

from features.model_coverage.catalog import Catalog
from features.token_usage.domain import classify_pair, normalize_usage, observation
from features.token_usage.service import TokenUsageService
from features.token_usage.transport import MAX_RESPONSE_BYTES, _attempt, headers
from shared.registry import Registry, RegistryError


def sse(events):
    return b"".join(b"data: " + (value.encode() if isinstance(value, str) else json.dumps(value).encode()) + b"\n\n" for value in events)


def chat_events():
    return [{"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2048}}, "[DONE]"]


def message_events(reason="end_turn"):
    return [{"type": "message_start", "message": {"usage": {"input_tokens": 2048}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
            {"type": "message_delta", "delta": {"stop_reason": reason}}, {"type": "message_stop"}]


def response_events():
    return [{"type": "response.created", "response": {"status": "in_progress"}},
            {"type": "response.completed", "response": {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}], "usage": {"input_tokens": 2048}}}]


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, hold=False):
        self.chunks, self.hold = chunks, hold
        self.consumed = 0
        self.closed = False
        self.entered = asyncio.Event()

    async def __aiter__(self):
        for chunk in self.chunks:
            self.consumed += len(chunk)
            yield chunk
        self.entered.set()
        if self.hold:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class StreamRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def attempt(self, stream, kind="openai", extra_headers=None):
        async def upstream(request):
            self.assertEqual(request.headers["accept-encoding"], "identity")
            return httpx.Response(200, headers={"content-type": "text/event-stream", **(extra_headers or {})}, stream=stream)
        endpoint = {"base_url": "http://127.0.0.1", "api_key": "synthetic-only", "model": "synthetic-model", "protocol": kind}
        self.assertEqual(headers(endpoint)["Accept-Encoding"], "identity")
        with patch("features.stability.app.egress.EGRESS_ALLOWLIST", ("127.0.0.1",)):
            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
                return await _attempt(client, endpoint, None)

    async def test_unterminated_line_stops_reading_at_limit_and_closes(self):
        stream = Chunks([b"x" * 8192] * (MAX_RESPONSE_BYTES // 8192 * 4))
        result = await self.attempt(stream)
        self.assertEqual(result["status"], "invalid_response")
        self.assertLessEqual(stream.consumed, MAX_RESPONSE_BYTES + 8192)
        self.assertTrue(stream.closed)

    async def test_single_large_event_and_gzip_expansion_are_bounded(self):
        raw = sse([{"choices": [{"delta": {"content": "x" * (MAX_RESPONSE_BYTES + 1024)}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2048}}, "[DONE]"])
        for body, headers in ((raw, {}), (gzip.compress(raw), {"content-encoding": "gzip"})):
            stream = Chunks([body[i:i + 127] for i in range(0, len(body), 127)])
            result = await self.attempt(stream, extra_headers=headers)
            self.assertEqual(result["status"], "invalid_response")
            self.assertTrue(stream.closed)

    async def test_single_raw_gzip_bomb_never_allocates_expanded_body(self):
        compressed = gzip.compress(b"x" * (32 * MAX_RESPONSE_BYTES))
        stream = Chunks([compressed])
        tracemalloc.start()
        try:
            result = await self.attempt(stream, extra_headers={"content-encoding": "gzip"})
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result["status"], "invalid_response")
        self.assertTrue(stream.closed)
        self.assertLess(peak, 8 * MAX_RESPONSE_BYTES, f"expanded_response_peak_bytes={peak}")

    async def test_single_oversized_raw_identity_chunk_is_rejected_before_copy(self):
        stream = Chunks([b"x" * (32 * MAX_RESPONSE_BYTES)])
        tracemalloc.start()
        try:
            result = await self.attempt(stream)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result["status"], "invalid_response")
        self.assertTrue(stream.closed)
        self.assertLess(peak, 8 * MAX_RESPONSE_BYTES, f"raw_response_peak_bytes={peak}")

    async def test_normal_compressed_streams_close_and_invalid_coding_is_rejected(self):
        body = sse(chat_events())
        raw_deflate = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        for encoding, compressed in (("gzip", gzip.compress(body)), ("deflate", zlib.compress(body)),
                                     ("deflate", raw_deflate.compress(body) + raw_deflate.flush())):
            stream = Chunks([compressed[i:i + 1] for i in range(len(compressed))])
            result = await self.attempt(stream, extra_headers={"content-encoding": encoding})
            self.assertTrue(result["valid"], encoding)
            self.assertTrue(stream.closed)
        for encoding, raw in (("br", body), ("gzip", gzip.compress(body)[:-3]), ("gzip", gzip.compress(body) + b"trailing")):
            stream = Chunks([raw])
            result = await self.attempt(stream, extra_headers={"content-encoding": encoding})
            self.assertEqual(result["status"], "invalid_response")
            self.assertTrue(stream.closed)

    async def test_valid_fragmented_utf8_multiline_sse_and_all_protocols_close(self):
        events = chat_events()
        events[0]["choices"][0]["delta"]["content"] = "完成"
        raw = ("data: " + json.dumps(events[0], ensure_ascii=False, indent=2).replace("\n", "\ndata: ") + "\r\n\r\ndata: [DONE]\r\n\r\n").encode()
        for kind, body in (("openai", raw), ("responses", sse(response_events())), ("anthropic", sse(message_events()))):
            stream = Chunks([body[i:i + 3] for i in range(0, len(body), 3)])
            result = await self.attempt(stream, kind)
            self.assertTrue(result["valid"], (kind, result))
            self.assertTrue(stream.closed)

    async def test_messages_truncation_or_missing_reason_is_never_normal(self):
        for reason in ("max_tokens", "refusal", "", None):
            result = await self.attempt(Chunks([sse(message_events(reason))]), "anthropic")
            self.assertFalse(result["valid"], reason)
            healthy = observation([normalize_usage({"input_tokens": 2048}, "responses")], status="completed", terminal=True)
            self.assertEqual(classify_pair(result, healthy)["conclusion"], "unable_to_determine")

    async def test_protocol_finish_reasons_and_response_error_refusal_are_invalid(self):
        chat = chat_events()
        chat[0]["choices"][0]["finish_reason"] = "end_turn"
        completed_error = {**response_events()[-1]["response"], "error": {"code": "synthetic_error"}}
        completed_incomplete = response_events()
        completed_incomplete[-1]["response"]["incomplete_details"] = {"reason": "max_output_tokens"}
        refused = {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "synthetic refusal"}]}], "usage": {"input_tokens": 2048}}
        cases = [("openai", sse(chat), {}), ("anthropic", sse(message_events("stop")), {}),
                 ("responses", json.dumps(completed_error).encode(), {"content-type": "application/json"}),
                 ("responses", sse(completed_incomplete), {}),
                 ("responses", json.dumps(refused).encode(), {"content-type": "application/json"})]
        for index, (kind, raw, headers) in enumerate(cases):
            with self.subTest(kind=kind, case=index):
                result = await self.attempt(Chunks([raw]), kind, headers)
                self.assertFalse(result["valid"])

    async def test_normal_nonstream_and_messages_stop_sequence_still_complete(self):
        chat = {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2048}}
        message = {"content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn", "usage": {"input_tokens": 2048}}
        for kind, data in (("openai", chat), ("anthropic", message), ("responses", response_events()[-1]["response"])):
            result = await self.attempt(Chunks([json.dumps(data).encode()]), kind, {"content-type": "application/json"})
            self.assertTrue(result["valid"], kind)
        result = await self.attempt(Chunks([sse(message_events("stop_sequence"))]), "anthropic")
        self.assertTrue(result["valid"])

    async def test_messages_heartbeats_preserve_finish_and_post_terminal_stays_invalid(self):
        events = message_events()
        events.insert(0, {"type": "ping"})
        events.insert(3, {"type": "ping"})
        healthy = await self.attempt(Chunks([sse(events)]), "anthropic")
        self.assertTrue(healthy["valid"])
        invalid = await self.attempt(Chunks([sse(events + [{"type": "ping"}])]), "anthropic")
        self.assertFalse(invalid["valid"])

    async def test_decoded_byte_limit_accepts_exact_boundary_and_rejects_one_more(self):
        event = sse(chat_events())
        comment = b":" + b"x" * (MAX_RESPONSE_BYTES - len(event) - 2) + b"\n"
        raw = comment + event
        self.assertEqual(len(raw), MAX_RESPONSE_BYTES)
        for body, expected in ((raw, True), (raw + b"\n", False)):
            result = await self.attempt(Chunks([body[i:i + 8192] for i in range(0, len(body), 8192)]))
            self.assertEqual(result["valid"], expected)

    async def test_protocol_terminal_order_rejects_extra_content_and_duplicate_finish(self):
        cases = [("responses", response_events() + [{"type": "response.output_text.delta", "delta": "late"}]),
                 ("responses", response_events() + [response_events()[-1]]),
                 ("anthropic", message_events() + [{"type": "content_block_delta", "delta": {"text": "late"}}]),
                 ("anthropic", message_events() + [{"type": "message_stop"}]),
                 ("anthropic", message_events()[1:]),
                 ("openai", chat_events()[:-1] + [{"choices": [{"delta": {"content": "late"}, "finish_reason": None}]}, "[DONE]"]),
                 ("openai", chat_events()[:-1] + [chat_events()[0], "[DONE]"]),
                 ("openai", chat_events() + ["[DONE]"])]
        for kind, events in cases:
            with self.subTest(kind=kind, event_count=len(events)):
                result = await self.attempt(Chunks([sse(events)]), kind)
                self.assertFalse(result["valid"])

    async def test_cancellation_closes_live_stream(self):
        stream = Chunks([sse(chat_events()[:-1])], hold=True)
        task = asyncio.create_task(self.attempt(stream))
        await asyncio.wait_for(stream.entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stream.closed)


class DurableRegressionTests(unittest.TestCase):
    def setUp(self):
        root = os.environ.get("EVAL_TEST_ARTIFACT_ROOT")
        if not root:
            raise RuntimeError("external_test_artifact_root_required")
        self.temp = tempfile.TemporaryDirectory(prefix="token-remediation-", dir=root)
        self.registry = Registry(Path(self.temp.name) / "platform")
        Catalog(self.registry).add("synthetic-model", "Synthetic", "Synthetic", "openai")
        self.candidate, self.reference = self.registry.import_records([
            {"name": "Candidate frozen", "base_url": "http://127.0.0.1:1", "api_key": "synthetic_candidate"},
            {"name": "Reference frozen", "base_url": "http://127.0.0.1:1", "api_key": "synthetic_reference"}])["ids"]
        self.svc = TokenUsageService(self.registry, clock=lambda: 1801)
        self.svc.save_binding("synthetic-model", "openai", self.reference, confirm_trusted=True)

    def tearDown(self):
        self.temp.cleanup()

    def submit(self):
        return self.svc.submit(self.candidate, "synthetic-model", "openai", confirm_live=True, idempotency_key="synthetic-event")

    def test_expired_cancelled_orphan_terminates_once_without_replay(self):
        for major in (False, True):
            with self.subTest(major=major):
                job = self.svc.submit(self.candidate, "synthetic-model", "openai", confirm_live=True, idempotency_key=f"orphan-{major}")
                row = self.svc.store.claim("crashed", active=True)
                if major:
                    c = observation([normalize_usage({"prompt_tokens": 4096}, "openai")], status="completed", terminal=True)
                    r = observation([normalize_usage({"prompt_tokens": 2048}, "openai")], status="completed", terminal=True)
                    result = classify_pair(c, r)
                    result["requests_sent"] = 2
                    for side, value in (("candidate", c), ("reference", r)):
                        self.svc.store.permit(row["id"], "crashed", side, 0)
                        self.svc.store.attempt(row["id"], "crashed", side, 0, value)
                    self.svc.store.publish(row["id"], "crashed", result)
                self.svc.store.permit(row["id"], "crashed", "candidate", 1 if major else 0)
                self.svc.store.cancel(row["id"])
                with self.svc.store.connect() as db:
                    db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (row["id"],))
                self.assertIsNone(self.svc.store.claim("recovered", active=True))
                final = self.svc.store.get(job["job_id"])
                self.assertEqual(final["status"], "cancelled")
                self.assertTrue(any(a["state"] == "unknown" for a in final["attempts"]))
                self.assertEqual(final["result"]["requests_sent"], 3 if major else 1)
                self.assertEqual(final["result"]["new_requests_sent"], 0)
                self.assertEqual(final["result"]["conclusion"], "high_risk" if major else "unable_to_determine")
                if major:
                    self.assertEqual(final["result"]["verification_status"], "cancelled_unknown")
                events = [e for e in self.svc.store.events("workbench", 0, 100)["events"] if e["job_id"] == job["job_id"]]
                self.assertEqual(sum(e["kind"] == "result" for e in events), 1)
                self.svc.store.claim("repeated", active=True)
                self.assertEqual(len([e for e in self.svc.store.events("workbench", 0, 100)["events"] if e["job_id"] == job["job_id"]]), len(events))

    def test_pause_existing_configuration_ignores_connection_failure_keeps_spec(self):
        config = self.svc.save_config(self.candidate, "synthetic-model", "openai", enabled=True, confirm_live=True)
        with patch.object(self.svc, "_spec", side_effect=RegistryError("synthetic connection disabled")):
            paused = self.svc.save_config(self.candidate, "synthetic-model", "openai", config_id=config["id"], enabled=False)
            self.assertFalse(paused["enabled"])
            self.assertEqual(paused["candidate"], config["candidate"])
            self.assertEqual(paused["frozen_hash"], config["frozen_hash"])
            self.assertEqual(paused["next_due"], config["next_due"])
            with self.assertRaises(RegistryError):
                self.svc.save_config(self.candidate, "synthetic-model", "openai", config_id=config["id"], enabled=True, confirm_live=True)
            with self.assertRaises(RegistryError):
                self.svc.save_config(self.reference, "synthetic-model", "openai", config_id=config["id"], enabled=False)

    def test_pause_after_real_reference_disable_and_legacy_cancelled_queue_recovery(self):
        config = self.svc.save_config(self.candidate, "synthetic-model", "openai", enabled=True, confirm_live=True)
        channel = self.registry.get(self.reference, secret=True)
        self.registry.save({**channel, "enabled": False}, channel_id=self.reference, version=channel["version"])
        paused = self.svc.save_config(self.candidate, "synthetic-model", "openai", config_id=config["id"], enabled=False)
        self.assertFalse(paused["enabled"])
        with self.assertRaises(RegistryError):
            self.svc.save_config(self.candidate, "synthetic-model", "openai", config_id=config["id"], enabled=True, confirm_live=True)
        channel = self.registry.get(self.reference, secret=True)
        self.registry.save({**channel, "enabled": True}, channel_id=self.reference, version=channel["version"])
        job = self.submit()
        with self.svc.store.connect() as db:
            db.execute("UPDATE jobs SET cancelled=1 WHERE id=?", (job["job_id"],))
        self.assertIsNone(self.svc.store.claim("legacy-recovery", active=True))
        self.assertEqual(self.svc.store.get(job["job_id"])["status"], "cancelled")
        self.assertEqual(self.svc.store.events("workbench", 0, 100)["events"][0]["result"]["reason"], "cancelled_not_sent")

    def test_new_history_freezes_names_but_rename_does_not_change_send_identity(self):
        job = self.submit()
        spec = self.svc._spec(self.candidate, "synthetic-model", "openai", source="fixed_retest")
        for channel_id in (self.candidate, self.reference):
            channel = self.registry.get(channel_id, secret=True)
            self.registry.save({**channel, "name": "Renamed later"}, channel_id=channel_id, version=channel["version"])
        current = self.svc._spec(self.candidate, "synthetic-model", "openai", source="fixed_retest")
        self.assertEqual(current["frozen_hash"], spec["frozen_hash"])
        self.assertEqual(current["condition_hash"], spec["condition_hash"])
        self.svc._resolve_frozen(spec)
        history = self.svc.store.get(job["job_id"])
        self.assertEqual(history["display_identity"]["candidate"]["name"], "Candidate frozen")
        self.assertEqual(history["display_identity"]["reference"]["name"], "Reference frozen")
        self.assertEqual(self.submit()["job_id"], job["job_id"])

    def test_display_rename_does_not_stop_existing_schedule_permit(self):
        config = self.svc.save_config(self.candidate, "synthetic-model", "openai", enabled=True, confirm_live=True)
        channel = self.registry.get(self.candidate, secret=True)
        self.registry.save({**channel, "name": "Renamed later"}, channel_id=self.candidate, version=channel["version"])
        updated = self.svc.save_config(self.candidate, "synthetic-model", "openai", enabled=True, confirm_live=True, config_id=config["id"])
        self.assertEqual(updated["frozen_hash"], config["frozen_hash"])
        self.assertIsNone(self.svc.store.schedule_stop_reason(config["id"], {k: v for k, v in config.items() if k not in {"id", "enabled", "next_due", "interval_seconds"}}))

    def test_frozen_names_redact_both_credentials_and_json_escaped_forms(self):
        candidate_key, reference_key = 'synthetic_pair_\\"candidate', 'synthetic_pair_reference'
        for channel_id, key, name in ((self.candidate, candidate_key, f"Candidate {candidate_key} / {reference_key} / {json.dumps(candidate_key)[1:-1]}"),
                                      (self.reference, reference_key, f"Reference {reference_key} / {candidate_key} / {json.dumps(candidate_key)[1:-1]}")):
            channel = self.registry.get(channel_id, secret=True)
            self.registry.save({**channel, "name": name, "api_key": key}, channel_id=channel_id, version=channel["version"])
        self.svc.save_binding("synthetic-model", "openai", self.reference, confirm_trusted=True)
        preview = self.svc.preview(self.candidate, "synthetic-model", "openai")
        config = self.svc.save_config(self.candidate, "synthetic-model", "openai")
        job = self.svc.submit(self.candidate, "synthetic-model", "openai", confirm_live=True, idempotency_key="safe-name-monitor", principal="monitor:synthetic")
        async def upstream(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2048}})
        self.svc.upstream_transport = httpx.MockTransport(upstream)
        with patch("features.stability.app.egress.EGRESS_ALLOWLIST", ("127.0.0.1",)):
            result = asyncio.run(self.svc.execute_one(active=True))
        self.assertEqual(result["result"]["conclusion"], "normal")
        history = self.svc.store.get(job["job_id"], principal="monitor:synthetic")
        events = self.svc.store.events("monitor:synthetic", 0, 100)
        self.assertEqual(len(events["events"]), 1)
        cancelled_job = self.svc.submit(self.candidate, "synthetic-model", "openai", confirm_live=True, idempotency_key="safe-name-cancel", principal="monitor:synthetic")
        cancelled = self.svc.store.cancel(cancelled_job["job_id"], principal="monitor:synthetic")
        after_cancel = self.svc.store.events("monitor:synthetic", 0, 100)
        self.assertEqual(after_cancel["events"][-1]["result"]["display_identity"], cancelled["display_identity"])
        with self.svc.store.connect() as db:
            stored = [dict(row) for row in db.execute("SELECT spec,result FROM jobs")]
        for value in (preview, config, job, result, history, events, stored, cancelled, after_cancel, self.svc.choices()):
            serialized = json.dumps(value)
            self.assertTrue(all(secret not in serialized and json.dumps(secret)[1:-1] not in serialized for secret in (candidate_key, reference_key)), "paired_credentials_absent_from_public_and_persisted_names")
        self.assertIn("[REDACTED]", preview["display_identity"]["candidate"]["name"])


class ArtifactBoundaryTests(unittest.TestCase):
    def test_script_resolves_symlinks_before_creating_any_test_data(self):
        root = os.environ.get("EVAL_TEST_ARTIFACT_ROOT")
        if not root:
            raise RuntimeError("external_test_artifact_root_required")
        with tempfile.TemporaryDirectory(prefix="token-path-", dir=root) as directory:
            base = Path(directory)
            fake_repo = base / "fixture-source"
            scripts = fake_repo / "scripts"
            scripts.mkdir(parents=True)
            script = scripts / "token_usage_ui.cjs"
            shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/token_usage_ui.cjs", script)
            business = base / "fixture-business-data"
            business.mkdir()
            source_link, business_link = base / "source-link", base / "business-link"
            source_link.symlink_to(fake_repo, target_is_directory=True)
            business_link.symlink_to(business, target_is_directory=True)
            safe_env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
            for forbidden in (fake_repo, source_link, source_link / "new-child", business_link / "new-child"):
                response = subprocess.run(["node", str(script), "--check-artifact-root"], env={**safe_env, "EVAL_TEST_ARTIFACT_ROOT": str(forbidden), "PLATFORM_DATA_DIR": str(business)}, capture_output=True, timeout=10)
                self.assertNotEqual(response.returncode, 0)
                self.assertIn(b"external artifact root required", response.stderr)
                self.assertFalse((fake_repo / "new-child").exists())
                self.assertFalse((business / "new-child").exists())
            allowed = base / "new-outside" / "nested"
            response = subprocess.run(["node", str(script), "--check-artifact-root"], env={**safe_env, "EVAL_TEST_ARTIFACT_ROOT": str(allowed), "PLATFORM_DATA_DIR": str(business)}, capture_output=True, timeout=10)
            self.assertEqual(response.returncode, 0, response.stderr)
            self.assertEqual(json.loads(response.stdout)["artifactRoot"], str(allowed))
            self.assertFalse(allowed.exists())


if __name__ == "__main__":
    unittest.main()
