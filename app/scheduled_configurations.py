"""精确连接配置的定时监测计划、版本和目标选择。"""
from __future__ import annotations

import hashlib
import json
import statistics
import time
from typing import Any

from fastapi import HTTPException

from . import store

MEASUREMENT_RULE_VERSION = "scheduled-measurement-v1"
THRESHOLD_VERSION = "scheduled-thresholds-v1"
MONITORING_THRESHOLDS = {
    "success_rate_drop": .10,
    "upstream_error_rate_rise": .05,
    "relative_speed_worsening": .25,
    "ttft_absolute_seconds": .5,
    "duration_absolute_seconds": 1.0,
}
MEASUREMENT_RULES = {
    "version": MEASUREMENT_RULE_VERSION,
    "requests_per_model": 10,
    "short_templates": 5,
    "medium_templates": 5,
    "ttft_min_valid": 8,
    "duration_min_valid": 4,
    "fixed_cooldown_seconds": 5,
    "replacement_limit": 1,
    "monitoring_thresholds": MONITORING_THRESHOLDS,
}
ANOMALY_PRIORITY = {
    "connectivity_anomaly": 4,
    "persistent": 3,
    "advisory": 2,
    "recovered": 1,
    "normal": 0,
}


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def primary_models() -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT primary_models.*,families.display_name FROM scheduled_primary_models primary_models "
        "LEFT JOIN model_family_models families ON families.model=primary_models.canonical_model "
        "AND families.archived_at IS NULL ORDER BY primary_models.sort_order,primary_models.canonical_model"
    )
    return [{
        "canonical_model": row["canonical_model"], "enabled": bool(row["enabled"]),
        "sort_order": row["sort_order"],
        "display_name": row.get("display_name") or row["canonical_model"],
    } for row in rows]


def enabled_primary_models() -> list[str]:
    return [row["canonical_model"] for row in primary_models() if row["enabled"]]


def _configuration(configuration_id: int) -> dict[str, Any]:
    row = store.get("channel_configurations", configuration_id)
    if not row:
        raise HTTPException(status_code=404, detail="精确连接配置不存在")
    return row


def _mappings(configuration_id: int) -> dict[str, dict[str, Any]]:
    rows = store.query(
        "SELECT * FROM configuration_model_mappings WHERE configuration_id=?",
        (configuration_id,),
    )
    return {row["canonical_model"]: row for row in rows}


def _configuration_out(configuration: dict[str, Any], models: list[str]) -> dict[str, Any]:
    mapping_rows = _mappings(configuration["id"])
    supported = [model for model in models if model in mapping_rows]
    family = store.get("model_families", configuration["family_id"])
    return {
        "id": configuration["id"], "channel_name": configuration["channel_name"],
        "display_name": configuration["display_name"],
        "family_id": configuration["family_id"],
        "family_name": (family or {}).get("name", "未分配家族"),
        "upstream_multiplier": configuration["upstream_multiplier"],
        "business_status": configuration["business_status"],
        "fingerprint": configuration["configuration_fingerprint"],
        "supported_primary_models": supported,
        "missing_primary_models": [model for model in models if model not in mapping_rows],
        "mappings": [{
            "canonical_model": model,
            "request_model": mapping_rows[model]["request_model"],
            "target_id": mapping_rows[model]["legacy_target_id"],
        } for model in supported],
    }


def configuration_options() -> dict[str, Any]:
    models = enabled_primary_models()
    rows = store.query(
        "SELECT * FROM channel_configurations WHERE business_status='online' "
        "ORDER BY family_id,upstream_multiplier,channel_name,display_name"
    )
    configurations = [_configuration_out(row, models) for row in rows]
    groups: dict[tuple[str, float], list[dict[str, Any]]] = {}
    for configuration in configurations:
        key = (configuration["family_name"], float(configuration["upstream_multiplier"]))
        groups.setdefault(key, []).append(configuration)
    return {
        "primary_models": primary_models(),
        "measurement_rules": MEASUREMENT_RULES,
        "threshold_version": THRESHOLD_VERSION,
        "groups": [{
            "family_name": family_name, "upstream_multiplier": multiplier,
            "label": f"{family_name} {multiplier:g}×", "configurations": configs,
        } for (family_name, multiplier), configs in groups.items()],
    }


