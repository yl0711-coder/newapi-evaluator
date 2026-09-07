from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from shared.config import DATA_DIR


REPORT_DIR = DATA_DIR / "admission"
DB_PATH = REPORT_DIR / "reports.db"
MAX_REPORTS = 30
MAX_REPORT_BYTES = 4 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at REAL NOT NULL,
  reported_at TEXT NOT NULL,
  status TEXT NOT NULL,
  candidate_url TEXT NOT NULL,
  candidate_model TEXT NOT NULL,
  reference_name TEXT NOT NULL,
  reference_model TEXT NOT NULL,
  report_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admission_reports_created ON reports(created_at DESC,id DESC);
"""

_connection: sqlite3.Connection | None = None
_lock = threading.RLock()


def init() -> None:
    global _connection
    with _lock:
        if _connection is not None:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            DB_PATH.parent.chmod(0o700)
        except OSError:
            pass
        _connection = sqlite3.connect(DB_PATH, check_same_thread=False)
        _connection.row_factory = sqlite3.Row
        _connection.execute("PRAGMA journal_mode=WAL")
        _connection.execute("PRAGMA secure_delete=ON")
        _connection.execute("PRAGMA busy_timeout=5000")
        _connection.executescript(SCHEMA)
        _connection.commit()
        try:
            DB_PATH.chmod(0o600)
        except OSError:
            pass


def close() -> None:
    global _connection
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    init()
    assert _connection is not None
    with _lock:
        current = _connection.cursor()
        try:
            yield current
            _connection.commit()
        except Exception:
            _connection.rollback()
            raise
        finally:
            current.close()


def _summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "created_at": float(row["created_at"]),
        "reported_at": row["reported_at"],
        "status": row["status"],
        "candidate_url": row["candidate_url"],
        "candidate_model": row["candidate_model"],
        "reference_name": row["reference_name"],
        "reference_model": row["reference_model"],
    }


def save_report(report: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("准入报告超过 4 MiB，未保存到历史；仍可在当前页面下载")
    candidate = report["candidate"]
    reference = report["reference"]
    with cursor() as cur:
        cur.execute(
            "INSERT INTO reports(created_at,reported_at,status,candidate_url,candidate_model,"
            "reference_name,reference_model,report_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                time.time(), report["created_at"], report["status"],
                candidate["base_url"], candidate["model"],
                reference.get("name") or reference.get("base_url") or "参照端",
                reference["model"], encoded,
            ),
        )
        report_id = int(cur.lastrowid)
        cur.execute(
            "DELETE FROM reports WHERE id IN ("
            "SELECT id FROM reports ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET ?)",
            (MAX_REPORTS,),
        )
        row = cur.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    assert row is not None
    return _summary(row)


def list_reports(limit: int = MAX_REPORTS) -> list[dict[str, Any]]:
    limit = min(MAX_REPORTS, max(1, int(limit)))
    with cursor() as cur:
        rows = cur.execute(
            "SELECT id,created_at,reported_at,status,candidate_url,candidate_model,"
            "reference_name,reference_model FROM reports ORDER BY created_at DESC,id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_summary(row) for row in rows]


def get_report(report_id: int) -> dict[str, Any] | None:
    with cursor() as cur:
        row = cur.execute("SELECT id,report_json FROM reports WHERE id=?", (report_id,)).fetchone()
    if row is None:
        return None
    report = json.loads(row["report_json"])
    return {"id": int(row["id"]), **report}


def delete_report(report_id: int) -> bool:
    with cursor() as cur:
        return cur.execute("DELETE FROM reports WHERE id=?", (report_id,)).rowcount > 0
