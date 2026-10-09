"""Synthetic Registry-to-plan atomic save; no discovery or upstream requests."""
import asyncio
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
from shared import registry as registry_module
from shared.registry import Registry
from features.model_coverage.catalog import Catalog
from features.model_coverage import service
from features.stability.app import storage, scheduler, integrity
from features.stability.app.main import app, ScheduleInput


class ScheduleCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scheduled-candidate-synthetic-")
        self.directory = Path(self.temp.name)
        self.old_registry, self.old_db = registry_module._registry, storage.DB_PATH
        storage.close()
        self.registry = Registry(self.directory)
        registry_module._registry = self.registry
        storage.DB_PATH = self.directory / "stability.db"
        self.catalog = Catalog(self.registry)
        self.channel = self.registry.save({"name": "Synthetic new online", "base_url": "https://synthetic.example/v1",
            "api_key": "synthetic-schedule-credential", "multiplier": 1, "status": "online"})
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self):
        await integrity.stop()
        await self.client.aclose()
        storage.close()
        storage.DB_PATH, registry_module._registry = self.old_db, self.old_registry
        self.temp.cleanup()

    def payload(self, name="Synthetic layered plan"):
        models = self.catalog.models()
        targets = [{"registry_channel_id": self.channel["id"], "model_id": model["id"],
                    "model": model["model"], "protocol": model["protocol"]}
                   for model in models if model["model"] in {"gpt-6-astra", "gpt-6.1-sol"}]
        return {"name": name, "daily_times": "09:30", "plan_version": "layered-integrity-v1", "targets": targets}

    async def test_fresh_online_zero_target_is_selectable_then_saved_atomically(self):
        self.assertEqual(storage.list_channels(), [])
        self.assertEqual(storage.list_schedules(), [])
        response = await self.client.get("/api/candidates")
        self.assertEqual(response.status_code, 200)
        row = response.json()["candidates"][0]
        self.assertEqual((row["status"], row["credential_status"], row["discovery_status"]), ("online", "available", "unknown"))
        astra = next(m for m in row["models"] if m["model"] == "gpt-6-astra")
        self.assertTrue(astra["eligible"])
        self.assertEqual(astra["target_ids"], [])
        self.assertEqual(storage.list_channels(), [])
        saved = await self.client.post("/api/schedules", json=self.payload())
        self.assertEqual(saved.status_code, 200, saved.text)
        plan = storage.get_schedule(saved.json()["id"])
        self.assertEqual(len(plan["channel_ids"]), 2)
        self.assertEqual(plan["layered_config"]["registry_channel_ids"], [self.channel["id"]])
        self.assertNotIn("synthetic-schedule-credential", json.dumps(response.json()))

    async def test_disabled_target_is_not_silently_revived(self):
        storage.upsert_channel({"name": "Disabled synthetic", "registry_channel_id": self.channel["id"],
                                "model": "gpt-6-astra", "protocol": "responses", "enabled": False})
        response = await self.client.post("/api/schedules", json=self.payload())
        self.assertEqual(response.status_code, 400)
        self.assertIn("target_disabled", response.json()["detail"])
        self.assertEqual(storage.list_schedules(), [])
        self.assertEqual(len(storage.list_channels()), 1)
        self.assertFalse(storage.list_channels()[0]["enabled"])

    async def test_credentials_missing_unreadable_do_not_hide_entire_coverage(self):
        # A previous discovered list must not survive an unusable credential.
        fingerprint = self.registry.connection_fingerprint(self.registry.resolve(self.channel["id"]))
        fetch_id = self.catalog.begin_fetch(self.channel["id"], "openai")
        self.catalog.finish_fetch(self.channel["id"], fetch_id, fingerprint, ["gpt-6-astra"])
        for value, expected in [("", "missing"), ("synthetic-corrupt-ciphertext", "unreadable")]:
            with self.registry.connect() as conn:
                conn.execute("UPDATE channels SET key_enc=? WHERE id=?", (value, self.channel["id"]))
            response = await self.client.get("/api/candidates")
            self.assertEqual(response.status_code, 200)
            row = response.json()["candidates"][0]
            self.assertEqual(row["credential_status"], expected)
            self.assertFalse(any(m["eligible"] for m in row["models"]))
            from features.model_coverage.api import router
            from fastapi import FastAPI
            coverage_app = FastAPI(); coverage_app.state.stability_available = True
            coverage_app.include_router(router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=coverage_app), base_url="http://testserver") as coverage_client:
                covered = await coverage_client.get("/api/model-coverage")
            self.assertEqual(covered.status_code, 200)
            for item in covered.json()["channels"][0]["models"]:
                self.assertNotEqual(item["availability"], "listed")
                self.assertEqual(item["measurement"]["samples"], 0)
            denied = await self.client.post("/api/schedules", json=self.payload())
            self.assertEqual(denied.status_code, 400)
            self.assertEqual(storage.list_channels(), [])

    async def test_duplicate_plan_failure_rolls_back_new_targets(self):
        data = ScheduleInput(**self.payload()).model_dump()
        original = storage.save_schedule_targets(data, data["targets"])
        other = self.registry.save({"name": "Second synthetic", "base_url": "https://other.synthetic.example/v1", "api_key": "other-synthetic-credential", "multiplier": 1, "status":"online"})
        changed = [{**t, "registry_channel_id": other["id"]} for t in data["targets"]]
        with self.assertRaises(Exception):
            storage.save_schedule_targets(data, changed)
        self.assertEqual(len(storage.list_channels()), 2)
        self.assertEqual(storage.get_schedule(original)["layered_config"]["registry_channel_ids"], [self.channel["id"]])

    async def test_concurrent_two_plans_reuse_one_target_per_model(self):
        first = ScheduleInput(**self.payload("Synthetic A")).model_dump()
        second = ScheduleInput(**self.payload("Synthetic B")).model_dump()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(storage.save_schedule_targets, data, data["targets"]) for data in (first, second)]
            ids = [f.result(timeout=10) for f in futures]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(len(storage.list_channels()), 2)
        self.assertEqual(storage.get_schedule(ids[0])["channel_ids"], storage.get_schedule(ids[1])["channel_ids"])

    async def test_illegal_protocol_rejected_before_any_target_write(self):
        body = self.payload(); body["targets"][0]["protocol"] = "openai"
        response = await self.client.post("/api/schedules", json=body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(storage.list_channels(), [])

    async def test_user_online_and_enable_required_without_external_binding(self):
        from features.stability.app import layered
        for changes in ({"status":"recorded"}, {"status":"online","enabled":False}):
            self.registry.save({**self.registry.get(self.channel["id"]), **changes, "api_key":""}, self.channel["id"], self.registry.get(self.channel["id"])["version"])
            row=layered.candidates()[0]
            self.assertFalse(any(m["eligible"] for m in row["models"]))
            self.assertEqual((await self.client.post("/api/schedules",json=self.payload())).status_code,400)
            self.assertEqual(storage.list_channels(),[])
            self.assertEqual(self.registry.get(self.channel["id"])["status"],changes["status"])
        self.registry.save({**self.registry.get(self.channel["id"]), "status":"online", "enabled":True, "api_key":""}, self.channel["id"], self.registry.get(self.channel["id"])["version"])
        self.assertEqual((await self.client.post("/api/schedules",json=self.payload())).status_code,200)
        with self.registry.connect() as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='production_coverage_snapshots'").fetchone())

    async def test_exact_mapping_and_connection_frozen_at_plan_save(self):
        from features.stability.app import layered
        astra=next(m for m in self.catalog.models() if m["model"]=="gpt-6-astra")
        self.catalog.bind(self.channel["id"],astra["id"],"synthetic-astra-alias","responses")
        saved=await self.client.post("/api/schedules",json=self.payload())
        self.assertEqual(saved.status_code,200,saved.text)
        plan=storage.get_schedule(saved.json()["id"])
        slot={"registry_channel_id":self.channel["id"],"model":"gpt-6-astra","protocol":"responses"}
        snapshot={**plan,"targets":storage.list_channels(ids=plan["channel_ids"])}
        resolved=integrity.resolve_target(slot,snapshot)
        self.assertEqual(resolved.channel["model"],"synthetic-astra-alias")
        self.catalog.bind(self.channel["id"],astra["id"],"synthetic-astra-alias-2","responses")
        self.assertIsNone(integrity.resolve_target(slot,snapshot))


if __name__ == "__main__":
    unittest.main()