def _validated_targets(configuration_ids: list[int], models: list[str]) -> list[dict[str, Any]]:
    unique = list(dict.fromkeys(int(value) for value in configuration_ids))
    if not unique:
        raise HTTPException(status_code=400, detail="定时计划至少需要选择一份已上线精确连接配置")
    output = []
    for configuration_id in unique:
        configuration = _configuration(configuration_id)
        if configuration["business_status"] != "online":
            raise HTTPException(status_code=400, detail="定时计划只能选择已上线精确连接配置")
        data = _configuration_out(configuration, models)
        if data["missing_primary_models"]:
            raise HTTPException(
                status_code=400,
                detail=(f"{configuration['display_name']} 缺少主测模型映射："
                        f"{', '.join(data['missing_primary_models'])}"),
            )
        output.append(configuration)
    return output


def _plan_snapshot(targets: list[dict[str, Any]], models: list[str]) -> dict[str, Any]:
    return {
        "configuration_fingerprints": [{
            "configuration_id": row["id"], "fingerprint": row["configuration_fingerprint"],
        } for row in targets],
        "primary_models": models,
        "measurement_rules": MEASUREMENT_RULES,
        "threshold_version": THRESHOLD_VERSION,
    }


def _current_version(schedule_id: int) -> dict[str, Any] | None:
    rows = store.query(
        "SELECT * FROM scheduled_plan_versions WHERE scheduled_test_id=? AND superseded_at IS NULL "
        "ORDER BY version DESC LIMIT 1",
        (schedule_id,),
    )
    return rows[0] if rows else None


def _new_version(schedule_id: int, user: dict[str, Any], reason: str, *, force: bool = False) -> dict[str, Any]:
    models = enabled_primary_models()
    if not models:
        raise HTTPException(status_code=400, detail="至少启用一个定时主测模型")
    links = store.query(
        "SELECT configuration_id FROM scheduled_configuration_targets WHERE scheduled_test_id=? ORDER BY id",
        (schedule_id,),
    )
    targets = _validated_targets([row["configuration_id"] for row in links], models)
    current = _current_version(schedule_id)
    now = time.time()
    next_version = int(current["version"]) + 1 if current else 1
    snapshot = _plan_snapshot(targets, models)
    if current:
        previous = {
            "configuration_fingerprints": store.loads(current["configuration_fingerprints_json"], []),
            "primary_models": store.loads(current["primary_models_json"], []),
            "measurement_rules": store.loads(current["measurement_rules_json"], {}),
            "threshold_version": current["threshold_version"],
        }
        if not force and _hash(previous) == _hash(snapshot):
            return current
        store.update("scheduled_plan_versions", current["id"], {"superseded_at": now})
    version_id = store.insert("scheduled_plan_versions", {
        "scheduled_test_id": schedule_id, "version": next_version,
        "configuration_fingerprints_json": store.dumps(snapshot["configuration_fingerprints"]),
        "primary_models_json": store.dumps(snapshot["primary_models"]),
        "measurement_rules_json": store.dumps(snapshot["measurement_rules"]),
        "threshold_version": snapshot["threshold_version"], "baseline_status": "building",
        "baseline_json": "{}", "created_by": user.get("id"), "reason": reason.strip(),
        "active_at": now, "created_at": now,
    })
    return store.get("scheduled_plan_versions", version_id) or {}


