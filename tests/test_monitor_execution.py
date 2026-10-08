"""Monitor execution regressions with synthetic channels and no external requests."""
import asyncio
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from features.model_coverage import internal_api, monitor
from features.model_coverage.monitor import MonitorStore
from features.stability.app import responses
from features.stability.app.scheduler import probe_semaphore
from tests import test_monitor_internal as fixtures


class MonitorExecutionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.MonitorContractTests.asyncSetUp
    asyncTearDown = fixtures.MonitorContractTests.asyncTearDown
    call = fixtures.MonitorContractTests.call
    job = fixtures.MonitorContractTests.job
    inventory = fixtures.MonitorContractTests.inventory
    expire_lease = fixtures.MonitorContractTests.expire_lease

    async def create(self, **changes):
        response = await self.call("POST", "/internal/v1/probe-jobs", self.job(**changes))
        self.assertEqual(response.status_code, 201)
        return response.json()["job_id"]

    async def wait_running(self, store, job_id):
        for _ in range(100):
            if store.job(job_id)["status"] == "running":
                return
            await asyncio.sleep(.005)
        self.fail("executor did not claim the synthetic job")

    async def test_cancel_during_shared_slot_wait_sends_nothing(self):
        job_id, calls = await self.create(), []
        store, slots = MonitorStore(self.registry), probe_semaphore()
        await slots.acquire()
        await slots.acquire()

        async def upstream(_client, channel, probe, **kwargs):
            if kwargs.get("before_send"):
                await kwargs["before_send"]()
            calls.append(probe["id"])
            return {"status": "completed", "usage_complete": False}

        try:
            with patch.object(internal_api.transport, "run_probe", upstream):
                runner = asyncio.create_task(internal_api.run_pending())
                await self.wait_running(store, job_id)
                store.cancel(job_id)
                slots.release()
                slots.release()
                await asyncio.wait_for(runner, 2)
            self.assertEqual(calls, [])
            self.assertEqual(store.job(job_id)["status"], "cancelled")
        finally:
            if slots.locked():
                slots.release()
                slots.release()

    async def test_queued_connection_change_rejects_before_send(self):
        job_id, calls = await self.create(), []
        self.registry.save({**self.channel, "api_key": "synthetic-replaced-credential"}, self.channel["id"], self.channel["version"])

        async def send(channel, probe):
            calls.append(probe["id"])
            return {"status": "completed", "usage_complete": False}

        await internal_api.run_pending(send)
        self.assertEqual(calls, [])
        self.assertEqual(MonitorStore(self.registry).job(job_id)["status"], "rejected")

    async def test_actual_credential_is_removed_from_published_result(self):
        await self.create(scenarios=["short_stream"])

        async def send(channel, probe):
            return {"status": "completed", "actual_model": channel["api_key"], "usage_complete": False,
                    "ttft_ms": channel["api_key"]}

        await internal_api.run_pending(send)
        page = MonitorStore(self.registry).results("0", 200)
        self.assertNotIn("synthetic-monitor-upstream-credential", json.dumps(page))
        self.assertIsNone(page["items"][0]["ttft_ms"])

    async def test_heartbeat_does_not_overwrite_committed_ledger(self):
        job_id = await self.create()
        store = MonitorStore(self.registry)
        store.claim("owner")
        with self.registry.connect() as conn:
            conn.execute("UPDATE monitor_probe_jobs SET progress_json=?,consumed_json=? WHERE job_id=?",
                         (json.dumps({"completed_requests": 1}), json.dumps({"requests": 1, "output_tokens_reported": 17}), job_id))
        self.assertEqual(store.heartbeat(job_id, "owner", {"completed_requests": 0}, {"requests": 0}), "continue")
        job = store.job(job_id)
        self.assertEqual(job["progress"]["completed_requests"], 1)
        self.assertEqual(job["budget_consumed"]["output_tokens_reported"], 17)

    async def test_malformed_fields_are_nonretryable_and_not_queued(self):
        for field, value in [("job_type", []), ("priority", {}), ("protocol", []), ("scenarios", [{}])]:
            with self.subTest(field=field):
                response = await self.call("POST", "/internal/v1/probe-jobs", self.job(**{field: value}))
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["error"]["retryable"])
        self.assertEqual(MonitorStore(self.registry).recent_jobs(), [])

    async def test_missing_usage_estimate_does_not_enter_reported_ledger(self):
        job_id = await self.create(scenarios=["short_stream"])

        async def send(channel, probe):
            return {"status": "completed", "usage_complete": False, "output_tokens": 99}

        await internal_api.run_pending(send)
        consumed = MonitorStore(self.registry).job(job_id)["budget_consumed"]
        self.assertIsNone(consumed["output_tokens_reported"])

    async def test_expiry_and_lost_lease_while_slots_stay_occupied(self):
        for cause in ("expiry", "lease"):
            with self.subTest(cause=cause):
                job_id = await self.create(idempotency_key="slot-" + cause)
                store, slots = MonitorStore(self.registry), probe_semaphore()
                await slots.acquire()
                await slots.acquire()
                runner = asyncio.create_task(internal_api.run_pending(limit=1))
                try:
                    await self.wait_running(store, job_id)
                    with self.registry.connect() as conn:
                        column = "expires_at" if cause == "expiry" else "lease_until"
                        conn.execute(f"UPDATE monitor_probe_jobs SET {column}=? WHERE job_id=?", (time.time() - 1, job_id))
                    await asyncio.wait_for(runner, 1)
                    job = store.job(job_id)
                    self.assertEqual(job["status"], "expired" if cause == "expiry" else "failed")
                    self.assertEqual(job["budget_consumed"]["requests"], 0)
                    self.assertEqual(store.results("0", 200)["items"], [])
                finally:
                    slots.release()
                    slots.release()
                    if not runner.done():
                        runner.cancel()
                        await asyncio.gather(runner, return_exceptions=True)

    async def test_healthy_slot_wait_keeps_short_lease_alive(self):
        job_id = await self.create()
        store, slots = MonitorStore(self.registry), probe_semaphore()
        await slots.acquire()
        await slots.acquire()
        try:
            with patch.object(MonitorStore, "LEASE_SECONDS", .15):
                runner = asyncio.create_task(internal_api.run_pending(limit=1))
                await self.wait_running(store, job_id)
                await asyncio.sleep(.3)
                self.assertEqual(store.recover_expired_leases(), 0)
                self.assertEqual(store.job(job_id)["status"], "running")
                store.cancel(job_id)
                await asyncio.wait_for(runner, 1)
            self.assertEqual(store.job(job_id)["status"], "cancelled")
        finally:
            slots.release()
            slots.release()

    async def test_identity_rebind_keeps_inflight_target_snapshot(self):
        job_id = await self.create()
        store, calls = MonitorStore(self.registry), []
        other = self.registry.save({"name": "Synthetic alternate", "base_url": "https://203.0.113.11/v1",
                                    "api_key": "synthetic-alternate-credential", "multiplier": 1})
        old_fingerprint = self.registry.connection_fingerprint(self.registry.get(self.channel["id"], secret=True))

        async def send(channel, probe):
            calls.append(probe["id"])
            store.bind_identity(self.channel["id"], None)
            store.bind_identity(other["id"], "newapi-channel-96")
            return {"status": "completed", "input_tokens_reported": 5, "output_tokens_reported": 7}

        await internal_api.run_pending(send)
        self.assertEqual(len(calls), 1)
        result = store.results("0", 200)["items"][0]
        self.assertEqual(result["target_snapshot"]["registry_channel_id"], self.channel["id"])
        self.assertEqual(result["target_snapshot"]["connection_fingerprint"], old_fingerprint)
        self.assertEqual(store.job(job_id)["status_reason"], "target_identity_changed")

    async def test_running_connection_disable_and_inventory_changes_stop_next_request(self):
        for cause in ("connection", "disabled", "inventory"):
            with self.subTest(cause=cause):
                if cause == "inventory":
                    response = await self.call("PUT", "/internal/v1/production-inventory/cfg-1", self.inventory())
                    self.assertEqual(response.status_code, 200)
                job_id = await self.create(idempotency_key="running-" + cause,
                                           **({"expected_inventory_version": "cfg-1"} if cause == "inventory" else {}))
                calls = []

                async def send(channel, probe):
                    calls.append(probe["id"])
                    if cause == "inventory":
                        new = {**self.inventory(config_fingerprint="sha256:changed"), "inventory_version": "cfg-2",
                               "generated_at": fixtures.iso(time.time())}
                        MonitorStore(self.registry).import_inventory("cfg-2", new, {})
                    else:
                        current = self.registry.get(self.channel["id"])
                        self.registry.save({**current, **({"api_key": "synthetic-next-credential"} if cause == "connection" else {"enabled": False})},
                                           current["id"], current["version"])
                    return {"status": "completed", "output_tokens_reported": 0, "input_tokens_reported": 0}

                await internal_api.run_pending(send)
                self.assertEqual(len(calls), 1)
                self.assertEqual(MonitorStore(self.registry).job(job_id)["progress"]["completed_requests"], 1)
                current = self.registry.get(self.channel["id"])
                self.registry.save({**current, "enabled": True, "api_key": "synthetic-monitor-upstream-credential"}, current["id"], current["version"])

    async def test_old_queued_job_without_original_fingerprint_is_rejected(self):
        job_id = await self.create()
        with self.registry.connect() as conn:
            conn.execute("UPDATE monitor_probe_jobs SET connection_fingerprint=NULL WHERE job_id=?", (job_id,))

        async def forbidden(channel, probe):
            self.fail("legacy job cannot invent an original connection")

        await internal_api.run_pending(forbidden)
        job = MonitorStore(self.registry).job(job_id)
        self.assertEqual((job["status"], job["status_reason"]), ("rejected", "connection_snapshot_missing"))

    async def test_completion_is_atomic_and_replay_is_idempotent(self):
        job_id = await self.create()
        store = MonitorStore(self.registry)
        job = store.claim("owner")
        channel = self.registry.get(self.channel["id"], secret=True)
        attempt = store.permit_attempt(job_id, "owner", channel, "short_stream", 1)
        body = {**attempt, "scenario": "short_stream", "input_tokens_reported": 0, "output_tokens_reported": 17}
        result_id = store.append_result(job_id, "owner", body)
        first = store.job(job_id)
        self.assertEqual((first["progress"]["completed_requests"], first["budget_consumed"]["requests"],
                          first["budget_consumed"]["output_tokens_reported"]), (1, 1, 17))
        self.assertEqual(store.append_result(job_id, "owner", {**body, "output_tokens_reported": 999}), result_id)
        self.assertEqual(store.job(job_id), first)
        with self.registry.connect() as conn:
            conn.execute("UPDATE monitor_probe_jobs SET lease_until=? WHERE job_id=?", (time.time() - 1, job_id))
        self.assertEqual(store.recover_expired_leases(), 1)
        recovered = store.job(job_id)
        self.assertEqual((recovered["progress"]["completed_requests"], recovered["budget_consumed"]["output_tokens_reported"],
                          recovered["budget_consumed"]["unknown_requests"]), (1, 17, 0))

    async def test_permitted_unknown_attempt_recovery_never_resends_or_revives_lease(self):
        job_id = await self.create()
        store = MonitorStore(self.registry)
        store.claim("owner")
        channel = self.registry.get(self.channel["id"], secret=True)
        store.permit_attempt(job_id, "owner", channel, "short_stream", 1)
        self.expire_lease(job_id)
        self.assertEqual(store.heartbeat(job_id, "owner"), "lost")
        self.assertEqual(store.recover_expired_leases(), 1)
        self.assertEqual(store.recover_expired_leases(), 0)

        async def forbidden(channel, probe):
            self.fail("unknown attempt must not be automatically resent")

        await internal_api.run_pending(forbidden)
        job = store.job(job_id)
        self.assertEqual((job["budget_consumed"]["requests"], job["budget_consumed"]["unknown_requests"],
                          job["progress"]["completed_requests"]), (1, 1, 0))
        self.assertIsNone(job["budget_consumed"]["output_tokens_reported"])

    async def test_four_usage_sources_and_explicit_zero_have_correct_totals(self):
        job_id = await self.create()
        readings = iter([
            {"output_tokens": 99, "output_tokens_estimated": 99},
            {"input_tokens_reported": 3, "output_tokens_reported": None},
            {"input_tokens_reported": 0, "output_tokens_reported": 0},
            {"input_tokens_reported": 4, "output_tokens_reported": 7},
        ])

        async def send(channel, probe):
            return {"status": "completed", "usage_complete": False, **next(readings)}

        await internal_api.run_pending(send)
        store = MonitorStore(self.registry)
        results = store.results("0", 200)["items"]
        self.assertEqual([r["usage_status"] for r in results], ["missing", "partial", "complete", "complete"])
        self.assertEqual(results[0]["output_tokens_estimated"], 99)
        consumed = store.job(job_id)["budget_consumed"]
        self.assertEqual((consumed["input_tokens_reported"], consumed["output_tokens_reported"],
                          consumed["output_tokens_reported_requests"], consumed["output_tokens_missing_requests"]), (7, 7, 2, 2))

    async def test_dns_timeout_keeps_health_responsive_and_does_not_queue_later(self):
        entered, release = threading.Event(), threading.Event()
        thread_ids = []

        def slow_dns(channel, protocol):
            thread_ids.append(threading.get_ident())
            entered.set()
            release.wait(2)

        try:
            with patch.object(internal_api, "egress_check", slow_dns), patch.object(internal_api, "DNS_TIMEOUT_SECONDS", .08):
                request = asyncio.create_task(self.call("POST", "/internal/v1/probe-jobs", self.job()))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                health = await asyncio.wait_for(self.client.get("/api/health"), .2)
                self.assertEqual(health.status_code, 200)
                self.assertNotEqual(thread_ids[0], threading.get_ident())
                response = await asyncio.wait_for(request, .3)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "egress_check_timeout"))
                release.set()
                await asyncio.sleep(.02)
            self.assertEqual(MonitorStore(self.registry).recent_jobs(), [])
        finally:
            release.set()

    async def test_repeated_timed_out_dns_cannot_exceed_worker_bound(self):
        release = threading.Event()
        entered = []

        def slow_dns(channel, protocol):
            entered.append(threading.get_ident())
            release.wait(2)

        try:
            with patch.object(internal_api, "egress_check", slow_dns), patch.object(internal_api, "DNS_TIMEOUT_SECONDS", .04):
                calls = [asyncio.create_task(self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="slow-" + str(i)))) for i in range(6)]
                replies = await asyncio.wait_for(asyncio.gather(*calls), .5)
                self.assertEqual([r.status_code for r in replies], [503] * 6)
                self.assertEqual(len(entered), internal_api.DNS_WORKERS)
                self.assertEqual(MonitorStore(self.registry).recent_jobs(), [])
        finally:
            release.set()
            await asyncio.sleep(.02)

    async def test_all_job_field_types_reject_without_queueing_or_retryable_errors(self):
        required_strings = ["job_type", "priority", "protocol", "probe_path", "channel_identity", "model", "expires_at", "idempotency_key"]
        cases = [(field, value) for field in required_strings for value in (None, True, [], {}, "", "x" * 1000)]
        cases.extend((field, value) for field in ("reason", "source_event_id", "expected_inventory_version", "not_before")
                     for value in (True, [], {}, "x" * 1000))
        cases.extend(("scenarios", value) for value in (None, True, {}, "short_stream", [], [{}], [[]], [True], [None],
                                                         ["short_stream", "short_stream"]))
        cases.extend(("rounds", value) for value in (None, True, [], {}, "2", 0, 6, 1.5))
        cases.extend(("budget", value) for value in (None, True, [], "", {}, {"unknown": 1}))
        for field, value in cases:
            with self.subTest(field=field, shape=type(value).__name__):
                response = await self.call("POST", "/internal/v1/probe-jobs", self.job(**{field: value}), idempotency="type-matrix")
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["error"]["retryable"])
        for field in monitor.BUDGET_LIMITS:
            values = (True, [], {}, "1", 0, -1, monitor.BUDGET_LIMITS[field] * 2, float("nan"), float("inf"))
            for value in values:
                with self.subTest(budget_field=field, shape=type(value).__name__):
                    budget = {**self.job()["budget"], field: value}
                    response = await self.call("POST", "/internal/v1/probe-jobs", self.job(budget=budget))
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.json()["error"]["retryable"])
        self.assertEqual(MonitorStore(self.registry).recent_jobs(), [])
        first = await self.call("POST", "/internal/v1/probe-jobs", self.job())
        second = await self.call("POST", "/internal/v1/probe-jobs", self.job())
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.json()["job_id"], second.json()["job_id"])

    async def test_inventory_fields_are_validated_before_collection_operations(self):
        fields = ["channel_identity", "display_name", "supplier_failure_domain_id", "enabled_status", "channel_type",
                  "production_role", "business_criticality", "config_fingerprint"]
        cases = [(field, value) for field in fields for value in (True, [], {}, "x" * 1000)]
        cases.extend((field, value) for field in ("groups", "models") for value in (None, True, {}, "x", [{}], [[]], [False], [None]))
        cases.extend(("newapi_channel_id", value) for value in (True, [], {}, "96", 0, -1, 1.5))
        for field, value in cases:
            with self.subTest(field=field, shape=type(value).__name__):
                response = await self.call("PUT", "/internal/v1/production-inventory/cfg-1", self.inventory(**{field: value}))
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["error"]["retryable"])
        self.assertIsNone(MonitorStore(self.registry).latest_inventory_version())

    async def test_completion_rollback_keeps_attempt_unknown_when_storage_fails(self):
        job_id = await self.create()
        store = MonitorStore(self.registry)
        store.claim("owner")
        channel = self.registry.get(self.channel["id"], secret=True)
        attempt = store.permit_attempt(job_id, "owner", channel, "short_stream", 1)
        with self.registry.connect() as conn:
            conn.execute("""CREATE TRIGGER synthetic_result_failure BEFORE INSERT ON monitor_probe_results
                BEGIN SELECT RAISE(ABORT, 'synthetic result persistence failure'); END""")
        with self.assertRaises(Exception):
            store.append_result(job_id, "owner", {**attempt, "output_tokens_reported": 17})
        job = store.job(job_id)
        self.assertEqual(job["progress"]["completed_requests"], 0)
        self.assertIsNone(job["budget_consumed"]["output_tokens_reported"])
        self.expire_lease(job_id)
        store.recover_expired_leases()
        self.assertEqual(store.job(job_id)["budget_consumed"]["unknown_requests"], 1)
        self.assertEqual(store.results("0", 200)["items"], [])

    async def test_cancel_before_permit_and_shutdown_after_permit_are_distinct(self):
        for phase in ("before", "after"):
            with self.subTest(phase=phase):
                job_id = await self.create(idempotency_key="interruption-" + phase)
                entered, hold = asyncio.Event(), asyncio.Event()

                async def send(channel, probe, *, before_send=None):
                    if phase == "after":
                        await before_send()
                    entered.set()
                    await hold.wait()
                    if phase == "before":
                        await before_send()
                    return {"status": "completed"}

                runner = asyncio.create_task(internal_api.run_pending(send, limit=1))
                await asyncio.wait_for(entered.wait(), 1)
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
                job = MonitorStore(self.registry).job(job_id)
                expected = 0 if phase == "before" else 1
                self.assertEqual((job["budget_consumed"]["requests"], job["budget_consumed"]["unknown_requests"],
                                  job["progress"]["completed_requests"]), (expected, expected, 0))
                self.assertEqual(job["status_reason"], "executor_stopped")

    async def test_execution_logs_never_include_exception_text_or_credential(self):
        job_id = await self.create()

        async def send(channel, probe):
            return {"status": "completed"}

        with patch.object(MonitorStore, "append_result", side_effect=RuntimeError("synthetic-monitor-upstream-credential")):
            with self.assertLogs(internal_api.logger, level="ERROR") as captured:
                await internal_api.run_pending(send)
        logs = "\n".join(captured.output)
        self.assertNotIn("synthetic-monitor-upstream-credential", logs)
        self.assertNotIn("Traceback", logs)
        job = MonitorStore(self.registry).job(job_id)
        self.assertEqual(job["budget_consumed"]["unknown_requests"], 1)

    async def test_real_http_path_runs_with_zero_reported_usage(self):
        job_id = await self.create(scenarios=["non_stream"], rounds=1)
        requests = []

        def upstream(request):
            requests.append(request.method)
            return httpx.Response(200, json={"model": "gpt-5.5", "choices": [{"message": {"content": "synthetic answer"}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 0, "completion_tokens": 0}})

        with patch.object(internal_api, "guarded_transport", lambda: httpx.MockTransport(upstream)):
            await internal_api.run_pending()
        store = MonitorStore(self.registry)
        self.assertEqual(requests, ["POST"])
        self.assertEqual((store.job(job_id)["budget_consumed"]["input_tokens_reported"],
                          store.job(job_id)["budget_consumed"]["output_tokens_reported"]), (0, 0))
        self.assertEqual(store.results("0", 200)["items"][0]["usage_status"], "complete")

    async def test_pre_send_egress_rejection_is_eval_side_without_attempt_or_event(self):
        job_id = await self.create()

        async def denied(_url):
            raise internal_api.EgressDenied("synthetic resolver rejection")

        def forbidden(request):
            self.fail("preflight policy rejection must not reach HTTP")

        with patch.object(internal_api.transport, "validate_url", denied), patch.object(internal_api, "guarded_transport", lambda: httpx.MockTransport(forbidden)):
            await internal_api.run_pending()
        store = MonitorStore(self.registry)
        job = store.job(job_id)
        self.assertEqual((job["status"], job["status_reason"], job["budget_consumed"]["requests"]), ("rejected", "eval_egress_denied", 0))
        self.assertEqual(store.results("0", 200)["items"], [])
        self.assertEqual(store.events("0", 200)["items"], [])

    async def test_display_changes_keep_connection_valid_and_input_budget_is_enforced(self):
        job_id = await self.create()
        current = self.registry.get(self.channel["id"])
        self.registry.save({**current, "name": "Synthetic renamed", "multiplier": 2, "note": "synthetic edit"}, current["id"], current["version"])
        calls = []

        async def send(channel, probe):
            calls.append(probe["id"])
            return {"status": "completed"}

        await internal_api.run_pending(send)
        self.assertEqual(len(calls), 4)
        self.assertEqual(MonitorStore(self.registry).job(job_id)["status"], "completed")
        bounded = await self.create(idempotency_key="input-budget")
        with self.registry.connect() as conn:
            row = conn.execute("SELECT budget_json FROM monitor_probe_jobs WHERE job_id=?", (bounded,)).fetchone()
            conn.execute("UPDATE monitor_probe_jobs SET budget_json=? WHERE job_id=?",
                         (json.dumps({**json.loads(row[0]), "max_input_tokens": 1}), bounded))
        await internal_api.run_pending(send)
        self.assertEqual(len(calls), 4)
        self.assertEqual(MonitorStore(self.registry).job(bounded)["status_reason"], "budget_exhausted")

    async def test_connection_change_during_creation_dns_is_not_queued(self):
        def change_connection(channel, protocol):
            current = self.registry.get(self.channel["id"])
            self.registry.save({**current, "api_key": "synthetic-during-dns"}, current["id"], current["version"])

        with patch.object(internal_api, "egress_check", change_connection):
            response = await self.call("POST", "/internal/v1/probe-jobs", self.job())
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "connection_changed"))
        self.assertEqual(MonitorStore(self.registry).recent_jobs(), [])

    async def test_repeated_and_concurrent_store_initialization_preserves_ledger(self):
        job_id = await self.create()
        stores = await asyncio.gather(*(asyncio.to_thread(MonitorStore, self.registry) for _ in range(4)))
        self.assertEqual([s.job(job_id)["status"] for s in stores], ["queued"] * 4)
        self.assertEqual(len(stores[0].recent_jobs()), 1)

    def assert_unsent_terminal(self, job_id, status, reason, http_calls):
        store = MonitorStore(self.registry)
        job = store.job(job_id)
        self.assertEqual((job["status"], job["status_reason"]), (status, reason))
        self.assertEqual([item["reason"] for item in job["skipped"]], [reason])
        self.assertEqual((job["budget_consumed"]["requests"], job["budget_consumed"]["unknown_requests"],
                          job["budget_consumed"]["input_tokens_reserved"], job["budget_consumed"]["output_tokens_reserved"],
                          job["progress"]["completed_requests"]), (0, 0, 0, 0, 0))
        self.assertIsNone(job["budget_consumed"]["input_tokens_reported"])
        self.assertIsNone(job["budget_consumed"]["output_tokens_reported"])
        self.assertEqual(http_calls, [])
        with self.registry.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM monitor_probe_attempts WHERE job_id=?", (job_id,)).fetchone()[0], 0)
        self.assertEqual(store.results("0", 200)["items"], [])
        self.assertEqual(store.events("0", 200)["items"], [])
        self.assertEqual(store.recover_expired_leases(), 0)
        self.assertEqual(store.job(job_id), job)

    async def preflight_job(self, cause, protocol, scenario):
        return await self.create(idempotency_key=f"preflight-{cause}-{protocol}-{scenario}", protocol=protocol,
                                 scenarios=[scenario], rounds=1,
                                 budget={"max_requests": 1, "max_input_tokens": 1000, "max_output_tokens": 10000, "max_cost_usd": 1})

    async def test_real_preflight_dns_failure_keeps_transport_connect_without_permit(self):
        current = self.registry.get(self.channel["id"])
        self.registry.save({**current, "base_url": "https://synthetic-monitor.invalid/v1"}, current["id"], current["version"])
        for protocol in ("openai", "anthropic", "responses"):
            for scenario in ("short_stream", "non_stream"):
                with self.subTest(protocol=protocol, scenario=scenario):
                    with patch.object(internal_api, "egress_check", lambda _channel, _protocol: None):
                        job_id = await self.preflight_job("dns", protocol, scenario)
                    http_calls = []

                    def forbidden(request):
                        http_calls.append(request.method)
                        return httpx.Response(500)

                    with patch("features.stability.app.egress.socket.getaddrinfo", side_effect=socket.gaierror(-2, "synthetic name resolution failure")), \
                         patch.object(internal_api, "guarded_transport", lambda: httpx.MockTransport(forbidden)):
                        await internal_api.run_pending()
                        self.assertEqual(await internal_api.run_pending(), 0)
                    self.assert_unsent_terminal(job_id, "failed", "transport_connect", http_calls)

    async def test_real_preflight_timeout_keeps_transport_timeout_without_permit(self):
        async def timed_out(_url):
            raise httpx.ConnectTimeout("synthetic preflight timeout")

        for protocol in ("openai", "anthropic", "responses"):
            for scenario in ("short_stream", "non_stream"):
                with self.subTest(protocol=protocol, scenario=scenario):
                    job_id = await self.preflight_job("timeout", protocol, scenario)
                    http_calls = []

                    def forbidden(request):
                        http_calls.append(request.method)
                        return httpx.Response(500)

                    target = responses if protocol == "responses" else internal_api.transport
                    with patch.object(target, "validate_url", timed_out), \
                         patch.object(internal_api, "guarded_transport", lambda: httpx.MockTransport(forbidden)):
                        await internal_api.run_pending()
                        self.assertEqual(await internal_api.run_pending(), 0)
                    self.assert_unsent_terminal(job_id, "failed", "transport_timeout", http_calls)

    async def test_real_preflight_policy_rejection_stays_eval_side_without_permit(self):
        for protocol in ("openai", "anthropic", "responses"):
            for scenario in ("short_stream", "non_stream"):
                with self.subTest(protocol=protocol, scenario=scenario):
                    job_id = await self.preflight_job("policy", protocol, scenario)
                    http_calls = []

                    def forbidden(request):
                        http_calls.append(request.method)
                        return httpx.Response(500)

                    with patch("features.stability.app.egress.EGRESS_ALLOWLIST", ()), \
                         patch.object(internal_api, "guarded_transport", lambda: httpx.MockTransport(forbidden)):
                        await internal_api.run_pending()
                        self.assertEqual(await internal_api.run_pending(), 0)
                    self.assert_unsent_terminal(job_id, "rejected", "eval_egress_denied", http_calls)

    async def test_recovery_can_probe_disabled_production_channel_with_enabled_eval_channel(self):
        imported = await self.call("PUT", "/internal/v1/production-inventory/cfg-1", self.inventory(enabled_status="disabled"))
        self.assertEqual(imported.status_code, 200)
        job_id = await self.create(job_type="recovery", expected_inventory_version="cfg-1", scenarios=["non_stream"], rounds=1)
        http_calls = []

        def upstream(request):
            http_calls.append(request.method)
            return httpx.Response(200, json={"model": "gpt-5.5", "choices": [{"message": {"content": "synthetic recovery answer"}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 3, "completion_tokens": 7}})

        with patch.object(internal_api, "guarded_transport", lambda: httpx.MockTransport(upstream)):
            await internal_api.run_pending()
        store = MonitorStore(self.registry)
        job = store.job(job_id)
        self.assertEqual((job["status"], job["progress"]["completed_requests"], job["budget_consumed"]["requests"]), ("completed", 1, 1))
        self.assertEqual(http_calls, ["POST"])
        self.assertEqual(store.inventory_channel("cfg-1", "newapi-channel-96")["enabled_status"], "disabled")
        result = store.results("0", 200)["items"][0]
        self.assertEqual(result["target_snapshot"]["inventory_version"], "cfg-1")

    async def test_recovery_still_rejects_disabled_eval_channel_without_permit(self):
        imported = await self.call("PUT", "/internal/v1/production-inventory/cfg-1", self.inventory(enabled_status="disabled"))
        self.assertEqual(imported.status_code, 200)
        job_id = await self.create(job_type="recovery", expected_inventory_version="cfg-1", scenarios=["non_stream"], rounds=1)
        current = self.registry.get(self.channel["id"])
        self.registry.save({**current, "enabled": False}, current["id"], current["version"])
        rejected = await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="disabled-eval-recovery", job_type="recovery", expected_inventory_version="cfg-1"))
        self.assertEqual((rejected.status_code, rejected.json()["error"]["code"]), (404, "channel_not_found"))
        http_calls = []

        def forbidden(request):
            http_calls.append(request.method)
            return httpx.Response(500)

        with patch.object(internal_api, "guarded_transport", lambda: httpx.MockTransport(forbidden)):
            await internal_api.run_pending()
        self.assert_unsent_terminal(job_id, "rejected", "channel_not_found", http_calls)

    async def test_recovery_rejects_production_status_drift_after_queueing(self):
        imported = await self.call("PUT", "/internal/v1/production-inventory/cfg-1", self.inventory(enabled_status="disabled"))
        self.assertEqual(imported.status_code, 200)
        job_id = await self.create(job_type="recovery", expected_inventory_version="cfg-1", scenarios=["non_stream"], rounds=1)
        latest = {**self.inventory(enabled_status="enabled"), "inventory_version": "cfg-2", "generated_at": fixtures.iso(time.time())}
        imported = await self.call("PUT", "/internal/v1/production-inventory/cfg-2", latest)
        self.assertEqual(imported.status_code, 200)
        http_calls = []

        def forbidden(request):
            http_calls.append(request.method)
            return httpx.Response(500)

        with patch.object(internal_api, "guarded_transport", lambda: httpx.MockTransport(forbidden)):
            await internal_api.run_pending()
        self.assert_unsent_terminal(job_id, "rejected", "inventory_version_conflict", http_calls)

    async def test_legacy_lease_recovery_preserves_proven_reservations_without_inventing_usage(self):
        cases = [({"requests": 1, "input_tokens_reserved": 64, "output_tokens_reserved": 128, "output_tokens_reported": 999}, (64, 128)),
                 ({"requests": 1, "input_tokens_reserved": 0, "output_tokens_reserved": 0}, (0, 0)),
                 ({}, (None, None))]
        for index, (old_consumed, reservations) in enumerate(cases):
            with self.subTest(case=index):
                job_id = await self.create(idempotency_key="legacy-ledger-" + str(index))
                store = MonitorStore(self.registry)
                store.claim("legacy-owner")
                with self.registry.connect() as conn:
                    conn.execute("INSERT INTO monitor_probe_results(result_id,job_id,body_json,created_at) VALUES(?,?,?,?)",
                                 ("legacy-result-" + str(index), job_id, json.dumps({"scenario": "short_stream", "round": 1}), time.time()))
                    conn.execute("UPDATE monitor_probe_jobs SET connection_fingerprint=NULL,consumed_json=?,lease_until=? WHERE job_id=?",
                                 (json.dumps(old_consumed), time.time() - 1, job_id))
                self.assertEqual(store.recover_expired_leases(), 1)
                job = store.job(job_id)
                consumed = job["budget_consumed"]
                self.assertEqual((job["status"], job["progress"]["completed_requests"], consumed["requests"]), ("partially_completed", 1, 1))
                self.assertEqual((consumed["input_tokens_reserved"], consumed["output_tokens_reserved"]), reservations)
                self.assertEqual(consumed["reservation_source"], "legacy_persisted")
                self.assertEqual(consumed["reported_usage_source"], "unavailable_legacy")
                self.assertIsNone(consumed["input_tokens_reported"])
                self.assertIsNone(consumed["output_tokens_reported"])
                self.assertEqual(store.recover_expired_leases(), 0)
                self.assertEqual(store.job(job_id), job)


if __name__ == "__main__":
    unittest.main()
