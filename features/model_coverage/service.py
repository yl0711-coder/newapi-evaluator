"""Derive coverage from saved discovery, actual schedules and measured observations."""
from __future__ import annotations

import hashlib
import json
import time

from shared.registry import Conflict, RegistryError, get_registry
from features.stability.app import storage, transport
from .catalog import Catalog


FRESH_SECONDS = 48 * 3600
SAMPLE_WINDOW_SECONDS = 7 * 86400
MIN_BATCHES = 3
RECENT_BATCHES = 5


def measured_state(observations, fingerprint, now):
    by_run = {}
    for item in observations:
        if item["connection_fingerprint"] == fingerprint:
            if item["run_id"] not in by_run:
                by_run[item["run_id"]] = {**item, "summary": {**item["summary"]}}
            else:
                summary = by_run[item["run_id"]]["summary"]
                other = item["summary"]
                summary["health_pass"] = bool(summary.get("health_pass") and other.get("health_pass"))
                for field in ("total", "completed"):
                    summary[field] = (summary.get(field) or 0) + (other.get(field) or 0)
                combined = dict(summary.get("failures") or {})
                for kind, count in (other.get("failures") or {}).items():
                    combined[kind] = combined.get(kind, 0) + count
                summary["failures"] = combined
    matching = list(by_run.values())
    base = {"status": "untested", "tested_at": None, "batches": 0, "samples": 0,
            "successes": 0, "speed": "unknown", "report_id": None, "failures": {}}
    if not matching:
        return {**base, "status": "connection_changed" if observations else "untested"}
    latest = matching[0]
    recent = [item for item in matching if now - item["tested_at"] <= SAMPLE_WINDOW_SECONDS][:RECENT_BATCHES]
    metrics = latest["summary"]
    speed = metrics.get("speed_assessment") or {}
    speed_status = speed.get("status", "unknown")
    if speed_status == "fixed":
        current, threshold = speed.get("current_p95_ms"), speed.get("threshold_p95_ms")
        speed_status = "unknown" if current is None or threshold is None else "slow" if current > threshold else "normal"
    failures = {}
    for item in recent:
        for kind, count in (item["summary"].get("failures") or {}).items():
            failures[kind] = failures.get(kind, 0) + count
    status = ("stale" if now - latest["tested_at"] > FRESH_SECONDS else "unstable" if any(not x["summary"].get("health_pass") for x in recent)
              else "stable" if len(recent) >= MIN_BATCHES else "observing")
    return {**base, "status": status, "tested_at": latest["tested_at"], "batches": len(recent),
            "samples": sum(x["summary"].get("total") or 0 for x in recent),
            "successes": sum(x["summary"].get("completed") or 0 for x in recent),
            "speed": speed_status, "report_id": latest["run_id"] if latest["report_available"] else None,
            "failures": failures}


def coverage():
    registry = get_registry()
    catalog = Catalog(registry)
    models, discoveries = catalog.models(), catalog.discoveries()
    mappings = catalog.mappings()
    targets, schedules = storage.list_channels(), storage.list_schedules()
    observations = storage.model_observations()
    indexed = {}
    for observation in observations:
        key = (observation["registry_channel_id"], observation["model"], observation["protocol"])
        indexed.setdefault(key, []).append(observation)
    rows = []
    for channel in registry.list():
        secret = registry.get(channel["id"], secret=True)
        stamp = registry.connection_fingerprint(secret)
        discovery = discoveries.get(channel["id"])
        valid_list = bool(discovery and discovery["succeeded_at"] and discovery["connection_fingerprint"] == stamp)
        ids = set(discovery["models"]) if valid_list else set()
        known = set()
        entries = []
        for model in models:
            binding = mappings.get((channel["id"], model["id"]), {})
            actual, protocol = binding.get("upstream_model", model["model"]), binding.get("protocol", model["protocol"])
            known.add(actual)
            matching = [t for t in targets if t["registry_channel_id"] == channel["id"] and t["model"] == actual and t["protocol"] == protocol]
            active_ids = {t["id"] for t in matching if t["enabled"]}
            linked = [s for s in schedules if any(t["id"] in s["channel_ids"] for t in matching)]
            active_plans = [s for s in linked if s["enabled"] and active_ids.intersection(s["channel_ids"])]
            enrollment = ("scheduled" if active_plans else "paused" if not channel["enabled"] or (matching and not active_ids)
                          or (linked and not any(s["enabled"] for s in linked)) else "unscheduled" if matching else "missing")
            availability = "listed" if actual in ids else "not_listed" if valid_list else "unknown"
            if discovery and discovery["error"]:
                availability = "fetch_failed"
            if discovery and discovery["succeeded_at"] and not valid_list:
                availability = "connection_changed"
            entries.append({**model, "channel_id": channel["id"], "upstream_model": actual, "protocol": protocol,
                            "availability": availability, "previously_listed": actual in ids,
                            "enrollment": enrollment, "schedules": [{"id": s["id"], "name": s["name"], "enabled": bool(s["enabled"])} for s in linked],
                            "measurement": measured_state(indexed.get((channel["id"], actual, protocol), []), stamp, time.time())})
        public_discovery = {k: discovery[k] for k in ("attempted_at", "succeeded_at", "error", "auth_kind")} if discovery else None
        rows.append({"channel_id": channel["id"], "models": entries, "discovery": public_discovery,
                     "discovered_models": sorted(ids), "new_models": sorted(ids - known)})
    return {"models": models, "channels": rows, "schedules": schedules,
            "rules": {"fresh_seconds": FRESH_SECONDS, "minimum_batches": MIN_BATCHES, "recent_batches": RECENT_BATCHES,
                      "sample_window_seconds": SAMPLE_WINDOW_SECONDS,
                      "requests_per_round": len(transport.PROBES)}}


