import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

_root = tempfile.TemporaryDirectory(prefix="workbench-tests-")
os.environ["PLATFORM_DATA_DIR"] = _root.name
os.environ.pop("PLATFORM_USERNAME", None)
os.environ.pop("PLATFORM_PASSWORD", None)
for _name in (
    "ADMISSION_FEISHU_APP_ID",
    "ADMISSION_FEISHU_APP_SECRET",
    "ADMISSION_FEISHU_APP_TOKEN",
    "ADMISSION_FEISHU_TABLE_ID",
):
    os.environ.pop(_name, None)

import httpcore
import httpx
from shared import registry as registry_module
from shared.registry import Registry, RegistryError
from shared.network import PublicNetwork
from shared.scheduler_lock import scheduler_lock
from shared.redaction import EventRedactor
from features.admission import api as admission
from features.admission import feishu as admission_feishu
from features.admission import storage as admission_storage
from features.reasoning import main as reasoning
from features.stability.app import storage, scheduler
from scripts.admission_channel_snapshot import sanitized_snapshot
from workbench import create_app


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_root.name)
        self.directory = Path(self.temp.name)
        self.registry = Registry(self.directory)
        registry_module._registry = self.registry
        admission_storage.close()
        admission_storage.DB_PATH = self.directory / "admission" / "reports.db"
        storage.close()
        storage.DB_PATH = self.directory / "stability.db"
        self.app = create_app()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        self.key = "sk-reference-never-public-123456789"
        self.record = {"base_url":"https://reference.example/v1", "api_key":self.key,
                       "multiplier":0.7, "scope":"codex", "status":"online"}
        self.channel = self.registry.save(self.record)

    async def asyncTearDown(self):
        await scheduler.stop()
        admission_storage.close()
        storage.close()
        admission.app.state.upstream_transport = None
        admission.app.state.feishu_settings = admission_feishu.FeishuSettings()
        admission.app.state.feishu_writer = None
        reasoning.app.state.upstream_transport = None
        await self.client.aclose()
        self.temp.cleanup()

    def candidate(self):
        return {"base_url":"https://candidate.example/v1", "api_key":"sk-ephemeral-candidate-0987654321",
                "model":"demo-model", "protocol":"openai"}

    def selection(self):
        return {"channel_id":self.channel["id"], "model":"demo-model", "protocol":"openai"}

    async def test_manual_candidate_not_stored_and_both_keys_redacted_across_chunks(self):
        candidate = self.candidate()
        before = self.registry.list()
        requests = []
        def handler(request):
            requests.append(request)
            key = candidate["api_key"] if request.url.host == "candidate.example" else self.key
            self.assertEqual(request.headers["authorization"], f"Bearer {key}")
            self.assertTrue(json.loads(request.content)["stream"])
            chunks = [key[:9], key[9:22], key[22:], " answer"]
            stream = ["data: " + json.dumps({"choices":[{"delta":{"content":chunk}, "finish_reason":None}]}) for chunk in chunks]
            stream += ['data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"completion_tokens":10}}', 'data: [DONE]', '']
            return httpx.Response(200, text="\n".join(stream))
        admission.app.state.upstream_transport = httpx.MockTransport(handler)
        with patch.object(admission.engine, "wait_between_questions", new=AsyncMock()):
            response = await self.client.post("/admission/api/compare", json={"candidate":candidate, "reference":self.selection(), "rounds":1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(requests), 10)
        self.assertNotIn(candidate["api_key"], response.text)
        self.assertNotIn(self.key, response.text)
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(events[-1]["type"], "run_finished")
        outputs = {}
        for event in events:
            if event["type"] == "chunk":
                key = (event["side"], event["question_id"])
                outputs[key] = outputs.get(key, "") + event["content"]
        self.assertTrue(all(value == "[REDACTED] answer" for value in outputs.values()), outputs)
        self.assertEqual(self.registry.list(), before)
        for path in self.directory.rglob("*.db*"):
            self.assertNotIn(candidate["api_key"].encode(), path.read_bytes())
            self.assertNotIn(self.key.encode(), path.read_bytes())
        self.assertEqual(storage.list_channels(), [])
        self.assertEqual(storage.list_schedules(), [])

    async def test_valid_admission_submission_queues_url_and_model_family(self):
        calls = []

        class Writer:
            async def create_record(self, fields):
                calls.append(fields)
                return "test-record-id"

        candidate = {
            **self.candidate(),
            "base_url": "https://candidate.example/v1",
            "model": "claude-opus-5",
            "protocol": "anthropic",
        }
        admission.app.state.feishu_settings = admission_feishu.FeishuSettings()
        admission.app.state.feishu_writer = Writer()

        async def fake_run_question(body, question, client):
            for side in ("candidate", "reference"):
                yield {
                    "type": "side_finished",
                    "question_id": question["id"],
                    "side": side,
                    "ok": True,
                    "status": "completed",
                }

        with patch.object(admission.engine, "run_question", fake_run_question), patch.object(
            admission.engine, "wait_between_questions", new=AsyncMock()
        ):
            response = await self.client.post(
                "/admission/api/compare",
                json={"candidate": candidate, "reference": self.selection(), "rounds": 1},
            )
        self.assertEqual(response.status_code, 200, response.text)
        if admission._feishu_tasks:
            await asyncio.gather(*tuple(admission._feishu_tasks))
        entry = admission_storage.latest_feishu_record()
        assert entry is not None
        self.assertEqual(entry["channel_url"], "https://candidate.example/v1")
        self.assertEqual(entry["test_group"], "Claude")
        self.assertEqual(entry["status"], "synced")
        self.assertEqual(
            calls,
            [{"渠道": "https://candidate.example/v1", "测试分组": "Claude"}],
        )
        self.assertNotIn("人工评判结果", entry["fields"])
        snapshot = sanitized_snapshot(entry)
        self.assertEqual(snapshot["test_group"], "Claude")
        self.assertNotIn(entry["channel_url"], json.dumps(snapshot, ensure_ascii=False))

    async def test_feishu_failure_does_not_block_admission_submission(self):
        class FailingWriter:
            async def create_record(self, fields):
                raise admission_feishu.FeishuError("飞书网络请求失败：synthetic")

        candidate = {**self.candidate(), "model": "gpt-5.6-sol"}
        admission.app.state.feishu_settings = admission_feishu.FeishuSettings()
        admission.app.state.feishu_writer = FailingWriter()

        async def fake_run_question(body, question, client):
            for side in ("candidate", "reference"):
                yield {
                    "type": "side_finished",
                    "question_id": question["id"],
                    "side": side,
                    "ok": True,
                    "status": "completed",
                }

        with patch.object(admission.engine, "run_question", fake_run_question), patch.object(
            admission.engine, "wait_between_questions", new=AsyncMock()
        ):
            response = await self.client.post(
                "/admission/api/compare",
                json={"candidate": candidate, "reference": self.selection(), "rounds": 1},
            )
        self.assertEqual(response.status_code, 200, response.text)
        if admission._feishu_tasks:
            await asyncio.gather(*tuple(admission._feishu_tasks))
        entry = admission_storage.latest_feishu_record()
        assert entry is not None
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["test_group"], "Codex")

    async def test_frontend_create_online_edit_keep_key_and_reject_stale_edit(self):
        response = await self.client.post("/api/registry/channels", json={**self.record,"base_url":"https://second.example/v1"})
        self.assertEqual(response.status_code, 200)
        channel = response.json()
        self.assertEqual(channel["status"], "online")
        self.assertNotIn(self.key, response.text)
        updated = {**self.record,"base_url":channel["base_url"],"api_key":"", "name":"updated", "version":channel["version"]}
        response = await self.client.put(f"/api/registry/channels/{channel['id']}",json=updated)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.registry.get(channel["id"],secret=True)["api_key"],self.key)
        stale = await self.client.put(f"/api/registry/channels/{channel['id']}",json=updated)
        self.assertEqual(stale.status_code,409)
        self.assertEqual(storage.list_schedules(),[])

    async def test_duplicate_manual_add_requires_explicit_edit(self):
        duplicate = await self.client.post("/api/registry/channels",json=self.record)
        self.assertEqual(duplicate.status_code,409)
        self.assertEqual(len(self.registry.list()),1)

    async def test_nonstream_reads_updated_shared_key(self):
        changed = "sk-rotated-reference-987654321"
        self.registry.save({**self.record,"api_key":changed},self.channel["id"],1)
        calls = []
        def handler(request):
            calls.append(request)
            self.assertFalse(json.loads(request.content)["stream"])
            self.assertEqual(request.headers["authorization"],f"Bearer {changed}")
            return httpx.Response(200,json={"choices":[{"message":{"content":f"Answer {changed}","reasoning_content":"Reasoning"},"finish_reason":"stop"}]})
        reasoning.app.state.upstream_transport = httpx.MockTransport(handler)
        response = await self.client.post("/reasoning/api/test",json={"endpoint":self.selection()})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(calls),5)
        self.assertNotIn(changed,response.text)
        self.assertEqual(response.json()["summary"]["complete_count"],5)
        self.assertNotIn("api_key",response.json()["endpoint"])

    async def test_only_explicit_targets_use_shared_credentials(self):
        self.registry.save({**self.record,"base_url":"https://unselected.example"})
        target = {"name":"chosen-model", "registry_channel_id":self.channel["id"], "model":"demo-model", "protocol":"openai", "enabled":True}
        response = await self.client.post("/stability/api/channels",json=target)
        self.assertEqual(response.status_code,200,response.text)
        targets = storage.list_channels(include_secrets=True)
        self.assertEqual(len(targets),1)
        self.assertEqual(targets[0]["api_key"],self.key)
        self.assertEqual(storage.list_schedules(),[])
        updated_key = "sk-new-test-key-123456789"
        self.registry.save({**self.record,"api_key":updated_key,"base_url":"https://updated.example/v1"},self.channel["id"],1)
        updated = storage.list_channels(include_secrets=True)[0]
        self.assertEqual(updated["api_key"],updated_key)
        self.assertEqual(updated["base_url"],"https://updated.example/v1")
        with storage.cursor() as cur:
            row = cur.execute("SELECT api_key_enc,base_url,registry_channel_id FROM channels").fetchone()
        self.assertEqual(tuple(row),("","",self.channel["id"]))

    async def test_report_groups_are_independent_from_inventory_multiplier(self):
        body = {"groups": [
            {"family": "Claude", "label": "0.5x", "registry_channel_ids": [self.channel["id"]]},
            {"family": "Claude", "label": "3.5x", "registry_channel_ids": [], "always_normal": True},
        ]}
        response = await self.client.put("/stability/api/settings/report-groups", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        groups = (await self.client.get("/stability/api/settings/report-groups")).json()["groups"]
        self.assertEqual(groups[0]["registry_channel_ids"], [self.channel["id"]])
        self.assertEqual(groups[0]["label"], "0.5x")
        self.assertEqual(groups[1]["always_normal"], True)
        invalid = await self.client.put("/stability/api/settings/report-groups", json={"groups": [
            {"family": "Codex", "label": "0.7x", "registry_channel_ids": [999999]},
        ]})
        self.assertEqual(invalid.status_code, 400)

    async def test_admission_reports_roll_over_at_thirty_without_keys(self):
        base_report = {
            "version": 1,
            "created_at": "2026-09-07T10:30:00.000Z",
            "status": "completed",
            "rounds": 1,
            "candidate": {
                "base_url": "https://candidate.example/v1", "model": "candidate-model",
                "protocol": "openai",
            },
            "reference": {
                "channel_id": self.channel["id"], "name": "reference", "base_url": self.channel["base_url"],
                "model": "reference-model", "protocol": "openai", "multiplier": 0.7,
            },
            "questions": [{
                "id": "easy-1", "title": "信息提取", "difficulty": "easy", "prompt": "测试题",
            }],
            "measurements": [{
                "type": "side_finished", "question_id": "easy-1", "side": "candidate",
                "round": 1, "ok": True, "status": "completed", "total_ms": 500,
            }],
            "responses": [{
                "round": 1, "question_id": "easy-1", "side": "candidate",
                "content": "safe answer", "reasoning": "",
            }],
            "summary": {"candidate": {"completed": 1}},
        }
        ids = []
        for index in range(31):
            response = await self.client.post(
                "/admission/api/reports",
                json={**base_report, "created_at": f"2026-09-07T10:30:{index:02d}.000Z"},
            )
            self.assertEqual(response.status_code, 201, response.text)
            ids.append(response.json()["id"])

        reports = (await self.client.get("/admission/api/reports")).json()["reports"]
        self.assertEqual(len(reports), 30)
        self.assertEqual(reports[0]["id"], ids[-1])
        self.assertEqual(reports[-1]["id"], ids[1])
        self.assertEqual((await self.client.get(f"/admission/api/reports/{ids[0]}")).status_code, 404)
        detail = await self.client.get(f"/admission/api/reports/{ids[-1]}")
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn("api_key", detail.text)
        self.assertNotIn(self.key, detail.text)
        deleted = await self.client.delete(f"/admission/api/reports/{ids[-1]}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(len((await self.client.get("/admission/api/reports")).json()["reports"]), 29)

        unsafe = {**base_report, "candidate": {**base_report["candidate"], "api_key": self.key}}
        response = await self.client.post("/admission/api/reports", json=unsafe)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(self.key, response.text)

    async def test_scheduled_report_waits_for_configured_delay(self):
        target = {
            "name": "delayed-report-target", "registry_channel_id": self.channel["id"],
            "model": "demo-model", "protocol": "openai", "enabled": True,
        }
        channel_id = (await self.client.post("/stability/api/channels", json=target)).json()["id"]
        payload = {
            "name": "delayed-report", "daily_times": "09:30", "timezone": "Asia/Shanghai",
            "channel_ids": [channel_id], "rounds": 1, "round_interval_seconds": 0,
            "notification_delay_seconds": 3300, "max_concurrency": 1,
            "min_success_rate": 0.95, "max_timeout_rate": 0.05,
            "max_stream_break_rate": 0, "max_p95_ms": 30000,
            "speed_threshold_mode": "adaptive", "speed_baseline_min_runs": 5,
            "speed_slow_ratio": 1.5, "enabled": True,
        }
        response = await self.client.post("/stability/api/schedules", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        schedule = storage.get_schedule(response.json()["id"])
        self.assertEqual(schedule["notification_delay_seconds"], 3300)
        self.assertEqual(schedule["speed_threshold_mode"], "adaptive")
        due = 1_800_000_000.0
        run_id = storage.create_run(schedule, due)
        storage.finish_run(run_id, "completed", {"verdict": "pass", "channels": []})
        self.assertEqual(storage.pending_notifications(due + 3299), [])
        ready = storage.pending_notifications(due + 3300)
        self.assertEqual([item["id"] for item in ready], [run_id])
        self.assertTrue(storage.claim_notification(run_id))
        self.assertFalse(storage.claim_notification(run_id))

    async def test_speed_baseline_uses_median_of_stable_scheduled_runs(self):
        target = {
            "name": "baseline-target", "registry_channel_id": self.channel["id"],
            "model": "demo-model", "protocol": "openai", "enabled": True,
        }
        channel_id = (await self.client.post("/stability/api/channels", json=target)).json()["id"]
        schedule_data = {
            "name": "baseline-plan", "daily_times": "09:30", "timezone": "Asia/Shanghai",
            "channel_ids": [channel_id], "rounds": 1, "round_interval_seconds": 0,
            "notification_delay_seconds": 0, "max_concurrency": 1,
            "min_success_rate": 0.95, "max_timeout_rate": 0.05,
            "max_stream_break_rate": 0, "max_p95_ms": 30000,
            "speed_threshold_mode": "adaptive", "speed_baseline_min_runs": 5,
            "speed_slow_ratio": 1.5, "enabled": True,
        }
        schedule_id = storage.upsert_schedule(schedule_data)
        schedule = storage.get_schedule(schedule_id)

        for index, p95 in enumerate((1000, 1200, 1400, 1600, 1800)):
            run_id = storage.create_run(schedule, 1_800_000_000 + index)
            for probe_index, latency in enumerate((p95 - 100, p95)):
                storage.add_probe_result(run_id, {
                    "channel_id": channel_id, "channel_name": "baseline-target", "model": "demo-model",
                    "round_number": 1, "probe_id": f"probe-{probe_index}", "ok": True,
                    "status": "completed", "latency_ms": latency,
                })
            storage.finish_run(run_id, "completed", {"verdict": "pass"})

        unstable_id = storage.create_run(schedule, 1_800_000_100)
        storage.add_probe_result(unstable_id, {
            "channel_id": channel_id, "channel_name": "baseline-target", "model": "demo-model",
            "round_number": 1, "probe_id": "failed", "ok": False,
            "status": "timeout", "latency_ms": 99999,
        })
        storage.finish_run(unstable_id, "completed", {"verdict": "fail"})

        baseline = storage.channel_latency_baseline(channel_id, "demo-model")
        self.assertEqual(baseline["sample_count"], 5)
        self.assertEqual(baseline["median_p95_latency_ms"], 1400)

    async def test_stability_history_keeps_five_days_and_unsettled_reports(self):
        target = {
            "name": "retention-target", "registry_channel_id": self.channel["id"],
            "model": "demo-model", "protocol": "openai", "enabled": True,
        }
        channel_id = (await self.client.post("/stability/api/channels", json=target)).json()["id"]
        schedule_id = storage.upsert_schedule({
            "name": "retention-plan", "daily_times": "09:30", "timezone": "Asia/Shanghai",
            "channel_ids": [channel_id], "rounds": 1, "round_interval_seconds": 0,
            "notification_delay_seconds": 0, "max_concurrency": 1,
            "min_success_rate": 0.95, "max_timeout_rate": 0.05,
            "max_stream_break_rate": 0, "max_p95_ms": 30000,
            "speed_threshold_mode": "adaptive", "speed_baseline_min_runs": 5,
            "speed_slow_ratio": 1.5, "enabled": True,
        })
        schedule = storage.get_schedule(schedule_id)
        now = 1_900_000_000.0

        old_settled = storage.create_run(schedule, now - 7 * 86400)
        storage.add_probe_result(old_settled, {
            "channel_id": channel_id, "channel_name": "retention-target", "model": "demo-model",
            "round_number": 1, "probe_id": "old", "ok": True,
            "status": "completed", "latency_ms": 1000,
        })
        storage.finish_run(old_settled, "completed", {"verdict": "pass"})
        storage.update_notification(old_settled, "sent")

        old_pending_notice = storage.create_run(schedule, now - 6 * 86400)
        storage.finish_run(old_pending_notice, "completed", {"verdict": "pass"})

        recent_settled = storage.create_run(schedule, now - 4 * 86400)
        storage.finish_run(recent_settled, "completed", {"verdict": "pass"})
        storage.update_notification(recent_settled, "sent")

        with storage.cursor() as cur:
            cur.execute("UPDATE runs SET finished_at=? WHERE id=?", (now - 7 * 86400, old_settled))
            cur.execute("UPDATE runs SET finished_at=? WHERE id=?", (now - 6 * 86400, old_pending_notice))
            cur.execute("UPDATE runs SET finished_at=? WHERE id=?", (now - 4 * 86400, recent_settled))

        self.assertEqual(storage.prune_run_history(now, 5), 1)
        self.assertIsNone(storage.get_run(old_settled))
        self.assertIsNotNone(storage.get_run(old_pending_notice))
        self.assertIsNotNone(storage.get_run(recent_settled))
        with storage.cursor() as cur:
            self.assertEqual(cur.execute(
                "SELECT count(*) FROM probe_results WHERE run_id=?", (old_settled,)
            ).fetchone()[0], 0)

    async def test_read_and_validation_interfaces_do_not_leak_keys(self):
        for url in ("/api/registry/channels","/stability/api/channel-inventory","/stability/api/channels"):
            response = await self.client.get(url)
            self.assertEqual(response.status_code,200)
            self.assertNotIn(self.key,response.text)
            self.assertNotIn('"key_enc"',response.text)
        response = await self.client.post("/reasoning/api/test",json={"endpoint":{**self.selection(),"api_key":self.key}})
        self.assertEqual(response.status_code,422)
        self.assertNotIn(self.key,response.text)
        response = await self.client.post("/admission/api/compare",json={"candidate":self.candidate(),"reference":self.candidate()})
        self.assertEqual(response.status_code,422)
        self.assertNotIn(self.candidate()["api_key"],response.text)
        self.assertEqual((await self.client.post("/reasoning/api/extract-channel",json={"text":"anything"})).status_code,404)

    async def test_import_idempotent_ignores_template_and_does_not_schedule(self):
        text = json.dumps([{**self.record,"base_url":"https://imported.example","model":"gpt-5.5"}])
        result = await self.client.post("/api/registry/import",json={"text":text})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()["added"],1)
        second = await self.client.post("/api/registry/import",json={"text":text})
        self.assertEqual(second.json()["skipped"],1)
        self.assertNotIn(self.key,result.text)
        self.assertNotIn("gpt-5.5",result.text)
        self.assertEqual(storage.list_channels(),[])
        self.assertEqual(storage.list_schedules(),[])

    async def test_disabled_channel_prevents_new_manual_tests(self):
        self.registry.save({**self.record,"enabled":False},self.channel["id"],1)
        response = await self.client.post("/reasoning/api/test",json={"endpoint":self.selection()})
        self.assertEqual(response.status_code,400)
        response = await self.client.post("/admission/api/compare",json={"candidate":self.candidate(),"reference":self.selection()})
        self.assertEqual(response.status_code,400)
        self.assertIsNone(admission_storage.latest_feishu_record())

    async def test_authentication_and_cross_site_writes(self):
        with patch.dict(os.environ,{"PLATFORM_USERNAME":"admin","PLATFORM_PASSWORD":"long-enough-test-password"}):
            app = create_app("channels")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://testserver") as client:
            self.assertEqual((await client.get("/api/registry/channels")).status_code,401)
            client.auth = ("admin","long-enough-test-password")
            self.assertEqual((await client.get("/api/registry/channels")).status_code,200)
            response = await client.post("/api/registry/channels",json=self.record,headers={"Origin":"https://evil.example"})
            self.assertEqual(response.status_code,403)

    async def test_each_mode_has_only_its_feature_and_same_registry(self):
        for mode in ("channels","admission","reasoning","stability"):
            app = create_app(mode)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://testserver") as client:
                    data = (await client.get("/api/platform")).json()
                    self.assertEqual([f["id"] for f in data["features"]],[] if mode == "channels" else [mode])
                    self.assertEqual((await client.get("/api/registry/channels")).json()["channels"][0]["id"],self.channel["id"])
                    self.assertEqual(scheduler.status()["running"],mode=="stability")
                    if mode != "channels":
                        self.assertEqual((await client.get(f"/{mode}/")).status_code,200)
        self.assertFalse(scheduler.status()["running"])

    async def test_all_mode_starts_and_stops_scheduler(self):
        async with self.app.router.lifespan_context(self.app):
            response = await self.client.get("/api/health")
            self.assertEqual(response.json()["status"],"ok")
            self.assertTrue(scheduler.status()["running"])
        self.assertFalse(scheduler.status()["running"])


class StorageAndNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_secret_does_not_corrupt_event_schema(self):
        redactor = EventRedactor(["type"])
        events = redactor.filter({"type":"chunk","question_id":"q1","side":"candidate","content":"type answer","reasoning":""})
        events += redactor.filter({"type":"side_finished","question_id":"q1","side":"candidate","ok":True})
        self.assertEqual(events[-1]["type"],"side_finished")
        self.assertEqual("".join(e.get("content","") for e in events),"[REDACTED] answer")

    async def test_network_blocks_rebinding_and_connects_to_validated_ip(self):
        backend = PublicNetwork()
        records = [(socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1",443))]
        with patch("shared.network.socket.getaddrinfo",return_value=records):
            with self.assertRaises(httpcore.ConnectError):
                await backend.connect_tcp("public-looking.example",443)
        records[0] = (socket.AF_INET,socket.SOCK_STREAM,6,"",("1.1.1.1",443))
        with patch("shared.network.socket.getaddrinfo",return_value=records), patch("httpcore._backends.auto.AutoBackend.connect_tcp",new=AsyncMock(return_value="stream")) as connect:
            self.assertEqual(await backend.connect_tcp("public-looking.example",443),"stream")
            self.assertEqual(connect.call_args.args[0],"1.1.1.1")

    async def test_import_atomicity_and_missing_master_key(self):
        with tempfile.TemporaryDirectory() as root:
            registry = Registry(Path(root))
            with self.assertRaises(RegistryError):
                registry.import_records([{"base_url":"https://valid.example","api_key":"valid-key","multiplier":1},
                                         {"base_url":"bad-url","api_key":"key","multiplier":1}])
            self.assertEqual(registry.list(),[])
            registry.key_path.unlink()
            with self.assertRaises(RegistryError):
                Registry(Path(root))

    async def test_scheduler_lock_prevents_two_instances(self):
        with tempfile.TemporaryDirectory() as root:
            with scheduler_lock(Path(root)):
                with self.assertRaises(RuntimeError):
                    with scheduler_lock(Path(root)):
                        pass


if __name__ == "__main__":
    unittest.main()
