"""专项推荐、计划快照和基础准入隔离自测。"""
import os
import time

os.environ.setdefault("TEST_DB_NAME", "selftest.db")
os.environ.setdefault("TEST_BOOTSTRAP_USERNAME", "selftest-admin")
os.environ.setdefault("TEST_BOOTSTRAP_PASSWORD", "selftest-password-2026")
os.environ.setdefault("TEST_COOKIE_SECURE", "0")
os.environ.setdefault("TEST_EGRESS_ALLOWLIST", "example.com")

from fastapi.testclient import TestClient  # noqa: E402

from app import main, report, runner, specialty, store  # noqa: E402
from selftest_session import login  # noqa: E402

failed: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        failed.append(name)


async def fake_submit(task_id: int) -> None:
    store.update("tasks", task_id, {"status": "queued"})


def step(name: str, ok: bool, extra: dict) -> dict:
    return {"step": name, "ok": ok, "reason": "" if ok else "响应不符合预期",
            "detail": "通过" if ok else "专项答案未达到标准", "latency": .2,
            "first_token": .05, "usage": {"prompt": 10, "completion": 20},
            "extra": extra}


def main_test() -> None:
    original_submit = runner.submit
    with TestClient(main.app) as client:
        login(client)
        runner.submit = fake_submit
        family = next(item for item in client.get("/api/model-families").json()
                      if item["name"] == "Codex")
        group = client.post("/api/platform-groups", json={
            "family_id": family["id"], "online_multiplier": 1.0,
        }).json()
        now = time.time()
        store.insert("supply_gap_recommendations", {
            "week_start": int(now) // 604800 * 604800,
            "platform_group": group["label"], "model": "gpt-5.6-sol",
            "usage_profile": "agent", "demand_level": "high",
            "request_count": 1000, "active_users": 80,
            "required_channels": 3, "qualified_channels": 1,
            "concentration": .8, "status": "candidate",
            "reasons_json": "[]", "suspected_fault_domains_json": "[]",
            "created_at": now, "updated_at": now,
        })
        preview = client.get(
            f"/api/admission-plan-preview?platform_group_id={group['id']}").json()
        check("基础测试不再自动预选专项", preview["recommended_specialties"] == [],
              preview)
        check("计划预览动态返回请求、费用、时间和版本",
              preview["estimate"]["requests"] > 0
              and preview["estimate"]["estimated_minutes"] > 0
              and preview["estimate"]["specialty_versions"] == {}, preview["estimate"])

        channel = client.post("/api/channels", json={
            "name": "专项渠道", "base_url": "https://example.com/v1",
            "api_key": "sk-specialty-test", "protocol": "openai",
        }).json()
        admission = client.post(
            f"/api/channels/{channel['id']}/admission-tasks", json={
                "platform_group_id": group["id"], "upstream_multiplier": .7,
                "specialty_profiles": [],
            }).json()
        plan = client.get(
            f"/api/test-plan-snapshots/{admission['test_plan_snapshot_id']}").json()
        check("计划快照固定不混入专项",
              plan["recommended_specialties"] == []
              and plan["selected_specialties"] == [], plan)
        task = store.get("tasks", admission["task_ids"][0])
        snapshot = store.loads(task["snapshot"], {})
        check("提交后保存不可变测试计划引用",
              snapshot["test_plan_snapshot_id"] == plan["id"]
              and snapshot["recommended_specialty_profiles"] == [], snapshot)

        basic = step("基础连通", True, {"reply": "ok"})
        specialty_failures = [step(f"专项·agent·{item['id']}", False, {
            "item": item["id"], "scored": 0.0, "graded": True,
            "specialty_profile": "agent", "dim": "专项·agent",
        }) for item in specialty.PACKS["agent"]["items"]]
        pack = {"name": "隔离测试", "version": "1", "required": ["基础连通"]}
        built = report.build(
            {"id": 1, "kind": "admission", "created_at": now}, pack,
            [basic, *specialty_failures], {
                "model": "gpt-test", "protocol": "openai",
                "specialty_profiles": ["agent"],
                "recommended_specialty_profiles": ["agent"],
            },
        )
        check("专项失败不否定基础准入结论",
              built["conclusion"]["code"] == "recommend"
              and built["metrics"]["pass_rate"] == 1.0, built["conclusion"])
        check("专项独立生成用途适配标签与证据",
              built["metrics"]["specialties"]["agent"]["status"] == "not_suitable"
              and built["metrics"]["specialties"]["agent"]["items"],
              built["metrics"]["specialties"])
    runner.submit = original_submit
    print("\n失败项：" + ("、".join(failed) if failed else "无"))


if __name__ == "__main__":
    main_test()
