"""One probe per dispatch; priority is chosen before acquiring shared HTTP capacity."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from shared.registry import get_registry, RegistryError
from features.integrity.execution import build_requests, execute_requests
from features.integrity.durable import IntegrityStore, default_pricing
from features.integrity.strategies import get_strategy
from features.integrity.scoring import project_observation
from features.integrity.transport import send_probe, send_capacity, send_probe_with_capacity
from . import timetable, storage, integrity, layered

_task = None
_reconciled = {}


@lru_cache(maxsize=3)
def strategy(method):
    return get_strategy(method)


@lru_cache(maxsize=3)
def fixed_requests(method):
    return build_requests(strategy(method), timetable.DEFAULTS)


def store():
    return IntegrityStore(get_registry())


def ensure_days(now):
    timetable.synchronize_controls()
    for schedule in storage.list_schedules(enabled_only=True):
        if schedule["plan_version"] != timetable.VERSION:
            continue
        day = datetime.fromtimestamp(now, ZoneInfo(schedule["timezone"])).date()
        key = (schedule["id"], str(day))
        if _reconciled.get(key) != schedule["updated_at"]:
            run_id = timetable.reconcile(schedule, day, now=now)
            if run_id is not None:
                _reconciled[key] = schedule["updated_at"]
                refresh_run(run_id)
    if len(_reconciled) > 1000:
        _reconciled.clear()


def publish(slot, job):
    manifest = strategy(slot["method"])
    config = slot["snapshot"]["layered_config"]
    baseline_id = config.get("baseline_ids", {}).get(str(slot["registry_channel_id"]), config.get("baseline_id") if len(config["registry_channel_ids"]) == 1 else None)
    summary = integrity.slot_summary(slot, job, manifest, {**config, "baseline_id": baseline_id}, job["target_snapshot"])
    with get_registry().connect() as conn:
        span = conn.execute("SELECT MIN(created_at),MAX(completed_at) FROM integrity_attempts WHERE job_id=?", (job["job_id"],)).fetchone()
    summary["sampling_started_at"] = span[0]
    summary["sampling_finished_at"] = span[1] if not summary["unknown"] else job["updated_at"]
    summary["sampling_end_basis"] = "local_wait_ended_upstream_unknown" if summary["unknown"] else "confirmed_results"
    summary["request_errors"] = sum(r.get("status", "completed") not in {"completed", "success", "ok"} for r in job["results"])
    final = job["status"]
    if final == "completed" and (summary["unknown"] or summary["not_run"]):
        final = "partially_completed"
    timetable.update_slot(slot["slot_key"], final, reason=job["reason"], summary=summary, job_id=job["job_id"])
    refresh_run(slot["run_id"])


def publish_progress(slot, job_id):
    """Read bounded numeric projections for this job, without rescoring its answers."""
    with get_registry().connect() as conn:
        row = conn.execute("""SELECT COUNT(*),SUM(status='completed'),SUM(status='unknown'),
            SUM(CASE WHEN status='completed' THEN COALESCE(json_extract(result_json,'$.valid'),0) ELSE 0 END),
            SUM(CASE WHEN status='completed' THEN COALESCE(json_extract(result_json,'$.correct'),0) ELSE 0 END),
            SUM(CASE WHEN status='completed' AND COALESCE(json_extract(result_json,'$.status'),'completed') NOT IN ('completed','success','ok') THEN 1 ELSE 0 END),
            MIN(created_at),MAX(completed_at) FROM integrity_attempts WHERE job_id=?""", (job_id,)).fetchone()
    attempted, confirmed, unknown, valid, correct, errors, started, finished = row
    planned = timetable.request_limits(slot["method"])["max_requests"]
    config = slot["snapshot"]["layered_config"]
    baseline = config.get("baseline_ids", {}).get(str(slot["registry_channel_id"]))
    summary = {"planned": planned, "attempted": attempted, "valid": valid or 0,
               "invalid": (confirmed or 0) - (valid or 0), "unknown": unknown or 0,
               "not_run": planned - attempted, "request_errors": errors or 0, "baseline_id": baseline,
               "score": {"status": "incomplete", "correct": correct or 0, "total": planned},
               "sampling_started_at": started, "sampling_finished_at": finished,
               "sampling_end_basis": "latest_confirmed_result_batch_in_progress", "calibration_status": "unvalidated"}
    timetable.update_slot(slot["slot_key"], "pending", summary=summary, last_probe=time.time())
    refresh_run(slot["run_id"])


def refresh_run(run_id):
    keys = ("planned", "attempted", "valid", "invalid", "unknown", "not_run")
    planned = "CASE method WHEN 'canary' THEN 192 WHEN 'modeltrace' THEN 3 ELSE 1 END"
    sums = ",".join(f"COALESCE(SUM(COALESCE(json_extract(summary_json,'$.{k}'),{planned if k in {'planned', 'not_run'} else 0})),0)" for k in keys)
    with get_registry().connect() as conn:
        counts = conn.execute("SELECT COUNT(*),SUM(status NOT IN ('completed','partially_completed','cancelled','expired','rejected','failed','skipped')),SUM(status!='completed')," + sums + " FROM integrity_occurrences WHERE run_id=?", (run_id,)).fetchone()
        day = conn.execute("SELECT * FROM integrity_timetable_days WHERE run_id=?", (run_id,)).fetchone()
    total, active, not_completed = counts[:3]
    counters = dict(zip(keys, counts[3:]))
    terminal = not active
    status = "cancelled" if day["cancelled"] else "completed" if terminal and not not_completed else "incomplete" if terminal else "running"
    summary = {"plan_version": timetable.VERSION, **counters, "empty_plan": not total,
               "verdict": "unmeasured" if not total else "observed" if status == "completed" else "incomplete",
               "calibration_status": "unvalidated", "metadata_status": "unavailable", "cost_stopping_limit": None}
    with storage.cursor() as cur:
        cur.execute("UPDATE runs SET status=?,summary_json=?,notify_status='disabled',started_at=COALESCE(started_at,?),finished_at=? WHERE id=?", (status, storage.dumps(summary), time.time(), time.time() if terminal else None, run_id))


def select_ready(now):
    registry = get_registry()
    with registry.connect() as conn:
        candidates = [dict(r) for r in conn.execute("""SELECT o.slot_key,o.dependency FROM integrity_occurrences o
          JOIN integrity_timetable_days d ON d.run_id=o.run_id
          WHERE o.status='pending' AND o.due<=? AND o.deadline>? AND d.cancelled=0
          ORDER BY o.priority,o.deadline,o.last_probe,o.registry_channel_id,o.due LIMIT 64""", (now, now))]
    for slot in candidates:
        if slot["dependency"]:
            dependency = timetable.get_slot(slot["dependency"])
            if dependency["status"] not in timetable.TERMINAL:
                continue
            if dependency["status"] != "completed" or not dependency["summary"].get("health_pass"):
                full = timetable.get_slot(slot["slot_key"])
                timetable.update_slot(slot["slot_key"], "skipped", reason="health_failed_or_unmeasured")
                refresh_run(full["run_id"])
                continue
        return timetable.get_slot(slot["slot_key"])
    return None


def settle_pending(now):
    """Bounded recovery/expiry reads never parse complete historical job results."""
    registry = get_registry()
    with registry.connect() as conn:
        rows = [timetable.decode(r) for r in conn.execute("""SELECT o.* FROM integrity_occurrences o
          LEFT JOIN integrity_jobs j ON j.job_id=o.job_id
          WHERE o.status IN ('pending','running') AND
          (o.deadline<=? OR j.status IN ('completed','partially_completed','cancelled','expired','rejected','failed'))
          ORDER BY o.deadline LIMIT 128""", (now,))]
    ledger = store()
    for slot in rows:
        job = ledger.job(slot["job_id"]) if slot["job_id"] else ledger.job_by_key(slot["slot_key"])
        if job and job["status"] in timetable.TERMINAL:
            publish(slot, job)
        elif slot["status"] != "running":
            timetable.update_slot(slot["slot_key"], "expired", reason="window_expired")
            refresh_run(slot["run_id"])
    if _task is None or _task.done():
        with registry.connect() as conn:
            conn.execute("UPDATE integrity_occurrences SET status='pending' WHERE status='running' AND job_id IN (SELECT job_id FROM integrity_jobs WHERE status='queued')")


async def execute_probe(slot, *, sender=None):
    ledger = store()
    manifest = strategy(slot["method"])
    config = slot["snapshot"]["layered_config"]
    requests = fixed_requests(slot["method"])
    job = ledger.job(slot["job_id"]) if slot["job_id"] else ledger.job_by_key(slot["slot_key"])
    if job and job["status"] in timetable.TERMINAL:
        publish(slot, job)
        return
    target = integrity.resolve_target(slot, slot["snapshot"])
    if target is None:
        timetable.update_slot(slot["slot_key"], "rejected", reason="target_unavailable")
        refresh_run(slot["run_id"])
        return
    try:
        if job is None:
            with get_registry().connect() as conn:
                day = conn.execute("SELECT limits_json,budget_timezone FROM integrity_timetable_days WHERE run_id=?", (slot["run_id"],)).fetchone()
            job = ledger.enqueue(idempotency_key=slot["slot_key"], target_snapshot=target.snapshot,
                strategy={"strategy_id": manifest.strategy_id, "manifest_hash": manifest.manifest_hash}, requests=requests,
                limits=timetable.request_limits(slot["method"]), not_before=slot["due"], deadline=slot["deadline"],
                pricing=default_pricing(slot["model"]), budget_scope="daily", budget_key=timetable.budget_key(slot["schedule_id"]),
                budget_date=slot["budget_date"], timezone=day["budget_timezone"],
                daily_limits=json.loads(day["limits_json"]), plan_version=timetable.VERSION, principal="workbench")
            timetable.update_slot(slot["slot_key"], "pending", job_id=job["job_id"])
        claimed = ledger.claim(job_id=job["job_id"], owner="timetable-scheduler")
        if not claimed:
            return
        timetable.update_slot(slot["slot_key"], "running", job_id=job["job_id"])
        def project(request, raw, started, finished):
            return {**project_observation(manifest, request.probe["id"], raw),
                    "input_tokens_reported": raw.get("input_tokens_reported"), "output_tokens_reported": raw.get("output_tokens_reported"),
                    "reasoning_tokens_reported": raw.get("reasoning_tokens_reported"), "duration_ms": raw.get("latency_ms"),
                    "status": raw.get("status", "unknown"),
                    "started_at": started, "finished_at": finished}
        state = await execute_requests(ledger.session(claimed["job_id"], claimed["owner"], reserve_guard=timetable.send_guard(slot)),
            requests, lambda: integrity.resolve_target(slot, slot["snapshot"]), sender or send_probe, project, yield_after=1)
        if state == "yielded":
            publish_progress(slot, job["job_id"])
        elif state != "lost":
            publish(slot, ledger.job(job["job_id"]))
    except asyncio.CancelledError:
        if job:
            publish(slot, ledger.job(job["job_id"]))
        raise
    except (ValueError, RegistryError, KeyError) as exc:
        timetable.update_slot(slot["slot_key"], "failed", reason=type(exc).__name__)
        refresh_run(slot["run_id"])


async def dispatch():
    # No slot coroutine holds a lease while waiting in a lower-priority queue.
    while True:
        now = time.time()
        ensure_days(now)
        settle_pending(now)
        if select_ready(now) is None:
            return
        async with send_capacity():
            # Selection/claim happen after both global guards are actually held.
            slot = select_ready(time.time())
            if slot is not None:
                await execute_probe(slot, sender=send_probe_with_capacity)
        await asyncio.sleep(0)


async def tick(now=None):
    global _task
    timetable.init_tables()
    now = time.time() if now is None else now
    store().recover_expired_leases()
    ensure_days(now)
    settle_pending(now)
    if _task is None or _task.done():
        _task = asyncio.create_task(dispatch(), name="integrity-timetable-dispatch")


async def stop():
    global _task
    if _task and not _task.done():
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
    _task = None
    _reconciled.clear()


def cancel_run(run_id):
    with get_registry().connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE integrity_timetable_days SET cancelled=1 WHERE run_id=?", (run_id,))
        conn.execute("UPDATE integrity_jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE job_id IN (SELECT job_id FROM integrity_occurrences WHERE run_id=? AND status IN ('pending','running'))", (run_id,))
        conn.execute("UPDATE integrity_occurrences SET status='cancelled',reason='cancel_requested' WHERE run_id=? AND status='pending'", (run_id,))
    refresh_run(run_id)


def resume_run(run_id):
    now = time.time()
    rows = timetable.slots(run_id)
    active = [r for r in rows if r["deadline"] > now and r["status"] in {"cancelled", "failed", "partially_completed"}]
    if not active:
        raise ValueError("原时间窗已结束，不能延长或重发")
    schedule = storage.get_schedule(active[0]["schedule_id"])
    if not schedule or not schedule["enabled"]:
        raise ValueError("计划已暂停或删除")
    with get_registry().connect() as conn:
        control = conn.execute("SELECT * FROM integrity_timetable_controls WHERE schedule_id=?", (schedule["id"],)).fetchone()
    ledger = store()
    for slot in active:
        if not timetable.selected(control, slot):
            continue
        if slot["job_id"]:
            ledger.resume(slot["job_id"])
        timetable.update_slot(slot["slot_key"], "pending", reason="explicit_resume")
    with get_registry().connect() as conn:
        conn.execute("UPDATE integrity_timetable_days SET cancelled=0 WHERE run_id=?", (run_id,))
    refresh_run(run_id)
