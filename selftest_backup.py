"""双库加密备份、幂等执行与非覆盖式恢复演练自测。"""
import os
from pathlib import Path

os.environ.setdefault("TEST_DB_NAME", "selftest.db")
os.environ.setdefault("TEST_METRIC_DB_NAME", "metrics-selftest.db")
os.environ.setdefault("TEST_BACKUP_DIR", str(Path("data/selftest-backups").resolve()))
os.environ.setdefault("TEST_BACKUP_KEY_PATH", str(Path("data/selftest-backup.key").resolve()))
os.environ.setdefault("TEST_BOOTSTRAP_USERNAME", "selftest-admin")
os.environ.setdefault("TEST_BOOTSTRAP_PASSWORD", "selftest-password-2026")
os.environ.setdefault("TEST_COOKIE_SECURE", "0")

from fastapi.testclient import TestClient  # noqa: E402

from app import backups, main, metric_store, store  # noqa: E402
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
        metric_store.insert("metric_sources", {
            "name": "备份指标源", "endpoint": "https://example.com/metrics",
            "protocol_version": "1", "created_at": 1.0, "updated_at": 1.0,
        })
        old_task_id = store.insert("tasks", {
            "kind": "inspect", "target_name": "历史报告", "status": "success",
            "snapshot": "{}", "progress": "{}",
            "report": store.dumps({"conclusion": {"verdict": "正常", "code": "recommend"},
                                   "metrics": {}}),
            "created_at": 1.0, "finished_at": 1.0,
        })
        first = backups.run_backup("2099-01-01")
        second = backups.run_backup("2099-01-01")
        path = Path(first["backup_path"])
        check("业务库与指标库合并为单个加密备份",
              path.exists() and path.suffix == ".enc"
              and set(first["manifest"]["databases"]) == {"business.db", "metrics.db"}
              and path.read_bytes()[:16] != b"SQLite format 3\x00", first)
        check("同一 UTC 日期备份幂等不重复生成", first["id"] == second["id"], second)
        archived_task = client.get(f"/api/tasks/{old_task_id}").json()
        check("旧报告压缩后仍可打开且不改写历史结论",
              store.get("tasks", old_task_id)["report"] == ""
              and archived_task["report"]["conclusion"]["verdict"] == "正常",
              archived_task)
        drill = backups.restore_drill(str(path))
        check("恢复演练校验双库哈希与 SQLite 完整性", drill["status"] == "success"
              and all(row["sha256_match"] and row["integrity"] == "ok"
                      for row in drill["checks"].values()), drill)
        status = client.get("/api/backups/status").json()
        check("备份页面显示双库容量与异地路径状态",
              {row["database"] for row in status["databases"]}
              == {"business", "metrics"}
              and status["offsite_configured"] is False, status)


if __name__ == "__main__":
    main_test()
    print("\n失败项：" + ("无" if not failed else str(failed)))
    raise SystemExit(1 if failed else 0)
