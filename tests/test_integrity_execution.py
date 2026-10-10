"""Durable integrity execution with synthetic channels, real parsers, and no sockets."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from features.integrity import transport
from features.integrity.durable import IntegrityStore, default_pricing
from features.integrity.execution import ExecutionStopped, ProbeRequest, ResolvedTarget, execute_requests, estimate_input_tokens
from shared.registry import Registry
from tests.test_monitor_transport import upstream_response, usage_for


class IntegrityExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="integrity-execution-")
        self.registry = Registry(Path(self.temp.name))
        self.channel = self.registry.save({"name": "Fixture", "base_url": "https://fixture.example/v1",
                                           "api_key": "fixture-integrity-credential", "multiplier": 1})
        self.store = IntegrityStore(self.registry)
        self.target = {"registry_channel_id": self.channel["id"], "connection_fingerprint": self.registry.connection_fingerprint(self.registry.resolve(self.channel["id"])),
                       "model": "gpt-6-astra", "protocol": "openai", "mapping_revision": "v1"}
        self.requests = [ProbeRequest(f"request-{i}", {"id": f"probe-{i}", "name": "Fixture", "prompt": "Independent synthetic request",
                                      "stream": False, "max_tokens": 10}, 400, 10) for i in range(2)]

    async def asyncTearDown(self):
        self.temp.cleanup()

    def enqueue(self, key="fixture-1", **changes):
        options = dict(idempotency_key=key, target_snapshot=self.target, strategy={"strategy_id": "fixture", "manifest_hash": "a" * 64},
                       requests=self.requests, limits={"max_requests": 5, "max_input_tokens": 5000, "max_output_tokens": 100},
                       pricing=default_pricing("gpt-6-astra"), deadline=time.time() + 60,
                       budget_date="2026-10-08", daily_limits={"max_requests": 5, "max_input_tokens": 10000, "max_output_tokens": 100})
        options.update(changes)
        return self.store.enqueue(**options)

    def resolve(self):
        channel = self.registry.resolve(self.channel["id"])
        snapshot = {**self.target, "connection_fingerprint": self.registry.connection_fingerprint(channel)}
        return ResolvedTarget({**channel, "model": self.target["model"], "protocol": self.target["protocol"]}, snapshot)

    def session(self, job):
        claimed = self.store.claim(job_id=job["job_id"], owner="fixture-owner")
        self.assertIsNotNone(claimed)
        return self.store.session(claimed["job_id"], claimed["owner"])

    @staticmethod
    def project(request, raw, started, finished):
        return {"status": raw["status"], "valid": raw["status"] == "completed", "input_tokens_reported": raw.get("input_tokens_reported"),
                "output_tokens_reported": raw.get("output_tokens_reported")}

    async def test_missing_usage_is_unknown_and_explicit_zero_is_zero(self):
        for key, raw, expected in [("missing", {"status": "completed"}, None),
                ("zero", {"status": "completed", "input_tokens_reported": 0, "output_tokens_reported": 0}, 0)]:
            job = self.enqueue(key, budget_key=key)
            async def send(channel, probe, *, before_send):
                await before_send()
                return raw
            self.assertEqual(await execute_requests(self.session(job), self.requests, self.resolve, send, self.project), "completed")
            fees = self.store.job(job["job_id"])["fees"]
            self.assertEqual(fees["estimated_usd"], expected)
            self.assertIsNone(fees["stopping_limit"])
            self.assertEqual(fees["unknown_requests"], 2 if expected is None else 0)

    async def test_same_day_over_three_dollars_and_missing_price_do_not_stop(self):
        for key, pricing in [("expensive", {"input_usd_per_million": 1000000, "output_usd_per_million": 1000000, "source": "configured_estimate"}), ("no-price", None)]:
            jobs = [self.enqueue(f"{key}-{i}", requests=[self.requests[0]], pricing=pricing, plan_version=f"v{i}", budget_key=key) for i in range(3)]
            sends = []
            async def send(channel, probe, *, before_send):
                await before_send()
                sends.append(probe["id"])
                return {"status": "completed", "input_tokens_reported": 10, "output_tokens_reported": 10}
            for job in jobs:
                self.assertEqual(await execute_requests(self.session(job), [self.requests[0]], self.resolve, send, self.project), "completed")
            self.assertEqual(len(sends), 3)
            fees = self.store.job(jobs[-1]["job_id"])["fees"]
            self.assertEqual(fees["estimated_usd"], 20 if pricing else None)
            self.assertEqual(self.store.daily_budget(budget_key=key, budget_date="2026-10-08")["attempted_requests"], 3)

    async def test_parallel_fixed_manifests_share_usage_without_a_daily_cap(self):
        jobs = [self.enqueue(f"parallel-{i}", requests=[self.requests[0]], plan_version=f"v{i}", daily_limits={"max_requests": 2, "max_input_tokens": 10000, "max_output_tokens": 100}) for i in range(4)]
        sessions = [self.session(job) for job in jobs]
        def reserve(session):
            try:
                return session.reserve(self.requests[0], self.resolve())
            except ExecutionStopped as exc:
                return exc.reason
        outcomes = await asyncio.gather(*(asyncio.to_thread(reserve, session) for session in sessions))
        self.assertTrue(all(isinstance(value, dict) for value in outcomes))
        self.assertEqual(len({value["attempt_id"] for value in outcomes}), 4)
        daily = self.store.daily_budget(budget_date="2026-10-08")
        self.assertEqual(daily["attempted_requests"], 4)
        self.assertEqual(daily["input_tokens_reserved"], 1600)

    async def test_reported_usage_above_estimates_survives_yield_and_does_not_resend(self):
        job = self.enqueue("high-usage")
        sent = []
        async def send(channel, probe, *, before_send):
            await before_send()
            sent.append(probe["id"])
            return {"status": "completed", "input_tokens_reported": 4390,
                    "output_tokens_reported": 11}
        self.assertEqual(await execute_requests(self.session(job), self.requests,
            self.resolve, send, self.project, yield_after=1), "yielded")
        self.assertEqual(self.store.job(job["job_id"])["status"], "queued")
        self.assertEqual(await execute_requests(self.session(job), self.requests,
            self.resolve, send, self.project, yield_after=1), "completed")
        final = self.store.job(job["job_id"])
        self.assertEqual(sent, ["probe-0", "probe-1"])
        self.assertEqual(final["consumed"]["requests"], 2)
        self.assertTrue(final["reservation_exceeded"])
        self.assertTrue(all(row["reservation_exceeded"] for row in final["results"]))
        self.assertEqual([row["input_tokens_reported"] for row in final["results"]], [4390, 4390])
        self.assertIsNone(self.store.claim(job_id=job["job_id"], owner="another-owner"))

    async def test_reported_usage_is_recorded_without_blocking_next_reservation(self):
        job = self.enqueue("direct-high-usage")
        session = self.session(job)
        first = session.reserve(self.requests[0], self.resolve())
        self.assertTrue(session.complete(self.requests[0], first,
            {"status": "completed", "valid": True, "input_tokens_reported": 4390,
             "output_tokens_reported": 11}))
        second = session.reserve(self.requests[1], self.resolve())
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        with self.assertRaisesRegex(ExecutionStopped, "attempt_already_permitted"):
            session.reserve(self.requests[1], self.resolve())

    async def test_single_completed_probe_with_high_usage_stays_completed(self):
        job = self.enqueue("single-high-usage", requests=self.requests[:1])
        async def send(channel, probe, *, before_send):
            await before_send()
            return {"status": "completed", "input_tokens_reported": 4390,
                    "output_tokens_reported": 11}
        self.assertEqual(await execute_requests(self.session(job), self.requests[:1],
            self.resolve, send, self.project), "completed")
        final = self.store.job(job["job_id"])
        self.assertTrue(final["reservation_exceeded"])
        self.assertEqual(final["reason"], "")

    async def test_recovery_retains_confirmed_unknown_and_fences_old_owner(self):
        job = self.enqueue()
        old = self.session(job)
        confirmed = old.reserve(self.requests[0], self.resolve())
        old.complete(self.requests[0], confirmed, {"valid": True, "status": "completed"})
        unknown = old.reserve(self.requests[1], self.resolve())
        with self.registry.connect() as conn:
            conn.execute("UPDATE integrity_jobs SET lease_until=? WHERE job_id=?", (time.time() - 1, job["job_id"]))
        self.assertEqual(self.store.recover_expired_leases(), 1)
        claimed = self.store.claim(job_id=job["job_id"], owner="new-owner")
        self.assertIsNotNone(claimed)
        self.assertFalse(old.complete(self.requests[1], unknown, {"status": "completed", "valid": True}))
        sends = []
        async def send(channel, probe):
            sends.append(probe)
            return {"status": "completed"}
        self.assertEqual(await execute_requests(self.store.session(job["job_id"], claimed["owner"]), self.requests, self.resolve, send, self.project), "partially_completed")
        final = self.store.job(job["job_id"])
        self.assertEqual(sends, [])
        self.assertEqual((len(final["results"]), final["consumed"]["requests"], final["consumed"]["unknown_requests"]), (1, 2, 1))
        self.assertIsNone(final["fees"]["estimated_usd"])

    async def test_same_caller_reclaim_fences_every_old_write(self):
        job = self.enqueue()
        old = self.session(job)
        attempt = old.reserve(self.requests[0], self.resolve())
        with self.registry.connect() as conn:
            conn.execute("UPDATE integrity_jobs SET lease_until=? WHERE job_id=?", (time.time()-1, job["job_id"]))
        claimed = self.store.claim(job_id=job["job_id"], owner="fixture-owner")
        self.assertNotEqual(old.owner, claimed["owner"])
        with self.assertRaisesRegex(ExecutionStopped, "executor_lease_lost"):
            old.reserve(self.requests[1], self.resolve())
        self.assertFalse(old.complete(self.requests[0], attempt, {"status":"completed", "valid":True}))
        self.assertFalse(old.finish("failed", "stale_executor", []))
        self.assertEqual(old.heartbeat(), "lost")
        self.assertEqual(self.store.job(job["job_id"])["status"], "running")
        current = self.store.session(job["job_id"], claimed["owner"])
        self.assertEqual(current.heartbeat(), "continue")
        self.assertTrue(current.finish("completed", "", []))

    async def test_cancel_waiting_for_transport_slot_sends_nothing_and_resume_preserves(self):
        job = self.enqueue()
        entered = asyncio.Event()
        async def send(channel, probe, *, before_send):
            entered.set()
            await asyncio.Event().wait()
            await before_send()
            self.fail("cancelled job sent a request")
        runner = asyncio.create_task(execute_requests(self.session(job), self.requests, self.resolve, send, self.project))
        await entered.wait()
        self.store.cancel(job["job_id"])
        self.assertEqual(await asyncio.wait_for(runner, 1), "cancelled")
        self.assertEqual(self.store.job(job["job_id"])["consumed"]["requests"], 0)
        self.assertEqual(self.store.resume(job["job_id"])["status"], "queued")

    async def test_cancel_inflight_retains_unknown_and_resume_never_repeats_it(self):
        job = self.enqueue()
        permitted = asyncio.Event()
        async def waiting(channel, probe, *, before_send):
            await before_send(); permitted.set()
            await asyncio.Event().wait()
        runner = asyncio.create_task(execute_requests(self.session(job), self.requests, self.resolve, waiting, self.project))
        await asyncio.wait_for(permitted.wait(), 1)
        self.store.cancel(job["job_id"])
        self.assertEqual(await asyncio.wait_for(runner, 1), "cancelled")
        self.assertEqual(self.store.job(job["job_id"])["consumed"]["unknown_requests"], 1)
        self.store.resume(job["job_id"])
        sent = []
        async def complete(channel, probe, *, before_send):
            await before_send(); sent.append(probe["id"])
            return {"status": "completed"}
        self.assertEqual(await execute_requests(self.session(job), self.requests, self.resolve, complete, self.project), "partially_completed")
        self.assertEqual(sent, ["probe-1"])
        self.assertEqual(self.store.job(job["job_id"])["consumed"]["requests"], 2)

    async def test_registry_status_and_mapping_changes_after_resolution_rejected_atomically(self):
        from features.integrity.execution import resolve_registry_target
        from features.model_coverage.catalog import Catalog
        self.registry.save({**self.registry.get(self.channel["id"]),"status":"online","enabled":True,"api_key":""}, self.channel["id"], self.registry.get(self.channel["id"])["version"])
        catalog=Catalog(self.registry)
        for case in ("status","mapping","disabled","mapping_manual"):
            self.registry.save({**self.registry.get(self.channel["id"]),"status":"online","enabled":True,"api_key":""}, self.channel["id"], self.registry.get(self.channel["id"])["version"])
            cached=resolve_registry_target(self.registry,self.channel["id"],"gpt-6-astra","responses",require_online=case in {"status","mapping"})
            self.target=cached.snapshot
            job=self.enqueue(key=case,budget_key=case)
            if case=="status":
                self.registry.save({**self.registry.get(self.channel["id"]),"status":"recorded","api_key":""}, self.channel["id"], self.registry.get(self.channel["id"])["version"])
            elif case=="disabled":
                self.registry.save({**self.registry.get(self.channel["id"]),"enabled":False,"api_key":""},self.channel["id"],self.registry.get(self.channel["id"])["version"])
            else:
                model=next(m for m in catalog.models() if m["model"]=="gpt-6-astra")
                catalog.bind(self.channel["id"],model["id"],"synthetic-new-alias-"+case,"responses")
            sent=[]
            async def send(channel,probe,*,before_send):
                await before_send();sent.append(probe["id"])
                return {"status":"completed"}
            self.assertEqual(await execute_requests(self.session(job),self.requests,lambda:cached,send,self.project),"rejected")
            self.assertEqual(sent,[])
            self.assertEqual(self.store.job(job["job_id"])["consumed"]["requests"],0)

    async def test_connection_credentials_mapping_and_enable_are_revalidated_at_send(self):
        for case in ("credential", "disabled", "mapping"):
            job = self.enqueue(case, budget_key=case)
            session = self.session(job)
            async def send(channel, probe, *, before_send):
                if case == "mapping":
                    self.target["mapping_revision"] = "v2"
                else:
                    current = self.registry.get(self.channel["id"])
                    self.registry.save({**current, **({"enabled": False} if case == "disabled" else {"api_key": "fixture-changed-credential"})}, current["id"], current["version"])
                await before_send()
                self.fail("changed target sent a request")
            def safe_resolve():
                try:
                    return self.resolve()
                except ValueError:
                    return None
            self.assertEqual(await execute_requests(session, self.requests, safe_resolve, send, self.project), "rejected")
            self.assertEqual(self.store.job(job["job_id"])["consumed"]["requests"], 0)
            current = self.registry.get(self.channel["id"])
            self.registry.save({**current, "enabled": True, "api_key": "fixture-integrity-credential"}, current["id"], current["version"])
            self.target["mapping_revision"] = "v1"

    async def test_token_daily_estimates_price_identity_and_date_accounting(self):
        fixed = self.enqueue("same-id", deadline=time.time() + 60)
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.enqueue("same-id", pricing={"input_usd_per_million": 20, "output_usd_per_million": 50, "source": "configured_estimate"}, deadline=fixed["deadline"])
        for key, changes in [("daily-estimate", {"daily_limits": {"max_requests": 1, "max_input_tokens": 1, "max_output_tokens": 1}}),
                ("token-estimate", {"limits": {"max_requests": 2, "max_input_tokens": 1, "max_output_tokens": 1}})]:
            job = self.enqueue(key, budget_key=key, **changes)
            async def send(channel, probe):
                return {"status": "completed"}
            self.assertEqual(await execute_requests(self.session(job), self.requests, self.resolve, send, self.project), "completed")
            final = self.store.job(job["job_id"])
            self.assertEqual(final["consumed"]["requests"], 2)
            self.assertFalse(final["budget_policy"]["token_limits_enforced"])
            self.assertFalse(final["budget_policy"]["daily_limits_enforced"])
        tomorrow = self.enqueue("new-day", budget_date="2026-10-09")
        session = self.session(tomorrow)
        session.reserve(self.requests[0], self.resolve())
        self.assertEqual(self.store.daily_budget(budget_date="2026-10-09")["attempted_requests"], 1)

    async def test_request_allowance_must_cover_the_complete_fixed_manifest(self):
        with self.assertRaisesRegex(ValueError, "request manifest exceeds max_requests"):
            self.enqueue("incomplete-allowance", limits={"max_requests": 1,
                "max_input_tokens": 5000, "max_output_tokens": 100})

    async def test_reservation_estimates_do_not_restrict_fixed_probe_parameters(self):
        request = ProbeRequest("small-estimate", self.requests[0].probe, 1, 1)
        job = self.enqueue("small-estimate", requests=[request])
        async def send(channel, probe, *, before_send):
            await before_send()
            self.assertEqual(probe["max_tokens"], 10)
            return {"status": "completed", "input_tokens_reported": 4390,
                    "output_tokens_reported": 11}
        self.assertEqual(await execute_requests(self.session(job), [request],
            self.resolve, send, self.project), "completed")

    async def test_response_body_is_not_persisted_and_projection_rejects_arbitrary_fields(self):
        job = self.enqueue()
        session = self.session(job)
        attempt = session.reserve(self.requests[0], self.resolve())
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            session.complete(self.requests[0], attempt, {"text": "fixture forbidden answer"})
        session.finish("failed", "projection_rejected", [])
        with self.registry.connect() as conn:
            dumps = " ".join(str(tuple(row)) for table in ("integrity_jobs", "integrity_attempts") for row in conn.execute("SELECT * FROM " + table))
        self.assertNotIn("Independent synthetic request", dumps)
        self.assertNotIn("fixture forbidden answer", dumps)
        self.assertNotIn("fixture-integrity-credential", dumps)

    async def test_deadline_cancels_inflight_unknown_without_resending(self):
        job = self.enqueue(deadline=time.time() + .15)
        async def send(channel, probe, *, before_send):
            await before_send()
            await asyncio.Event().wait()
        self.assertEqual(await asyncio.wait_for(execute_requests(self.session(job), self.requests, self.resolve, send, self.project), 1), "expired")
        final = self.store.job(job["job_id"])
        self.assertEqual(final["consumed"]["unknown_requests"], 1)
        self.assertEqual(final["fees"]["unknown_requests"], 1)
        self.assertIsNone(final["fees"]["estimated_usd"])
        with self.assertRaisesRegex(ValueError, "deadline_exceeded"):
            self.store.resume(job["job_id"])

    async def test_estimate_accounts_for_multibyte_prompt_system_and_protocol_framing(self):
        simple = estimate_input_tokens({"prompt": "a"})
        richer = estimate_input_tokens({"prompt": "中文", "system_prompt": "Synthetic system"})
        self.assertGreaterEqual(simple, 257)
        self.assertGreater(richer, simple)


class IntegrityTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_three_protocols_parse_text_terminal_usage_and_explicit_caps(self):
        for protocol in ("openai", "anthropic", "responses"):
            for streaming in (False, True):
                with self.subTest(protocol=protocol, streaming=streaming):
                    observed, order = [], []
                    async def preflight(url):
                        order.append("preflight")
                    async def permit():
                        order.append("permit")
                    def handler(request):
                        order.append("send")
                        observed.append(json.loads(request.content))
                        return upstream_response(protocol, streaming, usage=usage_for(protocol, 0, 0))
                    channel = {"base_url": "https://fixture.example", "api_key": "fixture-integrity-credential", "model": "fixture-model-v1", "protocol": protocol}
                    probe = {"prompt": "Independent synthetic prompt", "max_tokens": 32, "stream": streaming}
                    with patch.object(transport, "validate_url", preflight):
                        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                            result = await transport.run_probe(client, channel, probe, before_send=permit)
                    self.assertEqual(order, ["preflight", "permit", "send"])
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(result["text"], "fixture answer")
                    self.assertEqual((result["input_tokens_reported"], result["output_tokens_reported"]), (0, 0))
                    self.assertEqual(observed[0]["max_output_tokens" if protocol == "responses" else "max_tokens"], 32)

    async def test_parseable_truncated_tool_and_missing_end_are_invalid(self):
        channel = {"base_url": "https://fixture.example", "api_key": "fixture-integrity-credential", "model": "fixture-model-v1", "protocol": "openai"}
        cases = [(False, {"choices": [{"message": {"content": "123"}, "finish_reason": "length"}]}, "truncated"),
                 (False, {"choices": [{"message": {"content": "123", "tool_calls": [{"id": "fixture"}]}, "finish_reason": "stop"}]}, "tool_calls"),
                 (True, {"choices": [{"delta": {"content": "123"}}]}, "stream_break")]
        cases.append((True, {"choices": [{"delta": {"content": "123"}, "finish_reason": "stop"}]}, "stream_break"))
        for streaming, data, expected in cases:
            response = httpx.Response(200, text="data: " + json.dumps(data) + "\n\n") if streaming else httpx.Response(200, json=data)
            with patch.object(transport, "validate_url", AsyncMock()):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
                    result = await transport.run_probe(client, channel, {"prompt": "Fixture", "max_tokens": 32, "stream": streaming})
            self.assertEqual(result["status"], expected)
            self.assertFalse(result["valid"])
            self.assertEqual(result["text"], "")

    async def test_reasoning_usage_is_reported_separately_without_double_billing_output(self):
        channel={"base_url":"https://fixture.example","api_key":"fixture-integrity-credential","model":"fixture-model-v1","protocol":"responses"}
        response=httpx.Response(200,json={"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"Synthetic answer"}]}],
                                         "usage":{"input_tokens":3,"output_tokens":10,"output_tokens_details":{"reasoning_tokens":7}}})
        with patch.object(transport,"validate_url",AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:response)) as client:
                result=await transport.run_probe(client,channel,{"prompt":"Synthetic","max_tokens":32})
        self.assertEqual(result["status"],"completed")
        self.assertEqual((result["output_tokens_reported"],result["reasoning_tokens_reported"]),(10,7))