def resolve_items(selections):
    registry = get_registry()
    catalog = Catalog(registry)
    resolved = {}
    for selected in selections:
        channel = registry.resolve(selected["channel_id"])
        binding = catalog.binding(channel["id"], selected["model_id"])
        actual, protocol = binding["upstream_model"], binding["request_protocol"]
        key = (channel["id"], actual, protocol)
        resolved[key] = {"channel_id": channel["id"], "model_id": binding["id"], "model": actual, "protocol": protocol,
                         "label": binding["label"], "connection_fingerprint": registry.connection_fingerprint(channel)}
    if not resolved:
        raise RegistryError("请选择至少一个模型")
    return list(resolved.values())


def plan_preview(schedule_id, selections, *, cursor=None):
    items = resolve_items(selections)
    if cursor is None:
        schedule = storage.get_schedule(schedule_id)
        targets = storage.list_channels()
    else:
        row = cursor.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        schedule = dict(row) if row else None
        if schedule:
            schedule["channel_ids"] = [row[0] for row in cursor.execute(
                "SELECT channel_id FROM schedule_channels WHERE schedule_id=? ORDER BY channel_id", (schedule_id,))]
        targets = [dict(row) for row in cursor.execute("SELECT * FROM channels ORDER BY name")]
    if not schedule:
        raise KeyError("定时计划不存在")
    if not schedule["enabled"]:
        raise RegistryError("请选择启用的定时计划")
    additions = []
    for item in items:
        matches = [t for t in targets if t["registry_channel_id"] == item["channel_id"] and t["model"] == item["model"] and t["protocol"] == item["protocol"]]
        matches.sort(key=lambda t: (not (t["id"] in schedule["channel_ids"]), not t["enabled"], t["id"]))
        target = matches[0] if matches else None
        if target and not target["enabled"]:
            if cursor is None:
                other_plans = storage.list_schedules(enabled_only=True)
                would_resume_others = any(s["id"] != schedule_id and target["id"] in s["channel_ids"] for s in other_plans)
            else:
                would_resume_others = cursor.execute("""SELECT 1 FROM schedules s JOIN schedule_channels sc
                    ON sc.schedule_id=s.id WHERE s.enabled=1 AND s.id<>? AND sc.channel_id=?""",
                                                    (schedule_id, target["id"])).fetchone() is not None
            if would_resume_others:
                raise RegistryError("该暂停目标还关联其他启用计划，请先在定时测试中恢复或调整目标")
        additions.append({**item, "target_id": target["id"] if target else None,
                          "action": "skip" if target and target["enabled"] and target["id"] in schedule["channel_ids"]
                          else "enable" if target and not target["enabled"] else "attach" if target else "create"})
    requests = sum(x["action"] != "skip" for x in additions) * schedule["rounds"] * len(transport.PROBES)
    digest = hashlib.sha256(json.dumps([schedule, additions], sort_keys=True).encode()).hexdigest()
    return {"schedule_id": schedule_id, "schedule_name": schedule["name"], "daily_times": schedule["daily_times"],
            "items": additions, "added_requests_per_run": requests,
            "added_requests_per_day": requests * len(schedule["daily_times"].split(",")), "preview_token": digest}


