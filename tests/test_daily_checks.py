"""Daily deadlines, actual protocol parsing, frozen permits and notification outbox."""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI

from features.daily_checks import api, domain, reporting
from features.daily_checks.service import DailyChecks
from features.integrity.transport import run_probe, send_capacity
from features.model_coverage.catalog import Catalog
from features.model_coverage import api as model_api
from features.stability.app.scheduler import probe_semaphore
from features.token_usage.domain import classify_pair
from features.token_usage.service import TokenUsageService
from shared.feishu_notify import send_text, validate_webhook
from shared.registry import Registry, Conflict
from tests.fixtures.daily_checks import Upstream
from tests.test_token_usage import valid


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


class DomainTests(unittest.TestCase):
    def test_four_slots_and_strict_previous_daily_occurrence(self):
        slots = list(domain.slots(stamp("2026-10-15T12:00:00")))
        self.assertEqual([s["start_at"] for s in slots], [stamp("2026-10-15T" + t) for t in ("09:30:00", "13:00:00", "14:30:00", "17:00:00")])
        self.assertEqual([s["report_time"] for s in slots], ["10:30", "14:00", "15:30", "18:00"])
        self.assertEqual(domain.previous_start(slots[0]["start_at"]), stamp("2026-10-14T17:00:00"))
        self.assertEqual(domain.previous_start(slots[3]["start_at"]), slots[2]["start_at"])
        first = classify_pair(valid(2300), valid(2000), source="daily_scheduled", frozen_hash="f", occurrence=slots[0]["start_at"])
        second = classify_pair(valid(2300), valid(2000), previous=first, source="daily_scheduled", frozen_hash="f",
                               occurrence=slots[1]["start_at"], previous_occurrence=slots[0]["start_at"])
        self.assertEqual(second["conclusion"], "minor_anomaly")
        for source, frozen, due in (("fixed_retest", "f", slots[0]["start_at"]), ("daily_scheduled", "changed", slots[0]["start_at"]), ("daily_scheduled", "f", slots[1]["start_at"] - 1800)):
            result = classify_pair(valid(2300), valid(2000), previous=first, source=source, frozen_hash=frozen,
                                   occurrence=slots[1]["start_at"], previous_occurrence=due)
            self.assertEqual(result["conclusion"], "pending_confirmation")

    def test_family_small_samples_missing_metrics_and_conservative_identity(self):
        self.assertEqual(domain.families("codex / Claude"), ["GPT", "Claude"])
        self.assertEqual(domain.families("gpt6, anthropic"), ["GPT", "Claude"])
        self.assertEqual(domain.families("notcodex"), [])
        result = domain.performance([{"status": "completed", "latency_ms": None}, {"status": "unknown"}], 3)
        self.assertEqual((result["completion_rate"], result["not_run"], result["unknown"]), (.5, 1, 1))
        self.assertIsNone(result["latency_median_ms"])
        self.assertIsNone(result["p95_latency_ms"])
        score = {"status": "scored", "prediction": "Synthetic", "valid_answers": 3,
                 "results": [{"model": "Synthetic", "probability": .9, "score": 2}, {"model": "Other", "score": 1}]}
        self.assertIn("行为更接近", domain.identity_verdict(score, "completed", 0, 3, 3))
        self.assertEqual(domain.identity_verdict({**score, "source_verdict": "UNLISTED"}, "completed", 0, 3, 3), "无法判定")
        self.assertEqual(domain.identity_verdict(score, "completed", 1, 3, 3), "无法判定")


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_custom_models_are_used_in_actual_daily_requests(self):
        plan, models, mappings = self.svc.store.plan(), self.svc.catalog.models(), self.svc.catalog.mappings()
        app=FastAPI();app.include_router(model_api.router);app.include_router(api.router)
        with patch.object(model_api,'get_registry',return_value=self.registry), patch.object(api,'get_service',return_value=self.svc):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://loopback') as client:
                for family,model,protocol in [('GPT','gpt-synthetic-next','responses'),('Claude','claude-synthetic-next','anthropic')]:
                    result=await client.post('/api/model-coverage/models',json={'family':family,'model':model,'label':model,'protocol':protocol})
                    self.assertEqual(result.status_code,200)
                shared=(await client.get('/api/model-coverage/models')).json()['models']
                daily=(await client.get('/api/daily-checks/meta')).json()['models']
                self.assertEqual([m['model'] for m in shared],[m['model'] for m in daily])
        self.assertEqual(self.svc.store.plan(),plan)
        self.assertEqual(self.svc.catalog.models()[:len(models)],models)
        self.assertEqual(self.svc.catalog.mappings(),mappings)
        self.assertEqual(self.upstream.calls,[])
        self.assertEqual(self.notifications,[])
        for profile in self.profiles:
            profile['model']='gpt-synthetic-next' if profile['family']=='GPT' else 'claude-synthetic-next'
        self.enable(notify=False)
        batch=await self.batch()
        self.assertEqual(len(self.upstream.calls),10)
        self.assertEqual({r['model'] for r in self.upstream.calls},{'gpt-synthetic-next','claude-synthetic-next'})
        self.assertTrue(all(r['modeltrace']['label']=='无法判定' for r in self.svc.report(batch['id'])['channels']))

    async def asyncSetUp(self):
        root = os.environ.get("EVAL_TEST_ARTIFACT_ROOT") or os.environ.get("TMPDIR")
        if not root:
            raise RuntimeError("external_test_artifact_root_required")
        self.temp = tempfile.TemporaryDirectory(prefix="daily-test-", dir=root)
        self.registry = Registry(Path(self.temp.name))
        Catalog(self.registry)
        self.candidate = self.add("Synthetic candidate", "codex", "synthetic-candidate")
        self.claude = self.add("Synthetic Claude", "claude", "synthetic-claude-candidate")
        self.reference = self.add("Synthetic reference", "gpt / claude", "synthetic-reference", status="recorded")
        self.upstream = Upstream()
        self.tokens = TokenUsageService(self.registry, upstream_transport=httpx.MockTransport(self.upstream.handler))
        self.now = stamp("2026-10-15T09:29:00")
        self.clock_patch = patch("time.time", lambda: self.now)
        self.clock_patch.start()
        self.egress = patch("features.stability.app.egress.EGRESS_ALLOWLIST", ("127.0.0.1",))
        self.egress.start()
        self.notifications = []
        async def notifier(request):
            self.notifications.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 0})
        async def sender(channel, probe, *, before_send=None):
            async with send_capacity(), httpx.AsyncClient(transport=httpx.MockTransport(self.upstream.handler), trust_env=False) as client:
                return await run_probe(client, channel, probe, before_send=before_send)
        self.svc = DailyChecks(self.registry, token_service=self.tokens, sender=sender,
                               notification_transport=httpx.MockTransport(notifier), clock=lambda: self.now)
        self.webhook = patch.object(self.svc, "webhook", return_value="https://open.feishu.cn/open-apis/bot/v2/hook/synthetic")
        self.webhook.start()
        self.profiles = [{"family": "GPT", "model": "gpt-6-astra", "protocol": "responses", "reference_channel_id": self.reference},
                         {"family": "Claude", "model": "claude-opus-4.7", "protocol": "anthropic", "reference_channel_id": self.reference}]

    def add(self, name, scope, key, *, status="online", enabled=True):
        return self.registry.save({"name": name, "scope": scope, "api_key": key, "base_url": "http://127.0.0.1",
                                   "multiplier": .15, "status": status, "enabled": enabled})["id"]

    def change(self, channel, **values):
        old = self.registry.get(channel, secret=True)
        return self.registry.save({**old, **values}, channel_id=channel, version=old["version"])

    def enable(self, *, notify=True):
        preview = self.svc.preview(self.profiles, notify=notify)
        self.svc.save(self.profiles, enabled=True, notify=notify, confirm_live=True, confirm_notify=True,
                      confirm_trusted=True, preview_fingerprint=preview["fingerprint"])
        return preview

    async def batch(self, start="2026-10-15T09:30:00"):
        self.now = stamp(start)
        await self.svc.tick()
        await self.svc.worker
        await self.svc.notification
        return self.svc.store.batches()[0]

    async def asyncTearDown(self):
        await self.svc.stop()
        await self.tokens.stop()
        self.webhook.stop()
        self.egress.stop()
        self.clock_patch.stop()
        self.temp.cleanup()

    async def test_two_families_actual_parsing_score_report_and_zero_early_delivery(self):
        preview = self.enable()
        self.assertEqual((preview["initial_requests_per_batch"], preview["max_requests_per_batch"]), (10, 14))
        batch = await self.batch()
        self.assertEqual(len(self.upstream.calls), 10)
        report = self.svc.report(batch["id"])
        self.assertEqual(report["coverage"]["fully_measured_rows"], 2)
        self.assertTrue(all(r["modeltrace"]["score"]["valid_answers"] == 3 for r in report["channels"]))
        self.assertTrue(all(r["usage"]["conclusion"] == "normal" for r in report["channels"]))
        gpt = next(r for r in report["channels"] if r["family"] == "GPT")
        self.assertGreaterEqual(gpt["performance"]["usage_candidate"]["ttft_samples"], 1)
        self.assertEqual(self.notifications, [])
        await self.svc.tick()
        await self.svc.worker
        self.assertEqual(len(self.upstream.calls), 10)
        self.now = batch["report_at"]
        await self.svc.tick()
        await self.svc.notification
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(self.svc.detail(batch["id"])["notifications"][0]["status"], "sent")
        self.assertIn("Token/s", self.notifications[0]["content"]["text"])
        self.assertEqual(next(r for r in self.upstream.calls if r["path"].endswith("/messages"))["reasoning"], None)

    async def test_exact_four_batches_same_day_no_duplicate_requests_or_messages(self):
        self.enable()
        for start, due in zip(("09:30", "13:00", "14:30", "17:00"), ("10:30", "14:00", "15:30", "18:00")):
            await self.batch("2026-10-15T" + start + ":00")
            self.now = stamp("2026-10-15T" + due + ":00")
            await self.svc.tick()
            await self.svc.notification
            await self.svc.tick()
            await self.svc.worker
            await self.svc.notification
        self.assertEqual(len(self.upstream.calls), 40)
        self.assertEqual(len(self.notifications), 4)
        self.assertEqual(len(self.svc.store.batches()), 4)

    async def test_daily_minor_requires_strict_previous_slot_and_same_conditions(self):
        self.upstream.candidate_tokens = 2300
        self.enable(notify=False)
        first = await self.batch()
        self.assertTrue(all(r["usage"]["conclusion"] == "pending_confirmation" for r in self.svc.report(first["id"])["channels"]))
        second = await self.batch("2026-10-15T13:00:00")
        self.assertTrue(all(r["usage"]["conclusion"] == "minor_anomaly" for r in self.svc.report(second["id"])["channels"]))
        self.assertEqual(len(self.upstream.calls), 20)
        skipped = await self.batch("2026-10-15T17:00:00")
        self.assertTrue(all(r["usage"]["conclusion"] == "pending_confirmation" for r in self.svc.report(skipped["id"])["channels"]))

    async def test_missed_window_reports_without_requests_and_save_never_backfills(self):
        self.enable()
        batch = await self.batch("2026-10-15T10:31:00")
        self.assertEqual(self.upstream.calls, [])
        self.assertEqual(batch["status"], "finalized")
        self.assertTrue(all(r["modeltrace"]["reason"] == "start_window_missed" for r in batch["report"]["channels"]))
        self.svc.store.pause(self.now)
        self.enable()
        await self.svc.tick()
        await self.svc.worker
        self.assertEqual(len(self.svc.store.batches()), 1)

    async def test_inventory_keeps_unusable_and_unknown_online_denominator(self):
        disabled = self.add("Disabled", "gpt", "synthetic-disabled", enabled=False)
        unknown = self.add("Unknown", "notcodex", "synthetic-unknown")
        missing = self.add("Missing", "claude", "synthetic-missing")
        with self.registry.connect() as db:
            db.execute("UPDATE channels SET key_enc='' WHERE id=?", (missing,))
        multi = self.add("Both", "gpt/claude", "synthetic-both")
        preview = self.svc.preview(self.profiles)
        rows = {(r["channel_id"], r["family"]): r for r in preview["channels"]}
        self.assertEqual(preview["online_channels"], 6)
        self.assertEqual(rows[(disabled, "GPT")]["mt_reason"], "channel_disabled")
        self.assertEqual(rows[(unknown, "未识别")]["mt_reason"], "family_unrecognized")
        self.assertEqual(rows[(missing, "Claude")]["mt_reason"], "credential_unavailable")
        self.assertTrue((multi, "GPT") in rows and (multi, "Claude") in rows)

    async def test_preview_drift_and_confirmation_are_zero_send(self):
        preview = self.svc.preview(self.profiles)
        with self.assertRaises(ValueError):
            self.svc.save(self.profiles, enabled=True, preview_fingerprint=preview["fingerprint"])
        self.change(self.candidate, api_key="synthetic-replaced")
        with self.assertRaises(Conflict):
            self.svc.save(self.profiles, enabled=True, confirm_live=True, confirm_notify=True, confirm_trusted=True,
                          preview_fingerprint=preview["fingerprint"])
        self.assertEqual(self.upstream.calls, [])
        self.assertEqual(self.notifications, [])

    async def test_pause_or_channel_change_while_waiting_capacity_prevents_final_send(self):
        self.profiles = self.profiles[:1]
        semaphore = probe_semaphore()
        for day, change in enumerate(("paused", "offline", "key", "mapping", "reference")):
            with self.subTest(change=change):
                start = stamp("2026-10-15T09:30:00") + day * 86400
                self.now = start - 60
                self.enable(notify=False)
                before = len(self.upstream.calls)
                await semaphore.acquire()
                await semaphore.acquire()
                self.now = start
                await self.svc.tick()
                batch = self.svc.store.batches()[0]
                for _ in range(100):
                    if self.svc.store.items(batch["id"])[0]["mt_job_id"]:
                        break
                    await asyncio.sleep(.01)
                self.assertIsNotNone(self.svc.store.items(batch["id"])[0]["mt_job_id"])
                if change == "paused":
                    self.svc.store.pause(self.now)
                elif change == "offline":
                    self.change(self.candidate, status="recorded")
                    self.change(self.claude, status="recorded")
                elif change == "key":
                    self.change(self.candidate, api_key="synthetic-new-candidate")
                    self.change(self.claude, api_key="synthetic-new-claude-candidate")
                elif change == "mapping":
                    catalog = Catalog(self.registry)
                    model = next(m for m in catalog.models() if m["model"] == "gpt-6-astra")
                    catalog.bind(self.candidate, model["id"], "synthetic-astra", "responses")
                else:
                    self.change(self.reference, api_key="synthetic-new-reference")
                semaphore.release()
                semaphore.release()
                await self.svc.worker
                self.assertEqual(len(self.upstream.calls) - before, 3 if change == "reference" else 0)
                await self.svc.stop()
                self.change(self.candidate, status="online")
                self.change(self.claude, status="online")
                catalog = Catalog(self.registry)
                model = next(m for m in catalog.models() if m["model"] == "gpt-6-astra")
                catalog.bind(self.candidate, model["id"], "gpt-6-astra", "responses")

    async def test_daily_jobs_are_not_claimed_by_original_consumers(self):
        self.enable(notify=False)
        job = self.tokens.submit(self.candidate, "gpt-6-astra", "responses", confirm_live=True,
                                 principal="daily:synthetic", idempotency_key="isolated", occurrence=self.now)
        self.assertIsNone(await self.tokens.execute_one(active=True, active_only=True))
        self.assertIsNone(await self.tokens.execute_one(scheduled_only=True))
        self.assertEqual(self.tokens.store.get(job["job_id"], administrative=True)["status"], "queued")
        with self.assertRaises(Conflict):
            self.tokens.submit(self.candidate, "gpt-6-astra", "responses", confirm_live=True,
                               principal="daily:synthetic", idempotency_key="isolated", occurrence=self.now + 1)

    async def test_token_final_permits_recheck_plan_online_key_and_reference_revision(self):
        self.profiles = self.profiles[:1]
        semaphore = probe_semaphore()
        for day, change in enumerate(("paused", "offline", "key", "reference", "threshold")):
            with self.subTest(change=change):
                start = stamp("2026-10-15T09:30:00") + day * 86400
                self.now = start - 60
                self.enable(notify=False)
                self.now = start
                slot = next(s for s in domain.slots(self.now) if s["start_at"] == start)
                plan = self.svc.store.plan()
                batch_id = self.svc.store.create_batch(slot, plan, self.svc._inventory(plan["profiles"]), self.now)
                batch = self.svc.store.batch(batch_id)
                item = next(i for i in self.svc.store.items(batch_id) if i["family"] == "GPT")
                await semaphore.acquire()
                await semaphore.acquire()
                task = asyncio.create_task(self.svc._run_usage(item, batch))
                try:
                    for _ in range(100):
                        current = self.svc.store.items(batch_id)[0]
                        if current["usage_job_id"]:
                            break
                        await asyncio.sleep(.01)
                    self.assertIsNotNone(current["usage_job_id"])
                    if change == "paused":
                        self.svc.store.pause(self.now)
                    elif change == "offline":
                        self.change(self.candidate, status="recorded")
                    elif change == "key":
                        self.change(self.candidate, api_key="synthetic-new-candidate")
                    elif change == "reference":
                        self.change(self.reference, api_key="synthetic-new-reference")
                    else:
                        self.tokens.save_binding("gpt-6-astra", "responses", self.reference, confirm_trusted=True,
                            threshold_config={"minor_ratio": 1.2, "minor_difference": 200})
                finally:
                    semaphore.release()
                    semaphore.release()
                await asyncio.wait_for(task, 3)
                self.assertEqual(self.upstream.calls, [])
                self.assertTrue(all(a["state"] == "not_sent" for a in self.tokens.store.get(current["usage_job_id"], administrative=True)["attempts"]))
                self.change(self.candidate, status="online")

    async def test_report_deadline_stops_inflight_usage_without_extending_window(self):
        self.profiles = self.profiles[:1]
        entered = asyncio.Event()
        async def block(request, body):
            if body.get("stream"):
                entered.set()
                await asyncio.Event().wait()
        self.upstream.block = block
        self.enable()
        self.now = stamp("2026-10-15T09:30:00")
        await self.svc.tick()
        await asyncio.wait_for(entered.wait(), 3)
        batch = self.svc.store.batches()[0]
        self.now = batch["report_at"]
        await self.svc.tick()
        await self.svc.notification
        await self.svc.worker
        count = len(self.upstream.calls)
        self.upstream.block = None
        await self.svc.tick()
        await self.svc.worker
        await self.svc.notification
        self.assertEqual(len(self.upstream.calls), count)
        self.assertEqual(count, 5)
        self.assertEqual(len(self.notifications), 1)
        gpt = next(r for r in self.svc.report(batch["id"])["channels"] if r["family"] == "GPT")
        self.assertEqual(gpt["usage"]["conclusion"], "unable_to_determine")
        self.assertEqual(gpt["performance"]["usage_candidate"]["unknown"], 1)

    async def test_major_retains_first_evidence_and_missing_usage_is_undetermined(self):
        self.upstream.candidate_tokens = 4096
        self.enable(notify=False)
        batch = await self.batch()
        self.assertEqual(len(self.upstream.calls), 14)
        self.assertTrue(all(r["usage"]["conclusion"] == "high_risk" for r in self.svc.report(batch["id"])["channels"]))
        self.upstream.missing_usage = True
        batch = await self.batch("2026-10-15T13:00:00")
        self.assertTrue(all(r["usage"]["conclusion"] == "unable_to_determine" for r in self.svc.report(batch["id"])["channels"]))

    async def test_cancel_inflight_unknown_never_replays_and_notifications_wait_due(self):
        entered = asyncio.Event()
        async def block(request, body):
            entered.set()
            await asyncio.Event().wait()
        self.upstream.block = block
        self.enable()
        self.now = stamp("2026-10-15T09:30:00")
        await self.svc.tick()
        await asyncio.wait_for(entered.wait(), 2)
        batch = self.svc.store.batches()[0]
        await self.svc.cancel(batch["id"])
        self.upstream.block = None
        await self.svc.tick()
        await self.svc.worker
        await self.svc.notification
        self.assertEqual(len(self.upstream.calls), 1)
        self.assertEqual(self.notifications, [])
        self.assertEqual(self.svc.report(batch["id"])["channels"][0]["performance"]["modeltrace"]["unknown"], 1)
        self.now = batch["report_at"]
        await self.svc.tick()
        await self.svc.notification
        self.assertEqual(len(self.notifications), 1)

    async def test_restart_mt_continues_unattempted_requests_and_keeps_unknown(self):
        entered = asyncio.Event()
        async def block(request, body):
            entered.set()
            await asyncio.Event().wait()
        self.upstream.block = block
        self.enable(notify=False)
        self.now = stamp("2026-10-15T09:30:00")
        await self.svc.tick()
        await asyncio.wait_for(entered.wait(), 2)
        await self.svc.stop()
        self.upstream.block = None
        await self.svc.tick()
        await self.svc.worker
        report = self.svc.report(self.svc.store.batches()[0]["id"])
        gpt = next(r for r in report["channels"] if r["family"] == "GPT")
        self.assertEqual(gpt["performance"]["modeltrace"]["unknown"], 1)
        self.assertEqual(gpt["performance"]["modeltrace"]["completed"], 2)
        self.assertEqual(len(self.upstream.calls), 10)
        self.assertEqual(gpt["modeltrace"]["label"], "无法判定")

    async def test_notification_unknown_explicit_retry_and_settled_retention(self):
        self.enable()
        batch = await self.batch()
        self.now = batch["report_at"]
        report = self.svc.report(batch["id"])
        self.svc.store.finish(batch["id"], report, ["Synthetic first", "Synthetic second"], self.now)
        first = self.svc.store.claim_message(self.now)
        self.svc.store.message_result(first, "sent")
        self.svc.store.claim_message(self.now)
        self.svc.store.recover_notifications()
        await self.svc.deliver()
        self.assertEqual(self.notifications, [])
        self.assertEqual(self.svc.store.prune(self.now + 1), 0)
        self.assertEqual(self.svc.store.retry_notifications(batch["id"]), 1)
        await self.svc.deliver()
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(self.svc.store.prune(self.now + 1), 1)
        self.assertEqual(len(self.tokens.store.list(administrative=True)), 2)

    async def test_public_api_and_exports_scrub_names_and_do_not_expose_webhook(self):
        self.add("Other key owner", "other", "synthetic-third-key", status="recorded")
        self.change(self.candidate, name="synthetic-third-key https://secret.invalid/hook")
        self.enable()
        batch = await self.batch()
        app = FastAPI()
        app.include_router(api.router)
        with patch.object(api, "get_service", return_value=self.svc):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://loopback") as client:
                for path in ("/meta", "/batches", "/batches/" + batch["id"], "/batches/" + batch["id"] + "/export", "/batches/" + batch["id"] + "/export?format=markdown"):
                    response = await client.get("/api/daily-checks" + path)
                    self.assertEqual(response.status_code, 200)
                    for secret in ("synthetic-reference", "synthetic-candidate", "synthetic-claude-candidate", "synthetic-third-key", "secret.invalid", "hook/synthetic"):
                        self.assertNotIn(secret, response.text)
                rejected = await client.post("/api/daily-checks/batches/" + batch["id"] + "/retry-notification", json={})
                self.assertEqual(rejected.status_code, 422)
        for job in self.tokens.store.list(administrative=True):
            self.assertNotIn("synthetic-third-key", json.dumps(job))
            self.assertNotIn("secret.invalid", json.dumps(job))

    async def test_binding_revision_is_checked_in_atomic_token_permit(self):
        self.profiles = self.profiles[:1]
        self.enable(notify=False)
        original = self.tokens.store.permit
        def drift(job_id, owner, side, index):
            binding = self.tokens.store.binding("gpt-6-astra", "responses")
            self.tokens.store.save_binding("gpt-6-astra", "responses", binding["snapshot"],
                                           {"minor_ratio": 1.2, "minor_difference": 200})
            return original(job_id, owner, side, index)
        with patch.object(self.tokens.store, "permit", side_effect=drift):
            batch = await self.batch()
        self.assertEqual(len(self.upstream.calls), 3)
        row = next(r for r in self.svc.report(batch["id"])["channels"] if r["family"] == "GPT")
        self.assertEqual(row["usage"]["conclusion"], "unable_to_determine")


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sender_explicit_success_response_size_and_http_rejection(self):
        async def response(data, status=200):
            return await send_text("https://open.feishu.cn/open-apis/bot/v2/hook/synthetic", "Synthetic",
                transport=httpx.MockTransport(lambda _: httpx.Response(status, json=data)))
        for data in ({"code": 0}, {"StatusCode": 0}):
            await response(data)
        for data in ({}, {"code": 1}, [0], {"code": False}, {"code": 0.0}, {"code": 0, "StatusCode": 1}, {"code": 0, "padding": "x" * 33000}):
            with self.assertRaises(ValueError):
                await response(data)
        with self.assertRaises(httpx.HTTPStatusError):
            await response({}, 429)
        with self.assertRaises(ValueError):
            await send_text("https://open.feishu.cn/open-apis/bot/v2/hook/synthetic", "字" * 7000)
        for value in ("http://127.0.0.1/hook", "https://open.feishu.cn.evil.invalid/open-apis/bot/v2/hook/synthetic", "https://open.feishu.cn/open-apis/bot/v2/hook/synthetic?secret=x"):
            with self.assertRaises(ValueError):
                validate_webhook(value)

    async def test_split_safe_text_and_add_duplicate_error_categories(self):
        performance = {"modeltrace": domain.performance([{"status": "timeout"}], 3),
                       "usage_candidate": domain.performance([{"status": "unknown", "transport_status": "timeout"}], 1)}
        row = {"channel_name": "Synthetic" * 10, "channel_id": 1, "family": "GPT", "model": "synthetic-model", "multiplier": .15,
               "modeltrace": {"label": "无法判定"}, "usage": {"label": "无法判定"}, "performance": performance}
        report = {"batch_id": "daily-synthetic", "report_at": stamp("2026-10-15T10:30:00"),
                  "coverage": {"rows": 300, "online_channels": 300, "fully_measured_rows": 0}, "channels": [row] * 300}
        parts = reporting.notification_parts(report)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(json.dumps({"msg_type": "text", "content": {"text": p}}, ensure_ascii=False).encode()) < 20000 for p in parts))
        self.assertIn("timeout 2", parts[0])


if __name__ == "__main__":
    unittest.main()
