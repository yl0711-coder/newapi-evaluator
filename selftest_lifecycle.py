"""模型别名、生命周期和历史归档语义自测。"""
import os

os.environ.setdefault("TEST_DB_NAME", "selftest.db")
os.environ.setdefault("TEST_BOOTSTRAP_USERNAME", "selftest-admin")
os.environ.setdefault("TEST_BOOTSTRAP_PASSWORD", "selftest-password-2026")
os.environ.setdefault("TEST_COOKIE_SECURE", "0")
os.environ.setdefault("TEST_EGRESS_ALLOWLIST", "example.com")

from fastapi.testclient import TestClient  # noqa: E402

from app import lifecycle, main, store  # noqa: E402
from selftest_session import login  # noqa: E402

failed: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        failed.append(name)


def main_test() -> None:
    with TestClient(main.app) as client:
        login(client)
        family = next(item for item in client.get("/api/model-families").json()
                      if item["name"] == "Codex")
        created = client.post(f"/api/model-families/{family['id']}/models", json={
            "model": "GPT-LIFECYCLE-TEST", "display_name": "生命周期测试",
        })
        check("新模型 ID 被规范化且从实验状态开始",
              created.status_code == 200
              and created.json()["model"] == "gpt-lifecycle-test"
              and created.json()["lifecycle_status"] == "experimental", created.text)
        model = created.json()

        alias = client.post(f"/api/model-families/{family['id']}/aliases", json={
            "model_id": model["id"], "alias": "vendor-gpt-lifecycle",
        })
        check("渠道别名可创建并指向规范模型",
              alias.status_code == 200
              and alias.json()["canonical_model"] == "gpt-lifecycle-test", alias.text)
        matched = lifecycle.match_models(family["id"], ["vendor-gpt-lifecycle"])
        check("精确别名自动匹配并标明匹配类型",
              matched["matched"][0]["match_type"] == "alias"
              and matched["matched"][0]["confirmed"], matched)
        fuzzy = lifecycle.match_models(family["id"], ["vendor-gpt-lifecycl"])
        check("模糊匹配只给未确认候选",
              fuzzy["fuzzy_candidates"]
              and not fuzzy["fuzzy_candidates"][0]["confirmed"], fuzzy)

        enabled = client.post(
            f"/api/model-families/{family['id']}/models/{model['id']}/lifecycle",
            json={"to_status": "enabled", "reason": "实验验证通过"},
        )
        replacement = family["models"][0]
        retired = client.post(
            f"/api/model-families/{family['id']}/models/{model['id']}/lifecycle",
            json={"to_status": "retired", "reason": "已由新版本替代",
                  "replacement_model_id": replacement["id"]},
        )
        check("退役模型记录同家族替代模型并退出新模型包",
              enabled.status_code == 200 and retired.status_code == 200
              and retired.json()["replacement_model_id"] == replacement["id"]
              and not retired.json()["enabled"], retired.text)
        history = client.get(
            f"/api/lifecycle/history?object_type=model&object_id={model['id']}"
        ).json()
        check("生命周期迁移保留原因和操作人",
              len(history) == 2 and history[0]["reason"] == "已由新版本替代"
              and history[0]["actor"] == "selftest-admin", history)

        channel = client.post("/api/channels", json={
            "name": "归档测试渠道", "base_url": "https://example.com/v1",
            "api_key": "sk-lifecycle", "protocol": "openai",
        }).json()
        store.insert("targets", {
            "channel_id": channel["id"], "name": "历史模型", "protocol": "openai",
            "base_url": "https://example.com/v1", "model": "gpt-history",
            "key_enc": "", "source": "manual", "edited_fields": "[]",
            "recorded": 1, "status": "pending", "created_at": 1, "updated_at": 1,
        })
        archived = client.delete(f"/api/channels/{channel['id']}")
        stored = store.get("channels", channel["id"])
        check("有历史对象的渠道只能归档而不物理删除",
              archived.status_code == 200 and archived.json()["status"] == "archived"
              and stored is not None and stored["lifecycle_status"] == "archived",
              archived.text)
        check("归档渠道从默认运营列表隐藏",
              all(item["id"] != channel["id"] for item in client.get("/api/channels").json()))

    print("\n失败项：" + ("、".join(failed) if failed else "无"))


if __name__ == "__main__":
    main_test()
