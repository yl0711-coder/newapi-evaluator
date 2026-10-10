"""Ten-minute daily occurrences, immutable sampling identity and current send controls."""
from __future__ import annotations

import json
import time
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from shared.registry import get_registry
from features.integrity.execution import ExecutionStopped, build_requests
from features.integrity.strategies import get_strategy
from . import layered, storage

from .timetable_contract import VERSION, METHODS, GRID, DEFAULTS, WINDOWS, validate_config, local_epoch, build_slots

TERMINAL = layered.TERMINAL


def request_limits(method, config=None):
    return dict(_request_limits(method))


@lru_cache(maxsize=3)
def _request_limits(method):
    requests = build_requests(get_strategy(method), DEFAULTS)
    return {"max_requests": len(requests), "max_input_tokens": sum(r.input_tokens_reserved for r in requests),
            "max_output_tokens": sum(r.output_tokens_reserved for r in requests)}


def preview(config, timezone, day=None):
    config = validate_config(config)
    day = day or datetime.now(ZoneInfo(timezone)).date()
    rows = build_slots({"id": 1, "layered_config": config, "timezone": timezone}, day)
    limits = {k: 0 for k in request_limits("health")}
    for row in rows:
        for key, value in request_limits(row["method"], config).items():
            limits[key] += value
    return {**limits, "date": str(day), "timezone": timezone, "occurrences": len(rows),
            "channels": len(config["registry_channel_ids"]), "canary_batches": sum(r["method"] == "canary" for r in rows),
            "modeltrace_batches": sum(r["method"] == "modeltrace" for r in rows),
            "shared_health": sum(r["method"] == "health" for r in rows), "cost_stopping_limit": None,
            "windows_seconds": WINDOWS, "inflight": 1}


