"""Synthetic Feishu inventory fixtures; no external services or real credentials."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

from shared.feishu_channels import ChannelSync, SettingsInput
from shared.feishu_reader import FIELDS, FeishuReadError, FeishuReader, table_address
from shared.registry import Conflict, Registry, RegistryError


SYNTHETIC_SECRET = "synthetic-application-secret"
SYNTHETIC_KEY = "synthetic-upstream-key"
TABLE = "https://example.feishu.cn/base/SyntheticBase?table=tblSynthetic&view=vewSynthetic"


def record(identity="recOne", **fields):
    return {"record_id": identity, "fields": {"渠道名称": "Synthetic", "模型家族": "codex",
            "baseurl": {"text": "Website label", "link": "https://example.com/v1"},
            "apikey": [{"type": "text", "text": SYNTHETIC_KEY}], "倍率": 0.15, "是否在线": "已上线", **fields}}


class FeishuFixture:
    def __init__(self, rows):
        self.pages = [rows]
        self.calls = []
        self.failure_page = None
        self.schema = [{"field_name": key, "type": max(value)} for key, value in FIELDS.items()]
        self.field_page_size = 100
        self.loop = False

    def respond(self, request):
        self.calls.append((request.method, request.url.path))
        assert request.url.host == "open.feishu.cn"
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            assert json.loads(request.content)["app_secret"] == SYNTHETIC_SECRET
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "synthetic-tenant-token"})
        assert request.headers["authorization"] == "Bearer synthetic-tenant-token"
        if path.endswith("/get_node"):
            return httpx.Response(200, json={"code": 0, "data": {"node": {"obj_type": "bitable", "obj_token": "SyntheticBase"}}})
        if path.endswith("/fields"):
            assert request.method == "GET"
            assert 1 <= int(request.url.params["page_size"]) <= 100
            offset = int(request.url.params.get("page_token", "0"))
            end = offset + self.field_page_size
            return httpx.Response(200, json={"code": 0, "data": {"items": self.schema[offset:end],
                "has_more": end < len(self.schema), "page_token": str(end)}})
        assert path.endswith("/records/search") and request.method == "POST"
        assert 1 <= int(request.url.params["page_size"]) <= 500
        body = json.loads(request.content)
        assert body == {"field_names": list(FIELDS), "automatic_fields": False}
        page = int(request.url.params.get("page_token", "0"))
        if self.failure_page == page:
            return httpx.Response(403, json={"message": SYNTHETIC_SECRET + SYNTHETIC_KEY})
        more = page + 1 < len(self.pages) or self.loop
        return httpx.Response(200, json={"code": 0, "data": {"items": self.pages[page], "has_more": more,
                                                                     "page_token": "0" if self.loop else str(page + 1)}})

    def reader(self):
        return FeishuReader(transport=httpx.MockTransport(self.respond))


class FeishuSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = Registry(Path(self.temp.name))
        self.sync = ChannelSync(self.registry)
        self.sync.configure(SettingsInput(app_id="cli_synthetic", app_secret=SYNTHETIC_SECRET, table_url=TABLE))

    async def run_sync(self, rows):
        fixture = FeishuFixture(rows)
        return await self.sync.sync(fixture.reader())

    async def test_filter_mapping_and_encryption(self):
        rows = [record(), record("recOffline", 是否在线="已下线"), record("recNoKey", apikey="  "),
                record("recPending", 是否在线="待确认"), record("recNoURL", baseurl=""),
                record("recNoRate", 倍率=None), record("recUnknown", 是否在线="?")]
        result = await self.run_sync(rows)
        self.assertEqual({key: result[key] for key in ("added", "skipped", "pending")},
                         {"added": 1, "skipped": 3, "pending": 3})
        channel = self.registry.list()[0]
        self.assertEqual((channel["name"], channel["scope"], channel["multiplier"], channel["base_url"]),
                         ("Synthetic", "codex", .15, "https://example.com/v1"))
        self.assertTrue(channel["enabled"])
        self.assertEqual(channel["status"], "online")
        self.assertEqual(self.registry.resolve(channel["id"])["api_key"], SYNTHETIC_KEY)
        public = json.dumps([self.sync.public(), self.registry.list(), result])
        for secret in (SYNTHETIC_SECRET, SYNTHETIC_KEY, "synthetic-tenant-token"):
            self.assertNotIn(secret, public)
            for path in Path(self.temp.name).iterdir():
                self.assertNotIn(secret.encode(), path.read_bytes())

    async def test_idempotency_key_rotation_and_history_reference(self):
        await self.run_sync([record()])
        old = self.registry.list()[0]
        result = await self.run_sync([record()])
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(self.registry.get(old["id"])["version"], old["version"])
        self.registry.save({**old, "api_key": "", "note": "Keep local notes"}, old["id"], old["version"])
        result = await self.run_sync([record(apikey="synthetic-rotated-key", 渠道名称="Renamed", 倍率=.7)])
        self.assertEqual((result["added"], result["updated"]), (0, 1))
        current = self.registry.resolve(old["id"])
        self.assertEqual((current["name"], current["multiplier"], current["note"]), ("Renamed", .7, "Keep local notes"))
        self.assertEqual(current["api_key"], "synthetic-rotated-key")
        self.assertEqual(len(self.registry.list()), 1)

    async def test_offline_empty_key_and_reenable(self):
        await self.run_sync([record()])
        channel_id = self.registry.list()[0]["id"]
        for fields in ({"是否在线": "已下线"}, {"是否在线": "待确认"}, {"apikey": " "}):
            result = await self.run_sync([record(**fields)])
            self.assertEqual(result["disabled"], 1)
            with self.assertRaises(RegistryError):
                self.registry.resolve(channel_id)
            self.assertEqual((await self.run_sync([record()]))["updated"], 1)
            self.assertTrue(self.registry.resolve(channel_id)["enabled"])

    async def test_missing_rows_and_invalid_rows_preserve_channel(self):
        await self.run_sync([record()])
        before = self.registry.list()
        self.assertEqual((await self.run_sync([]))["missing"], 1)
        for fields in ({"倍率": 0}, {"倍率": "0.15"}, {"apikey": ["invalid"]},
                       {"是否在线": ""}, {"baseurl": "https://example.com?key=bad"}):
            self.assertEqual((await self.run_sync([record(**fields)]))["pending"], 1)
            self.assertEqual(self.registry.list(), before)

    async def test_readonly_online_inventory_uses_current_positive_members_and_preserves_registry(self):
        await self.run_sync([record()])
        before = self.registry.list()
        allowed = await self.sync.read_online_inventory(FeishuFixture([record()]).reader())
        self.assertEqual([(m["registry_id"], m["record_id"]) for m in allowed["members"]], [(before[0]["id"], "recOne")])
        self.assertTrue(allowed["generation"])
        for rows in ([], [record(是否在线="已下线")], [record(apikey="")], [record(倍率=0)],
                     [record(), record("recDuplicate")], [record(apikey="synthetic-new-key")]):
            with self.subTest(rows=len(rows)):
                self.assertEqual((await self.sync.read_online_inventory(FeishuFixture(rows).reader()))["members"], [])
                self.assertEqual(self.registry.list(), before)

    async def test_readonly_inventory_rejects_partial_pages_and_inflight_resync(self):
        await self.run_sync([record()])
        fixture = FeishuFixture([record()])
        fixture.pages.append([record("recTwo")])
        fixture.failure_page = 1
        with self.assertRaisesRegex(RegistryError, "白名单读取未完成"):
            await self.sync.read_online_inventory(fixture.reader())
        original = FeishuFixture([record()]).reader()
        sync = self.sync
        class ConcurrentReader:
            async def read(self, settings, secret):
                snapshot = await original.read(settings, secret)
                await sync.sync(FeishuFixture([record()]).reader())
                return snapshot
        with self.assertRaisesRegex(Conflict, "读取期间飞书设置变化"):
            await self.sync.read_online_inventory(ConcurrentReader())

    async def test_readonly_inventory_never_reenables_disabled_registry_channel(self):
        await self.run_sync([record()])
        row = self.registry.list()[0]
        self.registry.save({**row, "enabled": False}, row["id"], row["version"])
        self.assertEqual((await self.sync.read_online_inventory(FeishuFixture([record()]).reader()))["members"], [])
        self.assertFalse(self.registry.get(row["id"])["enabled"])

    async def test_different_families_remain_separate(self):
        result = await self.run_sync([record(), record("recClaude", 模型家族="claude")])
        self.assertEqual(result["added"], 2)
        self.assertEqual({x["scope"] for x in self.registry.list()}, {"codex", "claude"})

    async def test_duplicates_do_not_take_over_manual_channels(self):
        manual = self.registry.save({"base_url": "https://example.com/v1", "api_key": SYNTHETIC_KEY,
                                     "multiplier": .15, "scope": "codex", "name": "Manual"})
        result = await self.run_sync([record()])
        self.assertEqual((result["added"], result["pending"]), (0, 1))
        await self.run_sync([record(是否在线="已下线")])
        self.assertEqual(self.registry.get(manual["id"]), manual)

    async def test_duplicate_rows_and_opposite_states_do_not_disable_valid_connection(self):
        await self.run_sync([record()])
        before = self.registry.list()
        result = await self.run_sync([record(), record("recOther")])
        self.assertEqual(result["pending"], 2)
        result = await self.run_sync([record(是否在线="已下线"), record("recOther")])
        self.assertEqual((result["disabled"], result["pending"]), (0, 2))
        self.assertEqual(self.registry.list(), before)

    async def test_pages_complete_before_any_commit_and_failures_redact(self):
        await self.run_sync([record()])
        before, previous = self.registry.list(), self.sync.public()["last_result"]
        fixture = FeishuFixture([])
        fixture.pages = [[record(是否在线="已下线")], [record("recNew", baseurl="https://other.example/v1")]]
        fixture.failure_page = 1
        with self.assertRaises(FeishuReadError) as error:
            await self.sync.sync(fixture.reader())
        self.assertIn("HTTP 403", str(error.exception))
        self.assertNotIn(SYNTHETIC_SECRET, str(error.exception))
        self.assertEqual(self.registry.list(), before)
        self.assertEqual(self.sync.public()["last_result"], previous)
        self.assertTrue(self.sync.public()["last_error"])
        fixture.failure_page = None
        result = await self.sync.sync(fixture.reader())
        self.assertEqual((result["added"], result["disabled"]), (1, 1))

    async def test_field_schema_is_read_across_pages_before_records(self):
        fixture = FeishuFixture([record()])
        fixture.field_page_size = 2
        result = await self.sync.sync(fixture.reader())
        self.assertEqual(result["added"], 1)
        self.assertEqual([path.rsplit("/", 1)[-1] for _, path in fixture.calls],
                         ["internal", "fields", "fields", "fields", "search"])
        self.assertEqual(self.registry.resolve(self.registry.list()[0]["id"])["api_key"], SYNTHETIC_KEY)

    async def test_schema_duplicate_id_and_page_loop_reject_snapshot(self):
        fixtures = [FeishuFixture([record(), record()]), FeishuFixture([]), FeishuFixture([])]
        fixtures[1].schema = [x for x in fixtures[1].schema if x["field_name"] != "apikey"]
        fixtures[2].loop = True
        for fixture in fixtures:
            with self.assertRaises(FeishuReadError):
                await self.sync.sync(fixture.reader())
            self.assertFalse(self.registry.list())

    async def test_wiki_resolution_and_no_view_filter(self):
        self.sync.configure(SettingsInput(app_id="cli_synthetic", table_url=TABLE.replace("/base/", "/wiki/"), version=1))
        fixture = FeishuFixture([record()])
        self.assertEqual((await self.sync.sync(fixture.reader()))["added"], 1)
        self.assertTrue(any(path.endswith("/get_node") for _, path in fixture.calls))
        self.assertEqual(len(fixture.calls), 4)

    async def test_configuration_change_invalidates_inflight_result(self):
        owner = self.sync
        class Reader:
            async def read(self, settings, secret):
                result = await FeishuFixture([record()]).reader().read(settings, secret)
                owner.configure(SettingsInput(app_id="cli_synthetic", table_url=TABLE, version=1))
                return result
        with self.assertRaises(Conflict):
            await self.sync.sync(Reader())
        self.assertFalse(self.registry.list())
        self.assertEqual(self.sync.public()["version"], 2)

    async def test_concurrency_and_cancel_release_lease(self):
        entered = asyncio.Event()
        class Reader:
            async def read(self, settings, secret):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(self.sync.sync(Reader()))
        await asyncio.wait_for(entered.wait(), 2)
        with self.assertRaises(Conflict):
            await self.sync.sync(FeishuFixture([]).reader())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.sync.public()["running"])
        self.assertFalse(self.registry.list())

    async def test_settings_version_secret_retention_and_validation(self):
        with self.assertRaises(Conflict):
            self.sync.configure(SettingsInput(app_id="cli_synthetic", table_url=TABLE, version=0))
        with self.assertRaises(RegistryError):
            self.sync.configure(SettingsInput(app_id="cli_changed", table_url=TABLE, version=1))
        self.sync.configure(SettingsInput(app_id="cli_synthetic", table_url=TABLE, version=1))
        self.assertEqual((await self.run_sync([record()]))["added"], 1)
        for url in ("http://example.feishu.cn/base/SyntheticBase?table=tblSynthetic",
                    "https://example.feishu.cn.evil.test/base/SyntheticBase?table=tblSynthetic",
                    "https://" + "name:password@example.feishu.cn/base/SyntheticBase?table=tblSynthetic",
                    "https://example.feishu.cn/base/SyntheticBase"):
            with self.assertRaises(RegistryError):
                table_address(url)

    async def test_credentials_in_metadata_never_enter_public_results(self):
        for field in ("渠道名称", "模型家族", "baseurl"):
            result = await self.run_sync([record(**{field: SYNTHETIC_KEY})])
            self.assertEqual(result["pending"], 1)
            self.assertNotIn(SYNTHETIC_KEY, json.dumps(result))
        self.assertFalse(self.registry.list())

    async def test_local_transaction_rolls_back_all_rows_on_failure(self):
        original = self.registry._insert
        calls = 0
        def insert(conn, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic transaction failure")
            return original(conn, data)
        with patch.object(self.registry, "_insert", side_effect=insert):
            with self.assertRaises(RegistryError):
                await self.run_sync([record(), record("recSecond", 模型家族="claude")])
        self.assertFalse(self.registry.list())
        with self.registry.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM feishu_channel_bindings").fetchone()[0], 0)

    async def test_readonly_inspection_never_decrypts_or_changes_registry(self):
        await self.run_sync([record()])
        protected = [self.registry.path, self.registry.key_path]
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
        command = [sys.executable, "-B", "scripts/inspect_feishu_channels.py", "--data-dir", self.temp.name]
        output = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], text=True,
                                capture_output=True, timeout=10, check=True).stdout
        value = json.loads(output)
        self.assertEqual(value["requests_sent"], 0)
        self.assertFalse(value["credentials_decrypted"])
        self.assertTrue(value["configured"])
        self.assertNotIn(SYNTHETIC_SECRET, output)
        self.assertNotIn(SYNTHETIC_KEY, output)
        self.assertEqual({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}, before)

    async def test_redirect_error_payload_and_oversized_body_are_rejected(self):
        for response, expected_error in (
                (httpx.Response(302, headers={"Location": "https://evil.example"}), "HTTP 302"),
                (httpx.Response(200, json={"code": 123, "msg": SYNTHETIC_SECRET}), "飞书拒绝读取"),
                (httpx.Response(200, content=b" " * (4 * 1024 * 1024 + 1)), "飞书单页数据超过读取上限"),
                (httpx.Response(200, content=b"not-json"), "飞书返回的数据格式无效")):
            requests = []
            def handler(request):
                requests.append(request.url.host)
                return response
            with self.assertRaises(FeishuReadError) as caught:
                await self.sync.sync(FeishuReader(transport=httpx.MockTransport(handler)))
            self.assertNotIn(SYNTHETIC_SECRET, str(caught.exception))
            self.assertIn(expected_error, str(caught.exception))
            self.assertEqual(requests, ["open.feishu.cn"])
            self.assertFalse(self.registry.list())

    async def test_api_confirmation_no_network_on_get_or_configure(self):
        from workbench import create_app
        app = create_app("channels")
        fixture = FeishuFixture([record()])
        with patch("shared.feishu_channels.get_registry", return_value=self.registry), patch(
                "shared.feishu_channels.FeishuReader", return_value=fixture.reader()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                self.assertEqual((await client.get("/api/registry/feishu/settings")).status_code, 200)
                rejected = await client.post("/api/registry/feishu/sync", json={})
                self.assertEqual(rejected.status_code, 400)
                self.assertEqual(fixture.calls, [])
                response = await client.post("/api/registry/feishu/sync", json={"confirm_read": True})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["added"], 1)
                for secret in (SYNTHETIC_SECRET, SYNTHETIC_KEY):
                    self.assertNotIn(secret, response.text)
                rejected = await client.post("/api/registry/feishu/sync", json={"confirm_read": True},
                                             headers={"Origin": "https://evil.example"})
                self.assertEqual(rejected.status_code, 403)


if __name__ == "__main__":
    unittest.main()
