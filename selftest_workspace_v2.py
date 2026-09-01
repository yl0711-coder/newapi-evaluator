"""模型倍率工作台 V2 的隔离自测。"""
import asyncio
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("TEST_DB_NAME", "selftest.db")
os.environ.setdefault("TEST_BOOTSTRAP_USERNAME", "selftest-admin")
os.environ.setdefault("TEST_BOOTSTRAP_PASSWORD", "selftest-password-2026")
os.environ.setdefault("TEST_COOKIE_SECURE", "0")
os.environ.setdefault("TEST_EGRESS_ALLOWLIST", "example.com")

from fastapi.testclient import TestClient  # noqa: E402

from app import itembank, main, runner, scheduler, store  # noqa: E402
from selftest_session import login  # noqa: E402


failed: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        failed.append(name)


async def fake_submit(task_id: int) -> None:
    store.update("tasks", task_id, {"status": "queued"})


def capability_report(score: float = 0.9) -> dict:
    dims = {name: {
        "score": score, "weight": itembank.DIMENSIONS[name]["weight"],
        "coverage": 1.0, "graded": 3, "ungraded": 0,
    } for name in itembank.DIM_ORDER}
    return {
        "metrics": {
            "capability": {
                "pack_version": itembank.PACK_VERSION,
                "dims": dims, "items": {}, "overall": score,
                "coverage_enforced": True, "truncated_unscored": 0,
            },
            "gates": {"passed": True, "failed": []},
            "trust": {"ok": True, "reasons": []},
        },
        "conclusion": {"verdict": "推荐准入", "code": "recommend"},
    }


