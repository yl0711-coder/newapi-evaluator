"""持久任务租约、退避重试、失败队列和后台健康告警自测。"""
import asyncio
import os
import time

os.environ.setdefault("TEST_DB_NAME", "selftest.db")
os.environ.setdefault("TEST_BOOTSTRAP_USERNAME", "selftest-admin")
os.environ.setdefault("TEST_BOOTSTRAP_PASSWORD", "selftest-password-2026")
os.environ.setdefault("TEST_COOKIE_SECURE", "0")

from fastapi.testclient import TestClient  # noqa: E402

from app import main, runner, store  # noqa: E402
from selftest_session import login  # noqa: E402

failed: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        failed.append(name)


def task_row(status: str = "running", attempt_count: int = 0) -> int:
    return store.insert("tasks", {
        "kind": "inspect", "target_name": "可靠性自测", "status": status,
        "snapshot": "{}", "progress": "{}", "attempt_count": attempt_count,
        "max_attempts": 3, "created_at": time.time(),
    })


async def exercise_failures() -> tuple[dict, dict]:
    retry_id = task_row()
    runner._handle_executor_failure(retry_id, RuntimeError("temporary failure"))
    retry = store.get("tasks", retry_id)
    dead_id = task_row(attempt_count=2)
    runner._handle_executor_failure(dead_id, RuntimeError("final failure"))
    dead = store.get("tasks", dead_id)
    for timer in tuple(runner._retry_timers):
        timer.cancel()
    await asyncio.gather(*tuple(runner._retry_timers), return_exceptions=True)
    runner._retry_timers.clear()
    return retry, dead


def main_test() -> None:
    with TestClient(main.app) as client:
        login(client)
        leased_id = task_row("queued")
        store.insert("job_leases", {
            "task_id": leased_id, "owner_id": "another-worker",
            "lease_until": time.time() + 120, "heartbeat_at": time.time(),
            "acquired_at": time.time(),
        })
        check("有效租约阻止第二个 Worker 重复领取",
              runner._claim_lease(leased_id) is False)
        store.execute("UPDATE job_leases SET lease_until=? WHERE task_id=?",
                      (time.time() - 1, leased_id))
        check("失联 Worker 租约到期后可被回收",
              runner._claim_lease(leased_id) is True)
        runner._release_lease(leased_id)

        retry, dead = asyncio.run(exercise_failures())
        check("执行器暂时异常按指数退避进入持久排队",
              retry["status"] == "queued" and retry["attempt_count"] == 1
              and retry["next_attempt_at"] > time.time(), retry)
        check("达到最大次数后进入失败队列",
              dead["status"] == "failed" and dead["dead_lettered_at"]
              and dead["attempt_count"] == 3, dead)
        health = client.get("/api/health").json()
        check("失败队列生成可见的后台健康告警",
              health["status"] == "degraded" and health["dead_letter_tasks"] == 1
              and any(row["kind"] == "task_dead_letter" for row in health["alerts"]),
              health)
        alerts = client.get("/api/system-alerts").json()
        resolved = client.post(f"/api/system-alerts/{alerts[0]['id']}/resolve")
        check("后台告警可以人工确认关闭", resolved.status_code == 200
              and resolved.json()["status"] == "resolved", resolved.text)
        audit_rows = client.get("/api/audit-events", params={
            "object_type": "system-alerts", "action": "http.post",
        }).json()
        check("变更审计保存脱敏的前后摘要",
              audit_rows and audit_rows[0]["detail"]["before"]["status"] == "open"
              and audit_rows[0]["detail"]["after"]["status"] == "resolved",
              audit_rows[:1])
        metadata = client.patch(f"/api/tasks/{dead['id']}/metadata", json={
            "owner": "值班甲", "tags": ["批次-A", "需复盘"],
            "review_status": "in_review", "note": "等待供应商回复",
        })
        filtered = client.get("/api/tasks", params={
            "owner": "值班甲", "tag": "需复盘", "review_status": "in_review",
        }).json()
        check("任务支持负责人、标签、备注、审查状态和组合搜索",
              metadata.status_code == 200 and filtered
              and filtered[0]["id"] == dead["id"]
              and filtered[0]["operator_note"] == "等待供应商回复", filtered)


if __name__ == "__main__":
    main_test()
    print("\n失败项：" + ("无" if not failed else str(failed)))
    raise SystemExit(1 if failed else 0)
