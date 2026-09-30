"""Monitor—Eval internal contract v1.0 with synthetic data and a mocked upstream only."""
import json
import os
import secrets
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from shared import registry as registry_module
from shared.registry import Registry
from features.model_coverage import internal_api, monitor
from features.model_coverage.monitor import MonitorStore
from features.stability.app import storage
from workbench import create_app

KEY_ID, SECRET = "monitor-synthetic-1", "synthetic-monitor-signing-secret-000000"
ENV = {"EVAL_MONITOR_KEY_ID": KEY_ID, "EVAL_MONITOR_SECRET": SECRET, "PLATFORM_USERNAME": "", "PLATFORM_PASSWORD": ""}


async def _completed(channel):
    return {"status": "completed", "ttft_ms": 10, "latency_ms": 20, "usage_complete": True, "actual_model": channel["model"]}


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class MonitorContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        # Synthetic TEST-NET targets are allowlisted for admission checks only; no request is sent to them.
        self.egress = patch("features.stability.app.egress.EGRESS_ALLOWLIST", ("203.0.113.0/24",))
        self.egress.start()
        self.temp = tempfile.TemporaryDirectory(prefix="monitor-synthetic-")
        self.previous_registry, self.previous_db = registry_module._registry, storage.DB_PATH
        storage.close()
        self.registry = Registry(Path(self.temp.name))
        registry_module._registry = self.registry
        storage.DB_PATH = Path(self.temp.name) / "stability.db"
        self.channel = self.registry.save({"name": "Synthetic monitor channel", "base_url": "https://203.0.113.10/v1",
                                           "api_key": "synthetic-monitor-upstream-credential", "multiplier": 1})
        MonitorStore(self.registry).bind_identity(self.channel["id"], "newapi-channel-96")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        storage.close()
        storage.DB_PATH = self.previous_db
        registry_module._registry = self.previous_registry
        self.temp.cleanup()
        self.egress.stop()
        self.env.stop()

    async def call(self, method, path, body=None, *, secret=SECRET, key_id=KEY_ID, nonce=None, timestamp=None,
                   idempotency="auto", client="monitor", extra=None):
        raw = b"" if body is None else json.dumps(body).encode()
        timestamp = timestamp or iso(time.time())
        nonce = nonce or secrets.token_hex(12)
        headers = {"X-Nexus-Client": client, "X-Nexus-Key-Id": key_id, "X-Nexus-Timestamp": timestamp,
                   "X-Nexus-Nonce": nonce, "X-Nexus-Signature": monitor.sign(secret.encode(), method, path, timestamp, nonce, raw),
                   "Content-Type": "application/json", **(extra or {})}
        if idempotency == "auto" and method != "GET":
            idempotency = (body or {}).get("idempotency_key") or secrets.token_hex(8)
        if idempotency and idempotency != "auto":
            headers["Idempotency-Key"] = idempotency
        return await self.client.request(method, path, content=raw, headers=headers)

    def job(self, **changes):
        now = time.time()
        body = {"schema_version": "1.0", "idempotency_key": "INC-1:channel-96:gpt-5.5:openai:v1", "source_event_id": "INC-1",
                "job_type": "incident", "priority": "p1", "channel_identity": "newapi-channel-96", "model": "gpt-5.5",
                "protocol": "openai", "probe_path": "direct", "scenarios": ["short_stream", "long_stream"], "rounds": 2,
                "not_before": iso(now - 5), "expires_at": iso(now + 600),
                "budget": {"max_requests": 4, "max_input_tokens": 1000, "max_output_tokens": 2000, "max_cost_usd": 1.0},
                "reason": "synthetic stream interruption"}
        body.update(changes)
        return body

    # ---- access -----------------------------------------------------------------------------
    async def test_signature_is_required_and_workbench_login_is_not_accepted(self):
        path = "/internal/v1/probe-results?cursor=0"
        ok = await self.call("GET", path)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["schema_version"], "1.0")
        self.assertEqual((await self.client.get(path)).status_code, 403)
        cases = {"wrong_secret": (dict(secret="x" * 40), 401, "invalid_signature"),
                 "wrong_key": (dict(key_id="other-key"), 401, "invalid_credentials"),
                 "wrong_client": (dict(client="workbench"), 403, "client_not_allowed"),
                 "old_timestamp": (dict(timestamp=iso(time.time() - 900)), 401, "signature_expired")}
        for name, (options, status, code) in cases.items():
            with self.subTest(name):
                response = await self.call("GET", path, **options)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (status, code))
                self.assertEqual(response.json()["error"]["retryable"], False)
        with patch.dict(os.environ, {"PLATFORM_USERNAME": "admin", "PLATFORM_PASSWORD": "workbench-password-1"}):
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://testserver")
            async with client:
                basic = await client.get(path, auth=("admin", "workbench-password-1"))
                self.assertEqual(basic.status_code, 403, "workbench login must not open Monitor API")
                self.assertEqual((await client.get("/api/health", headers={"X-Nexus-Client": "monitor"})).status_code, 401)

    async def test_replayed_nonce_and_tampered_body_are_rejected(self):
        body = self.job()
        nonce = secrets.token_hex(12)
        first = await self.call("POST", "/internal/v1/probe-jobs", body, nonce=nonce)
        self.assertEqual(first.status_code, 201)
        replay = await self.call("POST", "/internal/v1/probe-jobs", body, nonce=nonce)
        self.assertEqual((replay.status_code, replay.json()["error"]["code"]), (401, "replayed_request"))
        timestamp, nonce = iso(time.time()), secrets.token_hex(12)
        signed = monitor.sign(SECRET.encode(), "POST", "/internal/v1/probe-jobs", timestamp, nonce, json.dumps(body).encode())
        tampered = await self.client.post("/internal/v1/probe-jobs", content=json.dumps({**body, "rounds": 5}).encode(), headers={
            "X-Nexus-Client": "monitor", "X-Nexus-Key-Id": KEY_ID, "X-Nexus-Timestamp": timestamp, "X-Nexus-Nonce": nonce,
            "X-Nexus-Signature": signed, "Idempotency-Key": body["idempotency_key"]})
        self.assertEqual(tampered.json()["error"]["code"], "invalid_signature")

    async def test_internal_api_is_closed_without_monitor_credentials(self):
        with patch.dict(os.environ, {"EVAL_MONITOR_KEY_ID": "", "EVAL_MONITOR_SECRET": ""}):
            response = await self.call("GET", "/internal/v1/probe-events")
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "monitor_access_disabled"))
        with patch.dict(os.environ, {"EVAL_MONITOR_SECRET": "short"}):
            response = await self.call("GET", "/internal/v1/probe-events", secret="short")
        self.assertEqual(response.json()["error"]["code"], "monitor_access_misconfigured")
        legacy = await self.client.get("/api/model-coverage/internal/v1/probe-results")
        self.assertEqual(legacy.status_code, 404, "unsigned duplicate entry was removed")

    # ---- inventory --------------------------------------------------------------------------
    def inventory(self, **changes):
        channel = {"channel_identity": "newapi-channel-96", "newapi_channel_id": 96, "display_name": "synthetic_pool",
                   "supplier_failure_domain_id": "supplier-synthetic-a", "enabled_status": "enabled", "groups": ["g1"],
                   "models": ["gpt-5.5", "synthetic-unlisted"], "channel_type": "openai-compatible",
                   "production_role": "primary", "business_criticality": "high", "config_fingerprint": "sha256:aaa"}
        channel.update(changes)
        unbound = {**channel, "channel_identity": "newapi-channel-7", "models": ["gpt-5.5"]}
        return {"schema_version": "1.0", "inventory_version": "cfg-1", "generated_at": iso(time.time() - 60),
                "newapi_version": "v1.0.0-rc.26", "channels": [channel, unbound]}

    async def test_inventory_is_idempotent_and_reports_gaps(self):
        first = await self.call("PUT", "/internal/v1/production-inventory/cfg-1", self.inventory())
        self.assertEqual(first.status_code, 200, first.text)
        data = first.json()
        self.assertEqual((data["accepted"], data["channel_count"]), (True, 2))
        reasons = {(g["channel_identity"], g["model"]): g["reason"] for g in data["coverage_gaps"]}
        self.assertEqual(reasons[("newapi-channel-96", "gpt-5.5")], "no_fresh_probe")
        self.assertEqual(reasons[("newapi-channel-96", "synthetic-unlisted")], "model_not_in_eval_catalog")
        self.assertEqual(reasons[("newapi-channel-7", "gpt-5.5")], "target_channel_not_verified")
        again = await self.call("PUT", "/internal/v1/production-inventory/cfg-1", self.inventory())
        self.assertEqual(again.json(), data)
        changed = await self.call("PUT", "/internal/v1/production-inventory/cfg-1", self.inventory(priority=1, models=["gpt-5.5"]))
        self.assertEqual((changed.status_code, changed.json()["error"]["code"]), (409, "inventory_version_conflict"))
        missing_key = await self.call("PUT", "/internal/v1/production-inventory/cfg-2", self.inventory(), idempotency=None)
        self.assertEqual(missing_key.json()["error"]["code"], "idempotency_key_required")
        secret = await self.call("PUT", "/internal/v1/production-inventory/cfg-3", self.inventory(display_name="sk-abcdefgh12345"))
        self.assertEqual(secret.status_code, 400)

    async def test_schema_major_mismatch_is_422(self):
        response = await self.call("PUT", "/internal/v1/production-inventory/cfg-9", {**self.inventory(), "schema_version": "2.0"})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (422, "unsupported_schema_version"))

    # ---- probe jobs -------------------------------------------------------------------------
    async def test_same_idempotency_key_creates_one_job(self):
        first = await self.call("POST", "/internal/v1/probe-jobs", self.job())
        second = await self.call("POST", "/internal/v1/probe-jobs", self.job())
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.json()["job_id"], second.json()["job_id"])
        self.assertEqual(first.json()["status"], "queued")
        conflict = await self.call("POST", "/internal/v1/probe-jobs", self.job(rounds=1))
        self.assertEqual((conflict.status_code, conflict.json()["error"]["code"]), (409, "idempotency_conflict"))
        mismatch = await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="other-key"), idempotency="header-key")
        self.assertEqual(mismatch.json()["error"]["code"], "idempotency_key_mismatch")
        with self.registry.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM monitor_probe_jobs").fetchone()[0], 1)

    async def test_contract_rejections(self):
        now = time.time()
        cases = {
            "unbound channel": (self.job(idempotency_key="r1", channel_identity="newapi-channel-404"), 422, "target_channel_not_verified"),
            "over budget": (self.job(idempotency_key="r2", budget={"max_requests": 3, "max_input_tokens": 1000, "max_output_tokens": 2000}), 422, "budget_exceeded"),
            "responses output floor": (self.job(idempotency_key="r3", protocol="responses"), 422, "budget_exceeded"),
            "end to end": (self.job(idempotency_key="r4", probe_path="end_to_end", test_identity="x", isolated_group="y",
                                    target_channel_identity="newapi-channel-96", exclude_from_business_metrics=True), 422, "end_to_end_isolation_unavailable"),
            "user content": (self.job(idempotency_key="r5", messages=[{"role": "user", "content": "real text"}]), 400, "user_content_forbidden"),
            "already expired": (self.job(idempotency_key="r6", expires_at=iso(now - 1)), 400, "invalid_time_window"),
            "unknown scenario": (self.job(idempotency_key="r7", scenarios=["free_prompt"]), 400, "invalid_field"),
            "unknown inventory": (self.job(idempotency_key="r8", expected_inventory_version="cfg-missing"), 409, "unknown_inventory_version"),
        }
        for name, (body, status, code) in cases.items():
            with self.subTest(name):
                response = await self.call("POST", "/internal/v1/probe-jobs", body)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (status, code), response.text)
        self.registry.save({**{k: self.channel[k] for k in ("name", "base_url", "scope", "multiplier", "note", "status")},
                            "enabled": False}, self.channel["id"], self.channel["version"])
        disabled = await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="r9"))
        self.assertEqual((disabled.status_code, disabled.json()["error"]["code"]), (404, "channel_not_found"))

    async def test_private_target_is_rejected_by_egress_policy(self):
        private = self.registry.save({"name": "Synthetic private", "base_url": "http://10.0.0.8/v1", "api_key": "synthetic-private-key", "multiplier": 1})
        MonitorStore(self.registry).bind_identity(private["id"], "newapi-channel-private")
        with patch.dict(os.environ, {"PLATFORM_EGRESS_ALLOWLIST": ""}), patch("features.stability.app.egress.EGRESS_ALLOWLIST", ()):
            response = await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="egress", channel_identity="newapi-channel-private"))
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (422, "egress_denied"))

    async def test_inventory_version_conflict_blocks_changed_target(self):
        await self.call("PUT", "/internal/v1/production-inventory/cfg-1", self.inventory())
        await self.call("PUT", "/internal/v1/production-inventory/cfg-2", {**self.inventory(config_fingerprint="sha256:bbb"), "inventory_version": "cfg-2", "generated_at": iso(time.time() - 5)})
        stale = await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="v1", expected_inventory_version="cfg-1"))
        self.assertEqual((stale.status_code, stale.json()["error"]["code"]), (409, "inventory_version_conflict"))
        current = await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="v2", expected_inventory_version="cfg-2"))
        self.assertEqual(current.status_code, 201, current.text)
        not_served = await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="v3", model="gpt-5.4", expected_inventory_version="cfg-2"))
        self.assertEqual(not_served.json()["error"]["code"], "model_not_in_inventory")

    # ---- executor, results and events -------------------------------------------------------
    async def run_with(self, statuses):
        """Mock only the upstream: each call pops the next synthetic transport result."""
        calls = []

        async def send(channel, probe):
            calls.append((channel["model"], probe["id"], channel["api_key"]))
            status = statuses.pop(0)
            return {"status": status, "ok": status == "completed", "latency_ms": 900, "ttft_ms": 120 if status != "timeout" else None,
                    "output_tokens": 40, "actual_model": channel["model"], "usage_complete": status == "completed",
                    "model_mismatch": False, "error": "HTTP 503" if status == "upstream_5xx" else ""}
        await internal_api.run_pending(send)
        return calls

    async def test_executor_publishes_ordered_results_and_reproduced_event(self):
        job_id = (await self.call("POST", "/internal/v1/probe-jobs", self.job())).json()["job_id"]
        calls = await self.run_with(["completed", "stream_break", "completed", "stream_break"])
        self.assertEqual([c[1] for c in calls], ["monitor-short_stream", "monitor-long_stream"] * 2)
        self.assertEqual({c[2] for c in calls}, {"synthetic-monitor-upstream-credential"})
        job = (await self.call("GET", f"/internal/v1/probe-jobs/{job_id}")).json()
        self.assertEqual((job["status"], job["progress"]["completed_requests"], job["budget_consumed"]["requests"]), ("completed", 4, 4))
        self.assertNotIn("synthetic-monitor-upstream-credential", json.dumps(job))
        page = (await self.call("GET", "/internal/v1/probe-results?cursor=0&limit=3")).json()
        self.assertEqual((len(page["items"]), page["has_more"]), (3, True))
        rest = (await self.call("GET", f"/internal/v1/probe-results?cursor={page['next_cursor']}&limit=3")).json()
        items = page["items"] + rest["items"]
        self.assertEqual([(i["scenario"], i["round"]) for i in items],
                         [("short_stream", 1), ("long_stream", 1), ("short_stream", 2), ("long_stream", 2)])
        broken = items[1]
        self.assertEqual((broken["outcome"], broken["error_category"], broken["stream_complete"], broken["usage_status"]),
                         ("interrupted", "stream_interrupted_midstream", False, "missing"))
        self.assertEqual((items[0]["outcome"], items[0]["error_category"], items[0]["http_status"]), ("success", None, None))
        self.assertTrue(all(i["exclude_from_business_metrics"] and i["job_id"] == job_id for i in items))
        self.assertEqual(job["result_ids"], [i["result_id"] for i in items])
        replay = (await self.call("GET", "/internal/v1/probe-results?cursor=0&limit=200")).json()["items"]
        self.assertEqual(replay, items, "re-pulling from an old cursor returns identical, unmodified results")
        events = (await self.call("GET", "/internal/v1/probe-events?cursor=0")).json()["items"]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["event_type"], events[0]["scenario"], events[0]["recommended_monitor_action"]),
                         ("probe_failure_reproduced", "long_stream", "compare_with_production_traffic"))
        self.assertEqual(events[0]["result_ids"], [items[1]["result_id"], items[3]["result_id"]])
        with self.registry.connect() as conn:
            stored = " ".join(r[0] for r in conn.execute("SELECT body_json FROM monitor_probe_results"))
        self.assertNotIn("synthetic-monitor-upstream-credential", stored)
        self.assertNotIn("整数", stored, "probe prompts are not stored in results")

    async def test_no_event_for_mixed_or_eval_side_failures(self):
        await self.call("POST", "/internal/v1/probe-jobs", self.job(scenarios=["short_stream"]))
        await self.run_with(["upstream_5xx", "timeout"])
        results = (await self.call("GET", "/internal/v1/probe-results")).json()["items"]
        self.assertEqual([(r["error_category"], r["http_status"]) for r in results], [("upstream_5xx", 503), ("transport_timeout", None)])
        self.assertEqual((await self.call("GET", "/internal/v1/probe-events")).json()["items"], [])
        await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="eval-side", scenarios=["short_stream"]))
        await self.run_with(["egress_denied", "egress_denied"])
        self.assertEqual((await self.call("GET", "/internal/v1/probe-events")).json()["items"], [])

    async def test_cancel_expiry_and_budget_stop_before_new_requests(self):
        queued = (await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="c1"))).json()["job_id"]
        first = await self.call("POST", f"/internal/v1/probe-jobs/{queued}/cancel", {}, idempotency="cancel-1")
        again = await self.call("POST", f"/internal/v1/probe-jobs/{queued}/cancel", {}, idempotency="cancel-2")
        self.assertEqual((first.status_code, first.json()["status"], again.json()["status"]), (202, "cancelled", "cancelled"))
        self.assertEqual(await self.run_with([]), [], "cancelled job sends nothing")
        # Cancel arrives while running: the request in flight finishes, the next ones are skipped.
        running = (await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="c2"))).json()["job_id"]
        store = MonitorStore(self.registry)

        async def send(channel, probe):
            store.cancel(running)
            return {"status": "completed", "ttft_ms": 10, "latency_ms": 20, "usage_complete": True, "actual_model": channel["model"]}
        await internal_api.run_pending(send)
        job = store.job(running)
        self.assertEqual((job["status"], job["progress"]["completed_requests"], len(job["skipped"])), ("cancelled", 1, 3))
        self.assertEqual({s["reason"] for s in job["skipped"]}, {"cancel_requested"})
        # Expired before start: no upstream request.
        expiring = (await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="c3", expires_at=iso(time.time() + 2)))).json()["job_id"]
        self.assertEqual(store.expire_due(time.time() + 5), 1)
        self.assertEqual(await self.run_with([]), [])
        self.assertEqual((store.job(expiring)["status"], store.job(expiring)["status_reason"]), ("expired", "expired_before_start"))
        # Budget re-checked before each request: stop cleanly with a clear reason.
        tight = (await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="c4"))).json()["job_id"]
        with self.registry.connect() as conn:
            budget = json.loads(conn.execute("SELECT budget_json FROM monitor_probe_jobs WHERE job_id=?", (tight,)).fetchone()[0])
            conn.execute("UPDATE monitor_probe_jobs SET budget_json=? WHERE job_id=?", (json.dumps({**budget, "max_requests": 2}), tight))
        calls = await self.run_with(["completed"] * 4)
        self.assertEqual(len(calls), 2)
        self.assertEqual((store.job(tight)["status"], store.job(tight)["status_reason"]), ("partially_completed", "budget_exhausted"))

    def expire_lease(self, job_id):
        with self.registry.connect() as conn:
            conn.execute("UPDATE monitor_probe_jobs SET lease_until=? WHERE job_id=?", (time.time() - 1, job_id))

    async def test_restart_recovery_keeps_jobs_readable(self):
        # Real startup path: a job left running by a stopped process is ended, and stays queryable.
        job_id = (await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="lease"))).json()["job_id"]
        store = MonitorStore(self.registry)
        self.assertEqual(store.claim("executor-old")["job_id"], job_id)
        self.expire_lease(job_id)
        with patch.dict(os.environ, {"EVAL_MONITOR_EXECUTOR": "live"}):
            self.assertTrue(await internal_api.start_executor())
        await internal_api.stop_executor()
        job = store.job(job_id)
        self.assertEqual((job["status"], job["status_reason"]), ("failed", "executor_lease_lost"))
        self.assertTrue(job["finished_at"].endswith("Z"))
        self.assertEqual((await self.call("GET", f"/internal/v1/probe-jobs/{job_id}")).status_code, 200)
        self.assertEqual((await self.client.get("/api/model-coverage/monitor/jobs")).status_code, 200)

    async def test_live_lease_in_another_process_is_not_recovered(self):
        job_id = (await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="live-lease"))).json()["job_id"]
        store = MonitorStore(self.registry)
        store.claim("executor-other")
        self.assertEqual(store.recover_expired_leases(), 0)
        self.assertEqual(store.job(job_id)["status"], "running")

    async def test_executor_stops_when_job_is_ended_elsewhere(self):
        job_id = (await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="lost"))).json()["job_id"]
        store, sent = MonitorStore(self.registry), []

        async def send(channel, probe):
            sent.append(probe["id"])
            if len(sent) == 1:  # another path ends the job while the first request is in flight
                self.expire_lease(job_id)
                store.recover_expired_leases()
            return {"status": "completed", "ttft_ms": 10, "latency_ms": 20, "usage_complete": True, "actual_model": channel["model"]}
        await internal_api.run_pending(send)
        self.assertEqual(len(sent), 1, "no new request after ownership is lost")
        results = (await self.call("GET", "/internal/v1/probe-results")).json()["items"]
        self.assertEqual(results, [], "the in-flight result is not recorded under a lost lease")
        self.assertEqual(store.job(job_id)["status"], "failed")

    async def test_executor_error_ends_job_immediately_and_queue_continues(self):
        broken = (await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="boom", priority="p0"))).json()["job_id"]
        healthy = (await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="after-boom", priority="p3"))).json()["job_id"]
        store, sent = MonitorStore(self.registry), []
        real_append = MonitorStore.append_result

        def append(self_, job_id, owner, body):
            if job_id == broken and len(sent) == 2:
                raise RuntimeError("synthetic storage failure")
            return real_append(self_, job_id, owner, body)

        async def send(channel, probe):
            sent.append(probe["id"])
            return await _completed(channel)
        with patch.object(MonitorStore, "append_result", append):
            await internal_api.run_pending(send)
        job = store.job(broken)
        self.assertEqual((job["status"], job["status_reason"]), ("partially_completed", "executor_error"))
        self.assertEqual(job["progress"]["completed_requests"], 1)
        self.assertEqual(job["budget_consumed"]["requests"], 2, "consumption keeps every request sent, including the in-flight one")
        self.assertEqual(len(job["skipped"]), 2)
        self.assertEqual({s["reason"] for s in job["skipped"]}, {"executor_error"})
        self.assertEqual(store.job(healthy)["status"], "completed", "one failing job does not block the queue")

    async def test_recovered_lease_counts_stored_results(self):
        job_id = (await self.call("POST", "/internal/v1/probe-jobs", self.job(idempotency_key="backfill"))).json()["job_id"]
        store = MonitorStore(self.registry)
        job = store.claim("executor-gone")
        store.append_result(job_id, "executor-gone", {"scenario": "short_stream"})
        self.expire_lease(job_id)
        store.recover_expired_leases()
        job = store.job(job_id)
        self.assertEqual((job["status"], job["progress"]["completed_requests"]), ("partially_completed", 1))

    async def test_oversized_body_is_refused_before_buffering(self):
        path = "/internal/v1/probe-jobs"
        declared = await self.call("POST", path, self.job(), extra={"Content-Length": str(monitor.MAX_BODY_BYTES + 1)})
        self.assertEqual((declared.status_code, declared.json()["error"]["code"]), (413, "request_too_large"))
        read = []

        async def stream():
            for _ in range(8):
                read.append(1)
                yield b"x" * (monitor.MAX_BODY_BYTES // 4)
        timestamp, nonce = iso(time.time()), secrets.token_hex(12)
        headers = {"X-Nexus-Client": "monitor", "X-Nexus-Key-Id": KEY_ID, "X-Nexus-Timestamp": timestamp,
                   "X-Nexus-Nonce": nonce, "X-Nexus-Signature": "v1=00", "Idempotency-Key": "big"}
        chunked = await self.client.post(path, content=stream(), headers=headers)
        self.assertEqual(chunked.status_code, 413)
        self.assertLessEqual(len(read), 5, "reading stops as soon as the limit is crossed")

    async def test_trailing_slash_is_404_without_redirect(self):
        response = await self.client.get("/internal/v1/probe-results/", headers={"Host": "evil.example"})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (404, "not_found"))
        self.assertNotIn("location", response.headers)
        request_id = response.json()["error"]["request_id"]
        self.assertTrue(request_id.startswith("eval-"))
        self.assertEqual(response.headers["x-request-id"], request_id)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    async def test_signature_covers_raw_encoded_path(self):
        job_id = (await self.call("POST", "/internal/v1/probe-jobs", self.job())).json()["job_id"]
        encoded = "/internal/v1/probe-jobs/" + job_id.replace("-", "%2D", 1)
        self.assertEqual((await self.call("GET", encoded)).status_code, 200, "signed over the bytes actually sent")
        timestamp, nonce = iso(time.time()), secrets.token_hex(12)
        decoded_sig = monitor.sign(SECRET.encode(), "GET", f"/internal/v1/probe-jobs/{job_id}", timestamp, nonce, b"")
        mismatch = await self.client.get(encoded, headers={"X-Nexus-Client": "monitor", "X-Nexus-Key-Id": KEY_ID,
                                         "X-Nexus-Timestamp": timestamp, "X-Nexus-Nonce": nonce, "X-Nexus-Signature": decoded_sig})
        self.assertEqual(mismatch.json()["error"]["code"], "invalid_signature")

    async def test_late_older_inventory_cannot_replace_current(self):
        newer = {**self.inventory(), "inventory_version": "cfg-new", "generated_at": iso(time.time() - 10)}
        older = {**self.inventory(), "inventory_version": "cfg-old", "generated_at": iso(time.time() - 3600)}
        self.assertEqual((await self.call("PUT", "/internal/v1/production-inventory/cfg-new", newer)).status_code, 200)
        late = await self.call("PUT", "/internal/v1/production-inventory/cfg-old", older)
        self.assertEqual((late.status_code, late.json()["error"]["code"]), (409, "invalid_inventory_version"))
        created = await self.call("POST", "/internal/v1/probe-jobs", self.job(expected_inventory_version="cfg-new"))
        self.assertEqual(created.status_code, 201, created.text)
        same_second = {**newer, "inventory_version": "cfg-same", "channels": [{**newer["channels"][0], "config_fingerprint": "sha256:zzz"}]}
        tie = await self.call("PUT", "/internal/v1/production-inventory/cfg-same", same_second)
        self.assertEqual((tie.status_code, tie.json()["error"]["code"]), (409, "invalid_inventory_version"))
        self.assertEqual(MonitorStore(self.registry).latest_inventory_version(), "cfg-new")

    async def test_cancel_requests_are_audited(self):
        job_id = (await self.call("POST", "/internal/v1/probe-jobs", self.job())).json()["job_id"]
        await self.call("POST", f"/internal/v1/probe-jobs/{job_id}/cancel", {}, idempotency="cancel-a")
        await self.call("POST", f"/internal/v1/probe-jobs/{job_id}/cancel", {}, idempotency="cancel-b")
        audit = MonitorStore(self.registry).audit(job_id)
        self.assertEqual([(a["idempotency_key"], a["status_before"], a["status_after"]) for a in audit],
                         [("cancel-a", "queued", "cancelled"), ("cancel-b", "cancelled", "cancelled")])

    async def test_cursor_validation_and_unknown_job(self):
        bad = await self.call("GET", "/internal/v1/probe-results?cursor=12")
        self.assertEqual((bad.status_code, bad.json()["error"]["code"]), (400, "invalid_cursor"))
        self.assertEqual((await self.call("GET", "/internal/v1/probe-results?limit=500")).status_code, 400)
        self.assertEqual((await self.call("GET", "/internal/v1/probe-jobs/probe-missing")).status_code, 404)

    async def test_executor_stays_off_unless_explicitly_live(self):
        with patch.dict(os.environ, {"EVAL_MONITOR_EXECUTOR": ""}):
            self.assertFalse(await internal_api.start_executor())
        self.assertEqual(internal_api.executor_status(), {"enabled": False, "running": False})

    # ---- workbench-side identity binding ----------------------------------------------------
    async def test_binding_is_unique_and_editable_from_workbench_api(self):
        other = self.registry.save({"name": "Synthetic second", "base_url": "https://203.0.113.11/v1", "api_key": "synthetic-second-key", "multiplier": 1})
        taken = await self.client.put(f"/api/model-coverage/monitor/identities/{other['id']}", json={"channel_identity": "newapi-channel-96"})
        self.assertEqual(taken.status_code, 409)
        bound = await self.client.put(f"/api/model-coverage/monitor/identities/{other['id']}", json={"channel_identity": "newapi-channel-97"})
        self.assertEqual(bound.json(), {"channel_id": other["id"], "channel_identity": "newapi-channel-97"})
        listed = (await self.client.get("/api/model-coverage/monitor/identities")).json()["identities"]
        self.assertEqual(listed, {str(self.channel["id"]): "newapi-channel-96", str(other["id"]): "newapi-channel-97"})
        cleared = await self.client.put(f"/api/model-coverage/monitor/identities/{other['id']}", json={"channel_identity": ""})
        self.assertIsNone(cleared.json()["channel_identity"])
        invalid = await self.client.put(f"/api/model-coverage/monitor/identities/{other['id']}", json={"channel_identity": "https://x"})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual((await self.client.put("/api/model-coverage/monitor/identities/9999", json={"channel_identity": "a"})).status_code, 404)


if __name__ == "__main__":
    unittest.main()
