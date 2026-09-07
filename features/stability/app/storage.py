from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from statistics import median
from typing import Any, Iterator

from .config import DB_PATH
from .security import decrypt, encrypt, mask
from shared.registry import get_registry


SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  base_url TEXT NOT NULL,
  model TEXT NOT NULL,
  protocol TEXT NOT NULL,
  api_key_enc TEXT NOT NULL,
  registry_channel_id INTEGER,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  daily_times TEXT NOT NULL,
  timezone TEXT NOT NULL,
  rounds INTEGER NOT NULL DEFAULT 3,
  round_interval_seconds INTEGER NOT NULL DEFAULT 15,
  notification_delay_seconds INTEGER NOT NULL DEFAULT 0,
  max_concurrency INTEGER NOT NULL DEFAULT 3,
  min_success_rate REAL NOT NULL DEFAULT 0.95,
  max_timeout_rate REAL NOT NULL DEFAULT 0.05,
  max_stream_break_rate REAL NOT NULL DEFAULT 0,
  max_p95_ms INTEGER NOT NULL DEFAULT 30000,
  speed_threshold_mode TEXT NOT NULL DEFAULT 'fixed',
  speed_baseline_min_runs INTEGER NOT NULL DEFAULT 5,
  speed_slow_ratio REAL NOT NULL DEFAULT 1.5,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_channels (
  schedule_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  PRIMARY KEY(schedule_id, channel_id),
  FOREIGN KEY(schedule_id) REFERENCES schedules(id) ON DELETE CASCADE,
  FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  schedule_id INTEGER,
  schedule_name TEXT NOT NULL,
  scheduled_for REAL NOT NULL,
  source TEXT NOT NULL DEFAULT 'schedule',
  status TEXT NOT NULL DEFAULT 'pending',
  lease_until REAL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  snapshot_json TEXT NOT NULL,
  summary_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  notify_status TEXT NOT NULL DEFAULT 'pending',
  notify_error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL,
  UNIQUE(schedule_id, scheduled_for, source)
);
CREATE TABLE IF NOT EXISTS probe_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  channel_id INTEGER,
  channel_name TEXT NOT NULL,
  model TEXT NOT NULL,
  round_number INTEGER NOT NULL,
  probe_id TEXT NOT NULL,
  ok INTEGER NOT NULL,
  status TEXT NOT NULL,
  latency_ms INTEGER,
  ttft_ms INTEGER,
  tokens_per_second REAL,
  output_tokens INTEGER,
  finish_reason TEXT NOT NULL DEFAULT '',
  actual_model TEXT NOT NULL DEFAULT '',
  usage_complete INTEGER NOT NULL DEFAULT 0,
  model_mismatch INTEGER NOT NULL DEFAULT 0,
  stream_break INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value_enc TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS report_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  family TEXT NOT NULL,
  label TEXT NOT NULL,
  registry_channel_ids_json TEXT NOT NULL DEFAULT '[]',
  always_normal INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(family, label)
);
CREATE INDEX IF NOT EXISTS idx_runs_status_due ON runs(status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_runs_retention ON runs(status,notify_status,finished_at);
CREATE INDEX IF NOT EXISTS idx_results_run ON probe_results(run_id);
"""

_connection: sqlite3.Connection | None = None
_lock = threading.RLock()


def init() -> None:
    global _connection
    with _lock:
        if _connection is not None:
            return
        _connection = sqlite3.connect(DB_PATH, check_same_thread=False)
        try:
            DB_PATH.chmod(0o600)
        except OSError:
            pass
        _connection.row_factory = sqlite3.Row
        _connection.execute("PRAGMA journal_mode=WAL")
        _connection.execute("PRAGMA secure_delete=ON")
        _connection.execute("PRAGMA foreign_keys=ON")
        _connection.execute("PRAGMA busy_timeout=5000")
        _connection.executescript(SCHEMA)
        for statement in (
            "ALTER TABLE channels ADD COLUMN registry_channel_id INTEGER",
            "ALTER TABLE schedules ADD COLUMN notification_delay_seconds INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE schedules ADD COLUMN speed_threshold_mode TEXT NOT NULL DEFAULT 'fixed'",
            "ALTER TABLE schedules ADD COLUMN speed_baseline_min_runs INTEGER NOT NULL DEFAULT 5",
            "ALTER TABLE schedules ADD COLUMN speed_slow_ratio REAL NOT NULL DEFAULT 1.5",
            "ALTER TABLE probe_results ADD COLUMN actual_model TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE probe_results ADD COLUMN usage_complete INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE probe_results ADD COLUMN model_mismatch INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE probe_results ADD COLUMN stream_break INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                _connection.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).casefold():
                    raise
        _connection.commit()


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
        cur = _connection.cursor()
        try:
            yield cur
            _connection.commit()
        except Exception:
            _connection.rollback()
            raise
        finally:
            cur.close()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default


def health() -> bool:
    try:
        with cursor() as cur:
            return cur.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        return False


def list_inventory(*, include_secrets: bool = False) -> list[dict[str, Any]]:
    registry = get_registry()
    rows = registry.list()
    return [registry.get(row["id"], secret=True) for row in rows] if include_secrets else rows


def add_inventory(data: dict[str, Any]) -> int | None:
    result = get_registry().import_records([data])
    return result["ids"][0] if result["added"] else None


def list_channels(*, include_secrets: bool = False, ids: list[int] | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM channels"
    params: tuple[Any, ...] = ()
    if ids is not None:
        if not ids:
            return []
        sql += f" WHERE id IN ({','.join('?' for _ in ids)})"
        params = tuple(ids)
    sql += " ORDER BY name"
    with cursor() as cur:
        rows = [dict(row) for row in cur.execute(sql, params).fetchall()]
    output = []
    for row in rows:
        item = {key: row[key] for key in ("id", "name", "model", "protocol", "enabled", "created_at", "updated_at", "registry_channel_id")}
        channel = get_registry().get(row["registry_channel_id"], secret=include_secrets)
        item.update(base_url=channel["base_url"], multiplier=channel["multiplier"], scope=channel["scope"],
                    registry_version=channel["version"], key_masked=channel["key_masked"],
                    enabled=bool(item["enabled"] and channel["enabled"]))
        if include_secrets:
            item["api_key"] = channel["api_key"]
        output.append(item)
    return output


def get_channel(channel_id: int, *, include_secret: bool = False) -> dict[str, Any] | None:
    rows = list_channels(include_secrets=include_secret, ids=[channel_id])
    return rows[0] if rows else None


def upsert_channel(data: dict[str, Any]) -> int:
    now = time.time()
    channel_id = data.get("id")
    registry_id = data.get("registry_channel_id")
    if not registry_id:
        registry_id = get_registry().save(data)["id"]
    get_registry().get(registry_id)
    with cursor() as cur:
        if channel_id:
            current = cur.execute("SELECT api_key_enc FROM channels WHERE id=?", (channel_id,)).fetchone()
            if not current:
                raise KeyError("渠道不存在")
            cur.execute(
                "UPDATE channels SET name=?,base_url='',model=?,protocol=?,api_key_enc='',registry_channel_id=?,enabled=?,updated_at=? WHERE id=?",
                (data["name"], data["model"], data["protocol"], registry_id, int(data["enabled"]), now, channel_id),
            )
            return int(channel_id)
        cur.execute(
            "INSERT INTO channels(name,base_url,model,protocol,api_key_enc,registry_channel_id,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (data["name"], "", data["model"], data["protocol"], "", registry_id, int(data["enabled"]), now, now),
        )
        return int(cur.lastrowid)


def delete_channel(channel_id: int) -> bool:
    with cursor() as cur:
        return cur.execute("DELETE FROM channels WHERE id=?", (channel_id,)).rowcount > 0


def _schedule_out(row: dict[str, Any]) -> dict[str, Any]:
    with cursor() as cur:
        ids = [item[0] for item in cur.execute(
            "SELECT channel_id FROM schedule_channels WHERE schedule_id=? ORDER BY channel_id", (row["id"],)
        ).fetchall()]
    row["channel_ids"] = ids
    return row


def list_schedules(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM schedules" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY name"
    with cursor() as cur:
        rows = [dict(row) for row in cur.execute(sql).fetchall()]
    return [_schedule_out(row) for row in rows]


def get_schedule(schedule_id: int) -> dict[str, Any] | None:
    with cursor() as cur:
        row = cur.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    return _schedule_out(dict(row)) if row else None


def upsert_schedule(data: dict[str, Any]) -> int:
    now = time.time()
    schedule_id = data.get("id")
    fields = (
        data["name"], data["daily_times"], data["timezone"], data["rounds"],
        data["round_interval_seconds"], data["notification_delay_seconds"],
        data["max_concurrency"], data["min_success_rate"],
        data["max_timeout_rate"], data["max_stream_break_rate"], data["max_p95_ms"],
        data["speed_threshold_mode"], data["speed_baseline_min_runs"], data["speed_slow_ratio"],
        int(data["enabled"]), now,
    )
    with cursor() as cur:
        if schedule_id:
            if not cur.execute("SELECT id FROM schedules WHERE id=?", (schedule_id,)).fetchone():
                raise KeyError("计划不存在")
            cur.execute(
                "UPDATE schedules SET name=?,daily_times=?,timezone=?,rounds=?,round_interval_seconds=?,notification_delay_seconds=?,max_concurrency=?,min_success_rate=?,max_timeout_rate=?,max_stream_break_rate=?,max_p95_ms=?,speed_threshold_mode=?,speed_baseline_min_runs=?,speed_slow_ratio=?,enabled=?,updated_at=? WHERE id=?",
                (*fields, schedule_id),
            )
        else:
            cur.execute(
                "INSERT INTO schedules(name,daily_times,timezone,rounds,round_interval_seconds,notification_delay_seconds,max_concurrency,min_success_rate,max_timeout_rate,max_stream_break_rate,max_p95_ms,speed_threshold_mode,speed_baseline_min_runs,speed_slow_ratio,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*fields[:-1], now, now),
            )
            schedule_id = int(cur.lastrowid)
        cur.execute("DELETE FROM schedule_channels WHERE schedule_id=?", (schedule_id,))
        cur.executemany(
            "INSERT INTO schedule_channels(schedule_id,channel_id) VALUES(?,?)",
            ((schedule_id, channel_id) for channel_id in data["channel_ids"]),
        )
    return int(schedule_id)


def delete_schedule(schedule_id: int) -> bool:
    with cursor() as cur:
        return cur.execute("DELETE FROM schedules WHERE id=?", (schedule_id,)).rowcount > 0


def create_run(schedule: dict[str, Any], scheduled_for: float, source: str = "schedule") -> int | None:
    snapshot = {key: schedule[key] for key in (
        "id", "name", "timezone", "rounds", "round_interval_seconds", "max_concurrency",
        "notification_delay_seconds", "min_success_rate", "max_timeout_rate",
        "max_stream_break_rate", "max_p95_ms", "speed_threshold_mode",
        "speed_baseline_min_runs", "speed_slow_ratio", "channel_ids",
    )}
    snapshot["report_groups"] = list_report_groups()
    now = time.time()
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO runs(schedule_id,schedule_name,scheduled_for,source,status,snapshot_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (schedule["id"], schedule["name"], scheduled_for, source, "pending", dumps(snapshot), now),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def recover_expired_runs(now: float) -> int:
    with cursor() as cur:
        return cur.execute(
            "UPDATE runs SET status='pending',error='上次执行中断，已重新排队',lease_until=NULL "
            "WHERE status='running' AND COALESCE(lease_until,0)<?",
            (now,),
        ).rowcount


def recover_all_running() -> int:
    with cursor() as cur:
        return cur.execute(
            "UPDATE runs SET status='pending',error='服务重启，任务已重新排队',lease_until=NULL WHERE status='running'"
        ).rowcount


def pending_runs(limit: int) -> list[dict[str, Any]]:
    with cursor() as cur:
        return [dict(row) for row in cur.execute(
            "SELECT * FROM runs WHERE status='pending' AND scheduled_for<=? ORDER BY scheduled_for LIMIT ?",
            (time.time(), limit),
        ).fetchall()]


def has_active_run(schedule_id: int) -> bool:
    with cursor() as cur:
        return cur.execute(
            "SELECT 1 FROM runs WHERE schedule_id=? AND status IN ('pending','running') LIMIT 1",
            (schedule_id,),
        ).fetchone() is not None


def claim_run(run_id: int, lease_seconds: int = 3600) -> bool:
    now = time.time()
    with cursor() as cur:
        return cur.execute(
            "UPDATE runs SET status='running',lease_until=?,attempt_count=attempt_count+1,started_at=COALESCE(started_at,?) WHERE id=? AND status='pending'",
            (now + lease_seconds, now, run_id),
        ).rowcount == 1


def extend_lease(run_id: int, lease_seconds: int = 3600) -> None:
    with cursor() as cur:
        cur.execute("UPDATE runs SET lease_until=? WHERE id=? AND status='running'", (time.time() + lease_seconds, run_id))


def add_probe_result(run_id: int, result: dict[str, Any]) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO probe_results(run_id,channel_id,channel_name,model,round_number,probe_id,ok,status,latency_ms,ttft_ms,tokens_per_second,output_tokens,finish_reason,actual_model,usage_complete,model_mismatch,stream_break,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, result["channel_id"], result["channel_name"], result["model"],
                result["round_number"], result["probe_id"], int(result["ok"]), result["status"],
                result.get("latency_ms"), result.get("ttft_ms"), result.get("tokens_per_second"),
                result.get("output_tokens"), result.get("finish_reason", ""), result.get("actual_model", ""),
                int(bool(result.get("usage_complete"))), int(bool(result.get("model_mismatch"))),
                int(bool(result.get("stream_break"))), result.get("error", ""), time.time(),
            ),
        )


def clear_probe_results(run_id: int) -> None:
    with cursor() as cur:
        cur.execute("DELETE FROM probe_results WHERE run_id=?", (run_id,))


def channel_latency_baseline(channel_id: int, model: str, max_runs: int = 30) -> dict[str, Any]:
    """Return the median of per-run successful-request P95 values from stable scheduled runs."""
    with cursor() as cur:
        run_ids = [int(row[0]) for row in cur.execute(
            "SELECT DISTINCT r.id FROM runs r JOIN probe_results p ON p.run_id=r.id "
            "WHERE p.channel_id=? AND p.model=? AND r.source='schedule' AND r.status='completed' "
            "ORDER BY r.scheduled_for DESC,r.id DESC LIMIT ?",
            (channel_id, model, max_runs),
        ).fetchall()]
        per_run_p95: list[float] = []
        for run_id in run_ids:
            rows = cur.execute(
                "SELECT ok,latency_ms FROM probe_results WHERE run_id=? AND channel_id=? AND model=?",
                (run_id, channel_id, model),
            ).fetchall()
            if not rows or not all(bool(row["ok"]) for row in rows):
                continue
            latencies = sorted(float(row["latency_ms"]) for row in rows if row["latency_ms"] is not None)
            if latencies:
                per_run_p95.append(latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)])
    return {
        "sample_count": len(per_run_p95),
        "median_p95_latency_ms": median(per_run_p95) if per_run_p95 else None,
    }


def finish_run(run_id: int, status: str, summary: dict[str, Any], error: str = "") -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE runs SET status=?,summary_json=?,error=?,lease_until=NULL,finished_at=? WHERE id=?",
            (status, dumps(summary), error[:300], time.time(), run_id),
        )


def update_notification(run_id: int, status: str, error: str = "") -> None:
    with cursor() as cur:
        cur.execute("UPDATE runs SET notify_status=?,notify_error=? WHERE id=?", (status, error[:300], run_id))


def pending_notifications(now: float, limit: int = 20) -> list[dict[str, Any]]:
    with cursor() as cur:
        rows = [dict(row) for row in cur.execute(
            "SELECT * FROM runs WHERE status IN ('completed','failed') AND notify_status='pending' "
            "ORDER BY scheduled_for,id",
        ).fetchall()]
    output: list[dict[str, Any]] = []
    for row in rows:
        snapshot = loads(row.pop("snapshot_json"), {})
        summary = loads(row.pop("summary_json"), {})
        delay = int(snapshot.get("notification_delay_seconds") or 0)
        due_at = float(row["scheduled_for"]) + delay if row["source"] == "schedule" else 0
        if due_at <= now:
            row["snapshot"] = snapshot
            row["summary"] = summary
            row["notification_due_at"] = due_at
            output.append(row)
            if len(output) >= limit:
                break
    return output


def claim_notification(run_id: int) -> bool:
    with cursor() as cur:
        return cur.execute(
            "UPDATE runs SET notify_status='sending',notify_error='' WHERE id=? AND notify_status='pending'",
            (run_id,),
        ).rowcount == 1


def recover_sending_notifications() -> int:
    with cursor() as cur:
        return cur.execute(
            "UPDATE runs SET notify_status='pending',notify_error='service restarted while sending' "
            "WHERE notify_status='sending'"
        ).rowcount


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    with cursor() as cur:
        rows = [dict(row) for row in cur.execute(
            "SELECT id,schedule_id,schedule_name,scheduled_for,source,status,summary_json,error,notify_status,notify_error,attempt_count,created_at,started_at,finished_at FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()]
    for row in rows:
        row["summary"] = loads(row.pop("summary_json"), {})
    return rows


def get_run(run_id: int) -> dict[str, Any] | None:
    with cursor() as cur:
        run = cur.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        results = [dict(row) for row in cur.execute(
            "SELECT * FROM probe_results WHERE run_id=? ORDER BY channel_name,round_number,id", (run_id,)
        ).fetchall()]
    if not run:
        return None
    output = dict(run)
    output["snapshot"] = loads(output.pop("snapshot_json"), {})
    output["summary"] = loads(output.pop("summary_json"), {})
    output["results"] = results
    return output


def prune_run_history(now: float, retention_days: int) -> int:
    """Delete settled stability reports older than the retention window.

    Pending/running tests and reports whose notification is pending or being sent are never removed.
    probe_results are removed by the existing ON DELETE CASCADE relationship.
    """
    cutoff = float(now) - max(1, int(retention_days)) * 86400
    with cursor() as cur:
        return cur.execute(
            "DELETE FROM runs WHERE status IN ('completed','failed') "
            "AND notify_status NOT IN ('pending','sending') "
            "AND COALESCE(finished_at,created_at)<?",
            (cutoff,),
        ).rowcount


def set_setting(key: str, value: str) -> None:
    with cursor() as cur:
        if not value:
            cur.execute("DELETE FROM settings WHERE key=?", (key,))
            return
        cur.execute(
            "INSERT INTO settings(key,value_enc,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_enc=excluded.value_enc,updated_at=excluded.updated_at",
            (key, encrypt(value), time.time()),
        )


def get_setting(key: str) -> str:
    with cursor() as cur:
        row = cur.execute("SELECT value_enc FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return ""
    try:
        return decrypt(row["value_enc"])
    except Exception:
        return ""


def list_report_groups() -> list[dict[str, Any]]:
    with cursor() as cur:
        rows = [dict(row) for row in cur.execute(
            "SELECT id,family,label,registry_channel_ids_json,always_normal,sort_order "
            "FROM report_groups ORDER BY sort_order,id"
        ).fetchall()]
    for row in rows:
        row["registry_channel_ids"] = [
            int(value) for value in loads(row.pop("registry_channel_ids_json"), [])
        ]
        row["always_normal"] = bool(row["always_normal"])
    return rows


def replace_report_groups(groups: list[dict[str, Any]]) -> None:
    now = time.time()
    with cursor() as cur:
        cur.execute("DELETE FROM report_groups")
        cur.executemany(
            "INSERT INTO report_groups(family,label,registry_channel_ids_json,always_normal,sort_order,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                (
                    group["family"], group["label"], dumps(group.get("registry_channel_ids", [])),
                    int(bool(group.get("always_normal"))), order, now, now,
                )
                for order, group in enumerate(groups)
            ),
        )
