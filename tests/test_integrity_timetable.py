"""Synthetic daily timetables, reservation races and conservative channel/time reports."""
import asyncio
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from shared import registry as registry_module
from shared.registry import Registry
from features.integrity.strategies import get_strategy
from features.integrity.execution import build_requests, resolve_registry_target
from features.integrity.scoring import score_strategy, project_observation
from features.stability.app import storage, timetable, timetable_executor as executor, integrity, layered
from features.stability.app.main import ScheduleInput, app
from features.stability.app.timetable_report import report, method_result


class TimetableContractTests(unittest.TestCase):
    def plan(self, config=None, zone="Asia/Shanghai", channels=(1,)):
        return {"id": 7, "timezone": zone, "layered_config": {**timetable.validate_config(config or {}), "registry_channel_ids": list(channels)}}

    def test_default_all_channels_all_days_exact_graph(self):
        for day in (date(2026, 10, 8), date(2026, 10, 10)):
            plan = self.plan(channels=range(1, 6))
            rows = timetable.build_slots(plan, day)
            self.assertEqual(sum(r["method"] == "modeltrace" for r in rows), 720)
            self.assertEqual(sum(r["method"] == "canary" for r in rows), 120)
            self.assertEqual(sum(r["method"] == "health" for r in rows), 720)
            preview = timetable.preview(plan["layered_config"], plan["timezone"], day)
            self.assertEqual(preview["max_requests"], 25920)
            self.assertGreater(preview["max_output_tokens"], 1000000)

    def test_irregular_sets_are_independent_complete_occurrences(self):
        plan = self.plan({"canary_times": ["09:20", "11:00", "16:40"], "modeltrace_times": ["09:10", "09:50", "14:30"]}, channels=(1, 2))
        rows = timetable.build_slots(plan, date(2026, 10, 8))
        self.assertEqual(sum(r["method"] == "canary" for r in rows), 6)
        self.assertEqual(len({r["slot_key"] for r in rows}), 24)
        self.assertEqual(timetable.preview(plan["layered_config"], plan["timezone"])["max_requests"], 1182)
        changed = self.plan({"canary_times": [], "modeltrace_times": ["09:10", "09:50", "14:30"]}, channels=(1, 2))
        old_mt = {r["slot_key"] for r in rows if r["method"] == "modeltrace"}
        self.assertEqual(old_mt, {r["slot_key"] for r in timetable.build_slots(changed, date(2026, 10, 8)) if r["method"] == "modeltrace"})

    def test_empty_and_invalid_times(self):
        plan = self.plan({"canary_times": [], "modeltrace_times": []})
        self.assertEqual(timetable.build_slots(plan, date(2026, 10, 8)), [])
        self.assertEqual(timetable.preview(plan["layered_config"], plan["timezone"])["max_requests"], 0)
        for value in (["9:20"], ["09:21"], ["24:00"], ["09:20", "09:20"], "09:20", [True]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                timetable.validate_config({"canary_times": value})

    def test_dst_gap_fold_and_cross_midnight_windows(self):
        plan = self.plan({"canary_times": ["01:30", "02:30", "23:50"], "modeltrace_times": []}, zone="America/New_York")
        spring = timetable.build_slots(plan, date(2026, 3, 8))
        self.assertEqual([r["clock"] for r in spring if r["method"] == "canary"], ["01:30", "23:50"])
        fall = timetable.build_slots(plan, date(2026, 11, 1))
        first = next(r for r in fall if r["clock"] == "01:30" and r["method"] == "canary")
        self.assertEqual(first["due"], 1793511000)
        overnight = next(r for r in fall if r["clock"] == "23:50" and r["method"] == "canary")
        self.assertEqual(overnight["deadline"] - overnight["due"], 3600)
        self.assertEqual(overnight["budget_date"], "2026-11-01")


class TimetableExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="timetable-synthetic-")
        self.root = Path(self.temp.name)
        self.old_registry, self.old_db = registry_module._registry, storage.DB_PATH
        storage.close()
        self.registry = Registry(self.root)
        registry_module._registry = self.registry
        storage.DB_PATH = self.root / "stability.db"
        self.channel = self.registry.save({"name": "Synthetic timetable", "base_url": "https://synthetic.example/v1", "api_key": "synthetic-timetable-credential", "multiplier": 2.5, "status": "online"})
        self.targets = [{"registry_channel_id": self.channel["id"], "model": "gpt-6-astra", "protocol": "responses"}]
        self.day = date(2026, 10, 8)
        self.start = timetable.local_epoch(self.day, "00:00", ZoneInfo("Asia/Shanghai"))
        self.now = self.start + 10 * 3600
        self.data = ScheduleInput(name="Synthetic daily", daily_times="00:00", timezone="Asia/Shanghai", targets=self.targets,
                                  layered_config={"canary_times": ["10:00", "10:10"], "modeltrace_times": ["10:00", "10:10"]}).model_dump()
        with patch("time.time", return_value=self.start):
            self.id = storage.save_schedule_targets(self.data, self.targets)
        self.plan = storage.get_schedule(self.id)
        self.run = timetable.reconcile(self.plan, self.day, now=self.start)
        self.sent = []

    async def asyncTearDown(self):
        await integrity.stop()
        storage.close()
        storage.DB_PATH, registry_module._registry = self.old_db, self.old_registry
        self.temp.cleanup()

    def row(self, method, clock="10:00"):
        return next(r for r in timetable.slots(self.run) if r["method"] == method and r["clock"] == clock)

    async def send(self, channel, probe, *, before_send):
        await before_send()
        self.sent.append(probe["id"])
        return {"status": "completed", "valid": True, "text": "synthetic invalid answer", "input_tokens_reported": 0, "output_tokens_reported": 0, "latency_ms": 1}

    async def execute(self, row):
        with patch("time.time", return_value=self.now), patch.object(executor, "send_probe", self.send):
            await executor.execute_probe(row)

    def save(self, config, now=None, enabled=True):
        data = {**self.data, "id": self.id, "enabled": enabled, "layered_config": config}
        with patch("time.time", return_value=now or self.now):
            storage.save_schedule_targets(ScheduleInput(**data).model_dump(), self.targets)
        self.plan = storage.get_schedule(self.id)
        timetable.reconcile(self.plan, self.day, now=now or self.now)

    async def test_shared_health_priority_and_one_probe_dispatch(self):
        with patch("time.time", return_value=self.now):
            self.assertEqual(executor.select_ready(self.now)["method"], "health")
        await self.execute(self.row("health"))
        with patch("time.time", return_value=self.now):
            self.assertEqual(executor.select_ready(self.now)["method"], "modeltrace")
        await self.execute(self.row("modeltrace"))
        self.assertEqual(self.row("modeltrace")["status"], "pending")
        self.assertEqual(executor.store().job(self.row("modeltrace")["job_id"])["consumed"]["requests"], 1)
        for _ in range(2):
            await self.execute(self.row("modeltrace"))
        self.assertEqual(self.row("modeltrace")["summary"]["attempted"], 3)
        await self.execute(self.row("canary"))
        self.assertEqual(executor.store().job(self.row("canary")["job_id"])["consumed"]["requests"], 1)
        self.assertEqual(len(self.sent), 5)

    async def test_save_between_resolution_and_permit_blocks_only_removed_method(self):
        async def race(channel, probe, *, before_send):
            self.save({"canary_times": [], "modeltrace_times": ["10:00", "10:10"]}, now=self.now + 1)
            await before_send()
            self.sent.append(probe["id"])
            return {}
        with patch("time.time", return_value=self.now), patch.object(executor, "send_probe", race):
            await executor.execute_probe(self.row("canary"))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.row("canary")["status"], "cancelled")
        self.assertEqual(self.row("modeltrace")["status"], "pending")
        self.assertEqual(self.row("health")["status"], "pending")

    async def test_cancel_after_permit_preserves_attempt_and_no_second_job(self):
        await self.execute(self.row("health"))
        for _ in range(3):
            await self.execute(self.row("modeltrace"))
        self.sent.clear()
        async def race(channel, probe, *, before_send):
            await before_send()
            self.sent.append(probe["id"])
            self.save({"canary_times": [], "modeltrace_times": ["10:00", "10:10"]}, now=self.now + 1)
            return {"status": "completed", "valid": True, "text": "synthetic answer", "input_tokens_reported": 0, "output_tokens_reported": 0}
        with patch("time.time", return_value=self.now), patch.object(executor, "send_probe", race):
            await executor.execute_probe(self.row("canary"))
        first = self.row("canary")
        self.assertEqual(first["summary"]["attempted"], 1)
        self.assertEqual(first["status"], "cancelled")
        self.save({"canary_times": ["10:00", "10:10"], "modeltrace_times": ["10:00", "10:10"]}, now=self.now + 2)
        await self.execute(self.row("canary"))
        self.assertEqual(self.row("canary")["job_id"], first["job_id"])
        self.assertEqual(len(self.sent), 1)

    async def test_future_zero_attempt_cancel_reselect_and_past_no_catchup(self):
        original = self.row("canary", "10:10")["slot_key"]
        self.save({"canary_times": [], "modeltrace_times": ["10:00", "10:10"]}, now=self.now + 1)
        self.assertEqual(self.row("canary", "10:10")["status"], "cancelled")
        self.save({"canary_times": ["09:50", "10:10"], "modeltrace_times": ["10:00", "10:10"]}, now=self.now + 2)
        self.assertEqual(self.row("canary", "10:10")["slot_key"], original)
        self.assertEqual(self.row("canary", "10:10")["status"], "pending")
        self.assertEqual(self.row("canary", "09:50")["status"], "skipped")
        self.assertEqual(self.row("canary", "09:50")["reason"], "not_scheduled_before_save")

    async def test_plan_budget_identity_and_old_unknown_reservations_survive_edit(self):
        await self.execute(self.row("health"))
        before = executor.store().daily_budget(budget_key=timetable.budget_key(self.id), budget_date=str(self.day), timezone=self.plan["timezone"])
        self.assertEqual(before["attempted_requests"], 1)
        self.save({"canary_times": [], "modeltrace_times": ["10:00", "10:10"]}, now=self.now + 1)
        await self.execute(self.row("modeltrace"))
        after = executor.store().daily_budget(budget_key=timetable.budget_key(self.id), budget_date=str(self.day), timezone=self.plan["timezone"])
        self.assertEqual(after["attempted_requests"], 2)
        self.assertEqual(after["max_requests"], 8)
        self.assertEqual(after["budget_key"], before["budget_key"])

    async def test_name_multiplier_frozen_at_first_permit_and_export(self):
        for _ in range(3):
            await self.execute(self.row("modeltrace"))
        updated = {**self.channel, "name": "Edited synthetic", "multiplier": 9, "api_key": ""}
        self.registry.save(updated, channel_id=self.channel["id"], version=self.channel["version"])
        rows = report(run_id=self.run)["rows"]
        old = next(r for r in rows if r["scheduled_at_utc"] == self.now)
        self.assertEqual((old["channel_name"], old["channel_multiplier"]), ("Synthetic timetable", 2.5))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            detail = await client.get(f"/api/runs/{self.run}")
            exported = await client.get(f"/api/runs/{self.run}/export")
        self.assertEqual(detail.json(), exported.json())
        self.assertNotIn("synthetic-timetable-credential", json.dumps(exported.json()))

    async def test_deadline_and_restart_terminal_reconstruction_no_resend(self):
        await self.execute(self.row("health"))
        completed = self.row("health")
        with self.registry.connect() as conn:
            conn.execute("UPDATE integrity_occurrences SET status='pending',summary_json='{}' WHERE slot_key=?", (completed["slot_key"],))
        with patch("time.time", return_value=self.now + 3601):
            executor.settle_pending(self.now + 3601)
        self.assertEqual(self.row("health")["status"], "completed")
        self.assertEqual(self.row("modeltrace")["status"], "expired")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(storage.pending_runs(100), [])

    async def test_orphan_run_recovery_and_mirror_save_recovery(self):
        tomorrow = date(2026, 10, 9)
        midnight = timetable.local_epoch(tomorrow, "00:00", ZoneInfo(self.plan["timezone"]))
        orphan = storage.create_run(self.plan, midnight, source="timetable-v2")
        self.assertEqual(timetable.reconcile(self.plan, tomorrow, now=midnight), orphan)
        self.assertEqual(len(timetable.slots(orphan)), 6)
        with storage.cursor() as cur:
            updated = self.plan["updated_at"] + 1
            cur.execute("UPDATE schedules SET updated_at=? WHERE id=?", (updated, self.id))
        with self.registry.connect() as conn:
            conn.execute("DELETE FROM integrity_timetable_controls WHERE schedule_id=?", (self.id,))
        fresh = storage.get_schedule(self.id)
        self.assertEqual(timetable.reconcile(fresh, self.day, now=self.start), self.run)
        with self.registry.connect() as conn:
            control = conn.execute("SELECT * FROM integrity_timetable_controls WHERE schedule_id=?", (self.id,)).fetchone()
        self.assertEqual(control["revision"], updated)
        self.assertEqual(json.loads(control["config_json"]), fresh["layered_config"])

    async def test_timezone_edit_cancels_old_utc_occurrence_at_permit(self):
        old = self.row("health")
        data = {**self.data, "id": self.id, "timezone": "UTC"}
        with patch("time.time", return_value=self.now - 1):
            storage.save_schedule_targets(ScheduleInput(**data).model_dump(), self.targets)
        fresh = storage.get_schedule(self.id)
        timetable.reconcile(fresh, self.day, now=self.now - 1)
        self.assertEqual(timetable.get_slot(old["slot_key"])["status"], "cancelled")
        await self.execute(old)
        self.assertEqual(self.sent, [])
        current = [s for s in timetable.slots(self.run) if s["method"] == "health" and s["status"] == "pending"]
        self.assertEqual(len(current), 2)
        self.assertTrue(all(s["due"] != old["due"] for s in current))

    async def test_queued_canary_reselects_after_real_shared_capacity(self):
        from features.integrity.transport import send_capacity
        await self.execute(self.row("health"))
        for _ in range(3):
            await self.execute(self.row("modeltrace"))
        self.sent.clear()
        entered = asyncio.Event()
        async def one(channel, probe, *, before_send):
            await before_send()
            self.sent.append(probe["id"])
            entered.set()
            raise asyncio.CancelledError()
        clock = [self.now + 599]
        with patch("time.time", side_effect=lambda: clock[0]), patch.object(executor, "send_probe_with_capacity", one):
            async with send_capacity():
                task = asyncio.create_task(executor.dispatch())
                await asyncio.sleep(.01)
                # No low-priority occurrence has been claimed while waiting.
                self.assertEqual(self.row("canary")["status"], "pending")
                clock[0] = self.now + 601
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.sent, ["health-1"])
        self.assertEqual(self.row("canary")["job_id"], None)

    async def test_unknown_restart_preserves_attempt_without_resend(self):
        async def interrupted(channel, probe, *, before_send):
            await before_send()
            self.sent.append(probe["id"])
            raise asyncio.CancelledError()
        with patch("time.time", return_value=self.now), patch.object(executor, "send_probe", interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await executor.execute_probe(self.row("health"))
        slot = self.row("health")
        self.assertEqual(executor.store().job(slot["job_id"])["consumed"]["unknown_requests"], 1)
        with self.registry.connect() as conn:
            conn.execute("UPDATE integrity_occurrences SET status='pending',summary_json='{}' WHERE slot_key=?", (slot["slot_key"],))
        executor.settle_pending(self.now)
        await self.execute(self.row("health"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.row("health")["summary"]["unknown"], 1)

    async def test_mirror_recovery_after_pause_delete_and_version_change(self):
        with storage.cursor() as cur:
            cur.execute("UPDATE schedules SET enabled=0,updated_at=updated_at+1 WHERE id=?", (self.id,))
        timetable.synchronize_controls()
        self.assertEqual(self.row("health")["status"], "cancelled")
        with self.registry.connect() as conn:
            conn.execute("UPDATE integrity_timetable_controls SET enabled=1 WHERE schedule_id=?", (self.id,))
        with storage.cursor() as cur:
            cur.execute("DELETE FROM schedules WHERE id=?", (self.id,))
        timetable.synchronize_controls()
        with self.registry.connect() as conn:
            self.assertEqual(conn.execute("SELECT enabled FROM integrity_timetable_controls WHERE schedule_id=?", (self.id,)).fetchone()[0], 0)

    async def test_pause_committed_before_api_reconcile_recovers_matching_mirror(self):
        executor.refresh_run(self.run)
        self.assertEqual(storage.get_run(self.run)["status"], "running")
        await self.execute(self.row("health"))
        completed = self.row("health")
        data = {**self.data, "id": self.id, "enabled": False}
        # Use the real save path, then simulate process loss before API reconcile.
        with patch("time.time", return_value=self.now + 1):
            storage.save_schedule_targets(ScheduleInput(**data).model_dump(), self.targets)
        self.assertEqual(self.row("modeltrace")["status"], "pending")
        with self.registry.connect() as conn:
            mirror = conn.execute("SELECT revision,enabled FROM integrity_timetable_controls WHERE schedule_id=?", (self.id,)).fetchone()
        self.assertEqual(mirror["revision"], storage.get_schedule(self.id)["updated_at"])
        self.assertEqual(mirror["enabled"], 0)
        timetable.synchronize_controls()
        timetable.synchronize_controls()
        self.assertTrue(all(s["status"] == "cancelled" for s in timetable.slots(self.run) if s["slot_key"] != completed["slot_key"]))
        self.assertEqual(self.row("health")["summary"], completed["summary"])
        self.assertEqual(self.row("health")["status"], "completed")
        parent = storage.get_run(self.run)
        self.assertEqual(parent["status"], "incomplete")
        self.assertEqual(parent["summary"]["planned"], 392)
        self.assertEqual(parent["summary"]["attempted"], 1)
        self.assertEqual(parent["summary"]["not_run"], 391)
        await self.execute(self.row("modeltrace"))
        self.assertEqual(len(self.sent), 1)

    async def test_deleted_schedule_sync_finishes_parent(self):
        executor.refresh_run(self.run)
        storage.delete_schedule(self.id)
        timetable.synchronize_controls()
        self.assertEqual(storage.get_run(self.run)["status"], "incomplete")
        self.assertEqual(storage.get_run(self.run)["summary"]["not_run"], 392)

    async def test_changed_version_sync_finishes_parent(self):
        executor.refresh_run(self.run)
        with storage.cursor() as cur:
            cur.execute("UPDATE schedules SET plan_version='ins-v2',updated_at=updated_at+1 WHERE id=?", (self.id,))
        timetable.synchronize_controls()
        self.assertEqual(storage.get_run(self.run)["status"], "incomplete")
        self.assertEqual(storage.get_run(self.run)["summary"]["not_run"], 392)

    async def test_retention_archives_report_before_delete_and_preserves_evidence(self):
        await self.execute(self.row("health"))
        job = executor.store().job(self.row("health")["job_id"])
        score = {"outcomes": {f"synthetic-{i}": True for i in range(192)}, "correct": 192,
                 "conditions": {"model": "gpt-6-astra", "protocol": "responses", "parameters": {}, "budget": {}}}
        for slot in timetable.slots(self.run):
            timetable.update_slot(slot["slot_key"], "completed", summary={"score": score if slot["method"] == "canary" else {}, "attempted": timetable.request_limits(slot["method"])["max_requests"], "not_run": 0})
        # First baseline operation is POST, without GET/list or a live tick.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post("/api/baselines", json={"slot_key": self.row("canary")["slot_key"], "label": "Synthetic preserved baseline"})
        self.assertEqual(response.status_code, 200)
        baseline = response.json()["id"]
        with patch("time.time", return_value=self.now):
            storage.finish_run(self.run, "completed", {})
            storage.update_notification(self.run, "disabled")
        original_cursor = storage.cursor

        @contextmanager
        def interrupted_cursor():
            with original_cursor() as cur:
                class InterruptedDelete:
                    def execute(self, sql, parameters=()):
                        if sql.startswith("DELETE FROM runs"):
                            raise RuntimeError("synthetic interruption after archive commit")
                        return cur.execute(sql, parameters)
                yield InterruptedDelete()

        with patch.object(storage, "cursor", interrupted_cursor), self.assertRaisesRegex(RuntimeError, "after archive commit"):
            storage.prune_run_history(self.now + 6 * 86400, 5)
        self.assertIsNotNone(storage.get_run(self.run))
        self.assertEqual(report(run_id=self.run)["total"], 0)
        self.assertEqual(storage.prune_run_history(self.now + 6 * 86400, 5), 1)
        self.assertIsNone(storage.get_run(self.run))
        self.assertEqual(storage.prune_run_history(self.now + 6 * 86400, 5), 0)
        self.assertEqual(executor.store().job(job["job_id"])["consumed"], job["consumed"])
        self.assertEqual(layered.get_baseline(baseline), score)
        self.assertEqual(len(timetable.slots(self.run)), 6)
        self.assertIsNone(timetable.reconcile(self.plan, self.day, now=self.now + 6 * 86400))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            self.assertEqual((await client.get(f"/api/runs/{self.run}")).status_code, 404)
            self.assertEqual((await client.get(f"/api/runs/{self.run}/export")).status_code, 404)

    async def test_retention_keeps_active_and_unknown_days(self):
        async def interrupted(channel, probe, *, before_send):
            await before_send()
            raise asyncio.CancelledError()
        with patch("time.time", return_value=self.now), patch.object(executor, "send_probe", interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await executor.execute_probe(self.row("health"))
        job = executor.store().job(self.row("health")["job_id"])
        self.assertEqual(job["consumed"]["unknown_requests"], 1)
        for status in ("running", "incomplete"):
            with storage.cursor() as cur:
                cur.execute("UPDATE runs SET status=?,created_at=?,finished_at=?,notify_status='disabled' WHERE id=?", (status, self.now, self.now, self.run))
            self.assertEqual(storage.prune_run_history(self.now + 6 * 86400, 5), 0)
            self.assertIsNotNone(storage.get_run(self.run))
            self.assertTrue(report(run_id=self.run)["rows"])
            self.assertEqual(executor.store().job(job["job_id"])["consumed"], job["consumed"])

    async def test_same_slot_method_snapshots_and_legacy_missing_multiplier(self):
        await self.execute(self.row("health"))
        for _ in range(3):
            await self.execute(self.row("modeltrace"))
        self.registry.save({**self.channel, "name": "Renamed synthetic", "multiplier": 9, "api_key": ""}, channel_id=self.channel["id"], version=self.channel["version"])
        await self.execute(self.row("canary"))
        rows = report(run_id=self.run)["rows"]
        row = next(r for r in rows if r["scheduled_at_utc"] == self.now)
        self.assertEqual(row["modeltrace"]["channel_multiplier"], 2.5)
        self.assertEqual(row["canary"]["channel_multiplier"], 9)
        self.assertIn("Renamed synthetic", row["channel_name"])
        with self.registry.connect() as conn:
            slot = self.row("modeltrace")
            slot["snapshot"]["layered_config"].pop("channel_multipliers", None)
            conn.execute("UPDATE integrity_occurrences SET snapshot_json=? WHERE slot_key=?", (storage.dumps(slot["snapshot"]), slot["slot_key"]))
        row = next(r for r in report(run_id=self.run)["rows"] if r["scheduled_at_utc"] == self.now)
        self.assertIsNone(row["modeltrace"]["channel_multiplier"])
        self.assertEqual(row["canary"]["channel_multiplier"], 9)

    async def test_empty_api_plan_reopens_without_defaults_or_requests(self):
        payload = {**self.data, "id": self.id, "layered_config": {"canary_times": [], "modeltrace_times": []}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            with patch("time.time", return_value=self.now):
                saved = await client.post("/api/schedules", json=payload)
                preview = await client.post("/api/timetable/preview", json=payload)
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(preview.json()["max_requests"], 0)
            plans = (await client.get("/api/schedules")).json()["schedules"]
        self.assertEqual(plans[0]["layered_config"]["canary_times"], [])
        self.assertEqual(plans[0]["layered_config"]["modeltrace_times"], [])
        self.assertEqual(self.sent, [])


    async def test_progress_uses_committed_attempts_and_future_is_not_anomaly(self):
        await self.execute(self.row("health"))
        await self.execute(self.row("modeltrace"))
        slot = self.row("modeltrace")
        self.assertEqual(slot["summary"]["attempted"], 1)
        self.assertEqual(slot["summary"]["valid"], 0)  # valid HTTP, invalid numeric answer
        self.assertEqual(slot["summary"]["request_errors"], 0)
        self.assertEqual(slot["summary"]["not_run"], 2)
        with patch("time.time", return_value=self.now):
            rows = report(run_id=self.run)["rows"]
            active = next(r for r in rows if r["scheduled_at_utc"] == self.now)
            future = next(r for r in rows if r["scheduled_at_utc"] > self.now)
            self.assertEqual(active["modeltrace"]["state"], "sampling")
            self.assertEqual(future["state"], "waiting")
            self.assertFalse(future["anomaly"])
            self.assertEqual(report(run_id=self.run, anomalies_only=True)["displayed"], 0)
        self.assertEqual(storage.get_run(self.run)["summary"]["attempted"], 2)

    async def test_timezone_change_keeps_original_daily_budget_scope(self):
        await self.execute(self.row("health"))
        original = executor.store().daily_budget(budget_key=timetable.budget_key(self.id), budget_date=str(self.day), timezone="Asia/Shanghai")
        with patch("time.time", return_value=self.now - 1):
            storage.save_schedule_targets(ScheduleInput(**{**self.data, "id": self.id, "timezone": "UTC"}).model_dump(), self.targets)
        timetable.reconcile(storage.get_schedule(self.id), self.day, now=self.now - 1)
        future = next(s for s in timetable.slots(self.run) if s["method"] == "health" and s["status"] == "pending")
        self.now = future["due"]
        await self.execute(future)
        after = executor.store().daily_budget(budget_key=timetable.budget_key(self.id), budget_date=str(self.day), timezone="Asia/Shanghai")
        self.assertEqual(original["attempted_requests"], 1)
        self.assertEqual(after["attempted_requests"], 2)
        self.assertEqual(executor.store().job(timetable.get_slot(future["slot_key"])["job_id"])["timezone"], "Asia/Shanghai")


    async def test_each_channel_uses_its_explicit_same_provider_baseline(self):
        second = self.registry.save({"name": "Synthetic second", "base_url": "https://second.synthetic.example/v1", "api_key": "synthetic-second-credential", "multiplier": 1, "status": "online"})
        targets = self.targets + [{"registry_channel_id": second["id"], "model": "gpt-6-astra", "protocol": "responses"}]
        data = {**self.data, "id": self.id, "targets": targets, "layered_config": {"canary_times": ["10:10"], "modeltrace_times": []}}
        with patch("time.time", return_value=self.now):
            storage.save_schedule_targets(ScheduleInput(**data).model_dump(), targets)
        timetable.reconcile(storage.get_schedule(self.id), self.day, now=self.now)
        baselines = {}
        manifest = get_strategy("canary")
        outputs = [project_observation(manifest, p.probe_id, {"status": "completed", "valid": True, "text": str(p.expected)}) for p in manifest.probes]
        layered.init_tables()
        for slot in [r for r in timetable.slots(self.run) if r["method"] == "canary" and r["clock"] == "10:10"]:
            resolved = resolve_registry_target(self.registry, slot["registry_channel_id"], "gpt-6-astra", "responses", require_online=True)
            conditions = integrity.conditions(slot, manifest, slot["snapshot"]["layered_config"], resolved.snapshot)
            score = score_strategy(manifest, outputs, expected_model="gpt-6-astra", conditions=conditions)
            with storage.cursor() as cur:
                cur.execute("INSERT INTO integrity_baselines(source_slot_key,label,score_json,selected_at) VALUES(?,?,?,?)", (slot["slot_key"]+":synthetic-reference", "Synthetic trusted", storage.dumps(score), self.now))
                baselines[str(slot["registry_channel_id"])] = cur.lastrowid
        data["layered_config"]["baseline_ids"] = baselines
        with patch("time.time", return_value=self.now + 1):
            storage.save_schedule_targets(ScheduleInput(**data).model_dump(), targets)
        timetable.reconcile(storage.get_schedule(self.id), self.day, now=self.now + 1)
        fake_job = {"results": outputs, "consumed": {"requests": 192, "unknown_requests": 0}, "status": "completed", "updated_at": self.now}
        for slot in [r for r in timetable.slots(self.run) if r["method"] == "canary" and r["clock"] == "10:10"]:
            target = slot["target_snapshot"]
            cfg = slot["snapshot"]["layered_config"]
            summary = integrity.slot_summary(slot, fake_job, manifest, {**cfg, "baseline_id": cfg["baseline_ids"][str(slot["registry_channel_id"])]}, target)
            self.assertEqual(summary["score"]["status"], "no_detected_degradation")
            self.assertEqual(summary["baseline_id"], baselines[str(slot["registry_channel_id"])])
            wrong = next(v for k, v in baselines.items() if k != str(slot["registry_channel_id"]))
            self.assertEqual(integrity.slot_summary(slot, fake_job, manifest, {**cfg, "baseline_id": wrong}, target)["score"]["status"], "invalid_comparison")

    async def test_canary_channels_rotate_at_probe_boundaries(self):
        second = self.registry.save({"name": "Synthetic fairness", "base_url": "https://fairness.synthetic.example/v1", "api_key": "synthetic-fairness-credential", "multiplier": 1, "status": "online"})
        targets = self.targets + [{"registry_channel_id": second["id"], "model": "gpt-6-astra", "protocol": "responses"}]
        data = {**self.data, "id": self.id, "targets": targets, "layered_config": {"canary_times": ["10:10"], "modeltrace_times": []}}
        with patch("time.time", return_value=self.now):
            storage.save_schedule_targets(ScheduleInput(**data).model_dump(), targets)
        timetable.reconcile(storage.get_schedule(self.id), self.day, now=self.now)
        self.now += 600
        rows = timetable.slots(self.run)
        for row in rows:
            if row["method"] == "health" and row["clock"] == "10:10":
                await self.execute(row)
        order = []
        for _ in range(4):
            with patch("time.time", return_value=self.now):
                row = executor.select_ready(self.now)
            order.append(row["registry_channel_id"])
            await self.execute(row)
            self.now += .01
        self.assertEqual(order, [self.channel["id"], second["id"], self.channel["id"], second["id"]])


    async def test_pause_delete_save_controls_block_at_reserve(self):
        self.save({"canary_times": ["10:00"], "modeltrace_times": ["10:00"]}, now=self.now - 1, enabled=False)
        await self.execute(self.row("health"))
        self.assertEqual(self.sent, [])
        storage.delete_schedule(self.id)
        self.assertEqual(len(timetable.slots(self.run)), 6)
        self.assertTrue(report(run_id=self.run)["rows"])


class TimetableReportTests(unittest.TestCase):
    def slot(self, score, errors=0):
        return {"method": "canary", "status": "completed", "reason": "", "slot_key": "synthetic",
                "summary": {"planned": 192, "attempted": 192, "not_run": 0, "unknown": 0, "request_errors": errors,
                            "baseline_id": 1, "score": {"correct": 182, "outcomes": {str(i): True for i in range(192)}, **score}}}

    def test_local_degradation_transport_errors_and_missing_baseline(self):
        family = self.slot({"status": "no_detected_degradation", "comparison": {"families": [{"multiplicity_adjusted_status": "degraded"}]}})
        self.assertEqual(method_result(family)["state"], "degraded")
        self.assertIn("局部", method_result(family)["label"])
        self.assertEqual(method_result(self.slot({"status": "degraded"}, errors=2))["state"], "review")
        self.assertEqual(method_result(self.slot({"status": "invalid_comparison"}))["state"], "reference_missing")
        current = self.slot({"status": "current_only"})
        current["summary"]["baseline_id"] = None
        self.assertEqual(method_result(current)["label"], "182/192 · 参照不足")

    def test_inconclusive_requires_review_and_baseline_map_validation(self):
        self.assertEqual(method_result(self.slot({"status": "inconclusive"}))["state"], "review")
        config = timetable.validate_config({"baseline_ids": {"1": 3, "2": 4}})
        self.assertEqual(config["baseline_ids"], {"1": 3, "2": 4})
        for value in ({"1": True}, {"01": 1}, {"1": 0}, []):
            with self.assertRaises(ValueError):
                timetable.validate_config({"baseline_ids": value})

    def test_missing_method_and_incomplete_never_normal(self):
        self.assertEqual(method_result(None)["label"], "未安排")
        slot = self.slot({"status": "degraded"})
        slot["summary"]["unknown"] = 1
        self.assertEqual(method_result(slot)["state"], "incomplete")
