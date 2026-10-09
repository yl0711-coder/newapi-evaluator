"""Authorized synthetic review packages; no public reference answers or upstream calls."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from shared.registry import Registry, RegistryError, Conflict
from features.integrity import service
from features.integrity.reference import reference_hash, reference_conditions, runtime_hash, SCORER_VERSION
from features.integrity.hlwy import hlwy_reference_hash, PROMPT_HASH, ALGORITHM_VERSION


def synthetic_reference(method="kbf", count=4, *, self_test=True, protocol="responses", thinking="low"):
    package = {"schema": f"integrity-{method}-reference/v1",
               "authorization": {"authorized": True, "basis": "self_authored", "source": "synthetic_regression_fixture"},
               "reference_model": "gpt-6-astra", "provider": "synthetic_provider", "endpoint": "https://example.invalid/v1",
               "protocol": protocol, "parameters": {"system_prompt": "", "wrapper": "", "thinking": thinking,
                    "max_output_tokens": 256, "retry_policy": {"max_retries": 0},
                    "sampling": {"temperature": 1.0, "anti_target": False} if method == "hlwy" else {"temperature": 1.0}},
               "budget": {"max_requests": count, "max_input_tokens": count * 4096,
                          "max_output_tokens": count * 256, "total_timeout_seconds": 120}}
    if method == "hlwy":
        package.update(algorithm_version=ALGORITHM_VERSION, prompt_hash=PROMPT_HASH,
                       scorer_hash=runtime_hash("hlwy.py"), observations=[42] * count)
    else:
        probes = [{"probe_id": f"synthetic-{i}", "prompt": f"Synthetic owned choice {i}: select 1, 2, 3 or 4.", "expected": 1} for i in range(count)]
        package.update(probes=probes, probe_hash=service._hash(probes), scorer_version=SCORER_VERSION,
                       scorer_hash=runtime_hash("reference.py"))
        if self_test:
            package["self_test"] = {"reference_model": "gpt-6-astra", "conditions": reference_conditions(package),
                                     "outcomes": {probe["probe_id"]: True for probe in probes}}
    package["package_hash"] = (hlwy_reference_hash if method == "hlwy" else reference_hash)(package)
    return package


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = os.environ.get("EVAL_TEST_ARTIFACT_ROOT")
        if not root:
            raise RuntimeError("EVAL_TEST_ARTIFACT_ROOT must identify the external isolated test root")
        self.directory = tempfile.TemporaryDirectory(prefix="review-unit-", dir=root)
        self.registry = Registry(Path(self.directory.name))
        self.channel = self.registry.save({"name": "Synthetic review channel", "base_url": "http://127.0.0.1:12345/v1",
                                           "api_key": "synthetic-review-credential", "status": "online"})
        self.svc = service.ReviewService(self.registry)
        self.live = patch.dict(os.environ, {"EVAL_INTEGRITY_EXECUTOR": "live"})
        self.live.start()
        self.calls = []

    async def asyncTearDown(self):
        self.live.stop()
        service.configure_monitor_resolver(None)
        self.directory.cleanup()

    def import_package(self, method="kbf", count=4, principal="workbench", **options):
        package = synthetic_reference(method, count, **options)
        self.svc.import_reference(package, package["package_hash"], principal=principal, confirm_authorized=True)
        return package

    def payload(self, package, **changes):
        b = package["budget"]
        value = dict(principal="workbench", registry_channel_id=self.channel["id"], model="gpt-6-astra", protocol="responses",
            strategy_id="hlwy" if "hlwy" in package["schema"] else "kbf", reference_hash=package["package_hash"],
            source_ref="synthetic-run:1", incident_id="synthetic-incident:1", idempotency_key="synthetic-request:1",
            limits={k:b[k] for k in ("max_requests", "max_input_tokens", "max_output_tokens")},
            budget_seconds=b["total_timeout_seconds"], confirm_live=True, conditions=reference_conditions(package))
        value.update(changes)
        return value

    async def send(self, channel, probe, *, before_send):
        await before_send()
        self.calls.append(probe["id"])
        return {"status": "completed", "valid": True, "text": "42" if probe["id"].startswith("hlwy") else "1",
                "input_tokens_reported": 10, "output_tokens_reported": 1}

    async def test_reference_authorization_hash_and_conditions_fail_before_requests(self):
        package = synthetic_reference()
        with self.assertRaises(RegistryError):
            self.svc.import_reference(package, package["package_hash"], confirm_authorized=False)
        with self.assertRaises(RegistryError):
            self.svc.import_reference(package, "0" * 64, confirm_authorized=True)
        with self.assertRaises(RegistryError):
            self.svc.enqueue(**self.payload(package))
        self.import_package()
        conditions = copy.deepcopy(reference_conditions(package)); conditions["parameters"]["thinking"] = "high"
        with self.assertRaises(RegistryError):
            self.svc.enqueue(**self.payload(package, conditions=conditions))
        await self.svc.run_pending(self.send)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.svc.list(principal="workbench"), [])

    async def test_browser_json_number_roundtrip_preserves_reference_hash(self):
        package = synthetic_reference()
        browser = copy.deepcopy(package)
        browser["parameters"]["sampling"]["temperature"] = 1
        browser["self_test"]["conditions"]["parameters"]["sampling"]["temperature"] = 1
        # JavaScript JSON.stringify emits 1 for JSON's 1.0.
        imported = self.svc.import_reference(browser, package["package_hash"], confirm_authorized=True)
        self.assertEqual(imported["reference_hash"], package["package_hash"])
        task = self.svc.enqueue(**self.payload(package))
        await self.svc.run_pending(self.send)
        self.assertEqual(self.svc.get(task["task_id"], principal="workbench")["status"], "completed")

    async def test_explicit_off_queue_and_idempotency_cooldown(self):
        package = self.import_package()
        with patch.dict(os.environ, {"EVAL_INTEGRITY_EXECUTOR": "off"}):
            task = self.svc.enqueue(**self.payload(package))
            self.assertEqual(await self.svc.run_pending(self.send), 0)
        repeated = self.svc.enqueue(**self.payload(package))
        cooled = self.svc.enqueue(**self.payload(package, source_ref="synthetic-run:2", incident_id="synthetic-incident:2", idempotency_key="synthetic-request:2"))
        self.assertEqual(task["task_id"], repeated["task_id"])
        self.assertEqual(task["task_id"], cooled["task_id"])
        with self.assertRaises(Conflict):
            self.svc.enqueue(**self.payload(package, source_ref="synthetic-conflicting-source"))
        self.assertEqual(self.calls, [])

    async def test_kbf_shared_loop_persists_projection_and_same_is_not_identity(self):
        package = self.import_package()
        task = self.svc.enqueue(**self.payload(package))
        self.assertEqual(await self.svc.run_pending(self.send), 1)
        task = self.svc.get(task["task_id"], principal="workbench")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["report"]["source_verdict"], "SAME")
        self.assertEqual(task["report"]["interpretation"], "no_significant_difference_detected")
        self.assertFalse(task["report"]["identity_authenticated"])
        self.assertEqual(task["report"]["target_valid"], 4)
        self.assertEqual(task["consumed"]["requests"], 4)
        public = json.dumps(task)
        self.assertNotIn("Synthetic owned choice", public)
        self.assertNotIn("synthetic-review-credential", public)
        self.assertNotIn('"text"', public)
        self.assertEqual(await self.svc.run_pending(self.send), 0)

    async def test_hlwy_frozen_request_set_and_projection(self):
        package = self.import_package("hlwy", count=5)
        task = self.svc.enqueue(**self.payload(package))
        await self.svc.run_pending(self.send)
        task = self.svc.get(task["task_id"], principal="workbench")
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(task["report"]["comparison"]["overall_score"], 1.0)
        self.assertEqual(task["report"]["target_total"], 5)
        self.assertEqual(task["report"]["target_valid"], 5)
        self.assertEqual(task["report"]["source_verdict"], "BEHAVIORAL_COMPARISON")

    async def test_cancel_then_resume_preserves_confirmed_samples(self):
        package = self.import_package()
        task = self.svc.enqueue(**self.payload(package)); job_id=task["task_id"]
        async def cancelled_send(channel, probe, *, before_send):
            value = await self.send(channel, probe, before_send=before_send)
            self.svc.store.cancel(job_id)
            return value
        await self.svc.run_pending(cancelled_send)
        first = self.svc.get(job_id, principal="workbench")
        self.assertEqual(first["status"], "cancelled")
        self.assertEqual(first["consumed"]["requests"], 1)
        self.assertEqual(first["report"]["target_total"], 4)
        self.assertEqual(first["report"]["target_invalid"], 3)
        self.svc.resume(job_id, principal="workbench")
        await self.svc.run_pending(self.send)
        final = self.svc.get(job_id, principal="workbench")
        self.assertEqual(final["status"], "completed")
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(len(set(self.calls)), 4)

    async def test_unknown_attempt_never_resent_or_refunded(self):
        package = self.import_package()
        task = self.svc.enqueue(**self.payload(package)); job_id = task["task_id"]
        claimed = self.svc.store.claim(job_id=job_id, owner="synthetic-expired-worker")
        session = self.svc.store.session(job_id, claimed["owner"])
        _, requests = self.svc._requests(package, "kbf")
        session.reserve(requests[0], self.svc._target(self.channel["id"], "gpt-6-astra", "responses"))
        with self.registry.connect() as conn:
            conn.execute("UPDATE integrity_jobs SET lease_until=? WHERE job_id=?", (time.time()-1,job_id))
        self.svc.store.recover_expired_leases()
        await self.svc.run_pending(self.send)
        result = self.svc.get(job_id, principal="workbench")
        self.assertEqual(result["status"], "partially_completed")
        self.assertEqual(result["consumed"]["requests"], 4)
        self.assertEqual(result["consumed"]["unknown_requests"], 1)
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn(requests[0].request_id, self.calls)
        charged = result["fees"]["estimated_usd"]
        self.svc.resume(job_id, principal="workbench")
        await self.svc.run_pending(self.send)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.svc.get(job_id, principal="workbench")["fees"]["estimated_usd"], charged)

    async def test_connection_change_blocks_resume_and_queued_send(self):
        package = self.import_package()
        task = self.svc.enqueue(**self.payload(package)); job_id = task["task_id"]
        self.registry.save({**self.channel, "api_key":"synthetic-replaced-credential"}, self.channel["id"], self.channel["version"])
        await self.svc.run_pending(self.send)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.svc.get(job_id, principal="workbench")["status"], "rejected")
        with self.assertRaises(RegistryError):
            self.svc.resume(job_id, principal="workbench")

    async def test_monitor_principal_cannot_read_workbench_results(self):
        package = self.import_package()
        local = self.svc.enqueue(**self.payload(package))
        with self.assertRaises(KeyError):
            self.svc.get(local["task_id"], principal="monitor")
        with self.assertRaises(KeyError):
            self.svc.cancel(local["task_id"], principal="monitor")
        self.assertEqual(self.svc.list(principal="monitor"), [])
        self.svc.import_reference(package, package["package_hash"], principal="monitor", confirm_authorized=True)
        snapshot = self.svc._target(self.channel["id"], "gpt-6-astra", "responses").snapshot
        service.configure_monitor_resolver(lambda _: self.svc._target(self.channel["id"], "gpt-6-astra", "responses"))
        external = self.svc.enqueue(**self.payload(package, principal="monitor", target_snapshot=snapshot))
        self.assertEqual(len(self.svc.list(principal="monitor")), 1)
        self.assertEqual(len(self.svc.list(principal="workbench", administrative=True)), 2)
        self.assertEqual(external["principal"], "monitor")

    async def test_token_budget_prevents_outbound_attempt(self):
        package = self.import_package()
        package["budget"]["max_input_tokens"] = 1
        package["self_test"]["conditions"] = reference_conditions(package)
        package["package_hash"] = reference_hash(package)
        self.svc.import_reference(package,package["package_hash"],confirm_authorized=True)
        body = self.payload(package)
        task=self.svc.enqueue(**body)
        await self.svc.run_pending(self.send)
        final=self.svc.get(task["task_id"], principal="workbench")
        self.assertEqual(self.calls, [])
        self.assertEqual(final["status"], "partially_completed")
        self.assertEqual(final["report"]["target_total"], 4)
        self.assertEqual(final["report"]["target_coverage"], 0)

    async def test_missing_self_test_and_invalid_results_remain_explicit(self):
        package=self.import_package(self_test=False)
        task=self.svc.enqueue(**self.payload(package))
        async def invalid(channel, probe, *, before_send):
            await before_send();self.calls.append(probe["id"])
            return {"status":"truncated", "valid":False,"text":"1"}
        await self.svc.run_pending(invalid)
        report=self.svc.get(task["task_id"],principal="workbench")["report"]
        self.assertEqual(report["source_verdict"], "UNKNOWN")
        self.assertEqual(report["target_total"], 4)
        self.assertEqual(report["target_invalid"], 4)
        self.assertEqual(report["target_valid"], 0)


if __name__ == "__main__":
    unittest.main()
