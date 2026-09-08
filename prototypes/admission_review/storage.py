from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .mapping import build_outbox_payload


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS admission_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  channel_url TEXT NOT NULL,
  model TEXT NOT NULL,
  protocol TEXT NOT NULL,
  credential_supplied INTEGER NOT NULL,
  test_outcome TEXT NOT NULL DEFAULT '',
  test_summary_json TEXT NOT NULL DEFAULT '{}',
  review_decision TEXT NOT NULL DEFAULT '',
  review_note TEXT NOT NULL DEFAULT '',
  reviewer TEXT NOT NULL DEFAULT '',
  reviewed_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_framework_runs_created
  ON admission_runs(created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS feishu_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL UNIQUE,
  provider TEXT NOT NULL DEFAULT 'feishu',
  status TEXT NOT NULL,
  record_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  FOREIGN KEY(run_id) REFERENCES admission_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_framework_outbox_status
  ON feishu_outbox(status,created_at,id);
"""


class FlowError(ValueError):
    pass


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def init(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                self.db_path.parent.chmod(0o700)
            except OSError:
                pass
            self._connection = sqlite3.connect(self.db_path, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA secure_delete=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.executescript(SCHEMA)
            self._connection.commit()
            try:
                self.db_path.chmod(0o600)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        self.init()
        assert self._connection is not None
        with self._lock:
            current = self._connection.cursor()
            try:
                yield current
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            finally:
                current.close()

    @staticmethod
    def _run_out(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["credential_supplied"] = bool(result["credential_supplied"])
        result["test_summary"] = json.loads(result.pop("test_summary_json"))
        return result

    @staticmethod
    def _outbox_out(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def create_run(self, channel: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO admission_runs(status,channel_name,channel_url,model,protocol,"
                "credential_supplied,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "testing", channel["channel_name"], channel["base_url"],
                    channel["model"], channel["protocol"],
                    1 if channel["credential_supplied"] else 0, now, now,
                ),
            )
            run_id = int(cur.lastrowid)
            row = cur.execute("SELECT * FROM admission_runs WHERE id=?", (run_id,)).fetchone()
        result = self._run_out(row)
        assert result is not None
        return result

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self.cursor() as cur:
            row = cur.execute("SELECT * FROM admission_runs WHERE id=?", (run_id,)).fetchone()
        return self._run_out(row)

    def latest_run(self) -> dict[str, Any] | None:
        with self.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM admission_runs ORDER BY created_at DESC,id DESC LIMIT 1"
            ).fetchone()
        return self._run_out(row)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM admission_runs ORDER BY created_at DESC,id DESC LIMIT ?",
                (min(100, max(1, limit)),),
            ).fetchall()
        return [item for row in rows if (item := self._run_out(row)) is not None]

    def finish_test(
        self, run_id: int, outcome: str, summary: dict[str, Any],
    ) -> dict[str, Any]:
        encoded = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        now = time.time()
        with self.cursor() as cur:
            row = cur.execute("SELECT * FROM admission_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise FlowError("准入测试记录不存在")
            if row["status"] == "awaiting_review":
                if row["test_outcome"] == outcome and row["test_summary_json"] == encoded:
                    result = self._run_out(row)
                    assert result is not None
                    return result
                raise FlowError("该准入测试已经提交人工确认")
            if row["status"] != "testing":
                raise FlowError("当前状态不能提交测试结果")
            cur.execute(
                "UPDATE admission_runs SET status='awaiting_review',test_outcome=?,"
                "test_summary_json=?,updated_at=? WHERE id=?",
                (outcome, encoded, now, run_id),
            )
            updated = cur.execute("SELECT * FROM admission_runs WHERE id=?", (run_id,)).fetchone()
        result = self._run_out(updated)
        assert result is not None
        return result

    def review(
        self, run_id: int, decision: str, reviewer: str, note: str,
    ) -> dict[str, Any]:
        now = time.time()
        with self.cursor() as cur:
            row = cur.execute("SELECT * FROM admission_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise FlowError("准入测试记录不存在")
            existing = self._run_out(row)
            assert existing is not None
            if row["status"] == "reviewed":
                if row["review_decision"] == decision:
                    return existing
                raise FlowError("该准入测试已经完成人工判定")
            if row["status"] != "awaiting_review":
                raise FlowError("必须先完成测试，才能进行人工判定")
            reviewed = {
                **existing,
                "status": "reviewed",
                "review_decision": decision,
                "reviewer": reviewer,
                "review_note": note,
                "reviewed_at": now,
                "updated_at": now,
            }
            payload = build_outbox_payload(reviewed)
            outbox_status = (
                "pending" if payload["mapping_status"] == "ready"
                else "awaiting_field_mapping"
            )
            cur.execute(
                "UPDATE admission_runs SET status='reviewed',review_decision=?,reviewer=?,"
                "review_note=?,reviewed_at=?,updated_at=? WHERE id=?",
                (decision, reviewer, note, now, now, run_id),
            )
            cur.execute(
                "INSERT INTO feishu_outbox(run_id,status,record_key,payload_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    run_id, outbox_status, payload["record_key"],
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")), now, now,
                ),
            )
            updated = cur.execute("SELECT * FROM admission_runs WHERE id=?", (run_id,)).fetchone()
        result = self._run_out(updated)
        assert result is not None
        return result

    def list_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM feishu_outbox ORDER BY created_at DESC,id DESC LIMIT ?",
                (min(200, max(1, limit)),),
            ).fetchall()
        return [item for row in rows if (item := self._outbox_out(row)) is not None]