def ensure_targets(cur, items, *, enable_paused=True):
    ids = []
    for item in items:
        rows = cur.execute("""SELECT id,enabled FROM channels WHERE registry_channel_id=? AND model=? AND protocol=?
            ORDER BY enabled DESC,id""", (item["channel_id"], item["model"], item["protocol"])).fetchall()
        target_id = item.get("target_id") or (rows[0]["id"] if rows else None)
        now = time.time()
        if target_id:
            if not enable_paused and not next(row["enabled"] for row in rows if row["id"] == target_id):
                raise RegistryError("所选模型的测试目标已暂停，请先通过加入计划明确恢复目标")
            cur.execute("UPDATE channels SET enabled=1,updated_at=? WHERE id=? AND enabled=0", (now, target_id))
        else:
            suffix = hashlib.sha256(json.dumps([item["channel_id"], item["model"], item["protocol"]]).encode()).hexdigest()[:10]
            name = f"{item['label'][:42]} · #{item['channel_id']} · {suffix}"
            target_id = cur.execute("""INSERT INTO channels(name,base_url,model,protocol,api_key_enc,
                registry_channel_id,enabled,created_at,updated_at) VALUES(?,'',?,?,'',?,1,?,?)""",
                                    (name, item["model"], item["protocol"], item["channel_id"], now, now)).lastrowid
        ids.append(target_id)
    return ids


def enroll(schedule_id, selections, preview_token):
    with storage.cursor() as cur:
        cur.execute("BEGIN IMMEDIATE")
        # Preview is recomputed under the same storage lock as the enrollment transaction.
        preview = plan_preview(schedule_id, selections, cursor=cur)
        if preview["preview_token"] != preview_token:
            raise Conflict("渠道、模型或计划已变化，请重新预览")
        ids = ensure_targets(cur, preview["items"])
        existing = {row[0] for row in cur.execute("SELECT channel_id FROM schedule_channels WHERE schedule_id=?", (schedule_id,))}
        if len(existing | set(ids)) > 100:
            raise RegistryError("加入后计划超过 100 个测试目标，请选择其他计划")
        cur.executemany("INSERT OR IGNORE INTO schedule_channels VALUES(?,?)", ((schedule_id, value) for value in ids))
        if any(item["action"] != "skip" for item in preview["items"]):
            cur.execute("UPDATE schedules SET updated_at=? WHERE id=?", (time.time(), schedule_id))
    return {"target_ids": ids, "added": sum(item["action"] != "skip" for item in preview["items"]),
            "skipped": sum(item["action"] == "skip" for item in preview["items"])}


def verify_once(selections):
    items = resolve_items(selections)
    with storage.cursor() as cur:
        cur.execute("BEGIN IMMEDIATE")
        ids = ensure_targets(cur, items, enable_paused=False)
        for row in cur.execute("SELECT snapshot_json FROM runs WHERE status IN ('pending','running')"):
            if set(ids).intersection(storage.loads(row[0], {}).get("channel_ids", [])):
                raise Conflict("所选模型已有等待或运行中的测试")
        now = time.time()
        snapshot = {"id": None, "name": "常用模型单次验证", "timezone": "Asia/Shanghai", "channel_ids": ids,
                    "rounds": 1, "round_interval_seconds": 0, "max_concurrency": 2,
                    "notification_delay_seconds": 0, "min_success_rate": 0.95,
                    "max_timeout_rate": 0.05, "max_stream_break_rate": 0, "max_p95_ms": 30000,
                    "speed_threshold_mode": "adaptive", "speed_baseline_min_runs": 5, "speed_slow_ratio": 1.5,
                    "report_groups": []}
        row = cur.execute("""INSERT INTO runs(schedule_id,schedule_name,scheduled_for,source,status,snapshot_json,
            notify_status,created_at) VALUES(NULL,?,?,'coverage','pending',?,'skipped',?)""",
                          (snapshot["name"], now, storage.dumps(snapshot), now))
        return row.lastrowid
