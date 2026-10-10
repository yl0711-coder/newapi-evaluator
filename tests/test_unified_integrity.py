"""Synthetic first-use channels and independent bounded three-method samples."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.registry import Registry, RegistryError, Conflict
from features.integrity.unified import UnifiedService
from features.integrity.strategies import get_strategy, load_bank


class UnifiedIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="unified-synthetic-")
        self.registry = Registry(Path(self.temp.name))
        self.channel = self.registry.save({"name":"Synthetic zero history", "base_url":"https://synthetic.example/v1",
            "api_key":"synthetic-unified-credential", "multiplier":1, "status":"recorded"})
        self.svc = UnifiedService(self.registry)
        self.env = patch.dict(os.environ, {"EVAL_INTEGRITY_EXECUTOR":"live"});self.env.start()
        self.sent = []

    async def asyncTearDown(self):
        self.env.stop();self.temp.cleanup()

    def submit(self, key="synthetic-unified", **changes):
        return self.svc.submit(registry_channel_id=self.channel["id"], idempotency_key=key, confirm_live=True, **changes)

    async def send(self, channel, probe, *, before_send):
        await before_send();self.sent.append(probe["id"])
        text = "OK" if probe["max_tokens"] == 32 else json.dumps([[42]*35]*9) if probe["max_tokens"] == 2500 else " ".join(["42"]*331)
        return {"status":"completed", "text":text, "input_tokens_reported":10, "output_tokens_reported":10}

    async def test_one_first_use_task_has_three_independent_results_and_eight_attempts(self):
        with self.registry.connect() as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name IN ('production_coverage_snapshots','monitor_channel_identities')").fetchone())
        task=self.submit();self.assertEqual(task["limits"]["max_requests"],8)
        self.assertEqual(self.registry.get(self.channel["id"])["status"], "recorded")
        self.assertFalse(set(task["target_snapshot"]) & {"channel_identity", "inventory_version", "production_hash"})
        self.assertTrue(self.svc.choices()[0]["available"])
        self.assertEqual(self.submit()["job_id"],task["job_id"])
        await self.svc.run_pending(self.send)
        final=self.svc.get(task["job_id"])
        self.assertEqual((final["status"],len(self.sent)),("completed",8))
        self.assertEqual([r["attempted"] for r in final["reports"]],[1,3,3])
        self.assertEqual([r["valid"] for r in final["reports"]],[1,3,3])
        self.assertEqual(len(set(r["request_id"] for r in final["results"])),8)
        self.assertTrue(all(r["metadata_status"]=="unavailable" and r["calibration_status"]=="unvalidated" for r in final["reports"]))
        self.assertEqual(len(load_bank(get_strategy("nerfed-api"))["models"]),16)
        self.assertNotEqual(get_strategy("nerfed-api").manifest_hash,get_strategy("modeltrace").manifest_hash)
        self.assertEqual(get_strategy("nerfed").execution_mode,"import_only")
        self.assertEqual(await self.svc.run_pending(self.send),0)
        with self.assertRaises(Conflict):self.submit(model="gpt-6.1-sol")

    async def test_health_failure_skips_all_long_probes_and_does_not_hide_denominator(self):
        task=self.submit()
        async def bad(channel,probe,*,before_send):
            await before_send();self.sent.append(probe["id"])
            return {"status":"timeout", "text":""}
        await self.svc.run_pending(bad)
        final=self.svc.get(task["job_id"])
        self.assertEqual(len(self.sent),1)
        self.assertEqual([r["not_run"] for r in final["reports"]],[1,3,3])
        self.assertTrue(all(r["status"]=="skipped" for r in final["reports"]))
        self.assertEqual(len(final["skipped"]),7)

    async def test_high_reported_usage_keeps_all_eight_independent_samples(self):
        task = self.submit(key="high-reported-usage")
        async def send(channel, probe, *, before_send):
            raw = await self.send(channel, probe, before_send=before_send)
            raw.update(input_tokens_reported=4390, output_tokens_reported=probe["max_tokens"] + 1)
            return raw
        await self.svc.run_pending(send)
        final = self.svc.get(task["job_id"])
        self.assertEqual((final["status"], len(self.sent)), ("completed", 8))
        self.assertEqual([r["attempted"] for r in final["reports"]], [1, 3, 3])
        self.assertEqual([r["valid"] for r in final["reports"]], [1, 3, 3])
        self.assertTrue(final["reservation_exceeded"])
        self.assertEqual(final["consumed"]["unknown_requests"], 0)
        self.assertEqual(await self.svc.run_pending(send), 0)

    async def test_method_invalid_keeps_other_methods_and_missing_usage_does_not_stop(self):
        task=self.submit()
        original=self.send
        async def mixed(channel,probe,*,before_send):
            raw=await original(channel,probe,before_send=before_send)
            if len(self.sent) in [3,4,5]:raw["text"]="invalid synthetic numbers"
            raw.pop("input_tokens_reported");raw.pop("output_tokens_reported")
            return raw
        await self.svc.run_pending(mixed)
        final=self.svc.get(task["job_id"])
        self.assertEqual([r["valid"] for r in final["reports"]],[1,0,3])
        self.assertEqual(final["reports"][1]["invalid"],3)
        self.assertEqual(final["consumed"]["requests"],8)
        self.assertIsNone(final["fees"]["estimated_usd"])
        self.assertIsNone(final["fees"]["stopping_limit"])

    async def test_cancel_unknown_resume_history_never_resends_permitted_request(self):
        task=self.submit();started=asyncio.Event()
        async def delayed(channel,probe,*,before_send):
            if probe["max_tokens"]==2048 and not started.is_set():
                await before_send();self.sent.append(probe["id"]);started.set();await asyncio.Event().wait()
            return await self.send(channel,probe,before_send=before_send)
        consumer=asyncio.create_task(self.svc.run_pending(delayed))
        await asyncio.wait_for(started.wait(),5)
        self.svc.cancel(task["job_id"],principal="workbench")
        await asyncio.wait_for(consumer,5)
        cancelled=self.svc.get(task["job_id"])
        self.assertEqual(cancelled["status"],"cancelled")
        self.assertEqual(cancelled["consumed"]["unknown_requests"],1)
        self.svc.resume(task["job_id"],principal="workbench")
        await self.svc.run_pending(self.send)
        final=self.svc.get(task["job_id"])
        self.assertEqual((final["consumed"]["requests"],len(self.sent)),(8,8))
        self.assertEqual(final["reports"][1]["valid"],2)
        self.assertEqual(final["reports"][1]["unknown"],1)
        self.assertEqual(final["reports"][2]["valid"],3)
        self.assertEqual(self.svc.list()[0]["job_id"],task["job_id"])

    async def test_sol_unsupported_modeltrace_and_connection_drift_rejected(self):
        task=self.submit(model="gpt-6.1-sol")
        self.assertEqual(task["reports"][1]["status"],"unsupported")
        self.assertEqual(task["limits"]["max_requests"],5)
        await self.svc.run_pending(self.send)
        self.assertEqual(len(self.sent),5)
        task=self.submit(key="changed-source")
        self.registry.save({**self.registry.get(self.channel["id"]), "base_url":"https://changed.synthetic.example/v1", "api_key":""}, self.channel["id"], self.registry.get(self.channel["id"])["version"])
        await self.svc.run_pending(self.send)
        self.assertEqual(self.svc.get(task["job_id"])["status"],"rejected")
        self.assertEqual(len(self.sent),5)
        self.assertTrue(self.svc.choices()[0]["available"])

    async def test_noisy_and_oversized_projections_never_abort_other_methods(self):
        for case in ("noise", "rows", "numbers", "huge_integer", "deep_json"):
            with self.subTest(case=case):
                task = self.submit(key="projection-"+case)
                start = len(self.sent)
                async def send(channel, probe, *, before_send):
                    raw = await self.send(channel, probe, before_send=before_send)
                    if probe["max_tokens"] == 2500:
                        raw["text"] = {"noise": json.dumps([[42]*35+["x"]*20]*9),
                            "rows": json.dumps([[]]*5000), "numbers": json.dumps([[42]*35]*9),
                            "huge_integer": "9"*5000, "deep_json": "["*2000+"0"+"]"*2000}[case]
                    if probe["max_tokens"] == 2048 and case in {"numbers", "huge_integer"}:
                        raw["text"] = " ".join(["42"]*5000) if case == "numbers" else "9"*5000
                    return raw
                await self.svc.run_pending(send)
                final = self.svc.get(task["job_id"])
                self.assertEqual(final["status"], "completed")
                self.assertEqual(len(self.sent)-start, 8)
                self.assertEqual([r["attempted"] for r in final["reports"]], [1,3,3])
                self.assertEqual(final["consumed"]["unknown_requests"], 0)
                expected = [1,3,3] if case == "noise" else [1,0,0] if case == "numbers" else [0,0,0] if case == "huge_integer" else [0,3,3]
                self.assertEqual([r["valid"] for r in final["reports"]], expected)

    async def test_invalid_registry_connections_and_protocol_cannot_submit(self):
        from features.model_coverage.catalog import Catalog
        with self.assertRaises(RegistryError): self.submit(protocol="openai")
        with self.assertRaises(RegistryError): self.svc.submit(registry_channel_id=True, idempotency_key="bad-id", confirm_live=True)
        with self.assertRaises(RegistryError): self.submit(target_snapshot={})
        for field, value in (("enabled",0),("key_enc",""),("key_enc","synthetic-unreadable"),("base_url","https://synthetic.example/v1?invalid=1")):
            with self.subTest(field=field,value=value):
                with self.registry.connect() as conn:
                    old = conn.execute(f"SELECT {field} FROM channels WHERE id=?", (self.channel["id"],)).fetchone()[0]
                    conn.execute(f"UPDATE channels SET {field}=? WHERE id=?", (value,self.channel["id"]))
                self.assertFalse(self.svc.choices()[0]["available"])
                with self.assertRaises(RegistryError): self.submit(key="invalid-"+field)
                with self.registry.connect() as conn:
                    conn.execute(f"UPDATE channels SET {field}=? WHERE id=?", (old,self.channel["id"]))
        task=self.submit(key="mapping-change")
        catalog=Catalog(self.registry);model=next(m for m in catalog.models() if m["model"]=="gpt-6-astra")
        catalog.bind(self.channel["id"],model["id"],"synthetic-astra-alias","responses")
        await self.svc.run_pending(self.send)
        self.assertEqual(self.svc.get(task["job_id"])["status"],"rejected")
        self.assertEqual(self.sent,[])
