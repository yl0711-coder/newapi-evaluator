"""人工上线、只读核验、飞书差异预览和 Outbox 自测。"""
import asyncio
import json
import os
import time

os.environ.setdefault("TEST_DB_NAME", "selftest.db")
os.environ.setdefault("TEST_BOOTSTRAP_USERNAME", "selftest-admin")
os.environ.setdefault("TEST_BOOTSTRAP_PASSWORD", "selftest-password-2026")
os.environ.setdefault("TEST_COOKIE_SECURE", "0")
os.environ.setdefault("TEST_EGRESS_ALLOWLIST", "example.com,open.feishu.cn")

from fastapi.testclient import TestClient  # noqa: E402

from app import external_sync, main, store  # noqa: E402
from selftest_session import login  # noqa: E402

failed: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        failed.append(name)


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeVerificationClient:
    def __init__(self, payload: dict, **_: object):
        self.payload = payload

    async def __aenter__(self) -> "FakeVerificationClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, *_: object, **__: object) -> FakeResponse:
        return FakeResponse(self.payload)


def main_test() -> None:
    with TestClient(main.app) as client:
        login(client)
        target = client.post("/api/targets", json={
            "name": "上线闭环渠道", "base_url": "https://example.com/v1?secret=no",
            "model": "gpt-launch", "api_key": "sk-never-sync", "protocol": "openai",
            "upstream_multiplier": 0.7,
        }).json()
        channel_id = target["channel_id"]
        task_id = store.insert("tasks", {
            "kind": "admission", "target_id": target["id"], "target_name": target["name"],
            "pack_name": "准入", "pack_version": "test-v1", "status": "success",
            "snapshot": "{}", "progress": "{}", "report": "{}",
            "created_at": time.time(), "finished_at": time.time(),
        })
        store.update("targets", target["id"], {
            "last_task_id": task_id, "last_verdict": "推荐准入",
        })
        launch = client.get(f"/api/channels/{channel_id}/launch").json()
        draft_text = json.dumps(launch["config_draft"], ensure_ascii=False)
        check("准入报告后生成不含 Key 的人工上线配置草案",
              launch["status"] == "tested" and "sk-never-sync" not in draft_text
              and "不会写生产配置" in draft_text, launch)
        confirmed = client.post(
            f"/api/channels/{channel_id}/launch/confirm",
            json={"owner_note": "负责人：测试组"},
        ).json()
        channel = store.get("channels", channel_id)
        check("用户确认技术建议后渠道进入已批准而不写生产",
              confirmed["status"] == "confirmed"
              and channel["lifecycle_status"] == "approved", confirmed)

        source = client.post("/api/online-verification/sources", json={
            "name": "中转站只读上线接口", "endpoint": "https://example.com/online",
            "token": "read-only-token", "protocol_version": "1",
        }).json()
        original_client = external_sync.httpx.AsyncClient
        external_sync.httpx.AsyncClient = lambda **kwargs: FakeVerificationClient({
            "version": "1", "channel": {
                "business_id": channel["business_id"], "status": "online",
                "models": [{"canonical_model": "gpt-launch"}],
            },
        }, **kwargs)
        verified_response = client.post(
            f"/api/channels/{channel_id}/launch/verify?source_id={source['id']}"
        )
        external_sync.httpx.AsyncClient = original_client
        verified = verified_response.json()
        check("只有只读接口确认业务 ID 与模型真实在线后才进入已核验",
              verified_response.status_code == 200 and verified["status"] == "verified"
              and store.get("channels", channel_id)["lifecycle_status"] == "online_verified",
              verified_response.text)

        client.put("/api/external-sync/feishu/settings", json={
            "app_id": "cli-test", "app_secret": "secret-test",
            "base_token": "base-test", "channel_table_id": "tbl-channel",
            "model_table_id": "tbl-model", "enabled": True,
        })
        original_token = external_sync._feishu_token
        original_list = external_sync._list_records
        original_upsert = external_sync._upsert_record
        remote: dict[str, list[dict]] = {"tbl-channel": [], "tbl-model": []}

        async def fake_token(_: dict) -> str:
            return "tenant-token"

        async def fake_list(_: dict, table_id: str, __: str) -> list[dict]:
            return remote[table_id]

        async def fake_upsert(
            _: dict, table_id: str, __: str, business_id: str,
            id_field: str, fields: dict, existing: dict | None,
        ) -> str:
            record_id = (existing or {}).get("record_id") or f"rec-{len(remote[table_id]) + 1}"
            record = {"record_id": record_id, "fields": fields.copy()}
            remote[table_id] = [item for item in remote[table_id]
                                if item.get("record_id") != record_id] + [record]
            return record_id

        external_sync._feishu_token = fake_token
        external_sync._list_records = fake_list
        external_sync._upsert_record = fake_upsert
        preview = client.post(
            f"/api/channels/{channel_id}/external-sync/feishu/preview"
        ).json()
        desired_text = json.dumps(preview["desired"], ensure_ascii=False)
        check("飞书同步前展示渠道表与模型映射表字段差异",
              preview["status"] == "preview"
              and preview["diff"]["channel"]["changes"]
              and preview["diff"]["models"], preview["diff"])
        check("飞书同步载荷不含 Key、完整查询参数和原始响应",
              "sk-never-sync" not in desired_text and "?secret=" not in desired_text,
              desired_text)
        client.post(f"/api/external-sync/jobs/{preview['id']}/confirm")
        synced = client.post(f"/api/external-sync/jobs/{preview['id']}/run").json()
        check("用户确认后按业务 ID 幂等 Upsert 两张表并推进已同步状态",
              synced["status"] == "success" and len(remote["tbl-channel"]) == 1
              and len(remote["tbl-model"]) == 1
              and store.get("channels", channel_id)["lifecycle_status"] == "synced",
              synced)

        remote["tbl-channel"][0]["fields"]["渠道名称"] = "飞书手工修改名"
        conflict = client.post(
            f"/api/channels/{channel_id}/external-sync/feishu/preview"
        ).json()
        blocked = client.post(f"/api/external-sync/jobs/{conflict['id']}/confirm")
        check("发现飞书手工修改冲突时只显示差异且拒绝覆盖",
              conflict["status"] == "conflict" and conflict["diff"]["conflicts"]
              and blocked.status_code == 400, conflict)

        external_sync._feishu_token = original_token
        external_sync._list_records = original_list
        external_sync._upsert_record = original_upsert

    print("\n失败项：" + ("、".join(failed) if failed else "无"))


if __name__ == "__main__":
    main_test()