def main_test() -> None:
    original_submit = runner.submit
    with TestClient(main.app) as client:
        login(client)
        runner.submit = fake_submit
        workspace_source = Path("web/workspace.js").read_text(encoding="utf-8")
        check("精确连接配置行常驻显示详情入口",
              'data-configuration-detail="${configuration.id}"' in workspace_source,
              "配置详情入口未出现在当前工作台列表中")
        families = client.get("/api/model-families").json()
        claude = next(family for family in families if family["name"] == "Claude")
        codex = next(family for family in families if family["name"] == "Codex")
        check("预置 Claude 三模型包", len([m for m in claude["models"] if m["enabled"]]) == 3)
        check("预置 Codex 两模型包", len([m for m in codex["models"] if m["enabled"]]) == 2)

        claude_group = client.post("/api/platform-groups", json={
            "family_id": claude["id"], "online_multiplier": 1.2}).json()
        codex_group = client.post("/api/platform-groups", json={
            "family_id": codex["id"], "online_multiplier": 1.0}).json()
        check("平台组为每个实际模型建立标杆槽位", len(claude_group["models"]) == 3)

        channel = client.post("/api/channels", json={
            "name": "同名渠道", "base_url": "https://example.com/v1",
            "api_key": "sk-workspace-v2", "protocol": "openai",
        }).json()
        admission = client.post(
            f"/api/channels/{channel['id']}/admission-tasks", json={
                "platform_group_id": claude_group["id"],
                "upstream_multiplier": 0.7,
            }).json()
        targets = [store.get("targets", target_id) for target_id in admission["target_ids"]]
        check("一次准入按 Claude 模型包创建三个任务", len(admission["task_ids"]) == 3)
        check("所有模型归入唯一平台组", all(
            target and target["platform_group_id"] == claude_group["id"]
            for target in targets), targets)
        check("上游倍率一次应用到完整模型包", all(
            target and target["upstream_multiplier"] == 0.7 for target in targets), targets)
        mismatch = client.post(
            f"/api/channels/{channel['id']}/admission-tasks", json={
                "platform_group_id": codex_group["id"],
                "upstream_multiplier": 0.5,
            })
        check("同一个 Key 禁止跨模型家族复用", mismatch.status_code == 409, mismatch.text)

        target = targets[0]
        assert target is not None
        task_id = scheduler.create_task_row("capability", target, include_hard=False)
        report = capability_report()
        store.update("tasks", task_id, {
            "status": "success", "report": store.dumps(report),
            "finished_at": time.time()})
        candidates = client.get(
            f"/api/targets/{target['id']}/benchmark-candidates").json()
        check("标杆弹窗默认候选只含完整有效结果",
              candidates and candidates[0]["task_id"] == task_id, candidates)
        slot = next(model for model in claude_group["models"]
                    if model["model"] == target["model"])
        bound = client.put(
            f"/api/platform-groups/{claude_group['id']}/benchmark-slots/{slot['id']}",
            json={"source_task_id": task_id, "tolerance": 0.1})
        check("逐实际模型设置平台标杆", bound.status_code == 200, bound.text)

        runner._make_recommendation(target, store.get("tasks", task_id), report)
        recommendation = store.query(
            "SELECT * FROM recommendations WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,))[0]
        result = store.loads(recommendation["result"], {})
        check("只有一个有效标杆时只比较意向组", len(result["comparisons"]) == 1
              and result["comparisons"][0]["group_id"] == claude_group["id"], result)

        alternate_group = client.post("/api/platform-groups", json={
            "family_id": claude["id"], "online_multiplier": 0.8}).json()
        weak_dims = {name: 0.65 for name in itembank.DIM_ORDER}
        weak_benchmark = store.insert("benchmarks", {
            "name": "Fable 低档技术标杆", "pack_version": itembank.PACK_VERSION,
            "model_hint": target["model"], "benchmark_model": target["model"],
            "dims": store.dumps(weak_dims), "items": "{}", "overall": 0.65,
            "tolerance": 0.1, "source": "manual", "created_at": time.time(),
            "updated_at": time.time(), "platform_group_id": alternate_group["id"],
        })
        alternate_slot = next(
            model for model in alternate_group["models"]
            if model["model"] == target["model"])
        store.update("platform_group_benchmarks", alternate_slot["id"], {
            "benchmark_id": weak_benchmark, "updated_at": time.time(),
        })
        lower_report = capability_report(0.7)
        lower_task_id = scheduler.create_task_row("capability", target, include_hard=False)
        store.update("tasks", lower_task_id, {
            "status": "success", "report": store.dumps(lower_report),
            "finished_at": time.time(),
        })
        store.update("targets", target["id"], {"upstream_multiplier": 9.9})
        runner._make_recommendation(
            store.get("targets", target["id"]), store.get("tasks", lower_task_id),
            lower_report,
        )
        alternate_recommendation = store.query(
            "SELECT * FROM recommendations WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (lower_task_id,),
        )[0]
        alternate_result = store.loads(alternate_recommendation["result"], {})
        check("同一次同版本证据可建议更合适的技术组",
              alternate_result["status"] == "alternate_group"
              and alternate_result["suggested_group_id"] == alternate_group["id"]
              and len(alternate_result["comparisons"]) == 2,
              alternate_result)
        decision = client.post(
            f"/api/recommendations/{alternate_recommendation['id']}/decide",
            json={"accept": True, "group_id": alternate_group["id"],
                  "note": "人工复核同意"},
        )
        decided_target = store.get("targets", target["id"])
        check("技术建议经人工确认后才改变归属且不看采购倍率",
              decision.status_code == 200
              and decided_target["platform_group_id"] == alternate_group["id"]
              and "不参与技术推荐" in alternate_result["commercial_boundary"],
              {"decision": decision.text, "target": decided_target})
        store.update("targets", target["id"], {
            "platform_group_id": claude_group["id"], "upstream_multiplier": 0.7,
        })

        workspace = client.get("/api/workspace").json()
        visible = next(group for group in workspace["platform_groups"]
                       if group["id"] == claude_group["id"])
        check("工作台按平台组和渠道上游倍率分层", len(visible["channel_groups"]) == 1
              and visible["channel_groups"][0]["upstream_multiplier"] == 0.7, visible)

        new_model = client.post(f"/api/model-families/{claude['id']}/models", json={
            "model": "claude-nova-5", "display_name": "Nova 5", "sort_order": 40,
        }).json()
        check("模型包允许新增模型", new_model["model"] == "claude-nova-5")
        workspace = client.get("/api/workspace").json()
        visible = next(group for group in workspace["platform_groups"]
                       if group["id"] == claude_group["id"])
        check("已有渠道显示新增模型待补测",
              "claude-nova-5" in visible["channel_groups"][0]["pending_models"], visible)
        supplement = client.post(
            f"/api/channels/{channel['id']}/platform-groups/{claude_group['id']}/supplement"
        ).json()
        check("一键补测只创建缺失模型", len(supplement["task_ids"]) == 1, supplement)

        client.put(f"/api/targets/{target['id']}/scheduled-test", json={"enabled": True})
        schedule_id = store.insert("scheduled_tests", {
            "name": "V2 定时", "report_minute": 540,
            "feishu_webhook_ids": "[]", "email_recipient_ids": "[]",
            "enabled": 1, "created_at": time.time(), "updated_at": time.time(),
        })
        schedule = store.get("scheduled_tests", schedule_id)
        asyncio.run(scheduler._ensure_run(schedule, datetime.now() + timedelta(minutes=30)))
        scheduled_task = store.query(
            "SELECT * FROM tasks WHERE kind='inspect' AND target_id=? ORDER BY id DESC LIMIT 1",
            (target["id"],))[0]
        scheduled_snapshot = store.loads(scheduled_task["snapshot"], {})
        check("定时报告直接使用平台组",
              scheduled_snapshot["scheduled_report_groups"] == [{
                  "model_family": "Claude", "online_multiplier": 1.2}],
              scheduled_snapshot)

        unbound = client.delete(
            f"/api/platform-groups/{claude_group['id']}/benchmark-slots/{slot['id']}")
        check("取消标杆保留历史并解绑当前槽位", unbound.status_code == 200
              and client.get(
                  f"/api/platform-groups/{claude_group['id']}/benchmark-slots/{slot['id']}/history"
              ).json(), unbound.text)

        legacy_channel = client.post("/api/channels", json={
            "name": "迁移渠道", "base_url": "https://example.com/legacy/v1",
            "api_key": "sk-legacy-workspace", "protocol": "anthropic",
        }).json()
        legacy_target = client.post(
            f"/api/channels/{legacy_channel['id']}/models", json={
                "name": "迁移渠道 · Fable 5", "model": "claude-fable-5",
            }).json()
        legacy_report_task = store.insert("tasks", {
            "kind": "capability", "target_id": legacy_target["id"],
            "target_name": legacy_target["name"], "status": "success",
            "pack_name": "迁移题库", "pack_version": itembank.PACK_VERSION,
            "snapshot": "{}", "progress": "{}",
            "report": store.dumps(capability_report()),
            "created_at": time.time(), "finished_at": time.time(),
        })
        legacy_benchmark = store.insert("benchmarks", {
            "name": "Fable 迁移标杆", "pack_version": itembank.PACK_VERSION,
            "model_hint": "claude-fable-5", "dims": "{}", "items": "{}",
            "overall": 0.9, "tolerance": 0.1, "source": "task",
            "source_task_id": legacy_report_task, "created_at": time.time(),
            "updated_at": time.time(),
        })
        unmatched_benchmark = store.insert("benchmarks", {
            "name": "无来源旧标杆", "pack_version": itembank.PACK_VERSION,
            "model_hint": "claude-fable-5", "dims": "{}", "items": "{}",
            "overall": 0.8, "tolerance": 0.1, "source": "manual",
            "created_at": time.time(), "updated_at": time.time(),
        })
        legacy_group = store.insert("groups", {
            "name": "claude-fable-5-1.8x组", "multiplier": 1.8,
            "benchmark_id": legacy_benchmark, "created_at": time.time(),
            "updated_at": time.time(),
        })
        unmatched_group = store.insert("groups", {
            "name": "claude-fable-5-1.7x组", "multiplier": 1.7,
            "benchmark_id": unmatched_benchmark, "created_at": time.time(),
            "updated_at": time.time(),
        })
        legacy_scheduled_group = store.insert("scheduled_report_groups", {
            "model_family": "Claude", "multiplier": 1.8,
            "created_at": time.time(), "updated_at": time.time(),
        })
        store.insert("scheduled_report_group_targets", {
            "group_id": legacy_scheduled_group, "target_id": legacy_target["id"],
            "created_at": time.time(),
        })
        counts_before = {
            "tasks": len(store.query("SELECT id FROM tasks")),
            "benchmarks": len(store.query("SELECT id FROM benchmarks")),
        }
        store.execute("DELETE FROM schema_meta WHERE key='platform_workspace_version'")
        store._migrate_platform_workspace()
        store.execute("UPDATE schema_meta SET value=value WHERE key='platform_workspace_version'")
        migrated_target = store.get("targets", legacy_target["id"])
        migrated_benchmark = store.get("benchmarks", legacy_benchmark)
        unmatched = store.get("benchmarks", unmatched_benchmark)
        check("迁移只绑定家族模型倍率均可由来源任务证明的旧标杆",
              migrated_target and migrated_target["platform_group_id"]
              and migrated_benchmark and migrated_benchmark["platform_group_id"]
              == migrated_target["platform_group_id"]
              and unmatched and unmatched["platform_group_id"] is None,
              {"matched_group": legacy_group, "unmatched_group": unmatched_group,
               "matched": migrated_benchmark, "unmatched": unmatched})
        check("迁移不丢历史任务和未匹配标杆", counts_before == {
            "tasks": len(store.query("SELECT id FROM tasks")),
            "benchmarks": len(store.query("SELECT id FROM benchmarks")),
        }, counts_before)

        regroup_destination = client.post("/api/platform-groups", json={
            "family_id": claude["id"], "online_multiplier": 1.6,
        }).json()
        report_before_regroup = store.get("tasks", task_id)
        regrouped = client.put(
            f"/api/targets/{target['id']}/platform-group", json={
                "platform_group_id": regroup_destination["id"],
            })
        regrouped_workspace = client.get("/api/workspace").json()
        destination_view = next(
            group for group in regrouped_workspace["platform_groups"]
            if group["id"] == regroup_destination["id"])
        source_view = next(
            group for group in regrouped_workspace["platform_groups"]
            if group["id"] == claude_group["id"])
        destination_ids = {
            model["id"] for channel_group in destination_view["channel_groups"]
            for model in channel_group["models"]
        }
        source_ids = {
            model["id"] for channel_group in source_view["channel_groups"]
            for model in channel_group["models"]
        }
        check("已有测试记录可移动到同家族其他平台倍率组",
              regrouped.status_code == 200 and target["id"] in destination_ids
              and target["id"] not in source_ids, regrouped.text)
        check("改组保留历史报告并让后续测试使用新归属",
              store.get("tasks", task_id) == report_before_regroup
              and regrouped.json()["platform_group_id"] == regroup_destination["id"],
              regrouped.json())
    runner.submit = original_submit


if __name__ == "__main__":
    main_test()
    print("\n失败项：" + ("无" if not failed else str(failed)))
    raise SystemExit(1 if failed else 0)
