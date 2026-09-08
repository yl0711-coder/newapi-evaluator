from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS admission_participations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_name TEXT NOT NULL,
  test_group TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_participations_created
  ON admission_participations(created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS feishu_participation_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  participation_id INTEGER NOT NULL UNIQUE,
  status TEXT NOT NULL,
  fields_json TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  feishu_record_id TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  FOREIGN KEY(participation_id) REFERENCES admission_participations(id)
);
CREATE INDEX IF NOT EXISTS idx_participation_outbox_status
  ON feishu_participation_outbox(status,created_at,id);
"""


class StoreError(ValueError):
    pass


class StoreConflict(StoreError):
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
            self._connection.execute(
                "UPDATE feishu_participation_outbox SET status='failed',"
                "last_error='上次写入未完成，可安全重试',updated_at=? WHERE status='sending'",
                (time.time(),),
            )
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
    def _entry_out(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["fields"] = json.loads(result.pop("fields_json"))
        return result

    @staticmethod
    def _select_sql() -> str:
        return (
            "SELECT p.id,p.channel_name,p.test_group,p.created_at,p.updated_at,"
            "o.id AS outbox_id,o.status AS sync_status,o.fields_json,o.attempts,"
            "o.last_error,o.feishu_record_id "
            "FROM admission_participations p "
            "JOIN feishu_participation_outbox o ON o.participation_id=p.id"
        )

    def create(
        self,
        channel_name: str,
        test_group: str,
        fields: dict[str, str],
        *,
        delivery_configured: bool,
    ) -> dict[str, Any]:
        now = time.time()
        encoded = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        status = "pending" if delivery_configured else "not_configured"
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO admission_participations(channel_name,test_group,created_at,updated_at) "
                "VALUES(?,?,?,?)",
                (channel_name, test_group, now, now),
            )
            participation_id = int(cur.lastrowid)
            cur.execute(
                "INSERT INTO feishu_participation_outbox(participation_id,status,fields_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?)",
                (participation_id, status, encoded, now, now),
            )
            row = cur.execute(
                f"{self._select_sql()} WHERE p.id=?", (participation_id,)
            ).fetchone()
        result = self._entry_out(row)
        assert result is not None
        return result

    def get(self, participation_id: int) -> dict[str, Any] | None:
        with self.cursor() as cur:
            row = cur.execute(
                f"{self._select_sql()} WHERE p.id=?", (participation_id,)
            ).fetchone()
        return self._entry_out(row)

    def latest(self) -> dict[str, Any] | None:
        with self.cursor() as cur:
            row = cur.execute(
                f"{self._select_sql()} ORDER BY p.created_at DESC,p.id DESC LIMIT 1"
            ).fetchone()
        return self._entry_out(row)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            rows = cur.execute(
                f"{self._select_sql()} ORDER BY p.created_at DESC,p.id DESC LIMIT ?",
                (min(200, max(1, limit)),),
            ).fetchall()
        return [item for row in rows if (item := self._entry_out(row)) is not None]

    def begin_delivery(self, participation_id: int) -> dict[str, Any]:
        now = time.time()
        with self.cursor() as cur:
            row = cur.execute(
                "SELECT status FROM feishu_participation_outbox WHERE participation_id=?",
                (participation_id,),
            ).fetchone()
            if row is None:
                raise StoreError("准入参与记录不存在")
            if row["status"] == "synced":
                existing = self.get(participation_id)
                assert existing is not None
                return existing
            if row["status"] == "sending":
                raise StoreConflict("该记录正在写入飞书，请勿重复提交")
            cur.execute(
                "UPDATE feishu_participation_outbox SET status='sending',attempts=attempts+1,"
                "last_error='',updated_at=? WHERE participation_id=?",
                (now, participation_id),
            )
            updated = cur.execute(
                f"{self._select_sql()} WHERE p.id=?", (participation_id,)
            ).fetchone()
        result = self._entry_out(updated)
        assert result is not None
        return result

    def mark_synced(self, participation_id: int, record_id: str) -> dict[str, Any]:
        now = time.time()
        with self.cursor() as cur:
            cur.execute(
                "UPDATE feishu_participation_outbox SET status='synced',feishu_record_id=?,"
                "last_error='',updated_at=? WHERE participation_id=?",
                (record_id, now, participation_id),
            )
            if cur.rowcount != 1:
                raise StoreError("准入参与记录不存在")
        result = self.get(participation_id)
        assert result is not None
        return result

    def mark_failed(self, participation_id: int, message: str) -> dict[str, Any]:
        now = time.time()
        with self.cursor() as cur:
            cur.execute(
                "UPDATE feishu_participation_outbox SET status='failed',last_error=?,updated_at=? "
                "WHERE participation_id=?",
                (message[:300], now, participation_id),
            )
            if cur.rowcount != 1:
                raise StoreError("准入参与记录不存在")
        result = self.get(participation_id)
        assert result is not None
        return result