def _version_out(version: dict[str, Any] | None) -> dict[str, Any] | None:
    if not version:
        return None
    return {
        "id": version["id"], "version": version["version"],
        "configuration_fingerprints": store.loads(version["configuration_fingerprints_json"], []),
        "primary_models": store.loads(version["primary_models_json"], []),
        "measurement_rules": store.loads(version["measurement_rules_json"], {}),
        "threshold_version": version["threshold_version"],
        "baseline_status": version["baseline_status"],
        "baseline": store.loads(version["baseline_json"], {}),
        "created_at": version["created_at"], "reason": version["reason"],
    }


def schedule_out(schedule: dict[str, Any]) -> dict[str, Any]:
    models = enabled_primary_models()
    target_rows = store.query(
        "SELECT configuration_id FROM scheduled_configuration_targets WHERE scheduled_test_id=? ORDER BY id",
        (schedule["id"],),
    )
    targets = [_configuration_out(_configuration(row["configuration_id"]), models) for row in target_rows]
    minute = int(schedule["report_minute"])
    versions = store.query(
        "SELECT * FROM scheduled_plan_versions WHERE scheduled_test_id=? ORDER BY version DESC LIMIT 20",
        (schedule["id"],),
    )
    recent_runs = store.query(
        "SELECT id,run_date,status,report_at,task_ids,plan_version_id FROM scheduled_runs "
        "WHERE scheduled_test_id=? ORDER BY report_at DESC LIMIT 8",
        (schedule["id"],),
    )
    current_version = _current_version(schedule["id"])
    current_report_tasks: list[dict[str, Any]] = []
    for run in recent_runs:
        if run["status"] != "complete" or not current_version \
                or run.get("plan_version_id") != current_version["id"]:
            continue
        tasks = [store.get("tasks", task_id) for task_id in store.loads(run["task_ids"], [])]
        if tasks and all(task and task.get("status") == "success" and (store.task_report(task, {}) or {}).get("kind") == "scheduled_measurement" for task in tasks):
            current_report_tasks = [{"task_id": task["id"], "label": task["target_name"]} for task in tasks if task]
            break
    anomaly_rows = store.query(
        "SELECT * FROM scheduled_anomaly_states WHERE scheduled_test_id=? "
        "AND plan_version_id=? ORDER BY configuration_id,canonical_model,category",
        (schedule["id"], current_version["id"] if current_version else -1),
    )
    anomaly_states = [{
        "configuration_id": row["configuration_id"], "canonical_model": row["canonical_model"],
        "category": row["category"], "state": row["state"],
        "details": store.loads(row["details_json"], {}), "updated_at": row["last_changed_at"],
    } for row in anomaly_rows]
    monitoring_state = max(
        (row["state"] for row in anomaly_states), key=lambda value: ANOMALY_PRIORITY.get(value, 0), default="normal",
    )
    return {
        "id": schedule["id"], "name": schedule["name"],
        "report_time": f"{minute // 60:02d}:{minute % 60:02d}",
        "feishu_webhook_ids": store.loads(schedule["feishu_webhook_ids"], []),
        "email_recipient_ids": store.loads(schedule["email_recipient_ids"], []),
        "enabled": bool(schedule["enabled"]), "paused_at": schedule.get("paused_at"),
        "pause_reason": schedule.get("pause_reason") or "",
        "missed_runs": int(schedule.get("missed_runs") or 0),
        "configuration_targets": targets, "current_version": _version_out(current_version),
        "version_history": [_version_out(version) for version in versions],
        "recent_runs": [{key: row[key] for key in ("id", "run_date", "status", "report_at", "plan_version_id")} for row in recent_runs],
        "current_report_tasks": current_report_tasks,
        "monitoring_state": monitoring_state,
        "affected_model_count": len({(row["configuration_id"], row["canonical_model"])
                                     for row in anomaly_states if row["state"] != "normal"}),
        "anomaly_states": anomaly_states,
        "created_at": schedule["created_at"], "updated_at": schedule["updated_at"],
    }


