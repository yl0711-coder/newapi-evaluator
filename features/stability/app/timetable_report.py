"""Body-free channel/time tables derived from each occurrence's frozen evidence."""
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from shared.registry import get_registry
from . import timetable


def method_result(slot):
    if slot is None:
        return {"label": "未安排", "state": "not_scheduled", "planned": 0}
    summary, score = slot["summary"], slot["summary"].get("score", {})
    planned = timetable.request_limits(slot["method"])["max_requests"]
    result = {"planned": planned, "attempted": summary.get("attempted", 0), "valid": summary.get("valid", 0),
              "unknown": summary.get("unknown", 0), "not_run": summary.get("not_run", planned),
              "request_errors": summary.get("request_errors", 0), "score": score, "status": slot["status"],
              "reason": slot["reason"], "baseline_id": summary.get("baseline_id"),
              "slot_key": slot["slot_key"], "sampling_started_at": summary.get("sampling_started_at"),
              "sampling_finished_at": summary.get("sampling_finished_at"), "fees": summary.get("fees", {})}
    if slot["status"] in {"pending", "running"} and time.time() < slot.get("deadline", 0):
        if result["attempted"]:
            return {**result, "label": f"采样中 · 已尝试 {result['attempted']}/{planned}", "state": "sampling"}
        return {**result, "label": "待采样", "state": "waiting"}
    if (slot["status"] != "completed" or result["attempted"] != planned or result["unknown"] or result["not_run"]):
        return {**result, "label": "未完成 / 未测", "state": "incomplete"}
    if result["request_errors"]:
        return {**result, "label": "请求异常，需复核", "state": "review"}
    if slot["method"] == "modeltrace":
        if score.get("source_verdict") == "MISMATCH":
            return {**result, "label": "指纹差异，需复核", "state": "review"}
        if score.get("status") == "scored" and score.get("prediction") == "gpt-6-astra":
            return {**result, "label": "与 Astra 参考相近（未校准）", "state": "observed"}
        return {**result, "label": "样本不足 / 结果不明确", "state": "review"}
    correct = score.get("correct", sum(bool(v) for v in score.get("outcomes", {}).values()))
    result["current_score"] = f"{correct}/192"
    families = (score.get("comparison") or {}).get("families", [])
    family_rows = families.values() if isinstance(families, dict) else families
    local_loss = any(v.get("multiplicity_adjusted_status") == "degraded" for v in family_rows)
    paired = bool(result["baseline_id"]) and len(score.get("outcomes", {})) == 192
    if paired and score.get("status") == "degraded":
        return {**result, "label": f"{correct}/192 · 疑似能力下降", "state": "degraded"}
    if paired and local_loss:
        return {**result, "label": f"{correct}/192 · 局部能力下降线索", "state": "degraded"}
    if score.get("status") in {"current_only", "invalid_comparison", "incomplete"} or not paired:
        return {**result, "label": f"{correct}/192 · 参照不足", "state": "reference_missing"}
    if score.get("status") == "no_detected_degradation":
        return {**result, "label": f"{correct}/192 · 未检出下降", "state": "observed"}
    return {**result, "label": f"{correct}/192 · 变化未确认，需复核", "state": "review"}


