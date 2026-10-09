"""Layered schedule consumer over the shared reserve/send/commit executor."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from shared.registry import get_registry, RegistryError
from features.integrity.execution import ResolvedTarget, execute_requests, build_requests
from features.integrity.durable import IntegrityStore, default_pricing
from features.integrity.transport import send_probe
from features.integrity.strategies import get_strategy
from features.integrity.scoring import project_observation, score_strategy
from . import layered, storage

_task = None


def store():
    return IntegrityStore(get_registry())


def resolve_target(slot, snapshot):
    registry = get_registry()
    target = next((t for t in snapshot["targets"] if t["registry_channel_id"] == slot["registry_channel_id"] and t["model"] == slot["model"] and t["protocol"] == slot["protocol"]), None)
    if not target:
        return None
    try:
        current = storage.get_channel(target["id"], include_secret=True)
        if not current or not current["enabled"] or any(current[k] != target[k] for k in ("registry_channel_id", "model", "protocol")):
            return None
        from features.integrity.execution import resolve_registry_target
        resolved = resolve_registry_target(registry, current["registry_channel_id"], current["model"],
                                           current["protocol"], require_online=True)
        bound = snapshot["layered_config"].get("target_bindings", {}).get(
            f"{current['registry_channel_id']}:{current['model']}:{current['protocol']}")
        if bound is None or resolved.snapshot != bound:
            return None
        return ResolvedTarget({**resolved.channel, "id": current["id"]},
                              {**resolved.snapshot, "target_id": str(current["id"])})
    except (RegistryError, KeyError):
        return None


def conditions(slot, manifest, config, target):
    return {"provider": f"registry:{slot['registry_channel_id']}:{target['connection_fingerprint']}",
            "model": slot["model"], "protocol": slot["protocol"],
            "parameters": {"reasoning_effort": config["reasoning_effort"], "stream": False,
                           "system_prompt_hash": layered.canonical_hash([p.system_prompt for p in manifest.probes]),
                           "wrapper": "integrity-transport-v1", "sampling": "fixed-probes",
                           "max_output_tokens": manifest.max_output_tokens, "retry_policy": "none"},
            "budget": {"max_requests": manifest.max_requests, "request_timeout_seconds": manifest.request_timeout_seconds,
                       "total_timeout_seconds": manifest.total_timeout_seconds,
                       "window": f"timetable-v2-{slot['deadline'] - slot['due']:g}" if "schedule_id" in slot else "overnight" if slot["method"] == "canary" else "daytime"}}


def slot_summary(slot, job, manifest, config, target):
    outputs = [{**r, "probe_id": r.get("probe_id", r.get("request_id"))} for r in job.get("results", [])]
    attempted = job["consumed"]["requests"]
    unknown = job["consumed"]["unknown_requests"]
    baseline = layered.get_baseline(config["baseline_id"]) if config.get("baseline_id") and slot["method"] == "canary" else None
    score = score_strategy(manifest, outputs, expected_model=slot["model"],
                           baseline=baseline if slot["method"] == "canary" else None,
                           conditions=conditions(slot, manifest, config, target))
    valid = sum(bool(r.get("valid")) for r in outputs)
    planned = len(manifest.probes)
    return {"planned": planned, "attempted": attempted, "valid": valid,
            "invalid": len(outputs) - valid, "unknown": unknown, "skipped": max(0, planned - attempted),
            "not_run": max(0, planned - attempted), "score": score,
            "health_pass": slot["method"] == "health" and job["status"] == "completed" and valid == 1,
            "health_coverage": {"model": slot["model"], "protocol": slot["protocol"]} if slot["method"] == "health" else None,
            "observed_at": job["updated_at"], "budget": job.get("consumed", {}), "fees": job.get("fees", {}),
            "pricing_status": job.get("fees", {}).get("pricing_status", "unknown"), "calibration_status": "unvalidated",
            "metadata_status": "unavailable", "baseline_id": config.get("baseline_id")}


def publish_slot(slot, job):
    """Rebuild publication from committed evidence without any sending gates."""
    run = storage.get_run(slot["run_id"])
    config = run["snapshot"]["layered_config"]
    manifest = get_strategy(slot["method"])
    summary = slot_summary(slot, job, manifest, config, job["target_snapshot"])
    final = job["status"]
    if final == "completed" and (summary["unknown"] or summary["not_run"]):
        final = "partially_completed"
    layered.update_slot(slot["slot_key"], final, reason=job["reason"], summary=summary,
                        job_id=job["job_id"], target=job["target_snapshot"])
    if final == "completed" and slot["method"] in {"traceone", "modeltrace"}:
        incident = layered.record_incident({**slot, "status": final, "summary": summary}, summary["score"], config)
        if incident:
            summary["incident_id"] = incident
            summary["review_suggestion"] = "人工复核建议；相关数字指纹不计独立多票"
            layered.update_slot(slot["slot_key"], final, summary=summary)


async def execute_slot(slot):
    run = storage.get_run(slot["run_id"])
    config = run["snapshot"]["layered_config"]
    manifest = get_strategy(slot["method"])
    requests = build_requests(manifest, config)
    ledger = store()
    job = ledger.job(slot["job_id"]) if slot.get("job_id") else ledger.job_by_key(slot["slot_key"])
    if job is not None and job["status"] in layered.TERMINAL:
        publish_slot(slot, job)
        return
    target = resolve_target(slot, run["snapshot"])
    if not target:
        layered.update_slot(slot["slot_key"], "skipped", reason="target_unavailable")
        return
    saved_target = slot.get("target_snapshot")
    if saved_target and saved_target != target.snapshot:
        layered.update_slot(slot["slot_key"], "rejected", reason="target_changed")
        return
    try:
        if slot.get("job_id"):
            job = ledger.job(slot["job_id"])
        else:
            job = ledger.job_by_key(slot["slot_key"])
            if job is not None:
                if job["target_snapshot"] != target.snapshot or job["strategy"].get("manifest_hash") != manifest.manifest_hash:
                    layered.update_slot(slot["slot_key"], "rejected", reason="target_or_strategy_changed")
                    return
                layered.update_slot(slot["slot_key"], "pending", job_id=job["job_id"], target=target.snapshot)
        if not slot.get("job_id") and job is None:
            day = datetime.fromisoformat(slot["budget_date"]).date()
            count = len(config["registry_channel_ids"])
            max_daily = count * 7 + (195 if day.weekday() < 5 else 0)
            job = ledger.enqueue(idempotency_key=slot["slot_key"], target_snapshot=target.snapshot,
                strategy={"strategy_id": manifest.strategy_id, "manifest_hash": manifest.manifest_hash}, requests=requests,
                limits={"max_requests": manifest.max_requests,
                        "max_input_tokens": sum(r.input_tokens_reserved for r in requests),
                        "max_output_tokens": sum(r.output_tokens_reserved for r in requests)},
                deadline=min(slot["deadline"], time.time() + manifest.total_timeout_seconds),
                pricing=default_pricing(slot["model"]), budget_scope="daily", budget_key="layered-default",
                budget_date=slot["budget_date"], timezone=run["snapshot"]["timezone"],
                daily_limits={"max_requests": max_daily, "max_input_tokens": 5000000, "max_output_tokens": 1000000},
                plan_version=slot["plan_hash"], principal="workbench")
            layered.update_slot(slot["slot_key"], "pending", job_id=job["job_id"], target=target.snapshot)
        claimed = ledger.claim(job_id=job["job_id"], owner="layered-scheduler")
        if claimed:
            layered.update_slot(slot["slot_key"], "running")
            def project(request, raw, started, finished):
                return {**project_observation(manifest, request.probe["id"], raw),
                        "input_tokens_reported": raw.get("input_tokens_reported"),
                        "output_tokens_reported": raw.get("output_tokens_reported"),
                        "reasoning_tokens_reported": raw.get("reasoning_tokens_reported"),
                        "duration_ms": raw.get("latency_ms")}
            await execute_requests(ledger.session(claimed["job_id"], claimed["owner"]), requests,
                lambda: resolve_target(slot, run["snapshot"]), send_probe, project)
        publish_slot(slot, ledger.job(job["job_id"]))
    except asyncio.CancelledError:
        # The shared attempt ledger marks any uncertain send as unknown on lease recovery.
        raise
    except (ValueError, RegistryError, KeyError) as exc:
        layered.update_slot(slot["slot_key"], "failed", reason=type(exc).__name__)


def refresh_run(run_id):
    run = storage.get_run(run_id)
    rows = layered.slots(run_id)
    counters = {k: 0 for k in ("planned", "attempted", "valid", "invalid", "unknown", "skipped", "not_run")}
    for slot in rows:
        summary = slot["summary"]
        if slot.get("job_id") and not summary:
            # Preserve already-permitted attempts even when a recovery window or
            # health gate prevents further sends and the slot has no published summary.
            job = store().job(slot["job_id"])
            summary = slot_summary(slot, job, get_strategy(slot["method"]),
                                   run["snapshot"]["layered_config"], job["target_snapshot"])
            layered.update_slot(slot["slot_key"], slot["status"], reason=slot["reason"], summary=summary)
        planned = len(get_strategy(slot["method"]).probes)
        for key in counters:
            counters[key] += summary.get(key, planned if key in {"planned", "skipped", "not_run"} else 0)
    terminal = all(s["status"] in layered.TERMINAL for s in rows)
    status = "cancelled" if run["status"] == "cancelled" else "completed" if terminal and all(s["status"] == "completed" for s in rows) else "incomplete" if terminal else "running"
    summary = {"plan_version": layered.VERSION, **counters, "calibration_status": "unvalidated",
               "metadata_status": "unavailable", "capability_coverage": "Astra only; Sol canary not scheduled",
               "pricing_status": "configured_estimate", "verdict": "incomplete" if status != "completed" else "observed",
               "daily_usage": store().daily_budget(budget_date=rows[0]["budget_date"], timezone=run["snapshot"]["timezone"]) if rows else None}
    with storage.cursor() as cur:
        cur.execute("UPDATE runs SET status=?,summary_json=?,notify_status='disabled',finished_at=? WHERE id=?", (status, storage.dumps(summary), time.time() if terminal else None, run_id))


def select_ready(now):
    rows = layered.slots()
    indexed = {s["slot_key"]: s for s in rows}
    for slot in rows:
        run = storage.get_run(slot["run_id"])
        if run["status"] == "cancelled" or slot["status"] in layered.TERMINAL:
            continue
        config = run["snapshot"]["layered_config"]
        if slot["due"] > now:
            continue
        if slot["deadline"] <= now:
            layered.update_slot(slot["slot_key"], "expired", reason="window_expired")
            continue
        if slot["method"] in {"modeltrace", "canary"} and not slot["registry_channel_id"]:
            health = [s for s in rows if s["run_id"] == slot["run_id"] and s["slot"] == "health-2"]
            if any(s["status"] not in layered.TERMINAL for s in health):
                continue
            healthy = [s["registry_channel_id"] for s in health if layered.health_gate(s, config, now) == "ready"]
            selected = layered.select_rotation(run["snapshot"]["id"], slot["budget_date"], healthy, now)
            if not selected:
                layered.update_slot(slot["slot_key"], "skipped", reason="no_healthy_rotation_target")
                continue
            health_slot = next((s for s in rows if s["run_id"] == slot["run_id"] and s["registry_channel_id"] == selected and s["slot"] == ("health-3" if slot["method"] == "canary" else "health-2")), None)
            layered.update_slot(slot["slot_key"], slot["status"], channel_id=selected, dependency=health_slot["slot_key"])
            slot = {**slot, "registry_channel_id": selected, "dependency": health_slot["slot_key"]}
        if slot["method"] != "health":
            dependency = indexed.get(slot["dependency"])
            gate = layered.health_gate(dependency, config, now)
            if gate == "waiting":
                continue
            if gate != "ready":
                layered.update_slot(slot["slot_key"], "skipped", reason=gate)
                continue
            previous = "astra-1" if slot["slot"] == "sol" else "sol" if slot["method"] == "modeltrace" else None
            if previous and any(s["run_id"] == slot["run_id"] and s["slot"] == previous and s["status"] not in layered.TERMINAL for s in rows):
                continue
        return slot
    return None


async def tick(now=None):
    global _task
    from features.integrity.service import executor_enabled
    if not executor_enabled():
        return
    now = time.time() if now is None else now
    from . import timetable_executor
    await timetable_executor.tick(now)
    layered.init_tables()
    store().recover_expired_leases()
    for schedule in storage.list_schedules(enabled_only=True):
        if schedule["plan_version"] != layered.VERSION:
            continue
        zone = ZoneInfo(schedule["timezone"])
        day = datetime.fromtimestamp(now, zone).date()
        due = layered.epoch(day, schedule["layered_config"]["health_times"][0], zone)
        if due >= schedule["created_at"] and now >= due:
            layered.create_day(schedule, day)
    # Any job completed before a process exit is read from the durable ledger, never cleared.
    for slot in layered.slots():
        if slot["status"] not in layered.TERMINAL or not slot["summary"]:
            ledger = store()
            job = ledger.job(slot["job_id"]) if slot.get("job_id") else ledger.job_by_key(slot["slot_key"])
            if job is None:
                continue
            if job["status"] in layered.TERMINAL:
                # Publication after a committed ledger is pure reconstruction. Do
                # not gate it on today's health, target, or expired sending window.
                publish_slot(slot, job)
                continue
            # Re-enter summary publication even when the job committed before process exit.
            # Claiming a terminal job sends nothing; its immutable results rebuild the slot.
            if slot["status"] not in layered.TERMINAL and (_task is None or _task.done()):
                layered.update_slot(slot["slot_key"], "pending", job_id=job["job_id"], target=job["target_snapshot"])
    if _task is None or _task.done():
        slot = select_ready(now)
        if slot:
            _task = asyncio.create_task(execute_slot(slot), name="layered-integrity-slot")
    for run_id in {s["run_id"] for s in layered.slots()}:
        refresh_run(run_id)


async def stop():
    global _task
    from . import timetable_executor
    await timetable_executor.stop()
    if _task and not _task.done():
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
    _task = None


def cancel_run(run_id):
    for slot in layered.slots(run_id):
        if slot.get("job_id"):
            store().cancel(slot["job_id"])
        if slot["status"] not in layered.TERMINAL:
            layered.update_slot(slot["slot_key"], "cancelled", reason="cancel_requested")
    with storage.cursor() as cur:
        cur.execute("UPDATE runs SET status='cancelled' WHERE id=?", (run_id,))


def resume_run(run_id):
    rows = layered.slots(run_id)
    if not any(s["deadline"] > time.time() and s["status"] in {"cancelled", "failed", "partially_completed"} for s in rows):
        raise ValueError("原时间窗已结束，不能延长或重发")
    for slot in rows:
        if slot["deadline"] <= time.time() or slot["status"] not in {"cancelled", "failed", "partially_completed"}:
            continue
        if slot.get("job_id"):
            store().resume(slot["job_id"])
        layered.update_slot(slot["slot_key"], "pending", reason="explicit_resume")
    with storage.cursor() as cur:
        cur.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