def list_schedules() -> list[dict[str, Any]]:
    return [schedule_out(schedule) for schedule in store.query(
        "SELECT * FROM scheduled_tests WHERE EXISTS (SELECT 1 FROM scheduled_configuration_targets targets "
        "WHERE targets.scheduled_test_id=scheduled_tests.id) ORDER BY report_minute,name"
    )]


def create_schedule(body: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    models = enabled_primary_models()
    targets = _validated_targets(body["configuration_ids"], models)
    now = time.time()
    schedule_id = store.insert("scheduled_tests", {
        "name": body["name"].strip(), "report_minute": int(body["report_minute"]),
        "feishu_webhook_ids": store.dumps(list(dict.fromkeys(body.get("feishu_webhook_ids") or []))),
        "email_recipient_ids": store.dumps(list(dict.fromkeys(body.get("email_recipient_ids") or []))),
        "enabled": 1 if body.get("enabled", True) else 0, "created_at": now, "updated_at": now,
    })
    for target in targets:
        store.insert("scheduled_configuration_targets", {
            "scheduled_test_id": schedule_id, "configuration_id": target["id"], "created_at": now,
        })
    _new_version(schedule_id, user, "创建精确连接配置定时计划")
    return schedule_out(store.get("scheduled_tests", schedule_id) or {})


def update_schedule(schedule_id: int, body: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    schedule = store.get("scheduled_tests", schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="定时计划不存在")
    models = enabled_primary_models()
    targets = _validated_targets(body["configuration_ids"], models)
    now = time.time()
    store.update("scheduled_tests", schedule_id, {
        "name": body["name"].strip(), "report_minute": int(body["report_minute"]),
        "feishu_webhook_ids": store.dumps(list(dict.fromkeys(body.get("feishu_webhook_ids") or []))),
        "email_recipient_ids": store.dumps(list(dict.fromkeys(body.get("email_recipient_ids") or []))),
        "enabled": 1 if body.get("enabled", True) else 0,
        "paused_at": None, "pause_reason": "", "updated_at": now,
    })
    current_ids = [row["configuration_id"] for row in store.query(
        "SELECT configuration_id FROM scheduled_configuration_targets WHERE scheduled_test_id=?", (schedule_id,)
    )]
    desired_ids = [row["id"] for row in targets]
    if current_ids != desired_ids:
        store.execute("DELETE FROM scheduled_configuration_targets WHERE scheduled_test_id=?", (schedule_id,))
        for target in targets:
            store.insert("scheduled_configuration_targets", {
                "scheduled_test_id": schedule_id, "configuration_id": target["id"], "created_at": now,
            })
        _new_version(schedule_id, user, "调整精确连接配置或主测条件")
    return schedule_out(store.get("scheduled_tests", schedule_id) or {})


def set_primary_models(models: list[str], user: dict[str, Any], reason: str) -> list[dict[str, Any]]:
    unique = list(dict.fromkeys(model.strip() for model in models if model.strip()))
    if not unique:
        raise HTTPException(status_code=400, detail="至少需要一个定时主测模型")
    valid = {
        row["model"] for row in store.query(
            "SELECT model FROM model_family_models WHERE enabled=1 AND archived_at IS NULL"
        )
    }
    invalid = sorted(set(unique) - valid)
    if invalid:
        raise HTTPException(status_code=400, detail=f"主测模型尚未在模型家族中启用：{', '.join(invalid)}")
    now = time.time()
    store.execute("UPDATE scheduled_primary_models SET enabled=0,updated_at=?", (now,))
    for order, model in enumerate(unique):
        existing = store.get("scheduled_primary_models", model, key="canonical_model")
        if existing:
            store.execute(
                "UPDATE scheduled_primary_models SET enabled=1,sort_order=?,updated_at=? WHERE canonical_model=?",
                (order, now, model),
            )
        else:
            store.insert("scheduled_primary_models", {
                "canonical_model": model, "enabled": 1, "sort_order": order, "updated_at": now,
            })
    for schedule in store.query(
        "SELECT * FROM scheduled_tests WHERE EXISTS (SELECT 1 FROM scheduled_configuration_targets targets "
        "WHERE targets.scheduled_test_id=scheduled_tests.id)"
    ):
        try:
            _new_version(schedule["id"], user, reason.strip())
        except HTTPException as exc:
            pause(schedule["id"], str(exc.detail))
    return primary_models()


def rebuild_baseline(schedule_id: int, user: dict[str, Any], reason: str) -> dict[str, Any]:
    schedule = store.get("scheduled_tests", schedule_id)
    if not schedule or not store.query(
        "SELECT id FROM scheduled_configuration_targets WHERE scheduled_test_id=? LIMIT 1", (schedule_id,)
    ):
        raise HTTPException(status_code=404, detail="精确连接配置定时计划不存在")
    clean_reason = reason.strip()
    if not clean_reason:
        raise HTTPException(status_code=400, detail="重建基线必须说明原因")
    _new_version(schedule_id, user, clean_reason, force=True)
    store.update("scheduled_tests", schedule_id, {"updated_at": time.time()})
    return schedule_out(store.get("scheduled_tests", schedule_id) or {})


def pause(schedule_id: int, reason: str) -> None:
    schedule = store.get("scheduled_tests", schedule_id)
    if not schedule:
        return
    missed = int(schedule.get("missed_runs") or 0) + (0 if schedule.get("paused_at") else 1)
    store.update("scheduled_tests", schedule_id, {
        "enabled": 0, "paused_at": time.time(), "pause_reason": reason[:1000],
        "missed_runs": missed, "updated_at": time.time(),
    })
    store.record_system_alert(
        f"scheduled-plan-paused:{schedule_id}", "scheduled_plan_paused",
        f"定时计划“{schedule['name']}”已暂停", reason[:500], "warning",
    )


def active_snapshot(schedule_id: int) -> dict[str, Any] | None:
    schedule = store.get("scheduled_tests", schedule_id)
    if not schedule or not schedule["enabled"] or schedule.get("paused_at"):
        return None
    version = _current_version(schedule_id)
    if not version:
        pause(schedule_id, "缺少有效的定时计划版本，请重新保存目标配置")
        return None
    models = store.loads(version["primary_models_json"], [])
    links = store.query(
        "SELECT configuration_id FROM scheduled_configuration_targets WHERE scheduled_test_id=? ORDER BY id",
        (schedule_id,),
    )
    try:
        targets = _validated_targets([row["configuration_id"] for row in links], models)
    except HTTPException as exc:
        pause(schedule_id, str(exc.detail))
        return None
    expected = store.loads(version["configuration_fingerprints_json"], [])
    actual = [{"configuration_id": row["id"], "fingerprint": row["configuration_fingerprint"]} for row in targets]
    if _hash(expected) != _hash(actual):
        pause(schedule_id, "精确连接配置已变化；请重新选择目标并生成新的计划版本")
        return None
    return {
        "schedule": schedule, "version": version, "models": models,
        "targets": [_configuration_out(target, models) for target in targets],
        "measurement_rules": store.loads(version["measurement_rules_json"], {}),
    }


def _eligible_report_model(model: dict[str, Any]) -> bool:
    speed = model.get("speed") or {}
    return bool(
        model.get("stability_denominator")
        and not model.get("attribution_pending_count")
        and speed.get("ttft_median") is not None
        and speed.get("short_duration_p95") is not None
        and speed.get("medium_duration_p95") is not None
    )


def _report_tasks_for_version(schedule_id: int, version_id: int) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Read successful, integrity-checked active report revisions in chronological run order."""
    output: list[tuple[dict[str, Any], dict[str, Any]]] = []
    runs = store.query(
        "SELECT * FROM scheduled_runs WHERE scheduled_test_id=? AND plan_version_id=? "
        "AND status IN ('complete','testing') ORDER BY report_at,id",
        (schedule_id, version_id),
    )
    for run in runs:
        for task_id in store.loads(run["task_ids"], []):
            task = store.get("tasks", task_id)
            if not task or task.get("status") != "success":
                continue
            report = store.task_report(task, {}) or {}
            if report.get("kind") != "scheduled_measurement" or not (report.get("integrity") or {}).get("ok"):
                continue
            output.append((run, {"task": task, "report": report}))
    return output


def _metric_hit(current: float | None, baseline: float | None, *, relative: float, absolute: float) -> bool:
    if current is None or baseline is None:
        return False
    return current >= baseline * (1 + relative) and current - baseline >= absolute


def _model_hits(
    model: dict[str, Any], baseline: dict[str, Any] | None, allow_relative: bool,
    thresholds: dict[str, float],
) -> dict[str, dict[str, Any]]:
    connection_errors = model.get("connection_errors") or []
    hits: dict[str, dict[str, Any]] = {
        "connection": {"hit": bool(connection_errors), "connection_errors": connection_errors},
        "stability": {"hit": False, "metrics": []},
        "speed": {"hit": False, "metrics": []},
    }
    if not baseline or not allow_relative:
        return hits
    stability_metrics = []
    if model.get("success_rate") is not None and baseline.get("success_rate") is not None \
            and float(baseline["success_rate"]) - float(model["success_rate"]) >= thresholds["success_rate_drop"]:
        stability_metrics.append("success_rate")
    if model.get("upstream_error_rate") is not None and baseline.get("upstream_error_rate") is not None \
            and float(model["upstream_error_rate"]) - float(baseline["upstream_error_rate"]) >= thresholds["upstream_error_rate_rise"]:
        stability_metrics.append("upstream_error_rate")
    hits["stability"] = {"hit": bool(stability_metrics), "metrics": stability_metrics}
    speed = model.get("speed") or {}
    speed_metrics = []
    if _metric_hit(speed.get("ttft_median"), baseline.get("ttft_median"),
                   relative=thresholds["relative_speed_worsening"], absolute=thresholds["ttft_absolute_seconds"]):
        speed_metrics.append("ttft_median")
    if _metric_hit(speed.get("short_duration_p95"), baseline.get("short_duration_p95"),
                   relative=thresholds["relative_speed_worsening"], absolute=thresholds["duration_absolute_seconds"]):
        speed_metrics.append("short_duration_p95")
    if _metric_hit(speed.get("medium_duration_p95"), baseline.get("medium_duration_p95"),
                   relative=thresholds["relative_speed_worsening"], absolute=thresholds["duration_absolute_seconds"]):
        speed_metrics.append("medium_duration_p95")
    hits["speed"] = {"hit": bool(speed_metrics), "metrics": speed_metrics}
    return hits


def _next_anomaly_state(category: str, previous: str, consecutive: int, hit: bool) -> tuple[str, int]:
    if category == "connection":
        if hit:
            return "connectivity_anomaly", consecutive + 1
        if previous == "connectivity_anomaly":
            return "recovered", 0
        return ("normal", 0) if previous == "recovered" else (previous, 0)
    if hit:
        return ("persistent" if consecutive + 1 >= 2 else "advisory", consecutive + 1)
    if previous in {"advisory", "persistent"}:
        return "recovered", 0
    if previous == "recovered":
        return "normal", 0
    return "normal", 0


def _store_anomaly_state(
    schedule_id: int, version_id: int, configuration_id: int, model: str, category: str,
    state: str, consecutive: int, task: dict[str, Any], report_version: int, details: dict[str, Any], reason: str,
) -> dict[str, Any] | None:
    existing = store.query(
        "SELECT * FROM scheduled_anomaly_states WHERE plan_version_id=? AND configuration_id=? "
        "AND canonical_model=? AND category=?",
        (version_id, configuration_id, model, category),
    )
    now = time.time()
    if not existing:
        state_id = store.insert("scheduled_anomaly_states", {
            "scheduled_test_id": schedule_id, "plan_version_id": version_id,
            "configuration_id": configuration_id, "canonical_model": model, "category": category,
            "state": state, "consecutive_hits": consecutive, "last_task_id": task["id"],
            "last_report_version": report_version, "last_changed_at": now,
            "details_json": store.dumps(details),
        })
        if state == "normal":
            return None
        store.insert("scheduled_anomaly_events", {
            "anomaly_state_id": state_id, "task_id": task["id"], "report_version": report_version,
            "previous_state": "normal", "next_state": state, "details_json": store.dumps(details),
            "reason": reason, "created_at": now,
        })
        return {"state": state, "category": category, "model": model, "configuration_id": configuration_id}
    row = existing[0]
    changed = row["state"] != state
    store.update("scheduled_anomaly_states", row["id"], {
        "state": state, "consecutive_hits": consecutive, "last_task_id": task["id"],
        "last_report_version": report_version, "last_changed_at": now if changed else row["last_changed_at"],
        "details_json": store.dumps(details),
    })
    if not changed:
        return None
    store.insert("scheduled_anomaly_events", {
        "anomaly_state_id": row["id"], "task_id": task["id"], "report_version": report_version,
        "previous_state": row["state"], "next_state": state, "details_json": store.dumps(details),
        "reason": reason, "created_at": now,
    })
    return {"state": state, "previous_state": row["state"], "category": category,
            "model": model, "configuration_id": configuration_id}


def recalculate_anomalies(schedule_id: int, version_id: int, *, reason: str) -> list[dict[str, Any]]:
    version = store.get("scheduled_plan_versions", version_id)
    if not version:
        return []
    baseline = store.loads(version["baseline_json"], {})
    thresholds = {
        **MONITORING_THRESHOLDS,
        **(store.loads(version["measurement_rules_json"], {}).get("monitoring_thresholds") or {}),
    }
    source_runs = set(baseline.get("source_runs") or [])
    histories: dict[tuple[int, str, str], tuple[str, int]] = {}
    finals: dict[tuple[int, str, str], dict[str, Any]] = {}
    for run, item in _report_tasks_for_version(schedule_id, version_id):
        task, report = item["task"], item["report"]
        for model in report.get("models") or []:
            key_base = (int(report["configuration_id"]), str(model["canonical_model"]))
            baseline_model = (baseline.get("models") or {}).get(f"{key_base[0]}:{key_base[1]}")
            hits = _model_hits(model, baseline_model, run["id"] not in source_runs, thresholds)
            for category, details in hits.items():
                key = (*key_base, category)
                previous, consecutive = histories.get(key, ("normal", 0))
                state, next_consecutive = _next_anomaly_state(category, previous, consecutive, bool(details["hit"]))
                histories[key] = (state, next_consecutive)
                finals[key] = {
                    "state": state, "consecutive": next_consecutive, "task": task,
                    "report_version": int(report.get("report_version") or 1), "details": details,
                }
    changes = []
    for (configuration_id, model, category), value in finals.items():
        change = _store_anomaly_state(
            schedule_id, version_id, configuration_id, model, category, value["state"], value["consecutive"],
            value["task"], value["report_version"], value["details"], reason,
        )
        if change:
            changes.append(change)
    return changes


def recalculate_anomalies_for_task(task_id: int) -> list[dict[str, Any]]:
    task = store.get("tasks", task_id)
    snapshot = store.loads((task or {}).get("snapshot"), {})
    run = store.get("scheduled_runs", snapshot.get("scheduled_run_id")) if snapshot.get("scheduled_run_id") else None
    if not run or not run.get("plan_version_id"):
        return []
    return recalculate_anomalies(run["scheduled_test_id"], run["plan_version_id"], reason="归因修正后的报告版本重新计算")


def anomaly_notification_text(schedule: dict[str, Any], changes: list[dict[str, Any]]) -> str:
    labels = {
        "connectivity_anomaly": "定时连接异常（高优先级）",
        "persistent": "持续异常",
        "advisory": "异常提示",
        "recovered": "已恢复",
    }
    relevant = [change for change in changes if change["state"] in labels]
    lines = [f"定时监测状态变更：{schedule['name']}"]
    for change in relevant:
        configuration = store.get("channel_configurations", change["configuration_id"]) or {}
        lines.append(
            f"- {configuration.get('display_name', change['configuration_id'])} · "
            f"{change['model']} · {change['category']}：{labels[change['state']]}"
        )
    return "\n".join(lines)


def record_completed_run(run: dict[str, Any], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只在当前计划版本尚未形成基线时收集前三份完整封存报告。"""
    version_id = run.get("plan_version_id")
    if not version_id:
        return []
    version = store.get("scheduled_plan_versions", int(version_id))
    if not version:
        return []
    if any(task.get("status") != "success" for task in tasks):
        return recalculate_anomalies(run["scheduled_test_id"], int(version_id), reason="封存报告更新")
    if version["baseline_status"] == "ready":
        return recalculate_anomalies(run["scheduled_test_id"], int(version_id), reason="封存报告更新")
    candidates: list[dict[str, Any]] = []
    complete_runs = store.query(
        "SELECT * FROM scheduled_runs WHERE plan_version_id=? AND status IN ('complete','testing') ORDER BY report_at ASC",
        (version_id,),
    )
    for completed in complete_runs:
        task_ids = store.loads(completed["task_ids"], [])
        task_reports = [(task_id, store.get("tasks", task_id)) for task_id in task_ids]
        reports = [(task_id, store.task_report(task, {})) for task_id, task in task_reports if task]
        if not reports or any(report.get("kind") != "scheduled_measurement" or not (report.get("integrity") or {}).get("ok") for _, report in reports):
            continue
        flattened = []
        for task_id, report in reports:
            for model in report.get("models") or []:
                flattened.append({
                    "configuration_id": report.get("configuration_id"), "task_id": task_id,
                    **model,
                })
        if flattened and all(_eligible_report_model(model) for model in flattened):
            candidates.append({"run_id": completed["id"], "models": flattened})
    if len(candidates) < 3:
        return recalculate_anomalies(run["scheduled_test_id"], int(version_id), reason="封存报告更新")
    selected = candidates[:3]
    keys = {(model["configuration_id"], model["canonical_model"]) for report in selected for model in report["models"]}
    baseline: dict[str, Any] = {"source_runs": [report["run_id"] for report in selected], "models": {}}
    for configuration_id, model_name in keys:
        values = [
            next((model for model in report["models"] if model["configuration_id"] == configuration_id and model["canonical_model"] == model_name), None)
            for report in selected
        ]
        if any(value is None for value in values):
            return recalculate_anomalies(run["scheduled_test_id"], int(version_id), reason="封存报告更新")
        entries = [value for value in values if value]
        baseline["models"][f"{configuration_id}:{model_name}"] = {
            "configuration_id": configuration_id, "canonical_model": model_name,
            "success_rate": statistics.median([float(entry["success_rate"]) for entry in entries]),
            "upstream_error_rate": statistics.median([float(entry["upstream_error_rate"]) for entry in entries]),
            "ttft_median": statistics.median([float(entry["speed"]["ttft_median"]) for entry in entries]),
            "short_duration_p95": statistics.median([float(entry["speed"]["short_duration_p95"]) for entry in entries]),
            "medium_duration_p95": statistics.median([float(entry["speed"]["medium_duration_p95"]) for entry in entries]),
            "source_reports": [{"run_id": report["run_id"]} for report in selected],
        }
    store.update("scheduled_plan_versions", int(version_id), {
        "baseline_status": "ready", "baseline_json": store.dumps(baseline),
    })
    return recalculate_anomalies(run["scheduled_test_id"], int(version_id), reason="初始监测基线已固定")
