from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx

from prototypes.admission_review.api import create_app
from prototypes.admission_review.cli import sanitized_snapshot
from prototypes.admission_review.feishu import BitableWriter, FeishuError, FeishuSettings
from prototypes.admission_review.groups import group_for_model
from prototypes.admission_review.storage import Store


SETTINGS = FeishuSettings(
    app_id="test-app",
    app_secret="synthetic-app-secret",
    app_token="test_app_token",
    table_id="test_table_id",
)


class StubWriter:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.calls: list[dict[str, str]] = []

    async def create_record(self, fields: dict[str, str]) -> str:
        self.calls.append(dict(fields))
        if self.failures:
            self.failures -= 1
            raise FeishuError("飞书网络请求失败：synthetic")
        return f"record-{len(self.calls)}"


class FrameworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="admission-feishu-framework-")
        self.store = Store(Path(self.temp.name) / "framework.db")
        self.clients: list[httpx.AsyncClient] = []

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.aclose()
        self.store.close()
        self.temp.cleanup()

    def client(
        self,
        settings: FeishuSettings | None = None,
        writer: Any = None,
    ) -> httpx.AsyncClient:
        app = create_app(self.store, settings or FeishuSettings(), writer)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.clients.append(client)
        return client

    async def test_unconfigured_flow_records_only_channel_and_group(self) -> None:
        client = self.client()
        response = await client.post("/api/participations", json={
            "channel": "候选渠道 A",
            "model": "claude-opus-5",
        })
        self.assertEqual(response.status_code, 201, response.text)
        item = response.json()
        self.assertEqual(item["test_group"], "Claude")
        self.assertEqual(item["sync_status"], "not_configured")
        self.assertEqual(item["fields"], {"渠道": "候选渠道 A", "测试分组": "Claude"})
        self.assertNotIn("人工评判结果", item["fields"])
        retry = await client.post(f"/api/participations/{item['id']}/retry")
        self.assertEqual(retry.status_code, 409)

    async def test_configured_flow_writes_exactly_two_fields(self) -> None:
        writer = StubWriter()
        client = self.client(SETTINGS, writer)
        response = await client.post("/api/participations", json={
            "channel": "候选渠道 B",
            "model": "gpt-5.6-sol",
        })
        self.assertEqual(response.status_code, 201, response.text)
        item = response.json()
        self.assertEqual(item["sync_status"], "synced")
        self.assertEqual(item["test_group"], "Codex")
        self.assertEqual(writer.calls, [{"渠道": "候选渠道 B", "测试分组": "Codex"}])
        repeated = await client.post(f"/api/participations/{item['id']}/retry")
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(len(writer.calls), 1)

    async def test_failed_delivery_can_be_retried_without_blocking_record(self) -> None:
        writer = StubWriter(failures=1)
        client = self.client(SETTINGS, writer)
        created = await client.post("/api/participations", json={
            "channel": "候选渠道 C",
            "model": "deepseek-v4-pro",
        })
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["sync_status"], "failed")
        self.assertEqual(created.json()["attempts"], 1)
        retried = await client.post(
            f"/api/participations/{created.json()['id']}/retry"
        )
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["sync_status"], "synced")
        self.assertEqual(retried.json()["attempts"], 2)

    async def test_model_group_uses_admission_families(self) -> None:
        expected = {
            "gpt-5.6-terra": "Codex",
            "claude-fable-5": "Claude",
            "glm-5.3": "智谱",
            "kimi-k3": "Kimi",
            "deepseek-v4-flash": "DeepSeek",
        }
        for model, group in expected.items():
            self.assertEqual(group_for_model(model), group)
        with self.assertRaises(ValueError):
            group_for_model("unknown-family-model")

    async def test_validation_error_does_not_echo_channel(self) -> None:
        client = self.client()
        channel = "private-channel-" + "q" * 180
        response = await client.post("/api/participations", json={
            "channel": channel,
            "model": "claude-opus-5",
        })
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn(channel, response.text)
        self.assertNotIn('"input"', response.text)

    async def test_one_command_snapshot_deidentifies_channel(self) -> None:
        item = self.store.create(
            "不可出现在验收报告中的渠道名",
            "Claude",
            {"渠道": "不可出现在验收报告中的渠道名", "测试分组": "Claude"},
            delivery_configured=False,
        )
        snapshot = sanitized_snapshot(item)
        encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertEqual(snapshot["channel_alias"], f"candidate-{item['id']}")
        self.assertEqual(snapshot["test_group"], "Claude")
        self.assertEqual(snapshot["written_field_count"], 2)
        self.assertNotIn(item["channel_name"], encoded)
        self.assertNotIn("fields", snapshot)


class FeishuClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_endpoints_receive_only_two_record_fields(self) -> None:
        requests: list[httpx.Request] = []
        tenant_token = "synthetic-" + "tenant-token"

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/tenant_access_token/internal"):
                return httpx.Response(200, json={
                    "code": 0,
                    "tenant_access_token": tenant_token,
                    "expire": 7200,
                })
            self.assertEqual(
                request.url.path,
                "/open-apis/bitable/v1/apps/test_app_token/tables/test_table_id/records",
            )
            self.assertEqual(request.headers["Authorization"], f"Bearer {tenant_token}")
            self.assertEqual(
                json.loads(request.content),
                {"fields": {"渠道": "候选渠道 D", "测试分组": "Claude"}},
            )
            return httpx.Response(200, json={
                "code": 0,
                "data": {"record": {"record_id": "record-from-feishu"}},
            })

        writer = BitableWriter(SETTINGS, transport=httpx.MockTransport(handler))
        record_id = await writer.create_record({"渠道": "候选渠道 D", "测试分组": "Claude"})
        self.assertEqual(record_id, "record-from-feishu")
        self.assertEqual(len(requests), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
