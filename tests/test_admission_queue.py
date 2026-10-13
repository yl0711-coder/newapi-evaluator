"""Admission FIFO, server lifetime and recovery through SQLite and mock upstream."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from shared import registry as registry_module
from shared.registry import Conflict, Registry
from features.admission import api, main as engine, storage, reporting
from features.admission.jobs import AdmissionManager


ARTIFACT_ROOT = Path(os.environ.get("TMPDIR", "/Users/lmurder/Desktop/api中转站/中转站极限测试数据/eval-token-usage-queue-20261012/development/admission/tmp"))


class AdmissionQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="admission-queue-", dir=ARTIFACT_ROOT)
        self.root = Path(self.temp.name)
        self.old_api_transport = api.app.state.upstream_transport
        self.old_api_token_check = api.app.state.token_usage_check
        self.registry = Registry(self.root / "registry")
        self.reference_key = "synthetic-reference-credential"
        self.channel = self.registry.save({"base_url": "https://reference.example/v1", "api_key": self.reference_key, "multiplier": 1})
        self.path = self.root / "admission" / "reports.db"
        self.sent = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.closed = []
        self.block_first_pair = False
        self.delay = patch.object(engine, "wait_between_questions", new=AsyncMock())
        self.delay.start()
        self.transport = httpx.MockTransport(self.handler)
        self.manager = AdmissionManager(self.path, self.registry, transport=self.transport)
        await self.manager.start()

    async def asyncTearDown(self):
        await self.manager.stop()
        if api.app.state.admission_manager is self.manager:
            api.app.state.admission_manager = None
        api.app.state.upstream_transport = self.old_api_transport
        api.app.state.token_usage_check = self.old_api_token_check
        self.delay.stop()
        storage.close()
        self.temp.cleanup()

    def body(self, name="first", *, rounds=1):
        return api.CompareInput(candidate={"base_url": f"https://{name}.example/v1", "api_key": f"synthetic-{name}-credential",
                                           "model": "synthetic-model", "protocol": "openai"},
                                reference={"channel_id": self.channel["id"], "model": "synthetic-model", "protocol": "openai"}, rounds=rounds)

    def handler(self, request):
        index = len(self.sent)
        self.sent.append((request.url.host, json.loads(request.content)))
        owner = self
        blocked = self.block_first_pair and index < 2
        if len(self.sent) >= 2:
            self.started.set()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"synthetic answer"},"finish_reason":null}]}\n\n'
                if blocked:
                    await owner.release.wait()
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"completion_tokens":4}}\n\ndata: [DONE]\n\n'
            async def aclose(self):
                owner.closed.append(index)
        return httpx.Response(200, stream=Stream())

    async def wait_terminal(self, task_id, *, manager=None):
        manager = manager or self.manager
        async with asyncio.timeout(5):
            while manager.public(task_id)["state"] not in {"succeeded", "failed", "cancelled"}:
                await asyncio.sleep(.005)
        return manager.public(task_id)

    async def test_concurrent_submissions_are_fifo_and_only_one_runs(self):
        self.block_first_pair = True
        jobs = await asyncio.gather(*(self.manager.enqueue(self.body(name)) for name in ("first", "second", "third")))
        await asyncio.wait_for(self.started.wait(), 2)
        self.assertEqual([job["id"] for job in jobs], sorted(job["id"] for job in jobs))
        self.assertEqual([self.manager.public(job["id"])["state"] for job in jobs], ["running", "queued", "queued"])
        self.assertEqual(len(self.sent), 2)
        self.release.set()
        for job in jobs:
            final = await self.wait_terminal(job["id"])
            self.assertEqual(final["state"], "succeeded")
            self.assertEqual(len(final["report"]["responses"]), 12)
            self.assertEqual(len(final["report"]["pairs"]), 6)
            self.assertIsNotNone(final["report_id"])
        candidates = [host for host, _ in self.sent if host != "reference.example"]
        self.assertEqual(candidates, ["first.example"] * 6 + ["second.example"] * 6 + ["third.example"] * 6)

    async def test_subscription_disconnect_does_not_cancel_backend_or_report(self):
        self.block_first_pair = True
        job = await self.manager.enqueue(self.body())
        await asyncio.wait_for(self.started.wait(), 2)
        class Disconnected:
            async def is_disconnected(self):
                return True
        events = [line async for line in self.manager.subscribe(job["id"], Disconnected())]
        self.assertTrue(events)
        self.assertEqual(self.manager.public(job["id"])["state"], "running")
        self.release.set()
        final = await self.wait_terminal(job["id"])
        self.assertEqual(final["report"]["status"], "completed")
        self.assertEqual(len(self.sent), 12)

    async def test_slow_subscriber_drains_final_events_after_backend_finishes(self):
        self.block_first_pair = True
        job = await self.manager.enqueue(self.body())
        await asyncio.wait_for(self.started.wait(), 2)
        class Connected:
            async def is_disconnected(self):
                return False
        stream = self.manager.subscribe(job["id"], Connected())
        first = await anext(stream)
        self.release.set()
        await self.wait_terminal(job["id"])
        events = [json.loads(first)] + [json.loads(line) async for line in stream]
        self.assertEqual(events[-1]["type"], "run_finished")
        self.assertEqual(len([event for event in events if event["type"] == "side_finished"]), 12)
        self.assertEqual(len([event for event in events if event["type"] == "question_finished"]), 6)

    async def test_running_cancel_closes_own_pair_and_queued_cancel_sends_nothing(self):
        self.block_first_pair = True
        first = await self.manager.enqueue(self.body())
        second = await self.manager.enqueue(self.body("second"))
        third = await self.manager.enqueue(self.body("third"))
        await asyncio.wait_for(self.started.wait(), 2)
        self.assertEqual((await self.manager.cancel(second["id"]))["state"], "cancelled")
        canceled = await self.manager.cancel(first["id"])
        self.assertEqual(canceled["state"], "cancelled")
        self.assertTrue({0, 1}.issubset(self.closed))
        self.assertEqual(canceled["progress"]["unknown"], 2)
        final = await self.wait_terminal(third["id"])
        self.assertEqual(final["state"], "succeeded")
        self.assertFalse(any(host == "second.example" for host, _ in self.sent))
        self.assertEqual(len(self.sent), 14)

    async def test_restart_blocks_fifo_and_resupply_never_replays_unknown(self):
        self.block_first_pair = True
        first = await self.manager.enqueue(self.body())
        second = await self.manager.enqueue(self.body("second"))
        await asyncio.wait_for(self.started.wait(), 2)
        await self.manager.stop()
        self.manager = AdmissionManager(self.path, self.registry, transport=self.transport)
        await self.manager.start()
        self.assertEqual(self.manager.public(first["id"])["state"], "waiting_credentials")
        self.assertEqual(self.manager.public(first["id"])["progress"]["unknown"], 2)
        self.manager.supply(second["id"], "synthetic-second-credential")
        self.assertIsNone(self.manager.store.queue.claim_next(task_type=storage.JOB_TYPE, serial=True))
        self.assertEqual(len(self.sent), 2)
        with self.assertRaises(Conflict):
            self.manager.supply(first["id"], "different-candidate-credential")
        self.manager.supply(first["id"], "synthetic-first-credential")
        final = await self.wait_terminal(first["id"])
        await self.wait_terminal(second["id"])
        self.assertEqual(final["progress"]["unknown"], 2)
        self.assertEqual(final["progress"]["confirmed"], 10)
        self.assertEqual(len(self.sent), 24)
        self.assertEqual([row["status"] for row in final["report"]["measurements"]].count("unknown"), 2)

    async def test_reference_change_blocks_credential_resupply(self):
        first = await self.manager.enqueue(self.body())
        await self.wait_terminal(first["id"])
        second = await self.manager.enqueue(self.body("second"))
        # Pause before the background consumer can claim the new submission.
        await self.manager.stop()
        self.manager = AdmissionManager(self.path, self.registry, transport=self.transport)
        await self.manager.start()
        self.registry.save({"base_url": "https://changed.example/v1", "api_key": self.reference_key, "multiplier": 1}, self.channel["id"], self.channel["version"])
        with self.assertRaises(Conflict):
            self.manager.supply(second["id"], "synthetic-second-credential")
        self.assertEqual(self.manager.public(second["id"])["state"], "waiting_credentials")

    async def test_confirmed_side_is_not_repeated_after_half_pair_restart(self):
        def half_pair(request):
            self.block_first_pair = request.url.host == "reference.example"
            return self.handler(request)
        self.manager.transport = httpx.MockTransport(half_pair)
        job = await self.manager.enqueue(self.body())
        async with asyncio.timeout(2):
            while self.manager.public(job["id"])["progress"]["confirmed"] != 1:
                await asyncio.sleep(.005)
        await self.manager.stop()
        self.manager = AdmissionManager(self.path, self.registry, transport=self.transport)
        await self.manager.start()
        self.manager.supply(job["id"], "synthetic-first-credential")
        final = await self.wait_terminal(job["id"])
        self.assertEqual(len(self.sent), 12)
        self.assertEqual(final["progress"]["confirmed"], 11)
        self.assertEqual(final["progress"]["unknown"], 1)
        first_candidate_prompt = self.sent[0][1]["messages"][-1]["content"]
        self.assertEqual(sum(host == "first.example" and body["messages"][-1]["content"] == first_candidate_prompt for host, body in self.sent), 1)

    async def test_idempotency_is_config_bound_and_keys_never_enter_artifacts(self):
        self.block_first_pair = True
        first = await self.manager.enqueue(self.body(), idempotency_key="synthetic-idempotency")
        replay = await self.manager.enqueue(self.body(), idempotency_key="synthetic-idempotency")
        self.assertEqual(first["id"], replay["id"])
        with self.assertRaises(Conflict):
            await self.manager.enqueue(self.body("other"), idempotency_key="synthetic-idempotency")
        await asyncio.wait_for(self.started.wait(), 2)
        output = json.dumps(self.manager.public(first["id"])) + json.dumps(self.manager.store.events(first["id"]))
        self.assertNotIn("synthetic-first-credential", output)
        self.assertNotIn(self.reference_key, output)
        await self.manager.cancel(first["id"])
        for path in self.path.parent.glob("reports.db*"):
            self.assertNotIn(b"synthetic-first-credential", path.read_bytes())
            self.assertNotIn(self.reference_key.encode(), path.read_bytes())

    async def test_runtime_budget_closes_streams_and_advances(self):
        await self.manager.stop()
        self.manager = AdmissionManager(self.path, self.registry, transport=self.transport, budget_seconds=1)
        await self.manager.start()
        self.block_first_pair = True
        first = await self.manager.enqueue(self.body())
        second = await self.manager.enqueue(self.body("second"))
        final = await self.wait_terminal(first["id"])
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["error"], "准入任务超过运行预算")
        self.assertTrue({0, 1}.issubset(self.closed))
        self.assertEqual((await self.wait_terminal(second["id"]))["state"], "succeeded")

    async def test_atomic_serial_claim_from_two_sqlite_connections(self):
        await self.manager.stop()
        # Stop only the consumer; submissions still use the same durable store.
        first = await self.manager.enqueue(self.body())
        second = await self.manager.enqueue(self.body("second"))
        another = storage.JobStore(self.path)
        claims = await asyncio.gather(asyncio.to_thread(self.manager.store.queue.claim_next, task_type=storage.JOB_TYPE, serial=True),
                                      asyncio.to_thread(another.queue.claim_next, task_type=storage.JOB_TYPE, serial=True))
        self.assertEqual([item["id"] for item in claims if item], [first["id"]])
        self.assertEqual(self.manager.public(second["id"])["state"], "queued")

    async def test_second_consumer_cannot_acquire_same_admission_directory(self):
        second = AdmissionManager(self.path, self.registry, transport=self.transport)
        with self.assertRaises(RuntimeError):
            await second.start()
        self.assertIsNone(second._consumer)
        self.assertIsNone(second._lock)

    async def test_invalid_job_and_credentials_inputs_never_echo_keys(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://testserver") as client:
            body = self.body().model_dump(mode="json")
            body["reference"] = {"api_key": "synthetic-private-input"}
            response = await client.post("/api/jobs", json=body)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("synthetic-private-input", response.text)
            response = await client.post("/api/jobs/1/credentials", json={"api_key": "synthetic-private-input", "base_url": "https://override.example"})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("synthetic-private-input", response.text)

    async def test_active_fifo_list_includes_old_head_beyond_history_limit(self):
        await self.manager.stop()
        jobs = [await self.manager.enqueue(self.body(f"queued-{index}")) for index in range(105)]
        listed = self.manager.list()
        self.assertEqual([row["id"] for row in listed], [row["id"] for row in jobs])
        self.assertEqual([row["queue_position"] for row in listed], list(range(1, 106)))

    async def test_report_retention_never_removes_active_queue_snapshot(self):
        await self.manager.stop()
        job = await self.manager.enqueue(self.body())
        report = {**job["report"], "status": "completed"}
        with patch.object(storage, "DB_PATH", self.path):
            storage.close()
            for _ in range(31):
                storage.save_report(report)
            self.assertEqual(len(storage.list_reports()), 30)
            active = self.manager.public(job["id"])
            self.assertEqual(active["state"], "queued")
            self.assertEqual(len(active["report"]["questions"]), 6)
            self.assertEqual(active["queue_position"], 1)
            storage.close()

    async def test_orphan_queue_record_is_failed_at_restart_without_sending(self):
        await self.manager.stop()
        orphan = self.manager.store.queue.enqueue(storage.JOB_TYPE, {"run_id": "synthetic-orphan", "configuration_digest": "synthetic-digest"},
                                                   idempotency_key="synthetic-orphan")
        self.manager = AdmissionManager(self.path, self.registry, transport=self.transport)
        await self.manager.start()
        self.assertEqual(self.manager.store.queue.get(orphan["id"])["state"], "failed")
        self.assertEqual(len(self.sent), 0)
        job = await self.manager.enqueue(self.body())
        self.assertEqual((await self.wait_terminal(job["id"]))["state"], "succeeded")
        self.assertEqual(len(self.sent), 12)

    async def test_cancel_committed_before_process_loss_recovers_terminal_report(self):
        self.block_first_pair = True
        job = await self.manager.enqueue(self.body())
        await asyncio.wait_for(self.started.wait(), 2)
        self.manager.store.queue.cancel(job["id"])
        await self.manager.stop()
        self.manager = AdmissionManager(self.path, self.registry, transport=self.transport)
        await self.manager.start()
        final = self.manager.public(job["id"])
        self.assertEqual(final["state"], "cancelled")
        self.assertEqual(final["report"]["status"], "canceled")
        self.assertIsNotNone(final["report_id"])
        self.assertEqual(final["progress"]["unknown"], 2)
        self.assertEqual(len(self.sent), 2)

    async def test_upstream_key_echo_is_redacted_in_reports_events_and_database(self):
        candidate_key = "synthetic-first-credential"
        def echo(request):
            fragments = [candidate_key[:8], candidate_key[8:], " ", self.reference_key[:9], self.reference_key[9:]]
            events = [{"model": candidate_key, "choices": [{"delta": {"content": text}, "finish_reason": None}]} for text in fragments]
            events.append({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 3}})
            raw = "".join("data: " + json.dumps(value) + "\n\n" for value in events) + "data: [DONE]\n\n"
            return httpx.Response(200, text=raw, headers={"x-request-id": self.reference_key})
        self.manager.transport = httpx.MockTransport(echo)
        job = await self.manager.enqueue(self.body())
        final = await self.wait_terminal(job["id"])
        self.assertTrue(all(row["content"] == "[REDACTED] [REDACTED]" for row in final["report"]["responses"]))
        output = json.dumps(final) + json.dumps(self.manager.store.events(job["id"]))
        for secret in (candidate_key, self.reference_key):
            self.assertNotIn(secret, output)
            for path in self.path.parent.glob("reports.db*"):
                self.assertNotIn(secret.encode(), path.read_bytes())

    def trusted(self, model, protocol):
        from features.token_usage.domain import DEFAULT_THRESHOLDS, condition_hash
        channel = {**self.registry.resolve(self.channel["id"]), "model": model, "protocol": protocol}
        resolved = SimpleNamespace(channel=channel, snapshot={"registry_channel_id": self.channel["id"],
            "connection_fingerprint": self.registry.connection_fingerprint(channel), "model": model, "protocol": protocol,
            "canonical_model": model, "mapping_revision": "synthetic-mapping"})
        return {"resolved": resolved, "revision": "synthetic-revision", "thresholds": dict(DEFAULT_THRESHOLDS),
                "condition_hash": condition_hash(DEFAULT_THRESHOLDS)}

    def major_json_transport(self, *, verification_gate=None):
        from features.token_usage.domain import FIXED_INPUT
        async def handler(request):
            body = json.loads(request.content)
            if body["messages"][-1]["content"] != FIXED_INPUT:
                return self.handler(request)
            index = len(self.sent)
            self.sent.append((request.url.host, body))
            if index >= 2 and verification_gate is not None:
                self.started.set()
                await verification_gate.wait()
            count = 3000 if request.url.host == "first.example" else 1000
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                                            "usage": {"prompt_tokens": count, "completion_tokens": 1}})
        return httpx.MockTransport(handler)

    async def test_token_major_is_persisted_before_verification_and_six_questions(self):
        from features.token_usage import transport as token_transport
        self.manager.token_check = token_transport.run_paired_check
        self.manager.token_reference = self.trusted
        owner = self
        def handler(request):
            body = json.loads(request.content)
            if body["messages"][-1]["content"] != token_transport.FIXED_INPUT:
                return self.handler(request)
            index = len(self.sent)
            self.sent.append((request.url.host, body))
            count = 3000 if request.url.host == "first.example" and index < 2 else 1000
            class TokenStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    if index >= 2:
                        await owner.release.wait()
                    data = {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": count, "completion_tokens": 1}}
                    yield ("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=TokenStream())
        self.manager.transport = httpx.MockTransport(handler)
        with patch.object(token_transport, "validate_url", new=AsyncMock()):
            job = await self.manager.enqueue(self.body())
            async with asyncio.timeout(2):
                while len(self.sent) < 4:
                    await asyncio.sleep(.005)
            snapshot = self.manager.public(job["id"])
            self.assertEqual(snapshot["report"]["token_usage"]["conclusion"], "high_risk")
            self.assertEqual(snapshot["report"]["token_usage"]["verification_status"], "pending")
            self.assertEqual(snapshot["report"]["measurements"], [])
            self.release.set()
            final = await self.wait_terminal(job["id"])
        self.assertEqual(final["report"]["token_usage"]["conclusion"], "high_risk")
        self.assertEqual(final["report"]["token_usage"]["verification_status"], "normal")
        self.assertEqual(final["report"]["token_usage"]["candidate_input_tokens"], 3000)
        self.assertEqual(final["report"]["token_usage"]["reference_input_tokens"], 1000)
        self.assertEqual(final["report"]["token_usage"]["ratio"], 3)
        self.assertEqual(final["report"]["token_usage"]["difference"], 2000)
        self.assertEqual(final["report"]["token_usage"]["requests_sent"], 4)
        self.assertEqual(len(self.sent), 16)
        self.assertEqual(len(final["report"]["measurements"]), 12)

    async def test_token_unknown_after_restart_is_never_replayed(self):
        from features.token_usage import transport as token_transport
        self.manager.token_check = token_transport.run_paired_check
        self.manager.token_reference = self.trusted
        self.block_first_pair = True
        with patch.object(token_transport, "validate_url", new=AsyncMock()):
            job = await self.manager.enqueue(self.body())
            await asyncio.wait_for(self.started.wait(), 2)
            await self.manager.stop()
            self.manager = AdmissionManager(self.path, self.registry, transport=self.transport,
                                            token_check=token_transport.run_paired_check, token_reference=self.trusted)
            await self.manager.start()
            self.manager.supply(job["id"], "synthetic-first-credential")
            final = await self.wait_terminal(job["id"])
        self.assertEqual(len(self.sent), 14)
        self.assertEqual(final["report"]["token_usage"]["conclusion"], "unable_to_determine")
        self.assertEqual(final["report"]["token_usage"]["requests_sent"], 2)
        self.assertEqual(final["report"]["token_usage"]["new_requests_sent"], 0)
        self.assertEqual(final["progress"]["unknown"], 2)

    async def test_missing_trusted_reference_sends_only_six_question_pairs(self):
        from features.token_usage.transport import run_paired_check
        self.manager.token_check = run_paired_check
        def missing(*_):
            raise KeyError("trusted_reference_missing")
        self.manager.token_reference = missing
        job = await self.manager.enqueue(self.body())
        final = await self.wait_terminal(job["id"])
        self.assertEqual(len(self.sent), 12)
        self.assertEqual(final["report"]["token_usage"]["reason"], "trusted_reference_missing_or_mismatch")
        self.assertEqual(final["report"]["token_usage"]["requests_sent"], 0)

    async def test_changed_trusted_thresholds_refuse_token_reuse_without_extra_sends(self):
        from features.token_usage.transport import run_paired_check
        from features.token_usage.domain import condition_hash
        await self.manager.stop()
        self.manager.token_check = run_paired_check
        self.manager.token_reference = self.trusted
        job = await self.manager.enqueue(self.body())
        def changed(model, protocol):
            binding = self.trusted(model, protocol)
            binding["thresholds"] = {"minor_ratio": 1.20, "minor_difference": 256}
            binding["revision"] = "changed-revision"
            binding["condition_hash"] = condition_hash(binding["thresholds"])
            return binding
        self.manager = AdmissionManager(self.path, self.registry, transport=self.transport,
                                        token_check=run_paired_check, token_reference=changed)
        await self.manager.start()
        self.manager.supply(job["id"], "synthetic-first-credential")
        final = await self.wait_terminal(job["id"])
        self.assertEqual(final["report"]["token_usage"]["reason"], "trusted_reference_missing_or_mismatch")
        self.assertEqual(final["report"]["token_usage"]["requests_sent"], 0)
        self.assertEqual(len(self.sent), 12)

    async def test_restarted_major_alert_survives_trusted_threshold_change_without_token_sends(self):
        from features.token_usage.transport import run_paired_check
        from features.token_usage.domain import FIXED_INPUT, condition_hash
        await self.manager.stop()
        self.manager.token_check = run_paired_check
        self.manager.token_reference = self.trusted
        job = await self.manager.enqueue(self.body())
        saved = self.manager.store.get(job["id"])
        first_alert = {
            "status": "completed", "severity": "high_risk", "conclusion": "high_risk",
            "reason": "major_input_token_anomaly", "verification_status": "pending",
            "candidate_input_tokens": 3000, "reference_input_tokens": 1000,
            "ratio": 3, "difference": 2000, "billing_status": "unverified", "source": "admission",
            "condition_hash": saved["configuration"]["token_reference"]["condition_hash"],
            "binding_revision": "synthetic-revision", "observed_at": "2026-10-12T00:00:00+00:00",
            "pairs": [{"pair_index": 0, "candidate": {"normalized_input_tokens": 3000},
                       "reference": {"normalized_input_tokens": 1000}}], "requests_sent": 2, "new_requests_sent": 2,
        }
        report = saved["report"]
        report["token_usage"] = first_alert
        self.manager.store.checkpoint(job["id"], report, {"type": "token_usage", "result": first_alert})
        for side in ("candidate", "reference"):
            self.manager.store.checkpoint(job["id"], report, {"type": "token_usage_attempt", "side": side, "pair_index": 0},
                                          step_id=f"token:{side}:0", state="confirmed", result={"status": "completed"})
        def changed(model, protocol):
            binding = self.trusted(model, protocol)
            binding["thresholds"] = {"minor_ratio": 1.20, "minor_difference": 256}
            binding["revision"] = "changed-revision"
            binding["condition_hash"] = condition_hash(binding["thresholds"])
            return binding
        self.manager = AdmissionManager(self.path, self.registry, transport=self.transport,
                                        token_check=run_paired_check, token_reference=changed)
        await self.manager.start()
        self.assertEqual(self.manager.public(job["id"])["state"], "waiting_credentials")
        self.manager.supply(job["id"], "synthetic-first-credential")
        final = await self.wait_terminal(job["id"])
        token = final["report"]["token_usage"]
        for field, value in first_alert.items():
            if field not in {"verification_status", "new_requests_sent"}:
                self.assertEqual(token[field], value, field)
        self.assertEqual(token["verification_status"], "trusted_reference_missing_or_mismatch")
        self.assertEqual(token["verification_reason"], "trusted_reference_missing_or_mismatch")
        self.assertEqual(token["new_requests_sent"], 0)
        self.assertEqual(final["state"], "succeeded")
        self.assertEqual(final["report"]["summary"]["overall"]["status"], "token_usage_high_risk")
        self.assertEqual(len(self.sent), 12)
        self.assertTrue(all(row["messages"][-1]["content"] != FIXED_INPUT for _, row in self.sent))
        self.assertEqual(final["progress"]["confirmed"], 14)
        self.assertEqual(final["progress"]["not_needed"], 2)

    async def test_binding_change_after_first_major_retains_alert_before_verification_send(self):
        from features.token_usage import transport as token_transport
        from features.token_usage.domain import condition_hash
        changed = False
        def trusted(model, protocol):
            binding = self.trusted(model, protocol)
            if changed:
                binding["thresholds"] = {"minor_ratio": 1.20, "minor_difference": 256}
                binding["revision"] = "changed-revision"
                binding["condition_hash"] = condition_hash(binding["thresholds"])
            return binding
        async def token_check(*args, on_result, **kwargs):
            async def published(result):
                nonlocal changed
                await on_result(result)
                if result["severity"] == "high_risk":
                    changed = True
            return await token_transport.run_paired_check(*args, on_result=published, **kwargs)
        self.manager.token_check = token_check
        self.manager.token_reference = trusted
        self.manager.transport = self.major_json_transport()
        with patch.object(token_transport, "validate_url", new=AsyncMock()):
            job = await self.manager.enqueue(self.body())
            final = await self.wait_terminal(job["id"])
        token = final["report"]["token_usage"]
        self.assertEqual(token["conclusion"], "high_risk")
        self.assertEqual(token["verification_status"], "trusted_reference_missing_or_mismatch")
        self.assertEqual(token["reason"], "major_input_token_anomaly")
        self.assertEqual(token["candidate_input_tokens"], 3000)
        self.assertEqual(token["reference_input_tokens"], 1000)
        self.assertEqual(token["requests_sent"], 2)
        self.assertEqual(token["new_requests_sent"], 2)
        self.assertEqual(len(token["pairs"]), 1)
        self.assertEqual(len(self.sent), 14)
        self.assertEqual(len(final["report"]["measurements"]), 12)
        self.assertEqual(final["state"], "succeeded")

    async def test_cancel_after_first_major_retains_alert_and_marks_verification_unknown(self):
        from features.token_usage import transport as token_transport
        self.manager.token_check = token_transport.run_paired_check
        self.manager.token_reference = self.trusted
        self.manager.transport = self.major_json_transport(verification_gate=self.release)
        with patch.object(token_transport, "validate_url", new=AsyncMock()):
            job = await self.manager.enqueue(self.body())
            await asyncio.wait_for(self.started.wait(), 2)
            self.assertEqual(self.manager.public(job["id"])["report"]["token_usage"]["conclusion"], "high_risk")
            final = await self.manager.cancel(job["id"])
        token = final["report"]["token_usage"]
        self.assertEqual(final["state"], "cancelled")
        self.assertIsNotNone(final["report_id"])
        self.assertEqual(token["conclusion"], "high_risk")
        self.assertEqual(token["verification_status"], "cancelled_unknown")
        self.assertEqual(token["verification_reason"], "cancelled_unknown")
        self.assertEqual(token["reason"], "major_input_token_anomaly")
        self.assertEqual(token["candidate_input_tokens"], 3000)
        self.assertEqual(token["reference_input_tokens"], 1000)
        self.assertEqual(len(token["pairs"]), 1)
        self.assertEqual(final["report"]["measurements"], [])
        self.assertGreaterEqual(final["progress"]["unknown"], 1)
        self.assertLessEqual(len(self.sent), 4)
        self.assertEqual(token["requests_sent"], len(self.sent))
        self.assertEqual(token["new_requests_sent"], len(self.sent))

    async def test_legacy_compare_submits_behind_existing_job_and_saves_server_report(self):
        self.block_first_pair = True
        first = await self.manager.enqueue(self.body())
        await asyncio.wait_for(self.started.wait(), 2)
        api.app.state.admission_manager = self.manager
        api.app.state.upstream_transport = self.transport
        api.app.state.token_usage_check = None
        with patch.object(storage, "DB_PATH", self.path), patch.object(registry_module, "_registry", self.registry):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://testserver") as client:
                body = self.body("second").model_dump(mode="json")
                body["candidate"]["api_key"] = "synthetic-second-credential"
                legacy = asyncio.create_task(client.post("/api/compare", json=body))
                async with asyncio.timeout(2):
                    while len(self.manager.list()) < 2:
                        await asyncio.sleep(.005)
                self.assertEqual(len(self.sent), 2)
                self.release.set()
                response = await asyncio.wait_for(legacy, 5)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(response.text.splitlines()[-1])["type"], "run_finished")
                jobs = (await client.get("/api/jobs")).json()["jobs"]
                self.assertEqual(len(jobs), 2)
                self.assertTrue(all(job["state"] == "succeeded" for job in jobs))
                self.assertTrue(all(job["report_id"] for job in jobs))
        candidates = [host for host, _ in self.sent if host != "reference.example"]
        self.assertEqual(candidates, ["first.example"] * 6 + ["second.example"] * 6)

    async def test_remediation_delete_clears_all_terminal_body_copies(self):
        job = await self.manager.enqueue(self.body())
        final = await self.wait_terminal(job["id"])
        marker = "terminal-body-deletion-marker"
        self.manager.store.checkpoint(job["id"], final["report"], {"type": "chunk", "content": marker},
                                      step_id="token:complete", state="confirmed", result={"content": marker})
        with patch.object(storage, "DB_PATH", self.path):
            storage.close()
            self.assertTrue(storage.delete_report(final["report_id"]))
            storage.close()
        saved = self.manager.store.get(job["id"])
        self.assertEqual(saved["report"], {})
        self.assertEqual(saved["step_results"], {})
        self.assertNotIn(marker, json.dumps(self.manager.store.events(job["id"])))
        replay = await self.manager.enqueue(self.body(), idempotency_key="new-independent-intent")
        self.assertNotEqual(replay["id"], job["id"])

    async def test_remediation_legacy_retention_clears_terminal_copies_preserves_active(self):
        first = await self.manager.enqueue(self.body())
        final = await self.wait_terminal(first["id"])
        await self.manager.stop()
        active = await self.manager.enqueue(self.body("second"))
        with patch.object(storage, "DB_PATH", self.path), patch.object(storage, "MAX_REPORTS", 1):
            storage.close()
            storage.save_report(final["report"])
            storage.close()
        self.assertEqual(self.manager.store.get(first["id"])["report"], {})
        self.assertEqual(self.manager.store.get(first["id"])["step_results"], {})
        self.assertEqual(self.manager.store.events(first["id"])["events"], [])
        self.assertEqual(len(self.manager.store.get(active["id"])["report"]["questions"]), 6)

    async def test_remediation_cancel_summary_equals_persisted_detail(self):
        self.block_first_pair = True
        job = await self.manager.enqueue(self.body())
        await asyncio.wait_for(self.started.wait(), 2)
        final = await self.manager.cancel(job["id"])
        saved = self.manager.store.get(job["id"])["report"]
        self.assertEqual(saved["summary"], final["report"]["summary"])
        self.assertEqual(saved["pairs"], final["report"]["pairs"])
        self.assertEqual(reporting.public_report(saved)["summary"], saved["summary"])
        with patch.object(storage, "DB_PATH", self.path):
            storage.close()
            self.assertEqual(storage.list_reports()[0]["overall_label"], saved["summary"]["overall"]["label"])
            self.assertEqual(storage.get_report(final["report_id"])["summary"], saved["summary"])
            storage.close()
        self.assertEqual(saved["summary"]["counts"]["unknown"], 1)
        self.assertNotEqual(saved["summary"]["overall"]["status"], "runtime_similar")

    async def test_remediation_restart_rejects_contract_drift_without_sending(self):
        await self.manager.stop()
        job = await self.manager.enqueue(self.body())
        with patch.object(engine, "SYSTEM_PROMPT", engine.SYSTEM_PROMPT + " changed"):
            self.manager = AdmissionManager(self.path, self.registry, transport=self.transport)
            await self.manager.start()
            self.assertEqual(self.manager.public(job["id"])["state"], "failed")
            self.assertIn("构造合同", self.manager.public(job["id"])["error"])
        self.assertEqual(len(self.sent), 0)

    async def test_remediation_retained_event_gap_is_non_success(self):
        job = await self.manager.enqueue(self.body())
        await self.wait_terminal(job["id"])
        with self.manager.store.connect() as conn:
            for number in range(storage.MAX_JOB_EVENTS + 2):
                self.manager.store._event(conn, job["id"], {"type": "chunk", "content": str(number)})
        class Connected:
            async def is_disconnected(self):
                return False
        events = [json.loads(line) async for line in self.manager.subscribe(job["id"], Connected())]
        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(events[0]["error_category"], "event_history_gap")
        self.assertIn(f"/api/jobs/{job['id']}", events[0]["snapshot_url"])
        self.assertFalse(any(event["type"] == "run_finished" for event in events))

    async def test_remediation_finish_retention_clears_events_results_and_contract(self):
        with patch.object(storage, "MAX_REPORTS", 1):
            first = await self.manager.enqueue(self.body())
            await self.wait_terminal(first["id"])
            second = await self.manager.enqueue(self.body("second"))
            await self.wait_terminal(second["id"])
        deleted = self.manager.store.get(first["id"])
        self.assertEqual(deleted["report"], {})
        self.assertEqual(deleted["step_results"], {})
        self.assertNotIn("request_contract", deleted["configuration"])
        self.assertEqual(self.manager.store.events(first["id"])["events"], [])

    async def test_remediation_slow_subscriber_gap_never_ends_with_success(self):
        self.block_first_pair = True
        job = await self.manager.enqueue(self.body())
        await asyncio.wait_for(self.started.wait(), 2)
        class Connected:
            async def is_disconnected(self):
                return False
        stream = self.manager.subscribe(job["id"], Connected())
        first = json.loads(await anext(stream))
        self.assertEqual(first["type"], "run_started")
        with self.manager.store.connect() as conn:
            for number in range(storage.MAX_JOB_EVENTS + 2):
                self.manager.store._event(conn, job["id"], {"type": "chunk", "content": str(number)})
        self.release.set()
        await self.wait_terminal(job["id"])
        events = [json.loads(line) async for line in stream]
        self.assertEqual(events[-1]["error_category"], "event_history_gap")
        self.assertFalse(any(event["type"] == "run_finished" for event in events))
        self.assertTrue(self.manager.store.events(job["id"], 0)["truncated"])

    async def test_remediation_frozen_names_survive_registry_rename(self):
        body = self.body()
        body.candidate.name = "候选 A <script>"
        original_name = self.channel["name"] or "未记录渠道名称"
        job = await self.manager.enqueue(body)
        self.registry.save({"name": "改名后的参照", "base_url": self.channel["base_url"], "api_key": self.reference_key, "multiplier": 1}, self.channel["id"], self.channel["version"])
        final = await self.wait_terminal(job["id"])
        self.assertEqual(final["candidate"]["name"], body.candidate.name)
        self.assertEqual(final["report"]["candidate"]["name"], body.candidate.name)
        self.assertEqual(final["reference"]["name"], original_name)
        self.assertEqual(final["report"]["reference"]["name"], original_name)
        self.assertTrue(all("name" not in payload for _, payload in self.sent))

    async def test_remediation_contract_rejects_variants_payload_headers_and_legacy(self):
        import copy
        from features.admission import request_contract
        await self.manager.stop()
        job = await self.manager.enqueue(self.body())
        config = self.manager.store.get(job["id"])["configuration"]
        questions = copy.deepcopy(engine.QUESTIONS)
        questions[0].setdefault("variants", []).append("changed variant")
        with patch.object(engine, "QUESTIONS", questions):
            with self.assertRaises(Conflict):
                self.manager._verify_contract(config)
        original = engine.build_payload
        def changed_payload(endpoint, question):
            return {**original(endpoint, question), "temperature": .7}
        with patch.object(engine, "build_payload", changed_payload):
            with self.assertRaises(Conflict):
                self.manager._verify_contract(config)
        with patch.object(engine, "ANTHROPIC_VERSION", "different-version"):
            anthropic = copy.deepcopy(config)
            for side in ("candidate", "reference"):
                anthropic[side]["protocol"] = "anthropic"
            anthropic["request_contract"] = request_contract.snapshot(anthropic, token_enabled=False)
            anthropic["request_contract_fingerprint"] = request_contract.fingerprint(anthropic["request_contract"])
        with self.assertRaises(Conflict):
            self.manager._verify_contract(anthropic)
        legacy = copy.deepcopy(config)
        legacy.pop("request_contract_fingerprint")
        with self.manager.store.connect() as conn:
            conn.execute("UPDATE admission_jobs SET configuration_json=? WHERE task_id=?", (json.dumps(legacy), job["id"]))
        self.manager = AdmissionManager(self.path, self.registry, transport=self.transport)
        await self.manager.start()
        self.assertEqual(self.manager.public(job["id"])["state"], "failed")
        self.assertEqual(len(self.sent), 0)
        self.assertTrue(self.manager.public(job["id"])["report"]["questions"])

    async def test_remediation_contract_change_during_run_prevents_next_pair(self):
        self.block_first_pair = True
        job = await self.manager.enqueue(self.body())
        await asyncio.wait_for(self.started.wait(), 2)
        with patch.object(engine, "SYSTEM_PROMPT", "Changed while running"):
            self.release.set()
            final = await self.wait_terminal(job["id"])
        self.assertEqual(final["state"], "failed")
        self.assertIn("构造合同", final["error"])
        self.assertEqual(len(self.sent), 2)

    async def test_remediation_token_precheck_is_read_only_and_does_not_filter_six_questions(self):
        self.manager.token_check = AsyncMock()
        self.manager.token_reference = self.trusted
        kwargs = dict(model="synthetic-model", reference_model="synthetic-model", candidate_protocol="openai", reference_protocol="openai", reference_channel_id=self.channel["id"])
        self.assertTrue(self.manager.token_precheck(**kwargs)["available"])
        kwargs["candidate_protocol"] = "anthropic"
        self.assertFalse(self.manager.token_precheck(**kwargs)["available"])
        self.assertEqual(len(self.sent), 0)
        self.assertEqual(self.manager.list(), [])

    async def test_remediation_contract_rejects_astra_effective_output_and_reasoning(self):
        from features.admission import request_contract
        await self.manager.stop()
        body = self.body()
        body.candidate.model = body.reference.model = engine.GPT6_MODEL
        body.candidate.protocol = body.reference.protocol = "responses"
        job = await self.manager.enqueue(body)
        configuration = self.manager.store.get(job["id"])["configuration"]
        body_payload = configuration["request_contract"]["requests"][0]["body"]
        self.assertEqual(body_payload["reasoning"]["effort"], "low")
        for name, value in (("GPT6_OUTPUT_LIMIT", 16384), ("GPT6_REASONING_EFFORT", "high")):
            with patch.object(engine, name, value):
                self.assertNotEqual(request_contract.fingerprint(request_contract.snapshot(configuration, token_enabled=False)), configuration["request_contract_fingerprint"])
                with self.assertRaises(Conflict):
                    self.manager._verify_contract(configuration)
        self.assertEqual(len(self.sent), 0)

    async def test_remediation_drift_after_interrupted_send_keeps_unknown_without_replay(self):
        self.block_first_pair = True
        job = await self.manager.enqueue(self.body())
        await asyncio.wait_for(self.started.wait(), 2)
        await self.manager.stop()
        with patch.object(engine, "SYSTEM_PROMPT", "new-contract-after-restart"):
            self.manager = AdmissionManager(self.path, self.registry, transport=self.transport)
            await self.manager.start()
            final = self.manager.public(job["id"])
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["progress"]["unknown"], 2)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len([item for item in final["report"]["measurements"] if item["status"] == "unknown"]), 2)

    async def test_remediation_initialization_cleans_old_deleted_terminal_copies(self):
        job = await self.manager.enqueue(self.body())
        final = await self.wait_terminal(job["id"])
        with self.manager.store.connect() as conn:
            conn.execute("DELETE FROM reports WHERE id=?", (final["report_id"],))
        self.assertTrue(self.manager.store.get(job["id"])["report"])
        migrated = storage.JobStore(self.path)
        tombstone = migrated.get(job["id"])
        self.assertEqual(tombstone["report"], {})
        self.assertEqual(tombstone["step_results"], {})
        self.assertNotIn("request_contract", tombstone["configuration"])
        self.assertEqual(migrated.events(job["id"])["events"], [])

    async def test_remediation_display_rename_does_not_change_idempotent_identity(self):
        self.block_first_pair = True
        body = self.body()
        body.candidate.name = "原候选名"
        first = await self.manager.enqueue(body, idempotency_key="display-independent-intent")
        old_name = first["reference"]["name"]
        self.registry.save({"name": "只改参照名", "base_url": self.channel["base_url"], "api_key": self.reference_key, "multiplier": 1}, self.channel["id"], self.channel["version"])
        body.candidate.name = "只改候选名"
        replay = await self.manager.enqueue(body, idempotency_key="display-independent-intent")
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(replay["candidate"]["name"], "原候选名")
        self.assertEqual(replay["reference"]["name"], old_name)
        await self.manager.cancel(first["id"])

    async def test_remediation_legacy_event_origin_cannot_be_inferred_from_event_type(self):
        job = await self.manager.enqueue(self.body())
        await self.wait_terminal(job["id"])
        with self.manager.store.connect() as conn:
            conn.execute("DELETE FROM admission_events WHERE task_id=?", (job["id"],))
            for number in range(storage.MAX_JOB_EVENTS):
                event = {"type": "credentials_required"} if number == 0 else {"type": "run_finished"} if number == storage.MAX_JOB_EVENTS - 1 else {"type": "chunk", "content": "legacy-tail"}
                conn.execute("INSERT INTO admission_events(task_id,event_json) VALUES(?,?)", (job["id"], json.dumps(event)))
            conn.execute("DELETE FROM admission_event_state WHERE task_id=?", (job["id"],))
        self.manager.store = storage.JobStore(self.path)
        self.assertTrue(self.manager.store.events(job["id"], 0)["truncated"])
        class Connected:
            async def is_disconnected(self):
                return False
        events = [json.loads(line) async for line in self.manager.subscribe(job["id"], Connected())]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["error_category"], "event_history_gap")


if __name__ == "__main__":
    unittest.main()
