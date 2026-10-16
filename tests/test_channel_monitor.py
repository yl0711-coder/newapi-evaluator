import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from features.channel_monitor import api, domain
from features.channel_monitor.newapi import NewAPI, NewAPIError, channel_projection
from features.channel_monitor.service import Service
from features.integrity.transport import run_probe
from shared.access import install_access
from shared.network import SocketEgressDenied
from shared.registry import Registry
from tests.fixtures.channel_monitor import Management, Models, OnlineInventory


class DomainTests(unittest.TestCase):
    def test_rc26_status_metadata_does_not_change_connection_identity(self):
        row = Management().rows[1]
        before = channel_projection(row)
        row.update(status=2, used_quota=50, other_info='{"status_reason":"manual operation","status_time":123}')
        self.assertEqual(channel_projection(row)["config_hash"], before["config_hash"])
        row["other_info"] = '{"status_reason":"manual operation","status_time":123,"route":"changed"}'
        self.assertNotEqual(channel_projection(row)["config_hash"], before["config_hash"])

    def test_rc26_default_settings_and_actual_organization_field(self):
        row = Management().rows[1]
        self.assertFalse(channel_projection(row)["overrides"])
        for field, value in (("openai_organization", "synthetic-org"),
                             ("setting", '{"force_format":true}'),
                             ("setting", '{"proxy":"http://127.0.0.1:9"}'),
                             ("setting", '{"unknown":false}'),
                             ("setting", '{"force_format":0}'),
                             ("settings", '{"disable_store":true}'),
                             ("param_override", '{"model":"other"}'),
                             ("header_override", '{"X-Synthetic":"value"}'),
                             ("status_code_mapping", '{"503":"200"}')):
            with self.subTest(field=field, value=value):
                self.assertTrue(channel_projection({**row, field: value})["overrides"])
        for value in ("[]", "null", "broken"):
            with self.assertRaises(NewAPIError):
                channel_projection({**row, "other_info": value})

    def test_final_integer_grading_and_unknown_are_distinct(self):
        for text in ("21", "解释含20\n**答案：21。**", "最终答案为21颗！", "0" * 5000 + "21"):
            self.assertIs(domain.grade_candy(text), True)
        for text in ("20", "答案是22个。", "2" * 5000):
            self.assertIs(domain.grade_candy(text), False)
        for text in (None, "", "分析中有21\n不能确定", "21 或22", "21.0", "-21"):
            self.assertIsNone(domain.grade_candy(text))

    def test_protection_covers_every_affected_group_and_model(self):
        inventory = [channel_projection(r) for r in Management(5).rows.values()]
        self.assertEqual(domain.protection(inventory[0], inventory), [])
        inventory[0]["groups"].append("second")
        inventory[0]["models"].append("other-model")
        deficient = domain.protection(inventory[0], inventory)
        self.assertEqual({(d["group"], d["model"], d["remaining"]) for d in deficient},
                         {("default", "other-model", 0), ("second", "gpt-6-astra", 0), ("second", "other-model", 0)})

    def test_identity_threshold_does_not_promote_unknown_or_conflict(self):
        score = {"status": "scored", "valid_answers": 3, "invalid_answers": 0, "expected_model": "expected",
                 "prediction": "other", "source_verdict": "MISMATCH",
                 "results": [{"model": "other", "probability": .95, "score": 1},
                             {"model": "expected", "probability": .05, "score": 0}]}
        self.assertEqual(domain.mt_verdict(score), "mismatch")
        self.assertEqual(domain.mt_verdict({**score, "valid_answers": 2}), "unknown")
        score["results"][0]["probability"] = .5
        self.assertEqual(domain.mt_verdict(score), "unknown")

    def test_upstream_comparison_preserves_case_sensitive_path(self):
        self.assertEqual(domain.upstream_root("https://EXAMPLE.com/Foo/v1/"), domain.upstream_root("https://example.com/Foo"))
        self.assertNotEqual(domain.upstream_root("https://example.com/Foo"), domain.upstream_root("https://example.com/foo"))

    def test_inspector_is_zero_database_and_network(self):
        result = subprocess.run([sys.executable, "-B", "-m", "features.channel_monitor", "inspect-config"],
                                capture_output=True, text=True, check=True)
        value = json.loads(result.stdout)
        self.assertEqual((value["interval_seconds"], value["max_attempts_per_question"]), (1800, 5))
        self.assertEqual(value["quality_rounds"], {"quality_failure": 2, "mt_mismatch": 3})
        self.assertTrue(value["modeltrace_after_completed_primary_candy"])
        self.assertEqual(value["requests_sent"], 0)
        self.assertFalse(value["credentials_read"] or value["database_opened"])


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        return NewAPI({"base_url": "http://127.0.0.1:9", "token": "synthetic-management", "user_id": 9},
                      transport=httpx.MockTransport(handler))

    async def test_management_version_pagination_and_single_status(self):
        management = Management(205)
        client = self.client(management.handler)
        await client.verify_version()
        self.assertEqual(len(await client.channels()), 205)
        self.assertEqual(sum(p == "/api/channel/" for _, p in management.calls), 3)
        await client.set_status(1, 2)
        self.assertEqual((await client.channel(1))["status"], 2)
        self.assertEqual(management.writes, [(1, 2)])
        with self.assertRaises(ValueError):
            await client.set_status(1, 3)

    async def test_false_ack_bad_version_and_incomplete_inventory_fail_closed(self):
        m = Management(); m.mode = "false"
        with self.assertRaisesRegex(NewAPIError, "newapi_status_not_changed"):
            await self.client(m.handler).set_status(1, 2)
        m.version = "v1.0.0-rc.27"
        with self.assertRaisesRegex(NewAPIError, "newapi_version_not_rc26"):
            await self.client(m.handler).verify_version()
        async def incomplete(request):
            return httpx.Response(200, json={"success": True, "data": {"total": 3, "items": []}})
        with self.assertRaisesRegex(NewAPIError, "newapi_inventory_incomplete"):
            await self.client(incomplete).channels()

    async def test_untrusted_error_body_is_not_returned(self):
        async def rejected(request):
            return httpx.Response(403, text="synthetic-management synthetic-body")
        with self.assertRaisesRegex(NewAPIError, "^newapi_http_error$"):
            await self.client(rejected).channel(1)

    async def test_management_timeout_is_safe_but_cancellation_propagates(self):
        client = self.client(Management().handler)
        for exc in (httpx.ReadTimeout("synthetic-management"), asyncio.TimeoutError("synthetic-management")):
            with patch.object(client, "_request", new=AsyncMock(side_effect=exc)):
                with self.assertRaisesRegex(NewAPIError, "^newapi_request_failed$"):
                    await client.channel(1)
        with patch.object(client, "_request", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await client.channel(1)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Registry(Path(self.tmp.name))
        self.now = 1_800_000_000.
        self.management, self.models = Management(), Models()
        self.inventory = OnlineInventory(self.registry)
        self.channel = self.registry.get(1)
        self.delays = []
        async def sleep(seconds):
            self.delays.append(seconds)
            await asyncio.sleep(0)
        self.validation = patch("features.integrity.transport.validate_url", new=AsyncMock(return_value=None))
        self.validation.start()
        async def sender(channel, probe, *, before_send):
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.models.handler)) as client:
                return await run_probe(client, channel, probe, before_send=before_send)
        self.svc = Service(self.registry, clock=lambda: self.now, sender=sender,
                           inventory_reader=self.inventory, sleep=sleep,
                           client_factory=lambda config: NewAPI(config, transport=httpx.MockTransport(self.management.handler)))
        self.svc.save_instance("http://127.0.0.1:9", "synthetic-management", 9)
        self.binding = await self.add(1, self.channel["id"])
        self.svc.enable(True, confirm_live=True, confirm_manage=True, confirm_inventory=True)

    async def asyncTearDown(self):
        await self.svc.stop()
        self.validation.stop()
        self.tmp.cleanup()

    async def add(self, remote, registry_id):
        return await self.svc.add_binding(remote_id=remote, registry_id=registry_id, model="gpt-6-astra",
                                           protocol="responses", expected="gpt-6-astra", confirm_live=True,
                                           confirm_same_credential=True)

    async def round(self, slots=1):
        self.now += 1800 * slots
        await self.svc.tick(self.now)
        if self.svc.worker:
            await self.svc.worker
        return self.svc.store.binding(self.binding["id"])

    async def second_binding(self):
        self.svc.enable(False)
        self.inventory.rows.append({"record_id": "record-2", "fields": {
            **self.inventory.rows[0]["fields"], "apikey": "synthetic-second-key", "渠道名称": "Synthetic second"}})
        self.inventory.import_rows()
        binding = await self.add(2, 2)
        second = Models()
        async def sender(channel, probe, *, before_send):
            models = second if channel["api_key"] == "synthetic-second-key" else self.models
            async with httpx.AsyncClient(transport=httpx.MockTransport(models.handler)) as client:
                return await run_probe(client, channel, probe, before_send=before_send)
        self.svc.sender = sender
        self.svc.enable(True, confirm_live=True, confirm_manage=True, confirm_inventory=True)
        return binding, second

    async def disable_first(self):
        self.models.queue = ["timeout"] * 5
        state = await self.round()
        self.assertEqual((state["owned_reason"], self.management.writes), ("transport", [(1, 2)]))

    async def test_save_and_read_do_not_send_models_or_status_changes(self):
        self.assertEqual((len(self.models.calls), self.management.writes), (0, []))
        self.assertFalse(self.svc.status()["running"])
        self.assertEqual(self.svc.store.plan()["next_due"], self.now+1800)
        public = json.dumps(self.svc.meta())
        self.assertNotIn("synthetic-management", public)
        self.assertNotIn("synthetic-model-key", public)
        self.assertNotIn(b"synthetic-management", self.registry.path.read_bytes())

    async def test_five_timeouts_below_four_recheck_once_and_keep_floor(self):
        self.management.rows = {1: self.management.rows[1]}
        self.models.queue = ["timeout"]*6
        state = await self.round()
        self.assertEqual((state["owned_reason"], self.management.rows[1]["status"]), ("", 1))
        self.assertEqual(len(self.models.calls), 6)
        self.assertEqual(self.delays, [2, 4, 8, 16, 60])
        self.assertIn("minimum_channels", [e["kind"] for e in self.svc.store.history()["events"]])
        attempts = self.svc.store.attempts(1)
        self.assertEqual([(a["probe"], a["attempt"]) for a in attempts if a["probe"] == "candy-recheck"], [("candy-recheck", 0)])

    async def test_four_failures_then_success_uses_fifth_chance_without_disable(self):
        self.models.queue = ["network", "timeout", "", 503, "答案：21"]
        await self.round()
        self.assertEqual(len(self.models.calls), 8)
        self.assertEqual(self.management.writes, [])
        attempts = self.svc.store.attempts(1)
        self.assertEqual([r["result"]["status"] for r in attempts if r["probe"] == "candy"],
                         ["network_error", "timeout", "empty_response", "upstream_5xx", "completed"])
        self.assertTrue(all(a["status"] == "confirmed" for a in attempts))

    async def test_http_auth_and_empty_failure_paths_have_five_distinct_attempts(self):
        for failure in (401, 404, 429, ""):
            self.models.queue = [failure]*5
            await self.round()
            self.assertEqual(self.management.rows[1]["status"], 2)
            await self.round()
            self.assertEqual(self.management.rows[1]["status"], 1)
        self.assertEqual(len(self.management.writes), 8)

    async def test_two_quality_rounds_then_one_matching_round_restores(self):
        self.models.phase = "mismatch"
        state = await self.round()
        self.assertEqual(state["quality_streak"], 1)
        self.assertEqual(self.management.writes, [])
        state = await self.round()
        self.assertEqual((state["owned_reason"], self.management.writes), ("quality", [(1, 2)]))
        self.assertEqual(len(self.models.calls), 8)
        trigger = self.svc.store.history()["actions"][0]["target"]
        self.assertEqual((trigger["trigger_outcome"], trigger["streak"], trigger["threshold"]),
                         ("quality_failure", 2, 2))
        self.models.phase = "healthy"
        state = await self.round()
        self.assertEqual(state["owned_reason"], "")
        self.assertEqual(self.management.writes, [(1, 2), (1, 1)])
        self.assertEqual(len(self.models.calls), 12)
        saved = json.dumps(self.svc.store.history()) + json.dumps(self.svc.store.attempts(3))
        self.assertNotIn("synthetic-model-key", saved)
        self.assertNotIn(domain.candy_probe()["prompt"], saved)
        self.assertNotIn('"text"', saved)

    async def test_correct_candy_still_runs_three_mt_probes(self):
        state = await self.round()
        self.assertEqual(state["last_result"]["outcome"], "healthy")
        self.assertEqual(state["last_result"].get("mt_verdict"), "match")
        self.assertEqual([c["candy"] for c in self.models.calls], [True, False, False, False])

    async def test_three_correct_candy_mismatch_rounds_disable_and_matching_round_restores(self):
        self.models.phase = "candy-pass-mismatch"
        for streak in (1, 2):
            state = await self.round()
            self.assertIs(state["last_result"]["candy_correct"], True)
            self.assertEqual(state["last_result"]["outcome"], "mt_mismatch")
            self.assertEqual(state["quality_streak"], streak)
            self.assertEqual(self.management.writes, [])
        state = await self.round()
        self.assertEqual((state["owned_reason"], self.management.writes), ("quality", [(1, 2)]))
        self.assertEqual(len(self.models.calls), 12)
        trigger = self.svc.store.history()["actions"][0]["target"]
        self.assertEqual({k: trigger[k] for k in ("trigger_outcome", "streak", "threshold")},
                         {"trigger_outcome": "mt_mismatch", "streak": 3, "threshold": 3})
        await self.round()
        self.assertEqual(self.management.writes, [(1, 2)])
        self.models.phase = "healthy"
        state = await self.round()
        self.assertEqual((state["owned_reason"], self.management.writes), ("", [(1, 2), (1, 1)]))
        self.assertEqual(self.svc.store.history()["actions"][-1]["target"], trigger)

    async def test_quality_and_transport_both_obey_four_channel_floor(self):
        self.management.rows.pop(5)
        self.models.phase = "mismatch"
        await self.round(); await self.round()
        self.assertEqual(self.management.writes, [])
        alarms = [e for e in self.svc.store.history()["events"] if e["kind"] == "minimum_channels"]
        self.assertEqual(alarms[0]["detail"]["deficient"], [{"group": "default", "model": "gpt-6-astra", "remaining": 3}])
        self.models.queue = ["timeout"]*6
        await self.round()
        self.assertEqual(self.management.writes, [])

    async def test_correct_candy_mt_mismatch_respects_four_channel_protection(self):
        self.management.rows.pop(5)
        self.models.phase = "candy-pass-mismatch"
        await self.round(); await self.round()
        self.assertEqual(self.svc.store.history()["events"], [])
        state = await self.round()
        self.assertEqual(state["quality_streak"], 3)
        self.assertEqual(self.management.writes, [])
        alarm = self.svc.store.history()["events"][0]
        self.assertEqual((alarm["kind"], alarm["detail"]["deficient"][0]["remaining"]), ("minimum_channels", 3))

    async def test_match_unknown_manual_and_missed_slot_break_three_round_streak(self):
        for interrupt in ("match", "mt_unknown", "candy_unknown", "manual", "missed"):
            with self.subTest(interrupt=interrupt):
                self.svc.enable(False)
                self.svc.enable(True, confirm_live=True, confirm_manage=True, confirm_inventory=True)
                self.models.phase = "candy-pass-mismatch"
                await self.round()
                self.assertEqual((await self.round())["quality_streak"], 2)
                if interrupt == "missed":
                    state = await self.round(slots=2)
                else:
                    if interrupt == "match":
                        self.models.phase = "healthy"
                    elif interrupt == "mt_unknown":
                        self.models.queue = ["答案：21", "无法确定", "无法确定", "无法确定"]
                    elif interrupt == "candy_unknown":
                        self.models.queue = ["无法确定"]
                    if interrupt == "manual":
                        await self.svc.run_now()
                        await self.svc.worker
                        state = self.svc.store.binding(self.binding["id"])
                    else:
                        state = await self.round()
                    self.assertEqual(state["quality_streak"], 0)
                    if interrupt in {"mt_unknown", "candy_unknown"}:
                        self.assertEqual(state["last_result"]["outcome"], "inconclusive")
                    self.models.phase = "candy-pass-mismatch"
                    state = await self.round()
                self.assertEqual(state["quality_streak"], 1)
                self.assertEqual((await self.round())["quality_streak"], 2)
                self.assertEqual(self.management.writes, [])

    async def test_candy_correct_and_wrong_mismatch_paths_do_not_combine(self):
        self.models.phase = "mismatch"
        self.assertEqual((await self.round())["quality_streak"], 1)
        self.models.phase = "candy-pass-mismatch"
        self.assertEqual((await self.round())["quality_streak"], 1)
        self.assertEqual((await self.round())["quality_streak"], 2)
        self.models.phase = "mismatch"
        self.assertEqual((await self.round())["quality_streak"], 1)
        self.assertEqual(self.management.writes, [])
        state = await self.round()
        self.assertEqual((state["owned_reason"], self.management.writes), ("quality", [(1, 2)]))

    async def test_started_manual_breaks_streak_before_interruption_or_rejection(self):
        for interruption in ("stop", "restart", "version_rejected"):
            with self.subTest(interruption=interruption):
                self.svc.enable(False)
                self.svc.enable(True, confirm_live=True, confirm_manage=True, confirm_inventory=True)
                self.models.phase = "candy-pass-mismatch"
                await self.round()
                self.assertEqual((await self.round())["quality_streak"], 2)
                next_due = self.svc.store.plan()["next_due"]
                self.now += 1
                try:
                    if interruption == "restart":
                        self.svc.store.begin(self.now, manual=True)
                    else:
                        if interruption == "stop":
                            self.models.entered.clear()
                            self.models.gate.clear()
                            self.models.queue = ["block"]
                        else:
                            self.management.version = "v1.0.0-rc.27"
                        await self.svc.run_now()
                    state = self.svc.store.binding(self.binding["id"])
                    due_after_begin = self.svc.store.plan()["next_due"]
                    if interruption == "stop":
                        await asyncio.wait_for(self.models.entered.wait(), 2)
                    elif interruption == "version_rejected":
                        await self.svc.worker
                finally:
                    await self.svc.stop()
                    self.svc.store.recover(self.now)
                    self.models.gate.set()
                    self.management.version = "v1.0.0-rc.26"
                self.assertEqual((state["quality_streak"], state["last_slot"]), (0, None))
                self.assertEqual(due_after_begin, next_due)
                self.assertEqual(self.svc.store.history()["rounds"][0]["status"],
                                 "partial" if interruption == "version_rejected" else "interrupted")
                self.assertEqual((await self.round())["quality_streak"], 1)
                self.assertEqual(self.management.writes, [])

    async def test_rejected_manual_during_scheduled_round_preserves_streak(self):
        self.models.phase = "candy-pass-mismatch"
        await self.round(); await self.round()
        self.models.queue = ["block"]
        self.now += 1800
        await self.svc.tick()
        await asyncio.wait_for(self.models.entered.wait(), 2)
        try:
            next_due = self.svc.store.plan()["next_due"]
            with self.assertRaisesRegex(ValueError, "已有轮次运行"):
                await self.svc.run_now()
            self.assertEqual(self.svc.store.binding(self.binding["id"])["quality_streak"], 2)
            self.assertEqual(self.svc.store.plan()["next_due"], next_due)
        finally:
            await self.svc.stop()
            self.models.gate.set()
        self.assertEqual(self.management.writes, [])

    async def test_correct_candy_then_five_mt_failures_disable_with_floor(self):
        self.models.queue = ["答案：21"] + ["timeout"]*5
        state = await self.round()
        self.assertEqual((state["owned_reason"], self.management.writes), ("transport", [(1, 2)]))
        self.assertIs(state["last_result"]["candy_correct"], True)
        mt = state["last_result"]["modeltrace"]
        self.assertEqual((mt["requested_answers"], mt["missing_answers"], len(mt["not_run"])), (1, 2, 2))
        self.assertEqual(state["last_result"]["mt_verdict"], "unknown")
        self.assertNotIn("minimum_channels", [e["kind"] for e in self.svc.store.history()["events"]])

    async def test_transport_recovery_requires_correct_candy_and_matching_mt(self):
        self.models.queue = ["timeout"]*5
        await self.round()
        for condition in ("mismatch", "unknown", "fault"):
            with self.subTest(condition=condition):
                self.models.phase = "candy-pass-mismatch"
                if condition == "unknown":
                    self.models.queue = ["无法确定"]
                elif condition == "fault":
                    self.models.queue = ["答案：21"] + ["timeout"]*5
                state = await self.round()
                self.assertEqual((state["owned_reason"], self.management.rows[1]["status"]), ("transport", 2))
                self.assertIsNotNone(state["last_result"]["modeltrace"])
        self.models.phase = "healthy"
        state = await self.round()
        self.assertEqual((state["owned_reason"], self.management.writes), ("", [(1, 2), (1, 1)]))

    async def test_quality_owner_is_not_downgraded_by_mt_transport_failure(self):
        self.models.phase = "candy-pass-mismatch"
        await self.round(); await self.round(); await self.round()
        self.models.queue = ["答案：21"] + ["timeout"]*5
        state = await self.round()
        self.assertEqual(state["owned_reason"], "quality")
        self.assertEqual(state["last_result"]["outcome"], "transport_failure")
        self.assertEqual(self.management.writes, [(1, 2)])
        self.models.phase = "healthy"
        self.models.queue = ["答案：20"]
        state = await self.round()
        self.assertEqual(state["owned_reason"], "quality")
        self.assertEqual(self.management.writes, [(1, 2)])
        self.assertEqual((await self.round())["owned_reason"], "")

    async def test_mt_egress_policy_rejection_blocks_transport_recovery(self):
        from features.stability.app.egress import EgressDenied
        original = self.models.handler
        for stage in ("preflight", "socket", "wrapped_socket"):
            with self.subTest(stage=stage):
                self.models.handler = original
                self.models.queue = ["timeout"] * 5
                if not self.svc.store.binding(self.binding["id"])["owned_reason"]:
                    await self.round()
                rejected = 0
                checked = 0

                async def preflight(*args, **kwargs):
                    nonlocal checked
                    checked += 1
                    if checked > 1:
                        raise EgressDenied("synthetic policy refusal")

                async def handler(request):
                    nonlocal rejected
                    if json.loads(request.content)["input"] != domain.candy_probe()["prompt"]:
                        rejected += 1
                        if stage == "wrapped_socket":
                            try:
                                raise SocketEgressDenied("synthetic policy refusal")
                            except SocketEgressDenied as exc:
                                raise httpx.ConnectError("synthetic wrapped refusal", request=request) from exc
                        raise SocketEgressDenied("synthetic policy refusal")
                    return await original(request)

                self.models.queue = []
                self.models.handler = handler
                if stage == "preflight":
                    with patch("features.integrity.transport.validate_url", new=AsyncMock(side_effect=preflight)):
                        state = await self.round()
                else:
                    state = await self.round()
                history = self.svc.store.history()
                self.assertEqual((state["owned_reason"], self.management.rows[1]["status"]), ("transport", 2))
                self.assertEqual(self.management.writes, [(1, 2)])
                self.assertEqual(history["rounds"][0]["status"], "partial")
                attempts = self.svc.store.attempts(history["rounds"][0]["id"])
                self.assertEqual(len(attempts), 1 if stage == "preflight" else 2)
                if stage != "preflight":
                    self.assertEqual(attempts[-1]["result"]["status"], "egress_denied")
                    self.assertEqual(attempts[-1]["status"], "confirmed")
                self.assertEqual(rejected, 0 if stage == "preflight" else 1)

    async def test_cancel_during_mt_does_not_restore_after_valid_candy(self):
        self.models.queue = ["timeout"]*5
        await self.round()
        self.models.queue = ["答案：21", "block"]
        self.now += 1800
        await self.svc.tick()
        await asyncio.wait_for(self.models.entered.wait(), 2)
        await self.svc.stop()
        self.assertEqual(self.management.writes, [(1, 2)])
        self.assertEqual(self.svc.store.binding(self.binding["id"])["owned_reason"], "transport")
        self.assertEqual(self.svc.store.history()["rounds"][0]["status"], "interrupted")

    async def interrupt_transport_recovery_during_mt(self, change):
        self.models.queue = ["timeout"]*5
        await self.round()
        self.models.queue = ["答案：21", "block"]
        task = asyncio.create_task(self.round())
        try:
            await asyncio.wait_for(self.models.entered.wait(), 2)
            change()
            self.models.gate.set()
            await asyncio.wait_for(task, 2)
        finally:
            self.models.gate.set()
            await self.svc.stop()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.management.writes, [(1, 2)])
        self.assertEqual(self.svc.store.binding(self.binding["id"])["owned_reason"], "transport")
        self.assertEqual(self.svc.store.history()["rounds"][0]["status"], "partial")

    async def test_registry_drift_during_mt_blocks_transport_recovery(self):
        def disable():
            row = self.registry.get(self.channel["id"])
            self.registry.save({**row, "enabled": False}, row["id"], row["version"])
        await self.interrupt_transport_recovery_during_mt(disable)

    async def test_expiry_during_mt_blocks_transport_recovery(self):
        def expire():
            self.now += 1100
        await self.interrupt_transport_recovery_during_mt(expire)

    async def test_concurrent_quality_candidates_recalculate_remaining_channels(self):
        self.svc.enable(False)
        second = self.registry.save({"name": "Synthetic second", "api_key": "synthetic-second-key", "base_url": self.channel["base_url"]})
        await self.add(2, second["id"])
        self.svc.enable(True, confirm_live=True, confirm_manage=True, confirm_inventory=True)
        self.models.phase = "mismatch"
        await self.round(); await self.round()
        self.assertEqual(sum(r["status"] == 1 for r in self.management.rows.values()), 4)
        self.assertEqual(len(self.management.writes), 1)
        self.assertEqual(sum(e["kind"] == "minimum_channels" for e in self.svc.store.history()["events"]), 1)

    async def test_missed_slot_and_unknown_break_quality_streak(self):
        self.models.phase = "mismatch"
        await self.round()
        state = await self.round(slots=2)
        self.assertEqual(state["quality_streak"], 1)
        self.models.queue = ["无法确定"]
        state = await self.round()
        self.assertEqual(state["quality_streak"], 0)
        await self.round()
        self.assertEqual(self.management.writes, [])
        self.assertEqual(len(self.svc.store.history()["rounds"]), 4)

    async def test_five_failures_in_modeltrace_also_disable_with_floor(self):
        self.models.queue = ["答案：20"] + ["timeout"]*5
        state = await self.round()
        self.assertEqual(state["owned_reason"], "transport")
        self.assertEqual(len(self.models.calls), 6)

    async def test_locked_and_externally_disabled_channels_are_not_recovered(self):
        self.svc.store.lock(self.binding["id"], True)
        await self.round()
        self.assertEqual(self.models.calls, [])
        self.svc.store.lock(self.binding["id"], False)
        self.management.rows[1]["status"] = 2
        await self.round()
        self.assertEqual((len(self.models.calls), self.management.writes), (0, []))

    async def test_unknown_management_write_is_locked_and_not_automatically_resent(self):
        self.management.mode = "timeout"
        self.models.queue = ["timeout"]*5
        state = await self.round()
        self.assertTrue(state["locked"])
        self.assertEqual(state["owned_reason"], "")
        self.assertEqual(self.svc.store.history()["actions"][0]["status"], "unknown")
        self.management.mode = "normal"
        await self.round()
        self.assertEqual(self.management.writes, [(1, 2)])

    async def test_false_ack_or_wrong_readback_never_claim_ownership(self):
        for mode in ("false", "no_change"):
            self.management.mode = mode
            self.management.rows[1]["status"] = 1
            self.svc.store.lock(self.binding["id"], False)
            self.models.queue = ["timeout"]*5
            state = await self.round()
            self.assertTrue(state["locked"])
            self.assertEqual(state["owned_reason"], "")

    async def test_interruption_after_four_failures_does_not_become_five(self):
        self.models.queue = ["timeout"]*4 + ["block"]
        self.now += 1800
        await self.svc.tick()
        await asyncio.wait_for(self.models.entered.wait(), 2)
        await self.svc.stop()
        self.assertEqual(self.management.writes, [])
        attempts = self.svc.store.attempts(1)
        self.assertEqual([a["status"] for a in attempts], ["confirmed"]*4 + ["unknown"])
        await self.round()
        self.assertEqual(self.management.rows[1]["status"], 1)

    async def test_plan_change_while_waiting_for_send_prevents_request(self):
        entered, gate = asyncio.Event(), asyncio.Event()
        async def waiting(channel, probe, *, before_send):
            entered.set(); await gate.wait(); await before_send()
            raise AssertionError("must not send")
        self.svc.sender = waiting
        self.now += 1800
        await self.svc.tick()
        await asyncio.wait_for(entered.wait(), 2)
        self.svc.enable(False)
        gate.set()
        await self.svc.worker
        self.assertEqual(self.svc.store.attempts(1), [])
        self.assertEqual(self.management.writes, [])

    def change_registry_mapping(self):
        model = next(m for m in self.svc.catalog.models() if m["model"] == "gpt-6-astra")
        self.svc.catalog.bind(self.channel["id"], model["id"], "gpt-6-astra-changed", "responses")

    async def registry_edit_during_final_send_guard(self, edit):
        entered, release = asyncio.Event(), asyncio.Event()
        checking_send = False
        original_management, original_sender = self.management.handler, self.svc.sender

        async def management(request):
            if checking_send and request.method == "GET" and request.url.path == "/api/channel/1":
                entered.set()
                await release.wait()
            return await original_management(request)

        async def sender(channel, probe, *, before_send):
            async def final_guard():
                nonlocal checking_send
                checking_send = True
                await before_send()
            return await original_sender(channel, probe, before_send=final_guard)

        self.management.handler, self.svc.sender = management, sender
        task = asyncio.create_task(self.round())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            edit()
            release.set()
            await asyncio.wait_for(task, 2)
        finally:
            release.set()
            await self.svc.stop()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.models.calls, [])
        self.assertEqual(self.svc.store.attempts(1), [])
        self.assertEqual(self.management.writes, [])
        self.assertEqual(self.svc.store.history()["rounds"][0]["status"], "partial")

    async def test_registry_disabled_during_final_management_read_blocks_model_request(self):
        def disable():
            row = self.registry.get(self.channel["id"])
            self.registry.save({**row, "enabled": False}, row["id"], row["version"])
        await self.registry_edit_during_final_send_guard(disable)

    async def test_registry_key_changed_during_final_management_read_blocks_model_request(self):
        def replace_key():
            row = self.registry.get(self.channel["id"])
            self.registry.save({**row, "api_key": "synthetic-replaced-key"}, row["id"], row["version"])
        await self.registry_edit_during_final_send_guard(replace_key)

    async def test_registry_mapping_changed_during_final_management_read_blocks_model_request(self):
        await self.registry_edit_during_final_send_guard(self.change_registry_mapping)

    async def registry_edit_during_action_read(self, path, edit):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.management.handler

        async def management(request):
            state = self.svc.store.binding(self.binding["id"])
            if request.method == "GET" and request.url.path == path and state["last_result"]:
                entered.set()
                await release.wait()
            return await original(request)

        self.management.handler = management
        task = asyncio.create_task(self.round())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            edit()
            release.set()
            await asyncio.wait_for(task, 2)
        finally:
            release.set()
            await self.svc.stop()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.management.writes, [])
        self.assertEqual(self.svc.store.history()["actions"], [])
        self.assertEqual(self.svc.store.binding(self.binding["id"])["owned_reason"], "")
        self.assertEqual(self.svc.store.history()["rounds"][0]["status"], "partial")

    async def test_registry_disabled_during_action_version_read_blocks_management_write(self):
        self.models.phase = "fault"
        def disable():
            row = self.registry.get(self.channel["id"])
            self.registry.save({**row, "enabled": False}, row["id"], row["version"])
        await self.registry_edit_during_action_read("/api/status", disable)
        self.assertEqual(len(self.models.calls), 5)

    async def test_registry_mapping_changed_during_quality_inventory_read_blocks_management_write(self):
        self.models.phase = "mismatch"
        await self.round()
        await self.registry_edit_during_action_read("/api/channel/", self.change_registry_mapping)

    async def test_remote_config_change_and_version_mismatch_prevent_actions(self):
        self.management.rows[1]["base_url"] = "http://127.0.0.1:9/other"
        await self.round()
        self.assertEqual(self.models.calls, [])
        self.management.rows[1]["base_url"] = self.channel["base_url"]
        self.management.version = "v1.0.0-rc.27"
        await self.round()
        self.assertEqual(self.models.calls, [])

    async def test_manual_takeover_during_write_preserves_human_ownership(self):
        async def takeover():
            self.svc.store.lock(self.binding["id"], True, takeover=True)
        self.management.on_write = takeover
        self.models.queue = ["timeout"]*5
        state = await self.round()
        self.assertTrue(state["locked"])
        self.assertEqual(state["owned_reason"], "")

    async def test_deleted_mapping_keeps_exact_safe_management_action_target(self):
        self.models.queue = ["timeout"] * 5
        await self.round()
        self.svc.enable(False)
        self.svc.store.lock(self.binding["id"], True, takeover=True)
        self.svc.store.remove(self.binding["id"])
        self.svc.save_instance("http://127.0.0.1:8", "synthetic-replacement", 8)
        target = self.svc.store.history()["actions"][0]["target"]
        self.assertEqual((target["remote_id"], target["registry_id"], target["model"], target["protocol"]),
                         (1, self.channel["id"], "gpt-6-astra", "responses"))
        self.assertEqual(target["condition_hash"], self.binding["condition_hash"])
        self.assertEqual(target["instance_fingerprint"],
                         domain.canonical_hash({"base_url": "http://127.0.0.1:9", "user_id": 9}))
        self.assertNotIn("synthetic-management", json.dumps(target))
        self.assertNotIn("http://", json.dumps(target))

    async def test_expired_slot_is_not_recorded_or_caught_up(self):
        self.now += 1800+1101
        await self.svc.tick()
        self.assertEqual(self.svc.store.history()["rounds"], [])
        self.assertEqual(self.models.calls, [])
        self.now = 1_800_003_600
        await self.svc.tick()
        await self.svc.worker
        self.assertEqual(len(self.models.calls), 4)
        await self.svc.tick()
        self.assertEqual(len(self.models.calls), 4)

    async def test_restart_marks_unfinished_permits_unknown_and_locks_actions(self):
        self.svc.recover()
        run = self.svc.store.begin(self.now+1800)
        self.now += 1800
        self.svc.store.reserve(run, self.binding, "candy", 0, self.now)
        self.svc.store.result(run, self.binding, {"outcome": "transport_failure"}, self.now)
        self.svc.store.reserve_action(run, self.binding, 2, "transport", self.now)
        self.svc.store.recover(self.now)
        self.assertTrue(self.svc.store.binding(self.binding["id"])["locked"])
        self.assertEqual(self.svc.store.attempts(run["id"])[0]["status"], "unknown")
        self.assertEqual(self.svc.store.history()["actions"][0]["status"], "unknown")
        self.assertEqual(self.management.writes, [])

    async def test_api_strict_inputs_and_explicit_activation(self):
        self.svc.enable(False)
        app = FastAPI(); install_access(app); app.include_router(api.router)
        with patch.object(api, "get_service", return_value=self.svc):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                response = await client.put("/api/channel-monitor/plan", json={"enabled": True})
                self.assertEqual(response.status_code, 422)
                response = await client.put("/api/channel-monitor/plan", json={"enabled": True, "confirm_live": True, "confirm_manage": True, "confirm_inventory": True})
                self.assertEqual(response.status_code, 200)
                response = await client.post("/api/channel-monitor/bindings", json={"remote_id": True, "token": "synthetic-input-secret"})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("synthetic-input-secret", response.text)

    async def test_floor_recheck_valid_response_keeps_channel_even_if_wrong(self):
        self.management.rows.pop(5)
        self.models.queue = ["timeout"] * 5 + ["答案：20"]
        state = await self.round()
        self.assertEqual(self.management.writes, [])
        self.assertEqual(len(self.models.calls), 6)
        self.assertEqual(self.delays, [2, 4, 8, 16, 60])
        self.assertEqual(state["quality_streak"], 0)
        self.assertEqual(state["last_result"]["outcome"], "transport_failure")
        event = self.svc.store.history()["events"][0]
        self.assertEqual((event["kind"], event["detail"]["completed"]), ("floor_connectivity_recheck", True))

    async def test_floor_recheck_cancelled_after_permit_is_unknown_and_never_disables(self):
        self.management.rows.pop(5)
        self.models.queue = ["timeout"] * 5 + ["block"]
        self.now += 1800
        await self.svc.tick()
        await asyncio.wait_for(self.models.entered.wait(), 2)
        await self.svc.stop()
        attempts = self.svc.store.attempts(1)
        self.assertEqual([(a["probe"], a["status"]) for a in attempts][-1], ("candy-recheck", "unknown"))
        self.assertEqual(len(attempts), 6)
        self.assertEqual(self.management.writes, [])

    async def test_backoff_cannot_extend_original_window_or_send_fifth_chance(self):
        async def sleep(seconds):
            self.now += 1090
        self.svc.sleep = sleep
        self.models.phase = "fault"
        await self.round()
        self.assertEqual(len(self.models.calls), 2)
        self.assertEqual(self.management.writes, [])
        self.assertEqual(self.svc.store.history()["rounds"][0]["status"], "partial")

    async def test_fresh_inventory_missing_offline_bad_or_duplicate_never_restores(self):
        await self.disable_first()
        original = self.inventory.rows[0]
        variants = [[], [{**original, "fields": {**original["fields"], "是否在线": "已下线"}}],
                    [{**original, "fields": {**original["fields"], "apikey": ""}}],
                    [{**original, "fields": {**original["fields"], "倍率": 0}}],
                    [original, {**original, "record_id": "duplicate"}]]
        for rows in variants:
            with self.subTest(rows=len(rows)):
                self.inventory.rows = rows
                await self.round()
                self.assertEqual(self.management.writes, [(1, 2)])
                self.assertEqual(len(self.models.calls), 5)
                self.assertTrue(self.registry.get(1)["enabled"])
        self.inventory.rows = [original]
        state = await self.round()
        self.assertEqual((state["owned_reason"], self.management.writes), ("", [(1, 2), (1, 1)]))

    async def test_failed_new_read_does_not_reuse_previous_round_inventory(self):
        await self.disable_first()
        self.models.phase = "candy-pass-mismatch"
        await self.round()
        calls = len(self.models.calls)
        self.inventory.failure = True
        await self.round()
        self.assertEqual(len(self.models.calls), calls)
        self.assertEqual(self.management.writes, [(1, 2)])
        self.assertEqual(self.svc.store.history()["rounds"][0]["status"], "partial")

    async def test_external_enable_releases_ownership_without_duplicate_enable(self):
        await self.disable_first()
        self.management.rows[1]["status"] = 1
        state = await self.round()
        self.assertEqual(state["owned_reason"], "")
        self.assertEqual(self.management.writes, [(1, 2)])
        self.assertEqual(len(self.models.calls), 5)

    async def test_resync_generation_change_during_mt_blocks_recovery(self):
        await self.interrupt_transport_recovery_during_mt(self.inventory.import_rows)

    async def test_new_settings_without_successful_sync_blocks_old_allowlist(self):
        await self.disable_first()
        from shared.feishu_channels import SettingsInput
        self.inventory.sync.configure(SettingsInput(app_id="cli_synthetic", version=1,
            table_url="https://synthetic.feishu.cn/base/another?table=tblSynthetic"))
        await self.round()
        self.assertEqual(self.management.writes, [(1, 2)])
        self.assertEqual(len(self.models.calls), 5)

    async def test_final_enable_transaction_rechecks_inventory_generation(self):
        await self.disable_first()
        original = self.management.handler
        async def handler(request):
            if request.url.path == "/api/status" and self.svc.store.binding(1)["last_result"]["outcome"] == "healthy":
                self.inventory.import_rows()
            return await original(request)
        self.management.handler = handler
        await self.round()
        self.assertEqual(self.management.writes, [(1, 2)])
        self.assertEqual(len(self.svc.store.history()["actions"]), 1)

    async def test_two_failed_channels_wait_for_all_results_then_open_persistent_circuit(self):
        _, second = await self.second_binding()
        self.models.phase = "fault"
        second.queue = ["block"] + ["timeout"] * 5
        self.now += 1800
        await self.svc.tick()
        await asyncio.wait_for(second.entered.wait(), 2)
        self.assertEqual(self.management.writes, [])
        second.gate.set()
        await self.svc.worker
        self.assertEqual(self.management.writes, [])
        self.assertTrue(self.svc.store.circuit()["opened"])
        from features.channel_monitor.storage import Store
        self.assertTrue(Store(self.registry).circuit()["opened"])
        self.assertTrue(all(row["status"] == 1 for row in self.management.rows.values()))

    async def test_circuit_release_requires_later_scheduled_complete_healthy_round(self):
        _, second = await self.second_binding()
        self.models.phase = second.phase = "fault"
        await self.round()
        self.assertTrue(self.svc.store.circuit()["opened"])
        self.models.phase = second.phase = "healthy"
        await self.svc.run_now()
        await self.svc.worker
        self.assertTrue(self.svc.store.circuit()["opened"])
        second.queue = ["无法确定"]
        await self.round()
        self.assertTrue(self.svc.store.circuit()["opened"])
        await self.round()
        self.assertFalse(self.svc.store.circuit()["opened"])
        self.assertEqual(self.management.writes, [])

    async def test_circuit_release_rechecks_current_unlocked_set_and_registry(self):
        _, second = await self.second_binding()
        self.svc.enable(False)
        third_channel = self.registry.save({"name": "Synthetic third", "api_key": "synthetic-third-key",
                                            "base_url": self.channel["base_url"]})
        third = await self.add(3, third_channel["id"])
        self.svc.store.lock(third["id"], True)
        self.svc.enable(True, confirm_live=True, confirm_manage=True, confirm_inventory=True)
        self.models.phase = second.phase = "fault"
        await self.round()
        self.assertTrue(self.svc.store.circuit()["opened"])
        self.models.phase = second.phase = "healthy"
        original = second.handler
        for change in (lambda: self.svc.store.lock(third["id"], False), self.change_registry_mapping):
            entered, release = asyncio.Event(), asyncio.Event()
            start = len(second.calls)
            async def delayed(request):
                if len(second.calls) == start + 3:
                    entered.set()
                    await release.wait()
                return await original(request)
            second.handler = delayed
            task = asyncio.create_task(self.round())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                change()
                release.set()
                await asyncio.wait_for(task, 2)
                self.assertTrue(self.svc.store.circuit()["opened"])
                self.assertEqual(self.management.writes, [])
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                second.handler = original
                self.svc.store.lock(third["id"], True)

    async def test_healthy_owned_replacement_is_confirmed_before_faulty_channel_disable(self):
        await self.disable_first()
        _, second = await self.second_binding()
        second.phase = "fault"
        await self.round()
        self.assertEqual(self.management.writes, [(1, 2), (1, 1), (2, 2)])
        self.assertEqual(sum(r["status"] == 1 for r in self.management.rows.values()), 4)
        self.assertEqual(self.svc.store.binding(1)["owned_reason"], "")
        self.assertEqual(self.svc.store.binding(2)["owned_reason"], "transport")
        recovery = self.svc.store.history()["actions"][1]["target"]
        self.assertTrue(recovery["online_inventory_hash"])

    async def test_unconfirmed_replacement_prevents_later_disable(self):
        await self.disable_first()
        _, second = await self.second_binding()
        second.phase = "fault"
        self.management.mode = "timeout"
        await self.round()
        self.assertEqual(self.management.writes, [(1, 2), (1, 1)])
        self.assertTrue(self.svc.store.binding(1)["locked"])
        self.assertEqual(self.svc.store.history()["actions"][0]["status"], "unknown")
        self.assertEqual(self.management.rows[2]["status"], 1)

    async def test_replacement_covering_only_some_combinations_cannot_authorize_disable(self):
        await self.disable_first()
        self.management.rows[2]["group"] = "default,second"
        _, second = await self.second_binding()
        second.phase = "fault"
        await self.round()
        self.assertEqual(self.management.writes, [(1, 2), (1, 1)])
        deficient = [e for e in self.svc.store.history()["events"] if e["kind"] == "minimum_channels"]
        self.assertEqual(deficient[0]["detail"]["deficient"], [{"group": "second", "model": "gpt-6-astra", "remaining": 0}])

    async def test_circuit_reset_requires_pause_confirmation_and_current_revision(self):
        _, second = await self.second_binding()
        self.models.phase = second.phase = "fault"
        await self.round()
        revision = self.svc.store.circuit()["revision"]
        app = FastAPI(); app.include_router(api.router)
        with patch.object(api, "get_service", return_value=self.svc):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                for body in ({"revision": revision}, {"revision": revision, "confirm_reset": False},
                             {"revision": revision, "confirm_reset": True}):
                    self.assertEqual((await client.post("/api/channel-monitor/circuit/reset", json=body)).status_code, 422)
                self.svc.enable(False)
                self.assertEqual((await client.post("/api/channel-monitor/circuit/reset", json={"revision": revision-1, "confirm_reset": True})).status_code, 422)
                response = await client.post("/api/channel-monitor/circuit/reset", json={"revision": revision, "confirm_reset": True})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.json()["opened"])
                self.assertFalse(self.svc.store.plan()["enabled"])


if __name__ == "__main__":
    unittest.main()
