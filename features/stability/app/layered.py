"""Versioned daily slots, health dependencies and persistent least-served rotation."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from shared.registry import get_registry, RegistryError
from features.model_coverage.catalog import Catalog, validate_protocol
from . import storage

VERSION = "layered-integrity-v1"
DEFAULTS = {
    "health_times": ["09:30", "12:30", "15:30", "18:00"],
    "astra_times": ["10:00", "15:35"], "sol_time": "15:50",
    "modeltrace_time": "16:10", "canary_time": "18:15",
    "day_deadline": "17:55", "canary_deadline": "08:55",
    "health_ttl_minutes": 60,
    "health_model": "gpt-6-astra", "health_protocol": "responses",
    "astra_model": "gpt-6-astra", "astra_protocol": "responses",
    "sol_model": "gpt-6.1-sol", "sol_protocol": "responses",
    "reasoning_effort": "low", "baseline_id": None,
    "consecutive_slots": 2, "incident_window_hours": 48, "incident_cooldown_days": 7,
}
TERMINAL = {"completed", "partially_completed", "cancelled", "expired", "rejected", "failed", "skipped"}


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_config(value):
    from .scheduler import parse_daily_times
    if not isinstance(value, dict) or set(value) - (set(DEFAULTS) | {"registry_channel_ids", "production_bindings", "target_bindings"}):
        raise ValueError("分层配置含未知字段")
    config = {**DEFAULTS, **value}
    if len(config["health_times"]) != 4 or len(config["astra_times"]) != 2:
        raise ValueError("每日固定四轮探活、两轮 Astra 指纹")
    for key in ("health_times", "astra_times"):
        config[key] = parse_daily_times(",".join(config[key]))
        if len(config[key]) != (4 if key == "health_times" else 2):
            raise ValueError("时刻不可重复")
    for key in ("sol_time", "modeltrace_time", "canary_time", "day_deadline", "canary_deadline"):
        if parse_daily_times(config[key]) != [config[key]]:
            raise ValueError("时刻格式无效")
    if not (config["health_times"][0] < config["astra_times"][0] < config["health_times"][1]
            < config["health_times"][2] < config["astra_times"][1] < config["sol_time"]
            < config["modeltrace_time"] < config["day_deadline"] < config["health_times"][3] < config["canary_time"]):
        raise ValueError("探活、错峰指纹和截止时刻的顺序无效")
    if not 1 <= config["health_ttl_minutes"] <= 180:
        raise ValueError("健康有效期无效")
    if config["reasoning_effort"] != "low" or config["astra_model"] != "gpt-6-astra" or config["sol_model"] != "gpt-6.1-sol":
        raise ValueError("当前版本固定 Astra / 6.1 Sol 与 low 条件")
    if not 2 <= config["consecutive_slots"] <= 10 or not 1 <= config["incident_window_hours"] <= 168 or not 1 <= config["incident_cooldown_days"] <= 30:
        raise ValueError("连续异常操作门槛无效")
    for kind in ("health", "astra", "sol"):
        validate_protocol(config[kind + "_protocol"], config[kind + "_model"])
    return config



def candidates():
    registry = get_registry()
    catalog = Catalog(registry)
    models, mappings, discoveries = catalog.models(), catalog.mappings(), catalog.discoveries()
    targets = storage.list_channels()
    rows = []
    for channel in registry.list():
        credential = channel["credential_status"]
        discovery = discoveries.get(channel["id"])
        entries = []
        for model in models:
            binding = mappings.get((channel["id"], model["id"]), {})
            actual = binding.get("upstream_model", model["model"])
            protocol = binding.get("protocol", model["protocol"])
            matching = [t for t in targets if (t["registry_channel_id"], t["model"], t["protocol"]) == (channel["id"], model["model"], protocol)]
            reason = "" if channel["enabled"] else "registry_disabled"
            if channel["status"] != "online":
                reason = reason or "registry_not_online"
            if credential != "available":
                reason = "credential_" + credential
            if matching and not any(t["enabled"] for t in matching):
                reason = "target_disabled"
            try:
                validate_protocol(protocol, model["model"], actual)
            except RegistryError:
                reason = "protocol_invalid"
            try:
                from features.integrity.execution import resolve_registry_target
                resolve_registry_target(registry, channel["id"], model["model"], protocol, require_online=True)
            except RegistryError as exc:
                reason = reason or str(exc)
            entries.append({"model_id": model["id"], "model": model["model"], "upstream_model": actual, "catalog_model": model["model"],
                            "label": model["label"], "protocol": protocol, "eligible": not reason,
                            "reason": reason,
                            "eval_coverage": "configured" if matching else "not_enrolled",
                            "target_ids": [t["id"] for t in matching]})
        rows.append({"registry_channel_id": channel["id"], "name": channel["name"],
                     "enabled": channel["enabled"], "status": channel["status"],
                     "source_kind": channel["source_kind"], "credential_status": credential,
                     "discovery_status": "unknown" if not discovery else "recorded",
                     "models": entries})
    return rows


def init_tables():
    with storage.cursor() as cur:
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS layered_slots (
          slot_key TEXT PRIMARY KEY, run_id INTEGER NOT NULL, plan_hash TEXT NOT NULL,
          budget_date TEXT NOT NULL, slot TEXT NOT NULL, registry_channel_id INTEGER,
          model TEXT NOT NULL, protocol TEXT NOT NULL, method TEXT NOT NULL,
          due REAL NOT NULL, deadline REAL NOT NULL, dependency TEXT,
          target_json TEXT NOT NULL DEFAULT '{}', job_id TEXT,
          status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
          summary_json TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL,
          FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS layered_rotation (
          schedule_id INTEGER NOT NULL, registry_channel_id INTEGER NOT NULL,
          last_served REAL NOT NULL, PRIMARY KEY(schedule_id,registry_channel_id));
        CREATE TABLE IF NOT EXISTS layered_day_selection (
          schedule_id INTEGER NOT NULL, budget_date TEXT NOT NULL, registry_channel_id INTEGER,
          PRIMARY KEY(schedule_id,budget_date));
        CREATE TABLE IF NOT EXISTS integrity_baselines (
          id INTEGER PRIMARY KEY AUTOINCREMENT, source_slot_key TEXT NOT NULL UNIQUE,
          label TEXT NOT NULL, score_json TEXT NOT NULL, selected_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS layered_incidents (
          incident_id TEXT PRIMARY KEY, target_key TEXT NOT NULL, conditions_hash TEXT NOT NULL,
          direction TEXT NOT NULL, source_slot_key TEXT NOT NULL, created_at REAL NOT NULL);
        """)


