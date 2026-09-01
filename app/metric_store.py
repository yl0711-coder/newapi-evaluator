"""生产指标独立 SQLite；与账号、任务和审计库隔离锁与容量。"""
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any

from .config import METRIC_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  endpoint TEXT NOT NULL,
  token_enc TEXT NOT NULL DEFAULT '',
  protocol_version TEXT NOT NULL DEFAULT '1',
  cursor TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  poll_interval_seconds INTEGER NOT NULL DEFAULT 60,
  last_attempt_at REAL,last_success_at REAL,last_error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_buckets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,source_id INTEGER NOT NULL,bucket_start INTEGER NOT NULL,
  platform_group TEXT NOT NULL,model_family TEXT NOT NULL DEFAULT '',model TEXT NOT NULL,
  usage_profile TEXT NOT NULL DEFAULT 'general',channel TEXT NOT NULL,
  supply_source TEXT NOT NULL DEFAULT '',output_length_band TEXT NOT NULL DEFAULT 'unknown',
  request_count INTEGER NOT NULL DEFAULT 0,attempt_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,failure_count INTEGER NOT NULL DEFAULT 0,
  retry_count INTEGER NOT NULL DEFAULT 0,failover_count INTEGER NOT NULL DEFAULT 0,
  auth_error_count INTEGER NOT NULL DEFAULT 0,rate_limit_count INTEGER NOT NULL DEFAULT 0,
  timeout_count INTEGER NOT NULL DEFAULT 0,stream_break_count INTEGER NOT NULL DEFAULT 0,
  upstream_5xx_count INTEGER NOT NULL DEFAULT 0,network_error_count INTEGER NOT NULL DEFAULT 0,
  protocol_error_count INTEGER NOT NULL DEFAULT 0,input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,ttft_p50_ms REAL,ttft_p95_ms REAL,
  latency_p50_ms REAL,latency_p95_ms REAL,generation_tps REAL,output_length_p50 REAL,
  output_length_p95 REAL,active_users INTEGER NOT NULL DEFAULT 0,
  top_user_request_share REAL NOT NULL DEFAULT 0,received_at REAL NOT NULL,updated_at REAL NOT NULL,
  UNIQUE(source_id,bucket_start,platform_group,model,usage_profile,channel,output_length_band),
  FOREIGN KEY(source_id) REFERENCES metric_sources(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_metric_buckets_time ON metric_buckets(bucket_start);
CREATE INDEX IF NOT EXISTS idx_metric_buckets_dimensions
  ON metric_buckets(platform_group,model,usage_profile,channel,bucket_start);
CREATE TABLE IF NOT EXISTS metric_collection_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,source_id INTEGER NOT NULL,cursor_before TEXT NOT NULL DEFAULT '',
  cursor_after TEXT NOT NULL DEFAULT '',received_count INTEGER NOT NULL DEFAULT 0,
  upserted_count INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL,finished_at REAL,
  FOREIGN KEY(source_id) REFERENCES metric_sources(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_metric_runs_source ON metric_collection_runs(source_id,started_at);
CREATE TABLE IF NOT EXISTS metric_retention_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,status TEXT NOT NULL,
  hourly_rows INTEGER NOT NULL DEFAULT 0,daily_rows INTEGER NOT NULL DEFAULT 0,
  minute_rows_deleted INTEGER NOT NULL DEFAULT 0,
  hourly_rows_deleted INTEGER NOT NULL DEFAULT 0,daily_rows_deleted INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',started_at REAL NOT NULL,finished_at REAL
);
CREATE TABLE IF NOT EXISTS hourly_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,period_start INTEGER NOT NULL,platform_group TEXT NOT NULL,
  model_family TEXT NOT NULL DEFAULT '',model TEXT NOT NULL,usage_profile TEXT NOT NULL,
  channel TEXT NOT NULL,output_length_band TEXT NOT NULL,metrics_json TEXT NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(period_start,platform_group,model,usage_profile,channel,output_length_band)
);
CREATE INDEX IF NOT EXISTS idx_hourly_metrics_time ON hourly_metrics(period_start);
CREATE TABLE IF NOT EXISTS daily_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,period_start INTEGER NOT NULL,platform_group TEXT NOT NULL,
  model_family TEXT NOT NULL DEFAULT '',model TEXT NOT NULL,usage_profile TEXT NOT NULL,
  channel TEXT NOT NULL,output_length_band TEXT NOT NULL,metrics_json TEXT NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(period_start,platform_group,model,usage_profile,channel,output_length_band)
);
CREATE INDEX IF NOT EXISTS idx_daily_metrics_time ON daily_metrics(period_start);
"""

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()


def init() -> None:
    global _conn
    if _conn is not None:
        return
    _conn = sqlite3.connect(METRIC_DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.executescript(SCHEMA)
    columns = {row["name"] for row in _conn.execute("PRAGMA table_info(metric_buckets)")}
    if "supply_source" not in columns:
        _conn.execute("ALTER TABLE metric_buckets ADD COLUMN supply_source TEXT NOT NULL DEFAULT ''")
        _conn.commit()


def migrate_from_business_store() -> int:
    """一次性复制旧单库指标；保留旧表只供回滚，不再写入。"""
    from . import store
    if query("SELECT COUNT(*) n FROM metric_sources")[0]["n"]:
        return 0
    legacy = store.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='metric_sources'"
    )
    if not legacy:
        return 0
    copied = 0
    for table in (
        "metric_sources", "metric_buckets", "metric_collection_runs",
        "hourly_metrics", "daily_metrics", "metric_retention_runs",
    ):
        rows = store.query(f"SELECT * FROM {table}")
        if not rows:
            continue
        allowed = {row["name"] for row in query(f"PRAGMA table_info({table})")}
        for row in rows:
            values = {key: value for key, value in row.items() if key in allowed}
            columns = ",".join(values)
            marks = ",".join("?" for _ in values)
            execute(
                f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ({marks})",
                tuple(values.values()),
            )
            copied += 1
    return copied


@contextmanager
def cursor():
    assert _conn is not None, "metric_store.init() 未调用"
    with _lock:
        cur = _conn.cursor()
        try:
            yield cur
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise
        finally:
            cur.close()


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with cursor() as cur:
        return [dict(row) for row in cur.execute(sql, params).fetchall()]


def execute(sql: str, params: tuple = ()) -> None:
    with cursor() as cur:
        cur.execute(sql, params)


def insert(table: str, data: dict[str, Any]) -> int:
    columns = ",".join(data)
    marks = ",".join("?" for _ in data)
    with cursor() as cur:
        cur.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(data.values()))
        return int(cur.lastrowid or 0)


def update(table: str, row_id: int, data: dict[str, Any]) -> None:
    sets = ",".join(f"{key}=?" for key in data)
    execute(f"UPDATE {table} SET {sets} WHERE id=?", (*data.values(), row_id))


def get(table: str, row_id: int) -> dict[str, Any] | None:
    rows = query(f"SELECT * FROM {table} WHERE id=?", (row_id,))
    return rows[0] if rows else None


def delete(table: str, row_id: int) -> None:
    execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