def table_rows(slots):
    grouped = {}
    for slot in slots:
        if slot["method"] not in timetable.METHODS:
            continue
        key = (slot["run_id"], slot["due"], slot["registry_channel_id"])
        grouped.setdefault(key, {})[slot["method"]] = slot
    rows = []
    labels = {"degraded": "疑似能力下降", "review": "需复核", "reference_missing": "参照不足", "incomplete": "未完成 / 未测", "observed": "正常观测", "waiting": "待采样", "sampling": "采样中"}
    for (_, due, channel_id), methods in grouped.items():
        example = next(iter(methods.values()))
        snapshot = example["snapshot"]
        timezone = snapshot["timezone"]
        zone = ZoneInfo(timezone)
        results = {m: method_result(methods.get(m)) for m in timetable.METHODS}
        displays = []
        for method, slot in methods.items():
            config = slot["snapshot"]["layered_config"]
            name = config.get("channel_names", {}).get(str(channel_id), f"Registry {channel_id}")
            multiplier = config.get("channel_multipliers", {}).get(str(channel_id))
            results[method].update(channel_name=name, channel_multiplier=multiplier)
            displays.append((method, name, multiplier))
        names = {v[1] for v in displays}
        multipliers = {v[2] for v in displays}
        channel_name = displays[0][1] if len(names) == 1 else " / ".join(f"{'Canary' if m == 'canary' else 'MT'}: {n}" for m, n, _ in displays)
        multiplier = displays[0][2] if len(multipliers) == 1 else None
        states = {r["state"] for r in results.values()} - {"not_scheduled"}
        state = next((s for s in ("degraded", "review", "incomplete", "reference_missing", "sampling", "waiting", "observed") if s in states), "incomplete")
        starts = [r["sampling_started_at"] for r in results.values() if r.get("sampling_started_at") is not None]
        ends = [r["sampling_finished_at"] for r in results.values() if r.get("sampling_finished_at") is not None]
        start, end = min(starts) if starts else None, max(ends) if ends else None
        period = f"{datetime.fromtimestamp(start, zone).isoformat()} – {datetime.fromtimestamp(end, zone).isoformat()}" if start is not None and end is not None else "未采样"
        rows.append({"run_id": example["run_id"], "date": example["budget_date"], "timezone": timezone,
                     "scheduled_at": datetime.fromtimestamp(due, zone).isoformat(), "scheduled_at_utc": due,
                     "registry_channel_id": channel_id, "channel_name": channel_name,
                     "channel_multiplier": multiplier, "method_multipliers": {m: v["channel_multiplier"] for m, v in results.items() if "channel_multiplier" in v},
                     "multiplier_basis": "用户配置的渠道倍率标记，未验证实际扣费",
                     "sampling_period": period, "sampling_started_at": start, "sampling_finished_at": end,
                     "canary": results["canary"], "modeltrace": results["modeltrace"], "state": state,
                     "status": labels[state], "anomaly": state not in {"observed", "waiting", "sampling"}, "calibration_status": "unvalidated"})
    return sorted(rows, key=lambda r: (r["scheduled_at_utc"], r["registry_channel_id"], r["run_id"]))


def report(*, run_id=None, day=None, channel_id=None, anomalies_only=False):
    filters, values = ["EXISTS (SELECT 1 FROM integrity_timetable_days d WHERE d.run_id=integrity_occurrences.run_id AND d.archived=0)"], []
    for field, value in (("run_id", run_id), ("budget_date", day), ("registry_channel_id", channel_id)):
        if value is not None:
            filters.append(field + "=?")
            values.append(value)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    with get_registry().connect() as conn:
        slots = [timetable.decode(r) for r in conn.execute("SELECT * FROM integrity_occurrences" + where + " ORDER BY due,registry_channel_id,run_id,method LIMIT 10001", values)]
        empty = [dict(r) for r in conn.execute("SELECT d.run_id,d.budget_date FROM integrity_timetable_days d WHERE d.archived=0 AND NOT EXISTS (SELECT 1 FROM integrity_occurrences o WHERE o.run_id=d.run_id)" + (" AND d.run_id=?" if run_id else " AND d.budget_date=?"), (run_id or day,))]
    truncated = len(slots) > 10000
    # A raw SQL cap can cut one method or a channel group. Drop the complete
    # boundary group so absence never masquerades as an unscheduled method.
    if truncated:
        boundary = (slots[-1]["run_id"], slots[-1]["due"], slots[-1]["registry_channel_id"])
        slots = [s for s in slots if (s["run_id"], s["due"], s["registry_channel_id"]) != boundary]
    rows = table_rows(slots)
    counts = dict(Counter(r["status"] for r in rows))
    visible = [r for r in rows if r["anomaly"]] if anomalies_only else rows
    return {"rows": visible, "counts": counts, "total": len(rows), "displayed": len(visible),
            "empty_plans": empty if channel_id is None else [], "truncated": truncated,
            "period_basis": "本地许可至收到结果/停止等待；unknown 的上游结束未确认", "calibration_status": "unvalidated"}