def epoch(day, clock, zone):
    return datetime.fromisoformat(f"{day.isoformat()}T{clock}:00").replace(tzinfo=zone).timestamp()


def build_slots(schedule, day):
    config = schedule["layered_config"]
    zone = ZoneInfo(schedule["timezone"])
    plan_hash = canonical_hash({"version": VERSION, "config": config, "timezone": schedule["timezone"]})
    ids = config["registry_channel_ids"]
    slots = []
    def add(slot, channel_id, method, model, protocol, clock, deadline, dependency=None):
        key = f"layered:{schedule['id']}:{plan_hash[:16]}:{day}:{slot}:{channel_id or 'rotation'}:{method}"
        slots.append(dict(slot_key=key, plan_hash=plan_hash, budget_date=day.isoformat(), slot=slot,
                          registry_channel_id=channel_id, model=model, protocol=protocol, method=method,
                          due=epoch(day, clock, zone), deadline=deadline, dependency=dependency))
        return key
    health_keys = {}
    for index, clock in enumerate(config["health_times"]):
        for channel_id in ids:
            deadline_clock = config["health_times"][index + 1] if index < 3 else config["canary_time"]
            health_keys[index, channel_id] = add(f"health-{index}", channel_id, "health", config["health_model"],
                config["health_protocol"], clock, epoch(day, deadline_clock, zone))
    for index, clock in enumerate(config["astra_times"]):
        for channel_id in ids:
            add(f"astra-{index}", channel_id, "traceone", config["astra_model"], config["astra_protocol"], clock,
                epoch(day, config["day_deadline"], zone), health_keys[0 if index == 0 else 2, channel_id])
    for channel_id in ids:
        add("sol", channel_id, "traceone", config["sol_model"], config["sol_protocol"], config["sol_time"],
            epoch(day, config["day_deadline"], zone), health_keys[2, channel_id])
    if day.weekday() < 5:
        add("modeltrace", None, "modeltrace", config["astra_model"], config["astra_protocol"], config["modeltrace_time"],
            epoch(day, config["day_deadline"], zone))
        add("canary", None, "canary", config["astra_model"], config["astra_protocol"], config["canary_time"],
            epoch(day + timedelta(days=1), config["canary_deadline"], zone))
    return slots


