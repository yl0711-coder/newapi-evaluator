"""Layered slot dependencies, persistent rotation and shared executor recovery."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from shared import registry as registry_module
from shared.registry import Registry
from features.integrity.strategies import get_strategy
from features.stability.app import storage, layered, integrity, scheduler
from features.stability.app.main import ScheduleInput, app
from tests.integrity_fixtures import production_binding


class ScheduledIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="layered-synthetic-")
        self.directory = Path(self.temp.name)
        self.old_registry, self.old_db = registry_module._registry, storage.DB_PATH
        storage.close()
        self.registry = Registry(self.directory)
        registry_module._registry = self.registry
        storage.DB_PATH = self.directory / "stability.db"
        self.channel = self.registry.save({"name": "Synthetic channel", "base_url": "https://synthetic.example/v1",
            "api_key": "synthetic-layered-credential", "multiplier": 1})
        production_binding(self.registry, self.channel["id"])
        self.targets = [{"registry_channel_id": self.channel["id"], "model": model, "protocol": "responses"}
                        for model in ("gpt-6-astra", "gpt-6.1-sol")]
        data = ScheduleInput(name="Synthetic layered", daily_times="09:30", targets=self.targets,
                             plan_version=layered.VERSION).model_dump()
        self.schedule_id = storage.save_schedule_targets(data, self.targets)
        self.schedule = storage.get_schedule(self.schedule_id)
        self.day = date(2026, 10, 8)
        self.zone = ZoneInfo("Asia/Shanghai")
        self.run_id = layered.create_day(self.schedule, self.day)

    async def asyncTearDown(self):
        await integrity.stop()
        storage.close()
        storage.DB_PATH, registry_module._registry = self.old_db, self.old_registry
        self.temp.cleanup()

    def row(self, slot):
        return next(s for s in layered.slots(self.run_id) if s["slot"] == slot)

    def finish_health(self, slot, now, valid=True):
        row = self.row(slot)
        layered.update_slot(row["slot_key"], "completed", summary={"planned": 1, "attempted": 1,
            "valid": int(valid), "invalid": int(not valid), "unknown": 0, "skipped": 0,
            "not_run": 0, "health_pass": valid, "observed_at": now,
            "health_coverage": {"model": row["model"], "protocol": row["protocol"]}})

    def settle_other_slots(self, preserve):
        for slot in layered.slots(self.run_id):
            if slot["slot"] not in preserve:
                layered.update_slot(slot["slot_key"], "skipped", reason="synthetic_prior_window")

    async def test_daily_planned_attempts_and_weekend_canary_axis(self):
        config = {**self.schedule["layered_config"], "registry_channel_ids": [1, 2, 3, 4, 5]}
        plan = {**self.schedule, "layered_config": config}
        weekday = layered.build_slots(plan, date(2026, 10, 5))
        weekend = layered.build_slots(plan, date(2026, 10, 10))
        self.assertEqual(sum(len(get_strategy(s["method"]).probes) for s in weekday), 230)
        self.assertEqual(sum(len(get_strategy(s["method"]).probes) for s in weekend), 35)
        self.assertEqual(sum(s["method"] == "canary" for s in weekday), 1)
        self.assertFalse(any(s["method"] == "canary" for s in weekend))
        self.assertEqual(self.row("canary")["deadline"], layered.epoch(date(2026, 10, 9), "08:55", self.zone))

    async def test_five_workday_rotation_persists_and_reuses_same_day(self):
        selections = []
        for day in (5, 6, 7, 8, 9):
            key = f"2026-10-{day:02d}"
            selected = layered.select_rotation(self.schedule_id, key, [1, 2, 3, 4, 5], day * 86400)
            selections.append(selected)
            self.assertEqual(layered.select_rotation(self.schedule_id, key, [5], day * 86400 + 1), selected)
        self.assertEqual(selections, [1, 2, 3, 4, 5])
        storage.close()
        self.assertEqual(layered.select_rotation(self.schedule_id, "2026-10-09", [1], 999999), 5)

    async def test_1530_health_dependency_cannot_race_and_failed_skips(self):
        now = layered.epoch(self.day, "15:36", self.zone)
        self.settle_other_slots({"health-2", "astra-1"})
        # The only ready slot is health, not Astra.
        self.assertEqual(integrity.select_ready(now)["method"], "health")
        self.finish_health("health-2", now, False)
        self.assertIsNone(integrity.select_ready(now))
        self.assertEqual(self.row("astra-1")["status"], "skipped")
        self.assertEqual(self.row("astra-1")["reason"], "health_failed_or_unmeasured")

    async def test_health_has_exact_model_coverage_and_expiry_adds_no_probe(self):
        now = layered.epoch(self.day, "15:36", self.zone)
        self.settle_other_slots({"health-2", "sol"})
        self.finish_health("health-2", now)
        ready = integrity.select_ready(now + 15 * 60)
        self.assertEqual(ready["slot"], "sol")
        self.assertEqual(self.row("health-2")["summary"]["health_coverage"]["model"], "gpt-6-astra")
        self.assertNotEqual(ready["model"], self.row("health-2")["model"])
        self.assertIsNone(integrity.select_ready(now + 61 * 60))
        self.assertEqual(self.row("sol")["reason"], "health_expired")
        self.assertEqual(len([s for s in layered.slots(self.run_id) if s["method"] == "health"]), 4)

    async def test_same_day_restart_does_not_duplicate_slots_or_make_legacy_pending(self):
        duplicate = layered.create_day(self.schedule, self.day)
        self.assertEqual(duplicate, self.run_id)
        self.assertEqual(len(layered.slots(self.run_id)), 9)
        self.assertEqual(storage.pending_runs(100), [])
        with patch.object(scheduler.storage, "list_schedules", return_value=[self.schedule]), patch.object(scheduler.storage, "create_run") as create:
            self.assertEqual(scheduler.ensure_due_runs(layered.epoch(self.day, "18:20", self.zone)), 0)
            create.assert_not_called()

    async def test_health_through_shared_executor_then_no_confirmed_resend(self):
        row = self.row("health-0")
        sent = []
        async def send(channel, probe, *, before_send):
            await before_send(); sent.append(probe["id"])
            return {"status": "completed", "valid": True, "text": "synthetic health response", "finish_reason": "stop",
                    "input_tokens_reported": 5, "output_tokens_reported": 3, "latency_ms": 2}
        # The test only moves its slot's window; no live transport is used.
        with storage.cursor() as cur:
            cur.execute("UPDATE layered_slots SET deadline=? WHERE slot_key=?", (time.time() + 60, row["slot_key"]))
        row = self.row("health-0")
        with patch.object(integrity, "send_probe", send):
            await integrity.execute_slot(row)
            completed = self.row("health-0")
            self.assertEqual(completed["status"], "completed", completed)
            self.assertEqual(completed["summary"]["attempted"], 1)
            await integrity.execute_slot(completed)
        self.assertEqual(sent, [get_strategy("health").probes[0].probe_id])
        self.assertTrue(self.row("health-0")["summary"]["health_pass"])
        self.assertNotIn("synthetic health response", json.dumps(storage.get_run(self.run_id)))

    async def test_not_run_failure_denominator_and_cancel_resume_preserve_original_window(self):
        self.settle_other_slots(set())
        integrity.refresh_run(self.run_id)
        summary = storage.get_run(self.run_id)["summary"]
        self.assertEqual(summary["planned"], 202)
        self.assertEqual(summary["valid"], 0)
        self.assertEqual(summary["skipped"], 202)
        self.assertEqual(storage.get_run(self.run_id)["status"], "incomplete")
        deadline = self.row("canary")["deadline"]
        layered.update_slot(self.row("canary")["slot_key"], "pending")
        integrity.cancel_run(self.run_id)
        self.assertEqual(self.row("canary")["status"], "cancelled")
        with patch.object(integrity.time, "time", return_value=deadline - 1):
            integrity.resume_run(self.run_id)
        self.assertEqual(self.row("canary")["deadline"], deadline)

    async def test_explicit_baseline_survives_history_retention_and_no_auto_promotion(self):
        row = self.row("canary")
        self.assertEqual(layered.list_baselines(), [])
        score = {"outcomes": {f"synthetic-{i}": i % 2 == 0 for i in range(192)}, "score": .5,
                 "conditions": {"provider": "synthetic", "model": "gpt-6-astra", "protocol": "responses", "parameters": {}, "budget": {}}}
        layered.update_slot(row["slot_key"], "completed", summary={"score": score, "attempted": 192})
        baseline = layered.lock_baseline(row["slot_key"], "Explicit synthetic baseline")
        storage.finish_run(self.run_id, "completed", {})
        storage.update_notification(self.run_id, "disabled")
        storage.prune_run_history(time.time() + 6 * 86400, 5)
        self.assertIsNone(storage.get_run(self.run_id))
        self.assertEqual(layered.list_baselines()[0]["id"], baseline)
        self.assertEqual(layered.list_baselines()[0]["score"]["score"], .5)

    async def test_old_report_json_and_export_unchanged(self):
        old = ScheduleInput(name="Legacy synthetic", daily_times="09:30", channel_ids=self.schedule["channel_ids"]).model_dump()
        old_id = storage.upsert_schedule(old)
        old_plan = storage.get_schedule(old_id)
        self.assertEqual(old_plan["plan_version"], "ins-v2")
        run_id = storage.create_run(old_plan, time.time(), "manual")
        storage.finish_run(run_id, "completed", {"verdict": "pass", "pass_rate": 1, "channels": []})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            detail = await client.get(f"/api/runs/{run_id}")
            exported = await client.get(f"/api/runs/{run_id}/export")
        self.assertEqual(detail.json(), exported.json())
        self.assertEqual(detail.json()["summary"]["pass_rate"], 1)
        self.assertEqual(detail.json()["results"], [])

    async def test_committed_job_and_missing_slot_binding_recover_without_resend(self):
        row = self.row("health-0")
        sent = []
        async def send(channel, probe, *, before_send):
            await before_send(); sent.append(probe["id"])
            return {"status": "completed", "valid": True, "text": "synthetic health",
                    "finish_reason": "stop", "input_tokens_reported": 1,
                    "output_tokens_reported": 1, "latency_ms": 1}
        with storage.cursor() as cur:
            cur.execute("UPDATE layered_slots SET deadline=? WHERE slot_key=?", (time.time()+60, row["slot_key"]))
        with patch.object(integrity, "send_probe", send):
            await integrity.execute_slot(self.row("health-0"))
            committed = self.row("health-0")
            original = integrity.store().job(committed["job_id"])
            # Simulate process death before the slot publishes its job ID/summary.
            with storage.cursor() as cur:
                cur.execute("UPDATE layered_slots SET job_id=NULL,status='pending',summary_json='{}' WHERE slot_key=?", (row["slot_key"],))
            await integrity.execute_slot(self.row("health-0"))
            restored = self.row("health-0")
            self.assertEqual(restored["job_id"], original["job_id"])
            self.assertEqual(restored["status"], "completed")
            self.assertTrue(restored["summary"]["health_pass"])
            self.assertEqual(integrity.store().job(restored["job_id"])["deadline"], original["deadline"])
            # Also cover death after durable terminal commit, before slot publication.
            layered.update_slot(row["slot_key"], "running", summary={})
            await integrity.execute_slot(self.row("health-0"))
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.row("health-0")["summary"]["attempted"], 1)

    async def test_production_snapshot_change_rejects_frozen_plan_without_send(self):
        production_binding(self.registry, self.channel["id"], version="synthetic-new-version")
        async def forbidden(*args, **kwargs):
            self.fail("changed production binding sent a request")
        with patch.object(integrity, "send_probe", forbidden):
            await integrity.execute_slot(self.row("health-0"))
        row = self.row("health-0")
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["reason"], "target_unavailable")
        self.assertEqual(integrity.store().list_jobs(), [])

    async def test_committed_canary_republishes_before_health_window_or_target_gates(self):
        self.settle_other_slots({"health-3", "canary"})
        now = time.time()
        self.finish_health("health-3", now)
        row = self.row("canary")
        layered.update_slot(row["slot_key"], "pending", channel_id=self.channel["id"], dependency=self.row("health-3")["slot_key"])
        with storage.cursor() as cur:
            cur.execute("UPDATE layered_slots SET due=?,deadline=? WHERE slot_key=?", (now-1,now+12*3600,row["slot_key"]))
        expected = {p.probe_id:str(p.expected) for p in get_strategy("canary").probes}
        sent = []
        async def send(channel, probe, *, before_send):
            await before_send();sent.append(probe["id"])
            return {"status":"completed", "text":expected[probe["id"]], "input_tokens_reported":1, "output_tokens_reported":1}
        with patch.object(integrity, "send_probe", send):
            await integrity.execute_slot(self.row("canary"))
        original = integrity.store().job(self.row("canary")["job_id"])
        self.assertEqual(original["consumed"]["requests"], 192)
        current = self.registry.get(self.channel["id"])
        self.registry.save({**current, "api_key":"synthetic-replaced"}, current["id"], current["version"])
        for bound in (True,False):
            with self.subTest(bound=bound):
                with storage.cursor() as cur:
                    cur.execute("UPDATE layered_slots SET status=?,summary_json='{}',job_id=? WHERE slot_key=?",
                        ("running" if bound else "pending",original["job_id"] if bound else None,row["slot_key"]))
                async def forbidden(*args, **kwargs):
                    self.fail("committed canary was resent")
                with patch.dict(os.environ, {"EVAL_INTEGRITY_EXECUTOR":"live"}), patch.object(integrity,"send_probe",forbidden):
                    await integrity.tick(now+13*3600)
                restored = self.row("canary")
                self.assertEqual(restored["job_id"], original["job_id"])
                self.assertEqual(restored["status"], "completed")
                self.assertEqual(restored["summary"]["attempted"], 192)
                self.assertEqual(restored["summary"]["valid"], 192)
                self.assertEqual(restored["summary"]["observed_at"], original["updated_at"])
                self.assertEqual(storage.get_run(self.run_id)["summary"]["attempted"], 193)
        self.assertEqual(len(sent), 192)

    async def test_incident_requires_consecutive_valid_conditions_and_cooldown(self):
        config = self.schedule["layered_config"]
        score = {"valid_answers": 1, "source_verdict": "SUSPICIOUS", "prediction": "synthetic-other",
                 "conditions": {"protocol": "responses", "effort": "low", "manifest": "synthetic-v1"}}
        first, second = self.row("astra-0"), self.row("astra-1")
        layered.update_slot(first["slot_key"], "completed", summary={"score": score})
        self.assertIsNone(layered.record_incident({**first, "status": "completed"}, score, config))
        layered.update_slot(second["slot_key"], "completed", summary={"score": score})
        incident = layered.record_incident({**second, "status": "completed"}, score, config)
        self.assertTrue(incident)
        self.assertEqual(layered.record_incident({**second, "status": "completed"}, score, config), incident)
        # A missing/invalid scheduled slot breaks continuity rather than disappearing.
        layered.update_slot(first["slot_key"], "skipped", summary={})
        self.assertIsNone(layered.record_incident({**second, "status": "completed"}, score, config))
        layered.update_slot(first["slot_key"], "completed", summary={"score": {**score, "conditions": {"effort": "high"}}})
        self.assertIsNone(layered.record_incident({**second, "status": "completed"}, score, config))


if __name__ == "__main__":
    unittest.main()
