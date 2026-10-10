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
from features.stability.app import storage, layered, integrity, scheduler, timetable
from features.stability.app.main import ScheduleInput, app


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
            "api_key": "synthetic-layered-credential", "multiplier": 1, "status":"online"})
        self.targets = [{"registry_channel_id": self.channel["id"], "model": model, "protocol": "responses"}
                        for model in ("gpt-6-astra", "gpt-6.1-sol")]
        data = ScheduleInput.model_construct(name="Synthetic layered", daily_times="09:30", targets=self.targets,
                             plan_version=layered.VERSION, layered_config=layered.validate_config({})).model_dump(warnings=False)
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

    async def test_same_day_restart_does_not_duplicate_slots_or_make_legacy_pending(self):
        duplicate = layered.create_day(self.schedule, self.day)
        self.assertEqual(duplicate, self.run_id)
        self.assertEqual(len(layered.slots(self.run_id)), 9)
        self.assertEqual(storage.pending_runs(100), [])
        with patch.object(scheduler.storage, "list_schedules", return_value=[self.schedule]), patch.object(scheduler.storage, "create_run") as create:
            self.assertEqual(scheduler.ensure_due_runs(layered.epoch(self.day, "18:20", self.zone)), 0)
            create.assert_not_called()

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
            with self.assertRaisesRegex(ValueError, "旧定时"):
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

    def historical_job(self, slot, *, completed=False):
        from features.integrity.execution import build_requests, resolve_registry_target
        from features.integrity.durable import default_pricing
        from features.integrity.scoring import project_observation
        manifest = get_strategy(slot["method"])
        requests = build_requests(manifest, self.schedule["layered_config"])
        target = resolve_registry_target(self.registry, self.channel["id"], slot["model"], slot["protocol"], require_online=True)
        ledger = integrity.store()
        job = ledger.enqueue(idempotency_key=slot["slot_key"], target_snapshot=target.snapshot,
            strategy={"strategy_id": manifest.strategy_id, "manifest_hash": manifest.manifest_hash},
            requests=requests, limits={"max_requests":len(requests),"max_input_tokens":sum(r.input_tokens_reserved for r in requests),"max_output_tokens":sum(r.output_tokens_reserved for r in requests)},
            deadline=time.time()+3600, pricing=default_pricing(slot["model"]), principal="workbench")
        claimed=ledger.claim(job_id=job["job_id"],owner="synthetic-legacy")
        session=ledger.session(job["job_id"],claimed["owner"])
        for request in requests if completed else requests[:1]:
            attempt=session.reserve(request,target)
            if completed:
                raw={"status":"completed","text":str(request.probe.get("expected", "OK"))}
                session.complete(request,attempt,project_observation(manifest,request.probe["id"],raw))
        if completed:
            session.finish("completed","",[])
        return ledger.job(job["job_id"]), session, requests[0], target

    async def test_retirement_preserves_unknown_and_fences_old_lease(self):
        row=self.row("health-0")
        job, session, request, target=self.historical_job(row)
        layered.update_slot(row["slot_key"],"running")
        with patch.object(integrity,"send_probe",side_effect=AssertionError("legacy request sent")):
            timetable.retire_legacy()
        retired=self.row("health-0")
        self.assertEqual((retired["status"],retired["summary"]["unknown"]),("cancelled",1))
        self.assertEqual(retired["job_id"],job["job_id"])
        self.assertEqual(integrity.store().job(job["job_id"])["consumed"]["unknown_requests"],1)
        self.assertFalse(session.complete(request,{"attempt_id":"late-result"},{"valid":True}))
        self.assertEqual(storage.get_run(self.run_id)["summary"]["unknown"],1)
        self.assertFalse(storage.has_active_run(self.schedule_id))

    async def test_retirement_rebuilds_committed_canary_before_cancelling(self):
        row=self.row("canary")
        row["registry_channel_id"]=self.channel["id"]
        layered.update_slot(row["slot_key"],"running",channel_id=self.channel["id"])
        job, _, _, _=self.historical_job(row,completed=True)
        with patch.object(integrity,"send_probe",side_effect=AssertionError("committed Canary resent")):
            timetable.retire_legacy()
        restored=self.row("canary")
        self.assertEqual((restored["status"],restored["summary"]["attempted"]),("completed",192))
        self.assertEqual(restored["job_id"],job["job_id"])
        self.assertEqual(restored["summary"]["observed_at"],job["updated_at"])

    async def test_retirement_recovers_parent_after_all_slots_settled(self):
        self.settle_other_slots(set())
        with storage.cursor() as cur:
            cur.execute("UPDATE runs SET status='running' WHERE id=?",(self.run_id,))
        timetable.retire_legacy()
        self.assertEqual(storage.get_run(self.run_id)["status"],"incomplete")
        self.assertFalse(storage.has_active_run(self.schedule_id))
        self.assertIsNone(integrity.select_ready(time.time()))

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