def create_day(schedule, day, *, source="schedule"):
    init_tables()
    zone = ZoneInfo(schedule["timezone"])
    due = epoch(day, schedule["layered_config"]["health_times"][0], zone)
    run_id = storage.create_run(schedule, due, source=source)
    if run_id is None:
        with storage.cursor() as cur:
            row = cur.execute("SELECT id FROM runs WHERE schedule_id=? AND scheduled_for=? AND source=?", (schedule["id"], due, source)).fetchone()
        if not row:
            return None
        run_id = int(row[0])
        schedule = storage.get_run(run_id)["snapshot"]
    rows = build_slots(schedule, day)
    with storage.cursor() as cur:
        for row in rows:
            cur.execute("INSERT OR IGNORE INTO layered_slots(slot_key,run_id,plan_hash,budget_date,slot,registry_channel_id,model,protocol,method,due,deadline,dependency,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (row["slot_key"], run_id, row["plan_hash"], row["budget_date"], row["slot"], row["registry_channel_id"], row["model"], row["protocol"], row["method"], row["due"], row["deadline"], row["dependency"], time.time()))
        cur.execute("UPDATE runs SET notify_status='disabled' WHERE id=?", (run_id,))
    return run_id


def slots(run_id=None):
    init_tables()
    with storage.cursor() as cur:
        rows = [dict(row) for row in cur.execute("SELECT * FROM layered_slots" + (" WHERE run_id=?" if run_id else "") + " ORDER BY due,slot_key", (run_id,) if run_id else ())]
    for row in rows:
        row["summary"] = storage.loads(row.pop("summary_json"), {})
        row["target_snapshot"] = storage.loads(row.pop("target_json"), {})
    return rows


def update_slot(key, status, *, reason="", summary=None, job_id=None, target=None, channel_id=None, dependency=None):
    with storage.cursor() as cur:
        cur.execute("UPDATE layered_slots SET status=?,reason=?,summary_json=COALESCE(?,summary_json),job_id=COALESCE(?,job_id),target_json=COALESCE(?,target_json),registry_channel_id=COALESCE(?,registry_channel_id),dependency=COALESCE(?,dependency),updated_at=? WHERE slot_key=?",
                    (status, reason, storage.dumps(summary) if summary is not None else None, job_id,
                     storage.dumps(target) if target else None, channel_id, dependency, time.time(), key))


def select_rotation(schedule_id, day, healthy_ids, now):
    with storage.cursor() as cur:
        cur.execute("BEGIN IMMEDIATE")
        selected = cur.execute("SELECT registry_channel_id FROM layered_day_selection WHERE schedule_id=? AND budget_date=?", (schedule_id, day)).fetchone()
        if selected:
            return selected[0]
        served = {row[0]: row[1] for row in cur.execute("SELECT registry_channel_id,last_served FROM layered_rotation WHERE schedule_id=?", (schedule_id,))}
        channel_id = min(healthy_ids, key=lambda value: (served.get(value, 0), value)) if healthy_ids else None
        cur.execute("INSERT INTO layered_day_selection VALUES(?,?,?)", (schedule_id, day, channel_id))
        if channel_id:
            cur.execute("INSERT INTO layered_rotation VALUES(?,?,?) ON CONFLICT(schedule_id,registry_channel_id) DO UPDATE SET last_served=excluded.last_served", (schedule_id, channel_id, now))
        return channel_id


def health_gate(dependency, config, now):
    if not dependency or dependency["status"] not in TERMINAL:
        return "waiting"
    if dependency["status"] != "completed" or not dependency["summary"].get("health_pass"):
        return "health_failed_or_unmeasured"
    if now - dependency["summary"].get("observed_at", 0) > config["health_ttl_minutes"] * 60:
        return "health_expired"
    return "ready"


def list_baselines():
    init_tables()
    with storage.cursor() as cur:
        rows = [dict(row) for row in cur.execute("SELECT * FROM integrity_baselines ORDER BY id DESC")]
    for row in rows:
        row["score"] = storage.loads(row.pop("score_json"), {})
    return rows


def lock_baseline(slot_key, label):
    row = next((s for s in slots() if s["slot_key"] == slot_key), None)
    score = (row or {}).get("summary", {}).get("score", {})
    outcomes = score.get("outcomes", {})
    if not row or row["method"] != "canary" or row["status"] != "completed" or len(outcomes) != 192 or row["summary"].get("attempted") != 192 or not score.get("conditions"):
        raise ValueError("只能显式锁定完整、同条件的 192 项结果")
    with storage.cursor() as cur:
        cur.execute("INSERT OR IGNORE INTO integrity_baselines(source_slot_key,label,score_json,selected_at) VALUES(?,?,?,?)", (slot_key, label[:80], storage.dumps(score), time.time()))
        return cur.execute("SELECT id FROM integrity_baselines WHERE source_slot_key=?", (slot_key,)).fetchone()[0]


def record_incident(slot, score, config):
    conditions = score.get("conditions")
    valid = bool(score.get("valid_answers")) and slot["status"] == "completed"
    direction = score.get("prediction") if score.get("source_verdict") in {"MISMATCH", "SUSPICIOUS"} else None
    if not valid or not conditions or not direction or direction == slot["model"]:
        return None
    target = f"{slot['registry_channel_id']}:{slot['model']}:{slot['method']}"
    stamp = canonical_hash(conditions)
    # Include earlier days while retaining gaps as explicit sequence breaks.
    recent = [s for s in slots() if s["method"] == slot["method"] and s["model"] == slot["model"] and s["registry_channel_id"] == slot["registry_channel_id"] and s["due"] <= slot["due"]]
    sequence = sorted(recent, key=lambda s: s["due"], reverse=True)[:config["consecutive_slots"]]
    if len(sequence) != config["consecutive_slots"] or slot["due"] - sequence[-1]["due"] > config["incident_window_hours"] * 3600:
        return None
    for entry in sequence:
        evidence = entry["summary"].get("score", {})
        if entry["status"] != "completed" or not evidence.get("valid_answers") or evidence.get("prediction") != direction or canonical_hash(evidence.get("conditions")) != stamp:
            return None
    with storage.cursor() as cur:
        existing = cur.execute("SELECT incident_id FROM layered_incidents WHERE target_key=? AND conditions_hash=? AND direction=? AND created_at>?", (target, stamp, str(direction), time.time() - config["incident_cooldown_days"] * 86400)).fetchone()
        if existing:
            return existing[0]
        identity = "incident-" + canonical_hash([target, stamp, direction, slot["slot_key"]])[:24]
        cur.execute("INSERT INTO layered_incidents VALUES(?,?,?,?,?,?)", (identity, target, stamp, str(direction), slot["slot_key"], time.time()))
        return identity
