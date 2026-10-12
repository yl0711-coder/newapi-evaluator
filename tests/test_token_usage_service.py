"""SQLite schedules, leases, early alerts, scoped Monitor HMAC and activation."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from features.model_coverage.catalog import Catalog
from features.model_coverage.monitor import rfc3339, sign
from features.stability.app.scheduler import probe_semaphore
from features.token_usage import api, internal_api, service
from features.token_usage.domain import MAX_REPORTED_TOKENS, observation
from shared.registry import Conflict, Registry
from tests.test_token_usage import Loopback


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        configured_root = os.environ.get("EVAL_TEST_ARTIFACT_ROOT") or os.environ.get("EVAL_TEST_EVIDENCE_ROOT") or os.environ.get("TMPDIR")
        if not configured_root:
            raise RuntimeError("external_test_artifact_root_required")
        root = Path(configured_root)
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="token-test-", dir=root)
        self.registry = Registry(Path(self.temp.name) / "platform")
        self.catalog = Catalog(self.registry)
        self.catalog.add("synthetic-model", "Synthetic", "Synthetic", "openai")
        self.fixture = Loopback()
        result = self.registry.import_records([{"name": "candidate", "base_url": self.fixture.url, "api_key": "fixture_candidate"},
                                               {"name": "reference", "base_url": self.fixture.url, "api_key": "fixture_reference"}])
        self.candidate, self.reference = result["ids"]
        self.now = 1801
        self.svc = service.TokenUsageService(self.registry, clock=lambda: self.now)
        self.svc.save_binding("synthetic-model", "openai", self.reference, confirm_trusted=True)
        self.egress = patch("features.stability.app.egress.EGRESS_ALLOWLIST", ("127.0.0.1",))
        self.egress.start()
        self.env = patch.dict(os.environ, {"PLATFORM_EGRESS_ALLOWLIST": "127.0.0.1"})
        self.env.start()
        self.secret = b"synthetic-monitor-secret-at-least-32-bytes"

    async def asyncTearDown(self):
        await self.svc.stop()
        self.egress.stop()
        self.env.stop()
        self.fixture.close()
        self.temp.cleanup()

    def submit(self, idem="synthetic-job", **kwargs):
        return self.svc.submit(self.candidate, "synthetic-model", "openai", confirm_live=True,
                               idempotency_key=idem, **kwargs)

    def schedule(self):
        self.now = 1801
        config = self.svc.save_config(self.candidate, "synthetic-model", "openai", enabled=True, confirm_live=True)
        self.now = 3600
        self.svc.schedule_tick()
        return config

    def change_schedule(self, config, change):
        candidate = self.candidate
        if change == "changed":
            candidate = self.registry.save({"name": "changed candidate", "base_url": self.fixture.url, "api_key": "fixture_candidate_changed"})["id"]
        return self.svc.save_config(candidate, "synthetic-model", "openai", enabled=change == "changed",
                                    confirm_live=True, config_id=config["id"])

    async def test_queued_schedule_pause_and_config_change_send_nothing(self):
        for change in ("paused", "changed"):
            with self.subTest(change=change):
                config = self.schedule()
                self.change_schedule(config, change)
                result = await self.svc.execute_one(scheduled_only=True)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["result"]["reason"], "schedule_" + change if change == "paused" else "schedule_config_changed")
                self.assertEqual(result["result"]["requests_sent"], 0)
                self.assertEqual(result["result"]["new_requests_sent"], 0)
                self.assertTrue(all(a["state"] == "not_sent" for a in result["attempts"]))
                self.assertEqual(len(self.fixture.requests), 0)

    async def test_schedule_pause_and_config_change_while_waiting_shared_capacity(self):
        semaphore = probe_semaphore()
        for change in ("paused", "changed"):
            with self.subTest(change=change):
                self.schedule()
                job = next(j for j in self.svc.store.list(administrative=True) if j["status"] == "queued")
                config = {"id": job["config_id"]}
                await semaphore.acquire()
                await semaphore.acquire()
                task = asyncio.create_task(self.svc.execute_one(scheduled_only=True))
                try:
                    for _ in range(100):
                        current = self.svc.store.get(job["job_id"], administrative=True)
                        if current["status"] == "running":
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(current["status"], "running")
                    self.assertTrue(all(a["state"] == "not_sent" for a in current["attempts"]))
                    self.change_schedule(config, change)
                finally:
                    semaphore.release()
                    semaphore.release()
                result = await asyncio.wait_for(task, 3)
                self.assertEqual(result["result"]["reason"], "schedule_paused" if change == "paused" else "schedule_config_changed")
                self.assertEqual(result["result"]["requests_sent"], 0)
                self.assertEqual(len(self.fixture.requests), 0)
                self.assertTrue(all(a["state"] == "not_sent" for a in result["attempts"]))

    async def test_schedule_pause_and_change_after_major_preserve_alert_without_extra_pair(self):
        for change in ("paused", "changed"):
            with self.subTest(change=change):
                calls, first_received = [], asyncio.Event()
                async def upstream(request):
                    calls.append(request)
                    if len(calls) == 2:
                        first_received.set()
                    count = 4096 if request.headers["authorization"] == "Bearer fixture_candidate" else 2048
                    return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": count}})
                self.svc.upstream_transport = httpx.MockTransport(upstream)
                config = self.schedule()
                job = next(j for j in self.svc.store.list(administrative=True) if j["status"] == "queued")
                async def stop_after_major():
                    await first_received.wait()
                    semaphore = probe_semaphore()
                    async with semaphore, semaphore:
                        for _ in range(100):
                            current = self.svc.store.get(job["job_id"], administrative=True)
                            if current["result"] and current["result"]["conclusion"] == "high_risk":
                                break
                            await asyncio.sleep(0.01)
                        self.assertEqual(current["result"]["conclusion"], "high_risk")
                        self.change_schedule(config, change)
                stop = asyncio.create_task(stop_after_major())
                execute = asyncio.create_task(self.svc.execute_one(scheduled_only=True))
                try:
                    _, result = await asyncio.wait_for(asyncio.gather(stop, execute), 3)
                finally:
                    stop.cancel()
                    execute.cancel()
                    await asyncio.gather(stop, execute, return_exceptions=True)
                self.assertEqual(len(calls), 2)
                self.assertEqual(result["result"]["conclusion"], "high_risk")
                self.assertEqual(result["result"]["verification_status"], "schedule_paused" if change == "paused" else "schedule_config_changed")
                self.assertEqual(result["result"]["requests_sent"], 2)
                self.assertEqual(result["result"]["new_requests_sent"], 2)
                self.assertTrue(all(a["state"] == "not_sent" for a in result["attempts"] if a["step_id"].endswith(":1")))

    async def test_canonical_astra_alias_keeps_fixed_payload_policy(self):
        model = next(row for row in self.catalog.models() if row["model"] == "gpt-6-astra")
        for channel in (self.candidate, self.reference):
            self.catalog.bind(channel, model["id"], "synthetic-astra-alias", "responses")
        calls = []
        async def upstream(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}], "usage": {"input_tokens": 2000, "output_tokens": 1}})
        self.svc.upstream_transport = httpx.MockTransport(upstream)
        self.svc.save_binding("gpt-6-astra", "responses", self.reference, confirm_trusted=True)
        self.svc.submit(self.candidate, "gpt-6-astra", "responses", confirm_live=True, idempotency_key="astra-alias")
        result = await self.svc.execute_one(active=True)
        self.assertEqual(result["result"]["conclusion"], "normal")
        self.assertEqual(len(calls), 2)
        for body in calls:
            self.assertEqual(body["model"], "synthetic-astra-alias")
            self.assertEqual(body["max_output_tokens"], 4096)
            self.assertEqual(body["reasoning"]["effort"], "low")

    async def test_cancel_first_pair_preserves_two_permits_as_unknown(self):
        entered, release, calls = asyncio.Event(), asyncio.Event(), []
        async def upstream(request):
            calls.append(request)
            if len(calls) == 2:
                entered.set()
            await release.wait()
            return httpx.Response(503)
        self.svc.upstream_transport = httpx.MockTransport(upstream)
        job = self.submit()
        task = asyncio.create_task(self.svc.execute_one(active=True))
        await asyncio.wait_for(entered.wait(), 3)
        self.svc.cancel(job["job_id"], administrative=True)
        await asyncio.gather(task, return_exceptions=True)
        result = self.svc.store.get(job["job_id"], administrative=True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["result"]["requests_sent"], 2)
        self.assertEqual(result["result"]["new_requests_sent"], 2)
        self.assertEqual(sum(a["state"] == "unknown" for a in result["attempts"]), 2)

    async def test_oversized_usage_finishes_unknown_and_consumer_runs_next_job(self):
        calls = []
        async def upstream(request):
            calls.append(request)
            count = 10**400 if len(calls) <= 2 and request.headers["authorization"] == "Bearer fixture_candidate" else 2000
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": count}})
        self.svc.upstream_transport = httpx.MockTransport(upstream)
        invalid = self.submit(idem="oversized-usage")
        normal = self.submit(idem="normal-after-oversized")
        await self.svc.start(active=True)
        for _ in range(150):
            second = self.svc.store.get(normal["job_id"], administrative=True)
            if second["status"] == "completed":
                break
            await asyncio.sleep(0.02)
        first = self.svc.store.get(invalid["job_id"], administrative=True)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["result"]["conclusion"], "unable_to_determine")
        self.assertEqual(first["result"]["reason"], "candidate_usage_invalid")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["result"]["conclusion"], "normal")
        self.assertEqual(len(calls), 4)
        self.assertTrue(self.svc.status()["running"])

    async def test_log_evidence_shares_reported_integer_limit(self):
        base = {"event_id": "synthetic-log", "observed_at": "2026-10-12T00:00:00Z", "source": "monitor_log"}
        for field in ("input_tokens_reported", "reference_tokens_reported", "first_input_tokens_reported", "final_input_tokens_reported"):
            self.assertEqual(api.LogEvidence.model_validate({**base, field: MAX_REPORTED_TOKENS}).model_dump()[field], MAX_REPORTED_TOKENS)
            for count in (MAX_REPORTED_TOKENS + 1, 10**400):
                with self.assertRaises(ValidationError):
                    api.LogEvidence.model_validate({**base, field: count})
            self.assertEqual(api.LogEvidence.model_json_schema()["properties"][field]["anyOf"][0]["maximum"], MAX_REPORTED_TOKENS)

    async def test_sqlite_idempotency_and_frozen_reference_drift(self):
        job = self.submit()
        self.assertEqual(self.submit()["job_id"], job["job_id"])
        self.assertEqual(len(self.fixture.requests), 0)
        channel = self.registry.get(self.reference, secret=True)
        self.registry.save({**channel, "api_key": "fixture_replaced"}, channel_id=self.reference, version=channel["version"])
        self.assertEqual(self.submit()["job_id"], job["job_id"])
        with self.assertRaises(Conflict):
            self.submit(log_evidence={"event_id": "changed-public-payload"})
        result = await self.svc.execute_one(active=True)
        self.assertEqual(result["result"]["conclusion"], "unable_to_determine")
        self.assertEqual(len(self.fixture.requests), 0)

    async def test_default_disabled_schedule_and_two_successive_minor_rounds(self):
        disabled = self.svc.save_config(self.candidate, "synthetic-model", "openai")
        self.assertFalse(disabled["enabled"])
        self.assertEqual(self.svc.schedule_tick(), 0)
        configured = self.svc.save_config(self.candidate, "synthetic-model", "openai", enabled=True, confirm_live=True, config_id=disabled["id"])
        self.fixture.counts = {"Bearer fixture_candidate": 2300, "Bearer fixture_reference": 2000}
        self.now = 3600
        self.assertEqual(self.svc.schedule_tick(), 1)
        self.assertEqual(self.svc.schedule_tick(), 0)
        first = await self.svc.execute_one(active=False)
        self.assertEqual(first["result"]["conclusion"], "pending_confirmation")
        self.now = 5400
        self.assertEqual(self.svc.schedule_tick(), 1)
        second = await self.svc.execute_one(active=False)
        self.assertEqual(second["result"]["conclusion"], "minor_anomaly")
        self.assertEqual(second["result"]["requests_sent"], 2)
        self.now = 36000
        self.assertEqual(self.svc.schedule_tick(), 1)
        self.assertEqual(self.svc.schedule_tick(), 0)
        self.assertEqual(len(self.svc.store.list(administrative=True)), 3)
        self.assertEqual(configured["interval_seconds"], 1800)

    async def test_old_queued_schedule_is_terminal_without_backfill_but_active_still_runs(self):
        self.svc.save_config(self.candidate, "synthetic-model", "openai", enabled=True, confirm_live=True)
        for occurrence in (3600, 5400, 7200):
            self.now = occurrence
            self.assertEqual(self.svc.schedule_tick(), 1)
        active = self.submit(idem="active-independent-of-scheduled-window")
        self.now = 9000
        self.assertEqual(self.svc.schedule_tick(), 1)
        for _ in range(3):
            old = await self.svc.execute_one(scheduled_only=True)
            self.assertEqual(old["status"], "completed")
            self.assertEqual(old["result"]["reason"], "missed_scheduled_window")
            self.assertEqual(old["result"]["requests_sent"], 0)
            self.assertEqual(old["result"]["new_requests_sent"], 0)
            self.assertTrue(all(a["state"] == "not_sent" for a in old["attempts"]))
            self.assertEqual(len(self.fixture.requests), 0)
        current = await self.svc.execute_one(scheduled_only=True)
        self.assertEqual(current["occurrence"], 9000)
        self.assertEqual(current["result"]["conclusion"], "normal")
        self.assertEqual(len(self.fixture.requests), 2)
        result = await self.svc.execute_one(active=True, active_only=True)
        self.assertEqual(result["job_id"], active["job_id"])
        self.assertEqual(result["result"]["conclusion"], "normal")
        self.assertEqual(len(self.fixture.requests), 4)
        scheduled_events = self.svc.store.events("monitor:observer", 0, 100)["events"]
        self.assertEqual(len(scheduled_events), 4)
        self.assertIsNone(await self.svc.execute_one(scheduled_only=True))

    async def test_old_schedule_recovery_preserves_major_and_unknown_without_sends(self):
        configured = self.svc.save_config(self.candidate, "synthetic-model", "openai", enabled=True, confirm_live=True)
        self.now = 3600
        self.svc.schedule_tick()
        row = self.svc.store.claim("crashed", scheduled_only=True)
        candidate = observation([{"input_tokens": 4096, "normalized_input_tokens": 4096, "invalid": False}], status="completed", terminal=True)
        reference = observation([{"input_tokens": 2048, "normalized_input_tokens": 2048, "invalid": False}], status="completed", terminal=True)
        major = service.classify_pair(candidate, reference, source="scheduled", frozen_hash=configured["frozen_hash"], occurrence=3600)
        major["requests_sent"] = 2
        for side, value in (("candidate", candidate), ("reference", reference)):
            self.svc.store.permit(row["id"], "crashed", side, 0)
            self.svc.store.attempt(row["id"], "crashed", side, 0, value)
        self.svc.store.publish(row["id"], "crashed", major)
        self.svc.store.permit(row["id"], "crashed", "candidate", 1)
        with self.svc.store.connect() as db:
            db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (row["id"],))
        self.now = 9000
        result = await self.svc.execute_one(scheduled_only=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["conclusion"], "high_risk")
        self.assertEqual(result["result"]["verification_status"], "missed_scheduled_window")
        self.assertEqual(result["result"]["requests_sent"], 3)
        self.assertEqual(result["result"]["new_requests_sent"], 0)
        self.assertEqual(len(self.fixture.requests), 0)
        states = {a["step_id"]: a["state"] for a in result["attempts"]}
        self.assertEqual(states["token:candidate:1"], "unknown")
        self.assertEqual(states["token:reference:1"], "not_sent")
        self.assertEqual(len(self.svc.store.events("monitor:observer", 0, 100)["events"]), 2)

    async def test_expired_sending_unknown_only_unsent_side_runs(self):
        job = self.submit()
        row = self.svc.store.claim("crashed", active=True)
        self.svc.store.permit(job["job_id"], "crashed", "candidate", 0)
        with self.svc.store.connect() as db:
            db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job["job_id"],))
        result = await self.svc.execute_one(active=True)
        self.assertEqual(len(self.fixture.requests), 1)
        self.assertEqual(result["result"]["conclusion"], "unable_to_determine")
        self.assertEqual(result["result"]["requests_sent"], 2)
        states = {a["step_id"]: a["state"] for a in result["attempts"]}
        self.assertEqual(states["token:candidate:0"], "unknown")
        self.assertEqual(states["token:reference:0"], "confirmed")
        self.assertIsNone(await self.svc.execute_one(active=True))

    async def test_cancel_after_major_retains_alert_and_unknown_verification(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def upstream(request):
            calls.append(1)
            if len(calls) > 2:
                if len(calls) == 4:
                    entered.set()
                await release.wait()
            c = 4096 if request.headers["authorization"] == "Bearer fixture_candidate" else 2048
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": c}})
        self.svc.upstream_transport = httpx.MockTransport(upstream)
        job = self.submit()
        task = asyncio.create_task(self.svc.execute_one(active=True))
        await asyncio.wait_for(entered.wait(), 3)
        current = self.svc.store.get(job["job_id"], administrative=True)
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["result"]["conclusion"], "high_risk")
        events = self.svc.store.events("monitor:any", 0, 100)
        self.assertEqual(events["events"], [])
        self.svc.cancel(job["job_id"], administrative=True)
        await asyncio.gather(task, return_exceptions=True)
        current = self.svc.store.get(job["job_id"], administrative=True)
        self.assertEqual(current["status"], "cancelled")
        self.assertEqual(current["result"]["conclusion"], "high_risk")
        self.assertEqual(current["result"]["verification_status"], "cancelled_unknown")
        self.assertEqual(current["result"]["requests_sent"], 4)
        self.assertEqual(current["result"]["new_requests_sent"], 4)
        self.assertTrue(any(a["state"] == "unknown" for a in current["attempts"]))
        self.assertEqual(len(calls), 4)

    async def test_off_keeps_active_queued_but_schedules_execute(self):
        job = self.submit()
        self.svc.save_config(self.candidate, "synthetic-model", "openai", enabled=True, confirm_live=True)
        self.now = 3600
        await self.svc.start(active=False)
        for _ in range(50):
            if self.fixture.requests:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.svc.store.get(job["job_id"], administrative=True)["status"], "queued")
        self.assertTrue(self.svc.status()["running"])
        await self.svc.stop()
        self.assertFalse(self.svc.status()["running"])
        self.assertGreaterEqual(len(self.fixture.requests), 2)

    async def test_active_claim_does_not_wait_scheduled_queue_and_cancel_keeps_runtime_alive(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def upstream(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}],"usage":{"prompt_tokens":2000}})
        self.svc.upstream_transport=httpx.MockTransport(upstream)
        self.svc.save_config(self.candidate,"synthetic-model","openai",enabled=True,confirm_live=True)
        self.now=3600
        await self.svc.start(active=True)
        await asyncio.wait_for(entered.wait(),3)
        active=self.submit(idem="active-while-scheduled")
        for _ in range(50):
            if self.svc.store.get(active["job_id"],administrative=True)["status"]=="running":
                break
            await asyncio.sleep(.02)
        self.assertEqual(self.svc.store.get(active["job_id"],administrative=True)["status"],"running")
        self.svc.cancel(active["job_id"],administrative=True)
        release.set()
        for _ in range(50):
            if self.svc.store.get(active["job_id"],administrative=True)["status"]=="cancelled":
                break
            await asyncio.sleep(.02)
        self.assertTrue(self.svc.status()["running"])
        self.assertEqual(self.svc.store.get(active["job_id"],administrative=True)["status"],"cancelled")
        second=self.submit(idem="after-cancel")
        for _ in range(75):
            if self.svc.store.get(second["job_id"],administrative=True)["status"]=="completed":
                break
            await asyncio.sleep(.02)
        self.assertEqual(self.svc.store.get(second["job_id"],administrative=True)["status"],"completed")

    async def test_threshold_revision_changes_reject_frozen_job(self):
        job=self.submit()
        first=self.svc.admission_binding_for("synthetic-model","openai")
        self.svc.save_binding("synthetic-model","openai",self.reference,confirm_trusted=True,threshold_config={"minor_ratio":1.25,"minor_difference":200})
        second=self.svc.admission_binding_for("synthetic-model","openai")
        self.assertNotEqual(first["revision"],second["revision"])
        self.assertNotEqual(first["condition_hash"],second["condition_hash"])
        result=await self.svc.execute_one(active=True)
        self.assertEqual(result["result"]["conclusion"],"unable_to_determine")
        self.assertEqual(len(self.fixture.requests),0)

    async def test_hmac_nonce_idempotency_principal_and_event_cursor(self):
        app = FastAPI()
        app.include_router(api.router)
        app.include_router(internal_api.internal_router)
        async def signed(client, method, path, body=None, *, key="synthetic-monitor", nonce=None, idem="synthetic-event-idem"):
            raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
            stamp, nonce = rfc3339(time.time()), nonce or secrets.token_hex(12)
            headers = {"x-nexus-client": "monitor", "x-nexus-key-id": key, "x-nexus-timestamp": stamp,
                       "x-nexus-nonce": nonce, "x-nexus-signature": sign(self.secret, method, path, stamp, nonce, raw), "idempotency-key": idem}
            with patch.dict(os.environ, {"EVAL_MONITOR_KEY_ID": key, "EVAL_MONITOR_SECRET": self.secret.decode()}):
                return await client.request(method, path, content=raw, headers=headers)
        body = {"schema_version": "1.0", "target": {"registry_channel_id": self.candidate, "model": "synthetic-model", "protocol": "openai"}, "event_id": "synthetic-event", "confirm_live": True}
        with patch("features.token_usage.service.get_service", return_value=self.svc), patch("features.model_coverage.internal_api.get_registry", return_value=self.registry):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://loopback") as client:
                unsigned = await client.post("/internal/v1/token-usage-jobs", json=body)
                self.assertEqual(unsigned.status_code, 503)
                created = await signed(client, "POST", "/internal/v1/token-usage-jobs", body)
                self.assertEqual(created.status_code, 202, created.text)
                job_id = created.json()["job_id"]
                duplicate = await signed(client, "POST", "/internal/v1/token-usage-jobs", body)
                self.assertEqual(duplicate.json()["job_id"], job_id)
                changed = await signed(client, "POST", "/internal/v1/token-usage-jobs", {**body, "event_id": "synthetic-changed"})
                self.assertEqual(changed.status_code, 409)
                nonce = secrets.token_hex(12)
                self.assertEqual((await signed(client, "GET", f"/internal/v1/token-usage-jobs/{job_id}", nonce=nonce)).status_code, 200)
                self.assertEqual((await signed(client, "GET", f"/internal/v1/token-usage-jobs/{job_id}", nonce=nonce)).status_code, 401)
                await self.svc.execute_one(active=True)
                foreign = await signed(client, "GET", f"/internal/v1/token-usage-jobs/{job_id}", key="other-monitor")
                self.assertEqual(foreign.status_code, 404)
                events = await signed(client, "GET", "/internal/v1/token-usage-events?after=0&limit=100")
                self.assertEqual(len(events.json()["events"]), 1)
                cursor = events.json()["next_cursor"]
                empty = await signed(client, "GET", f"/internal/v1/token-usage-events?after={cursor}&limit=100")
                self.assertEqual(empty.json()["events"], [])
                foreign_events = await signed(client, "GET", "/internal/v1/token-usage-events?after=0&limit=100", key="other-monitor")
                self.assertEqual(foreign_events.json()["events"], [])
                channel = self.registry.get(self.reference, secret=True)
                self.registry.save({**channel, "api_key": "fixture_replaced"}, channel_id=self.reference, version=channel["version"])
                repeated = await signed(client, "POST", "/internal/v1/token-usage-jobs", body, idem="new-key-for-original-event")
                self.assertEqual(repeated.status_code, 202)
                self.assertEqual(repeated.json()["job_id"], job_id)
                changed_after_drift = await signed(client, "POST", "/internal/v1/token-usage-jobs", {**body, "log_evidence": {"event_id": "changed-log", "observed_at": "2026-10-12T00:00:00Z", "source": "monitor_log"}})
                self.assertEqual(changed_after_drift.status_code, 409)
                new_after_drift = await signed(client, "POST", "/internal/v1/token-usage-jobs", {**body, "event_id": "fresh-after-drift"}, idem="fresh-after-drift")
                self.assertEqual(new_after_drift.status_code, 409)
                self.svc.save_binding("synthetic-model", "openai", self.reference, confirm_trusted=True, threshold_config={"minor_ratio": 1.25, "minor_difference": 200})
                repeated_after_binding_change = await signed(client, "POST", "/internal/v1/token-usage-jobs", body)
                self.assertEqual(repeated_after_binding_change.status_code, 202)
                self.assertEqual(repeated_after_binding_change.json()["job_id"], job_id)
                invalid = await signed(client, "POST", "/internal/v1/token-usage-jobs", {**body, "api_key": "synthetic-prohibited"})
                self.assertEqual(invalid.status_code, 400)
                self.assertNotIn("fixture_candidate", json.dumps(events.json()))
                queued = await signed(client,"POST","/internal/v1/token-usage-jobs",{**body,"event_id":"synthetic-cancel-event"},idem="synthetic-cancel-create")
                cancelled = await signed(client,"POST",f"/internal/v1/token-usage-jobs/{queued.json()['job_id']}/cancel",{},idem="synthetic-cancel-write")
                self.assertEqual(cancelled.status_code,202)
                new_events = await signed(client,"GET",f"/internal/v1/token-usage-events?after={cursor}&limit=100")
                self.assertEqual(len(new_events.json()["events"]),1)
                self.assertEqual(new_events.json()["events"][0]["result"]["reason"],"cancelled_not_sent")


if __name__ == "__main__":
    unittest.main()