def init_tables(registry=None):
    with (registry or get_registry()).connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS integrity_timetable_controls (
          schedule_id INTEGER PRIMARY KEY, revision REAL NOT NULL, enabled INTEGER NOT NULL,
          timezone TEXT NOT NULL, config_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS integrity_timetable_days (
          schedule_id INTEGER NOT NULL, budget_date TEXT NOT NULL, run_id INTEGER NOT NULL UNIQUE,
          cancelled INTEGER NOT NULL DEFAULT 0, limits_json TEXT NOT NULL DEFAULT '{}', budget_timezone TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(schedule_id,budget_date));
        CREATE TABLE IF NOT EXISTS integrity_occurrences (
          slot_key TEXT PRIMARY KEY, schedule_id INTEGER NOT NULL, run_id INTEGER NOT NULL,
          budget_date TEXT NOT NULL, clock TEXT NOT NULL, registry_channel_id INTEGER NOT NULL,
          method TEXT NOT NULL, due REAL NOT NULL, deadline REAL NOT NULL, dependency TEXT,
          priority INTEGER NOT NULL, revision REAL NOT NULL, snapshot_json TEXT NOT NULL,
          job_id TEXT, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
          summary_json TEXT NOT NULL DEFAULT '{}', last_probe REAL NOT NULL DEFAULT 0,
          updated_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_integrity_occurrences_due ON integrity_occurrences(status,due,deadline,priority);
        CREATE INDEX IF NOT EXISTS idx_integrity_occurrences_day ON integrity_occurrences(schedule_id,budget_date);
        CREATE INDEX IF NOT EXISTS idx_integrity_occurrences_run ON integrity_occurrences(run_id,due,registry_channel_id);
        """)
        if "budget_timezone" not in {r[1] for r in conn.execute("PRAGMA table_info(integrity_timetable_days)")}:
            conn.execute("ALTER TABLE integrity_timetable_days ADD COLUMN budget_timezone TEXT NOT NULL DEFAULT ''")
        if "archived" not in {r[1] for r in conn.execute("PRAGMA table_info(integrity_timetable_days)")}:
            conn.execute("ALTER TABLE integrity_timetable_days ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")


def write_control(conn, schedule_id, revision, enabled, timezone, config):
    conn.execute("INSERT INTO integrity_timetable_controls VALUES(?,?,?,?,?) ON CONFLICT(schedule_id) DO UPDATE SET revision=excluded.revision,enabled=excluded.enabled,timezone=excluded.timezone,config_json=excluded.config_json",
                 (schedule_id, revision, int(enabled), timezone, storage.dumps(config)))


def disable_control(conn, schedule_id):
    conn.execute("UPDATE integrity_timetable_controls SET enabled=0 WHERE schedule_id=?", (schedule_id,))
    conn.execute("UPDATE integrity_jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE job_id IN (SELECT job_id FROM integrity_occurrences WHERE schedule_id=? AND status IN ('pending','running'))", (schedule_id,))
    conn.execute("UPDATE integrity_occurrences SET status='cancelled',reason='schedule_cancelled',updated_at=? WHERE schedule_id=? AND status='pending'", (time.time(), schedule_id))


def selected(control, row):
    config = json.loads(control["config_json"])
    if (not control["enabled"] or row["registry_channel_id"] not in config["registry_channel_ids"]
            or local_epoch(row["budget_date"], row["clock"], ZoneInfo(control["timezone"])) != row["due"]):
        return False
    if row["method"] == "health":
        return any(row["clock"] in config[m + "_times"] for m in METHODS)
    return row["clock"] in config[row["method"] + "_times"]


def decode(row):
    row = dict(row)
    row["summary"] = storage.loads(row.pop("summary_json"), {})
    row["snapshot"] = storage.loads(row.pop("snapshot_json"), {})
    row["model"], row["protocol"] = "gpt-6-astra", "responses"
    row["slot"] = row["clock"] + "-" + row["method"]
    row["target_snapshot"] = row["snapshot"].get("layered_config", {}).get("target_bindings", {}).get(f"{row['registry_channel_id']}:gpt-6-astra:responses", {})
    return row


def slots(run_id):
    with get_registry().connect() as conn:
        return [decode(r) for r in conn.execute("SELECT * FROM integrity_occurrences WHERE run_id=? ORDER BY due,registry_channel_id,method", (run_id,))]


def get_slot(key):
    with get_registry().connect() as conn:
        row = conn.execute("SELECT * FROM integrity_occurrences WHERE slot_key=?", (key,)).fetchone()
    return decode(row) if row else None


def update_slot(key, status, *, reason="", summary=None, job_id=None, last_probe=None):
    with get_registry().connect() as conn:
        conn.execute("UPDATE integrity_occurrences SET status=?,reason=?,summary_json=COALESCE(?,summary_json),job_id=COALESCE(?,job_id),last_probe=COALESCE(?,last_probe),updated_at=? WHERE slot_key=?",
                     (status, reason, storage.dumps(summary) if summary is not None else None, job_id, last_probe, time.time(), key))


def reconcile(schedule, day, *, now=None):
    """Keep one occurrence across revisions; only zero-attempt future slots may reopen."""
    now = time.time() if now is None else now
    registry = get_registry()
    from features.integrity.durable import IntegrityStore
    IntegrityStore(registry)
    init_tables(registry)
    rows = build_slots(schedule, day)
    midnight = local_epoch(day, "00:00", ZoneInfo(schedule["timezone"]))
    if midnight is None:
        midnight = datetime.fromisoformat(str(day)).replace(tzinfo=ZoneInfo(schedule["timezone"])).timestamp()
    with registry.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = storage.get_schedule(schedule["id"])
        if current is None or current["plan_version"] != VERSION:
            disable_control(conn, schedule["id"])
            return None
        if current["updated_at"] != schedule["updated_at"]:
            return None
        control = conn.execute("SELECT * FROM integrity_timetable_controls WHERE schedule_id=?", (schedule["id"],)).fetchone()
        if control is None or control["revision"] != schedule["updated_at"]:
            # The stability save can commit just before the Registry mirror.
            # Rebuild from the exact committed source snapshot, without changing
            # its target bindings. The sending guard still rejects target drift.
            current = storage.get_schedule(schedule["id"])
            if current is None or current["plan_version"] != VERSION or current["updated_at"] != schedule["updated_at"]:
                return None
            validate_config(current["layered_config"])
            if not current["layered_config"].get("target_bindings"):
                return None
            write_control(conn, current["id"], current["updated_at"], current["enabled"], current["timezone"], current["layered_config"])
            control = conn.execute("SELECT * FROM integrity_timetable_controls WHERE schedule_id=?", (schedule["id"],)).fetchone()
        saved = conn.execute("SELECT * FROM integrity_timetable_days WHERE schedule_id=? AND budget_date=?", (schedule["id"], str(day))).fetchone()
        if saved:
            if saved["archived"]:
                return None
            run_id = saved["run_id"]
        else:
            run_id = storage.create_run(schedule, midnight, source="timetable-v2")
            if run_id is None:
                # A previous process may have committed the stability run before
                # its Registry day transaction. Reattach that stable identity.
                with storage.cursor() as cur:
                    existing = cur.execute("SELECT id FROM runs WHERE schedule_id=? AND scheduled_for=? AND source='timetable-v2'", (schedule["id"], midnight)).fetchone()
                if existing is None:
                    return None
                run_id = existing[0]
            conn.execute("INSERT INTO integrity_timetable_days(schedule_id,budget_date,run_id,budget_timezone) VALUES(?,?,?,?)", (schedule["id"], str(day), run_id, schedule["timezone"]))
        snapshot = storage.get_run(run_id)["snapshot"]
        if saved and not saved["budget_timezone"]:
            conn.execute("UPDATE integrity_timetable_days SET budget_timezone=? WHERE run_id=?", (snapshot["timezone"], run_id))
        snapshot = {**snapshot, "layered_config": schedule["layered_config"], "timezone": schedule["timezone"],
                    "revision": schedule["updated_at"], "targets": [{k: t[k] for k in ("id", "registry_channel_id", "model", "protocol")} for t in storage.list_channels(ids=schedule["channel_ids"])]}
        for row in rows:
            state = "pending" if row["due"] >= schedule["updated_at"] else "skipped"
            reason = "" if state == "pending" else "not_scheduled_before_save"
            conn.execute("INSERT OR IGNORE INTO integrity_occurrences(slot_key,schedule_id,run_id,budget_date,clock,registry_channel_id,method,due,deadline,dependency,priority,revision,snapshot_json,status,reason,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (row["slot_key"], row["schedule_id"], run_id, row["budget_date"], row["clock"], row["registry_channel_id"], row["method"], row["due"], row["deadline"], row["dependency"], row["priority"], schedule["updated_at"], storage.dumps(snapshot), state, reason, now))
            old = conn.execute("SELECT o.*, (SELECT COUNT(*) FROM integrity_attempts a WHERE a.job_id=o.job_id) AS attempts FROM integrity_occurrences o WHERE slot_key=?", (row["slot_key"],)).fetchone()
            if old["attempts"] == 0 and old["due"] > now and old["status"] in {"pending", "cancelled"} and control["enabled"] and not (saved and saved["cancelled"]):
                conn.execute("UPDATE integrity_occurrences SET status='pending',reason='',revision=?,snapshot_json=?,priority=?,deadline=?,updated_at=? WHERE slot_key=?", (schedule["updated_at"], storage.dumps(snapshot), row["priority"], row["deadline"], now, row["slot_key"]))
                if old["job_id"]:
                    target = snapshot["layered_config"]["target_bindings"][f"{row['registry_channel_id']}:gpt-6-astra:responses"]
                    target_id = next(t["id"] for t in snapshot["targets"] if t["registry_channel_id"] == row["registry_channel_id"])
                    conn.execute("UPDATE integrity_jobs SET status='queued',cancel_requested=0,target_json=? WHERE job_id=? AND status IN ('queued','cancelled')", (storage.dumps({**target, "target_id": str(target_id)}), old["job_id"]))
        current = conn.execute("SELECT * FROM integrity_occurrences WHERE schedule_id=? AND status IN ('pending','running')", (schedule["id"],)).fetchall()
        for row in current:
            if selected(control, row):
                continue
            if row["job_id"]:
                conn.execute("UPDATE integrity_jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE job_id=?", (row["job_id"],))
            if row["status"] != "running":
                conn.execute("UPDATE integrity_occurrences SET status='cancelled',reason='time_removed',updated_at=? WHERE slot_key=?", (now, row["slot_key"]))
        for budget_day in conn.execute("SELECT budget_date FROM integrity_timetable_days WHERE schedule_id=? AND (budget_date=? OR run_id IN (SELECT run_id FROM integrity_occurrences WHERE schedule_id=? AND status IN ('pending','running') AND deadline>?))", (schedule["id"], str(day), schedule["id"], now)).fetchall():
            caps = daily_limits(conn, schedule["id"], budget_day[0], control)
            conn.execute("UPDATE integrity_timetable_days SET limits_json=? WHERE schedule_id=? AND budget_date=?", (storage.dumps(caps), schedule["id"], budget_day[0]))
    with storage.cursor() as cur:
        cur.execute("UPDATE runs SET notify_status='disabled' WHERE id=?", (run_id,))
    return run_id


def synchronize_controls():
    """Converge interrupted save/pause/delete mirrors from committed schedules."""
    schedules = {s["id"]: s for s in storage.list_schedules()}
    affected_schedules = set()
    with get_registry().connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ids = {r[0] for r in conn.execute("SELECT schedule_id FROM integrity_timetable_controls")}
        ids |= {i for i, s in schedules.items() if s["plan_version"] == VERSION}
        for schedule_id in ids:
            current = storage.get_schedule(schedule_id)
            if current is None or current["plan_version"] != VERSION:
                disable_control(conn, schedule_id)
                affected_schedules.add(schedule_id)
                continue
            control = conn.execute("SELECT revision,enabled FROM integrity_timetable_controls WHERE schedule_id=?", (schedule_id,)).fetchone()
            if control is None or control["revision"] != current["updated_at"] or bool(control["enabled"]) != bool(current["enabled"]):
                affected_schedules.add(schedule_id)
                validate_config(current["layered_config"])
                write_control(conn, schedule_id, current["updated_at"], current["enabled"], current["timezone"], current["layered_config"])
                for row in conn.execute("SELECT * FROM integrity_occurrences WHERE schedule_id=? AND status IN ('pending','running')", (schedule_id,)).fetchall():
                    restored = conn.execute("SELECT * FROM integrity_timetable_controls WHERE schedule_id=?", (schedule_id,)).fetchone()
                    if not selected(restored, row):
                        if row["job_id"]:
                            conn.execute("UPDATE integrity_jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE job_id=?", (row["job_id"],))
                        if row["status"] == "pending":
                            conn.execute("UPDATE integrity_occurrences SET status='cancelled',reason='time_removed' WHERE slot_key=?", (row["slot_key"],))
            # Both saves can commit before the API reconciles its occurrences.
            # An already-matching disabled mirror still needs this idempotent
            # cancellation on restart; completed evidence is left untouched.
            if not current["enabled"]:
                disable_control(conn, schedule_id)
                affected_schedules.add(schedule_id)
        known_runs = {r[0] for schedule_id in affected_schedules for r in conn.execute("SELECT run_id FROM integrity_timetable_days WHERE schedule_id=? AND archived=0", (schedule_id,))}
    # A crash can leave the source aggregate running after its Registry slots
    # have already been cancelled. Refresh only active, attached parents after
    # the control transaction commits, including deleted/changed-version plans.
    if not known_runs:
        return
    with storage.cursor() as cur:
        active_runs = [r[0] for r in cur.execute("SELECT id FROM runs WHERE source='timetable-v2' AND status IN ('pending','running')") if r[0] in known_runs]
    from .timetable_executor import refresh_run
    for run_id in active_runs:
        refresh_run(run_id)


def send_guard(slot):
    def guard(conn, job, request):
        row = conn.execute("SELECT * FROM integrity_occurrences WHERE slot_key=?", (slot["slot_key"],)).fetchone()
        control = conn.execute("SELECT * FROM integrity_timetable_controls WHERE schedule_id=?", (slot["schedule_id"],)).fetchone()
        day = conn.execute("SELECT * FROM integrity_timetable_days WHERE run_id=?", (slot["run_id"],)).fetchone()
        current = storage.get_schedule(slot["schedule_id"])
        if (row is None or control is None or current is None or not current["enabled"] or current["plan_version"] != VERSION or current["updated_at"] != control["revision"]
                or day["cancelled"] or row["status"] in {"cancelled", "skipped", "expired"} or not selected(control, row)):
            raise ExecutionStopped("cancelled", "schedule_time_cancelled")
        # This hook runs after the shared transport capacity is acquired. A
        # higher-priority occurrence may have become due while we were waiting.
        candidates = conn.execute("""SELECT o.*,c.config_json,c.enabled,c.timezone FROM integrity_occurrences o
            JOIN integrity_timetable_days d ON d.run_id=o.run_id
            JOIN integrity_timetable_controls c ON c.schedule_id=o.schedule_id
            LEFT JOIN integrity_occurrences h ON h.slot_key=o.dependency
            WHERE o.status='pending' AND o.due<=? AND o.deadline>? AND d.cancelled=0
            AND (o.dependency IS NULL OR (h.status='completed' AND json_extract(h.summary_json,'$.health_pass')=1))
            ORDER BY o.priority,o.deadline,o.last_probe,o.registry_channel_id,o.due LIMIT 64""", (time.time(), time.time()))
        order = lambda r: (r["priority"], r["deadline"], r["last_probe"], r["registry_channel_id"], r["due"])
        for other in candidates:
            if order(other) >= order(row):
                break
            if selected(other, other):
                raise ExecutionStopped("yielded", "higher_priority_ready")
        config = json.loads(control["config_json"])
        bound = config["target_bindings"].get(f"{slot['registry_channel_id']}:gpt-6-astra:responses")
        frozen = json.loads(job["target_json"])
        if not bound or any(frozen.get(k) != value for k, value in bound.items()):
            raise ExecutionStopped("rejected", "schedule_target_changed")
        if not conn.execute("SELECT 1 FROM integrity_attempts WHERE job_id=?", (job["job_id"],)).fetchone():
            channel = get_registry().public(conn.execute("SELECT * FROM channels WHERE id=?", (slot["registry_channel_id"],)).fetchone())
            snapshot = json.loads(row["snapshot_json"])
            snapshot["layered_config"]["channel_names"][str(channel["id"])] = channel["name"]
            snapshot["layered_config"].setdefault("channel_multipliers", {})[str(channel["id"])] = channel["multiplier"]
            conn.execute("UPDATE integrity_occurrences SET snapshot_json=? WHERE slot_key=?", (storage.dumps(snapshot), slot["slot_key"]))
        return json.loads(day["limits_json"])
    return guard


def daily_limits(conn, schedule_id, day, control=None):
    """Estimate the fixed graph for previews/accounting, never a sending cap."""
    control = control or conn.execute("SELECT * FROM integrity_timetable_controls WHERE schedule_id=?", (schedule_id,)).fetchone()
    limits = {k: 0 for k in request_limits("health")}
    # Retain consumed and unknown estimates across revisions in the same ledger.
    for row in conn.execute("SELECT o.*, (SELECT COUNT(*) FROM integrity_attempts a WHERE a.job_id=o.job_id) AS attempts, (SELECT COALESCE(SUM(input_cap),0) FROM integrity_attempts a WHERE a.job_id=o.job_id) AS input_used, (SELECT COALESCE(SUM(output_cap),0) FROM integrity_attempts a WHERE a.job_id=o.job_id) AS output_used FROM integrity_occurrences o WHERE schedule_id=? AND budget_date=?", (schedule_id, day)):
        caps = request_limits(row["method"]) if selected(control, row) and row["reason"] != "not_scheduled_before_save" else {"max_requests": row["attempts"], "max_input_tokens": row["input_used"], "max_output_tokens": row["output_used"]}
        for key in limits:
            limits[key] += caps[key]
    return limits


def budget_key(schedule_id):
    return f"timetable:{schedule_id}"
