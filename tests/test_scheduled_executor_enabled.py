"""Saved schedules run through the normal lifespan with a self-authored HTTP Mock."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx

from shared import registry as registry_module
from shared.registry import Registry
from features.integrity import service
from features.stability.app import config, egress, integrity, layered, scheduler, storage, timetable
from features.stability.app.main import ScheduleInput
from workbench import create_app


class ScheduledExecutorEnabledTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scheduled-executor-synthetic-")
        self.directory = Path(self.temp.name)
        self.old_registry, self.old_db = registry_module._registry, storage.DB_PATH
        storage.close()
        self.registry = Registry(self.directory)
        registry_module._registry = self.registry
        storage.DB_PATH = self.directory / "stability.db"
        self.requests = []
        self.handlers = set()
        self.server = await asyncio.start_server(self.upstream, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.channel = self.registry.save({
            "name": "Synthetic scheduled channel", "base_url": f"http://127.0.0.1:{port}/v1",
            "api_key": "synthetic-scheduled-credential", "multiplier": 0.12, "status": "online"})
        self.day = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        self.midnight = timetable.local_epoch(self.day, "00:00", ZoneInfo("Asia/Shanghai"))
        self.clock = self.midnight
        self.patches = ExitStack()
        self.patches.enter_context(patch.object(config, "DATA_DIR", self.directory))
        self.patches.enter_context(patch.object(egress, "EGRESS_ALLOWLIST", ("127.0.0.1",)))
        self.patches.enter_context(patch.object(scheduler, "POLL_SECONDS", 0.005))
        self.patches.enter_context(patch("time.time", side_effect=lambda: self.clock))
        self.patches.enter_context(patch.dict(os.environ, {
            "PLATFORM_USERNAME": "", "PLATFORM_PASSWORD": "",
            "PLATFORM_EGRESS_ALLOWLIST": "127.0.0.1", "EVAL_MONITOR_EXECUTOR": "off"}))

    async def asyncTearDown(self):
        await scheduler.stop()
        await service.stop_executor()
        self.server.close()
        await self.server.wait_closed()
        for task in self.handlers:
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)
        self.patches.close()
        storage.close()
        storage.DB_PATH, registry_module._registry = self.old_db, self.old_registry
        self.temp.cleanup()

    async def upstream(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            headers = dict(line.split(b":", 1) for line in header.split(b"\r\n")[1:] if b":" in line)
            body = json.loads(await reader.readexactly(int(headers[b"Content-Length"])))
            self.requests.append(body["max_output_tokens"])
            # Fixed synthetic answers retain the real transport and scoring paths.
            answer = "OK" if body["max_output_tokens"] == 32 else (
                json.dumps([1 + (i * 17) % 355 for i in range(331)])
                if body["max_output_tokens"] == 2048 else "0")
            response = {"status": "completed", "model": "gpt-6-astra",
                        "output": [{"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": answer}]}],
                        "usage": {"input_tokens": 0, "output_tokens": 0}}
            encoded = json.dumps(response).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                         + f"Content-Length: {len(encoded)}\r\n\r\n".encode() + encoded)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.handlers.discard(task)

    async def wait_for(self, predicate):
        # Event-loop time is monotonic; only the product scheduling clock is controlled.
        async def wait():
            while not predicate():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(wait(), 15)

    def payload(self, *, canary=True, enabled=True):
        return {"name": "Synthetic automatic sampling", "daily_times": "00:00",
                "timezone": "Asia/Shanghai", "enabled": enabled,
                "targets": [{"registry_channel_id": self.channel["id"],
                             "model": "gpt-6-astra", "protocol": "responses"}],
                "layered_config": {"canary_times": ["00:10"] if canary else [],
                                   "modeltrace_times": ["00:10"]}}

    async def verify_automatic_sampling(self, executor_mode):
        with patch.dict(os.environ):
            if executor_mode is None:
                os.environ.pop("EVAL_INTEGRITY_EXECUTOR", None)
            else:
                os.environ["EVAL_INTEGRITY_EXECUTOR"] = executor_mode
            app = create_app("stability")
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                    saved = await client.post("/stability/api/schedules", json=self.payload())
                    self.assertEqual(saved.status_code, 200)
                    run_id = storage.list_runs()[0]["id"]
                    await self.wait_for(lambda: scheduler.status()["last_tick_at"] == self.clock)
                    self.assertEqual(self.requests, [])
                    root_health = (await client.get("/api/health")).json()
                    stability_health = (await client.get("/stability/api/health")).json()
                    for state in (root_health, stability_health):
                        self.assertEqual(state["status"], "ok")
                        self.assertTrue(state["scheduled_integrity_executor"]["enabled"])
                        self.assertTrue(state["scheduled_integrity_executor"]["running"])
                    self.assertFalse(root_health["integrity_executor"]["enabled"])
                    self.clock += 600
                    await self.wait_for(lambda: all(s["status"] in timetable.TERMINAL for s in timetable.slots(run_id)))
                    methods = {s["method"]: s for s in timetable.slots(run_id)}
                    for method, count in (("health", 1), ("modeltrace", 3), ("canary", 192)):
                        self.assertEqual(methods[method]["status"], "completed")
                        self.assertEqual(methods[method]["summary"]["attempted"], count)
                        self.assertEqual(methods[method]["summary"]["valid"], count)
                    self.assertEqual(len(self.requests), 196)
                    report = (await client.get("/stability/api/timetable/report", params={"date": str(self.day)})).json()
                    self.assertEqual(len(report["rows"]), 1)
                    row = report["rows"][0]
                    self.assertEqual(row["registry_channel_id"], self.channel["id"])
                    self.assertNotIn("待采样", json.dumps(row, ensure_ascii=False))
            self.assertFalse(integrity.executor_status()["running"])

    async def test_unset_manual_switch_runs_scheduled_mt_and_full_canary(self):
        await self.verify_automatic_sampling(None)

    async def test_explicit_off_manual_switch_runs_scheduled_mt_and_full_canary(self):
        await self.verify_automatic_sampling("off")

    async def test_pausing_saved_schedule_prevents_scheduled_requests(self):
        with patch.dict(os.environ, {"EVAL_INTEGRITY_EXECUTOR": "off"}):
            app = create_app("stability")
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                    payload = self.payload()
                    saved = await client.post("/stability/api/schedules", json=payload)
                    self.assertEqual(saved.status_code, 200)
                    run_id = storage.list_runs()[0]["id"]
                    self.clock += 1
                    paused = await client.post("/stability/api/schedules", json={**payload, "id": saved.json()["id"], "enabled": False})
                    self.assertEqual(paused.status_code, 200)
                    self.clock += 600
                    await self.wait_for(lambda: scheduler.status()["last_tick_at"] == self.clock)
                    self.assertEqual(self.requests, [])
                    self.assertTrue(all(s["status"] == "cancelled" for s in timetable.slots(run_id)))


    def v1_plan(self, name):
        targets = [{"registry_channel_id": self.channel["id"], "model": model, "protocol": "responses"}
                   for model in ("gpt-6-astra", "gpt-6.1-sol")]
        data = ScheduleInput(name=name, daily_times="09:30", timezone="Asia/Shanghai",
                             plan_version=layered.VERSION, targets=targets).model_dump()
        schedule_id = storage.save_schedule_targets(data, targets)
        schedule = storage.get_schedule(schedule_id)
        run_id = layered.create_day(schedule, self.day)
        return data, targets, schedule, run_id

    async def test_v1_pause_and_delete_cancel_existing_pending_slots(self):
        for action in ("pause", "delete"):
            with self.subTest(action=action):
                data, targets, schedule, run_id = self.v1_plan("Synthetic v1 " + action)
                if action == "pause":
                    storage.save_schedule_targets({**data, "id": schedule["id"], "enabled": False}, targets)
                else:
                    storage.delete_schedule(schedule["id"])
                self.clock = self.midnight + 9.5 * 3600
                self.assertIsNone(integrity.select_ready(self.clock))
                rows = layered.slots(run_id)
                self.assertTrue(rows)
                self.assertTrue(all(row["status"] == "cancelled" for row in rows))
                self.assertEqual(self.requests, [])

    async def test_v1_permit_rechecks_pause_and_delete_after_target_resolution(self):
        for action in ("pause", "delete"):
            with self.subTest(action=action):
                data, targets, schedule, run_id = self.v1_plan("Synthetic v1 permit " + action)
                slot = next(row for row in layered.slots(run_id) if row["slot"] == "health-0")
                original = integrity.resolve_target
                resolutions = 0

                def resolve_then_change(*args):
                    nonlocal resolutions
                    resolved = original(*args)
                    resolutions += 1
                    # A second process can commit a pause after the final fresh
                    # target read, immediately before reserve obtains its lock.
                    if resolutions == 3:
                        if action == "pause":
                            storage.save_schedule_targets({**data, "id": schedule["id"], "enabled": False}, targets)
                        else:
                            storage.delete_schedule(schedule["id"])
                    return resolved

                with patch.object(integrity, "resolve_target", side_effect=resolve_then_change):
                    await integrity.execute_slot(slot)
                finished = next(row for row in layered.slots(run_id) if row["slot_key"] == slot["slot_key"])
                self.assertEqual(finished["status"], "cancelled")
                self.assertEqual(finished["reason"], "schedule_paused_or_deleted")
                self.assertEqual(integrity.store().job(finished["job_id"])["consumed"]["requests"], 0)
                self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
