"""定时测试调度：提前创建巡检任务，到发送时间汇总并投递。"""
import asyncio
import time
from datetime import datetime, timedelta

from . import (notifications, packs, paired_admission, paired_evidence, runner,
               scheduled_configurations, scheduled_measurement, specialty, store)
from .security import mask

_task: asyncio.Task | None = None
CHECK_INTERVAL = 5.0
TEST_LEAD_MINUTES = 30
TERMINAL_STATUSES = {"success", "partial", "failed", "cancelled", "interrupted"}
SPEED_CHANGE_LIMIT = 0.20
P95_CHANGE_LIMIT = 0.20
PASS_RATE_CHANGE_LIMIT = 0.10


async def start() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="scheduler")


async def stop() -> None:
    if _task:
        _task.cancel()


def is_running() -> bool:
    return bool(_task and not _task.done())


async def _loop() -> None:
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            store.record_system_alert(
                "scheduler-loop", "scheduler",
                "每日调度器执行失败", str(exc)[:500], "critical",
            )
        await asyncio.sleep(CHECK_INTERVAL)


async def _tick() -> None:
    """每条计划每天形成一个持久化批次，重复扫描不会重复建任务或投递。"""
    paired_admission.expire_waiting_truth()
    paired_evidence.purge_expired_raw()
    now = datetime.now()
    for schedule in store.query("SELECT * FROM scheduled_tests WHERE enabled=1"):
        minute = int(schedule["report_minute"])
        for day_offset in (0, 1):
            report_at = (now + timedelta(days=day_offset)).replace(
                hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
            start_at = report_at - timedelta(minutes=TEST_LEAD_MINUTES)
            if start_at <= now < report_at:
                await _ensure_run(schedule, report_at)

    for run in store.query("SELECT * FROM scheduled_runs WHERE status!='complete'"):
        tasks = _run_tasks(run)
        complete = all(t["status"] in TERMINAL_STATUSES for t in tasks)
        anomaly_changes = []
        if complete:
            if run.get("plan_version_id"):
                anomaly_changes = scheduled_configurations.record_completed_run(run, tasks)
            else:
                _record_scores(run, tasks)
        notification_changes = [change for change in anomaly_changes if change["state"] != "normal"]
        if notification_changes:
            schedule = store.get("scheduled_tests", run["scheduled_test_id"])
            if schedule:
                await notifications.deliver_summary(
                    schedule, run,
                    f"anomaly-v{run.get('plan_version_id')}",
                    scheduled_configurations.anomaly_notification_text(schedule, notification_changes),
                )
        if not run["initial_sent"] and time.time() >= run["report_at"]:
            await _send_summary(run, tasks, "initial", complete)
            run = store.get("scheduled_runs", run["id"]) or run
        if run["initial_sent"] and complete:
            if not run["initial_complete"] and not run["supplement_sent"]:
                await _send_summary(run, tasks, "supplement", True)
            store.update("scheduled_runs", run["id"], {
                "status": "complete", "updated_at": time.time()})


async def _ensure_run(schedule: dict, report_at: datetime) -> None:
    run_date = report_at.strftime("%Y-%m-%d")
    if store.query(
        "SELECT id FROM scheduled_runs WHERE scheduled_test_id=? AND run_date=?",
        (schedule["id"], run_date),
    ):
        return
    configuration_plan = scheduled_configurations.active_snapshot(schedule["id"])
    has_configuration_targets = bool(store.query(
        "SELECT id FROM scheduled_configuration_targets WHERE scheduled_test_id=? LIMIT 1",
        (schedule["id"],),
    ))
    if has_configuration_targets:
        if not configuration_plan:
            return
        now = time.time()
        version = configuration_plan["version"]
        run_id = store.insert("scheduled_runs", {
            "scheduled_test_id": schedule["id"], "run_date": run_date,
            "report_at": report_at.timestamp(), "task_ids": "[]", "status": "testing",
            "plan_version_id": version["id"],
            "configuration_snapshot_json": store.dumps({
                "configuration_fingerprints": store.loads(version["configuration_fingerprints_json"], []),
                "primary_models": configuration_plan["models"],
                "measurement_rules": configuration_plan["measurement_rules"],
            }),
            "created_at": now, "updated_at": now,
        })
        task_ids = []
        for target in configuration_plan["targets"]:
            configuration = store.get("channel_configurations", target["id"])
            if not configuration:
                continue
            task_id = scheduled_measurement.create_task(
                run_id, version, configuration, list(configuration_plan["models"]),
            )
            store.add_event(task_id, "定时测试触发：全部主测模型各 10 次标准化流式请求", stage="调度")
            await runner.submit(task_id)
            task_ids.append(task_id)
        store.update("scheduled_runs", run_id, {"task_ids": store.dumps(task_ids), "updated_at": time.time()})
        return
    targets = store.query(
        "SELECT targets.*,channels.name channel_name FROM targets JOIN scheduled_test_targets "
        "ON scheduled_test_targets.target_id=targets.id "
        "LEFT JOIN channels ON channels.id=targets.channel_id ORDER BY targets.id")
    run_id = store.insert("scheduled_runs", {
        "scheduled_test_id": schedule["id"], "run_date": run_date,
        "report_at": report_at.timestamp(), "task_ids": "[]",
        "status": "testing", "created_at": time.time(), "updated_at": time.time(),
    })
    task_ids = []
    for target in targets:
        report_groups = store.query(
            "SELECT model_families.name model_family,"
            "platform_groups.multiplier online_multiplier "
            "FROM platform_groups JOIN model_families "
            "ON model_families.id=platform_groups.family_id "
            "WHERE platform_groups.id=?", (target.get("platform_group_id"),)) \
            if target.get("platform_group_id") else []
        target_family = report_groups[0]["model_family"] if report_groups \
            else model_family(target["model"])
        task_id = create_task_row("inspect", target, task_options={
            "scheduled_run_id": run_id, "inspect_rounds": 3,
            "round_interval_seconds": 15,
            "scheduled_report_groups": report_groups,
            "channel_name": target["channel_name"] or target["name"],
            "model_family": target_family,
            "upstream_multiplier": target["upstream_multiplier"],
        })
        store.add_event(task_id, "定时测试触发：3 轮，轮次间隔 15 秒", stage="调度")
        await runner.submit(task_id)
        task_ids.append(task_id)
    store.update("scheduled_runs", run_id, {
        "task_ids": store.dumps(task_ids), "updated_at": time.time()})


def _run_tasks(run: dict) -> list[dict]:
    task_ids = store.loads(run["task_ids"], [])
    if not task_ids:
        return []
    marks = ",".join("?" for _ in task_ids)
    return store.query(f"SELECT * FROM tasks WHERE id IN ({marks}) ORDER BY id", tuple(task_ids))


async def _send_summary(run: dict, tasks: list[dict], phase: str, complete: bool) -> None:
    schedule = store.get("scheduled_tests", run["scheduled_test_id"])
    if not schedule:
        return
    summary, warning_targets = build_summary(schedule, run, tasks, complete)
    await notifications.deliver_summary(schedule, run, phase, summary)
    patch = {
        "initial_sent" if phase == "initial" else "supplement_sent": 1,
        "initial_summary" if phase == "initial" else "final_summary": summary,
        "updated_at": time.time(),
    }
    if phase == "initial":
        patch["initial_complete"] = 1 if complete else 0
    store.update("scheduled_runs", run["id"], patch)
    if warning_targets:
        for target_id in warning_targets:
            trend = store.query("SELECT * FROM inspect_trends WHERE target_id=?", (target_id,))[0]
            store.update("inspect_trends", trend["id"], {
                "warning_active": 1, "updated_at": time.time()})


def _task_metrics(task: dict) -> dict:
    report = store.task_report(task, {})
    metrics = report.get("metrics") or {}
    snapshot = store.loads(task.get("snapshot"), {})
    score_total = metrics.get("capability_total") or 0
    report_groups = [{
        "model_family": group["model_family"],
        "online_multiplier": group.get("online_multiplier", group.get("multiplier")),
    } for group in snapshot.get("scheduled_report_groups", [])]
    return {
        "task_id": task["id"], "target_id": task.get("target_id"),
        "finished_at": task.get("finished_at") or task.get("created_at") or time.time(),
        "name": task["target_name"], "status": task["status"],
        "channel": snapshot.get("channel_name") or task["target_name"],
        "model": snapshot.get("model") or task["target_name"],
        "model_family": snapshot.get("model_family") or model_family(
            snapshot.get("model") or task["target_name"]),
        "upstream_multiplier": snapshot.get("upstream_multiplier"),
        "report_groups": report_groups,
        "pass_rate": metrics.get("pass_rate"),
        "p95_latency": metrics.get("p95_latency"),
        "speed": (metrics.get("performance") or {}).get("median_tokens_per_second"),
        "timeout_rate": metrics.get("timeout_rate"),
        "stream_break_rate": metrics.get("stream_break_rate"),
        "score": metrics.get("capability_score", 0) / score_total if score_total else None,
    }


def model_family(model: str) -> str:
    key = model.casefold()
    if "claude" in key:
        return "Claude"
    if "codex" in key:
        return "Codex"
    return model


def _previous_metrics(row: dict) -> dict | None:
    if not row["target_id"]:
        return None
    matches = store.query(
        "SELECT * FROM scheduled_metric_samples WHERE target_id=? AND task_id!=? "
        "AND finished_at<? ORDER BY finished_at DESC LIMIT 1",
        (row["target_id"], row["task_id"], row["finished_at"]))
    return matches[0] if matches else None


def _admission_metrics(row: dict) -> dict | None:
    if not row["target_id"]:
        return None
    target = store.get("targets", row["target_id"])
    baseline = store.loads((target or {}).get("baseline"), {})
    task = store.get("tasks", baseline.get("task_id")) if baseline.get("task_id") else None
    if not task or task.get("target_id") != row["target_id"] \
            or task.get("kind") != "admission" \
            or (task.get("finished_at") or 0) > row["finished_at"]:
        matches = store.query(
            "SELECT * FROM tasks WHERE target_id=? AND kind='admission' "
            "AND status IN ('success','partial') AND report!='' AND finished_at<=? "
            "ORDER BY finished_at ASC LIMIT 1",
            (row["target_id"], row["finished_at"]),
        )
        task = matches[0] if matches else None
    return _task_metrics(task) if task else None


def _ratio_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous


def _analyze_row(row: dict) -> dict:
    previous = _previous_metrics(row)
    admission = _admission_metrics(row)
    speed_change = _ratio_change(row["speed"], (admission or {}).get("speed"))
    p95_change = _ratio_change(row["p95_latency"], (admission or {}).get("p95_latency"))
    pass_change = None if row["pass_rate"] is None or not previous \
        or previous.get("pass_rate") is None else row["pass_rate"] - previous["pass_rate"]
    reasons = []
    if row["status"] in TERMINAL_STATUSES and row["status"] != "success":
        reasons.append("测试失败")
    if (row["timeout_rate"] or 0) > 0:
        reasons.append("出现超时")
    if (row["stream_break_rate"] or 0) > 0:
        reasons.append("出现断流")
    if pass_change is not None and pass_change <= -PASS_RATE_CHANGE_LIMIT:
        reasons.append("稳定性下降")
    if (speed_change is not None and speed_change <= -SPEED_CHANGE_LIMIT) \
            or (p95_change is not None and p95_change >= P95_CHANGE_LIMIT):
        reasons.append("速度缓慢")
    return {
        **row, "previous": previous, "admission": admission, "reasons": reasons,
        "speed_change": speed_change, "p95_change": p95_change,
        "pass_change": pass_change,
    }


def _admission_trend_text(row: dict) -> str:
    changes = []
    if row["speed_change"] is not None:
        changes.append(f"速度 {row['speed_change']:+.0%}")
    if row["p95_change"] is not None:
        changes.append(f"P95 {row['p95_change']:+.0%}")
    return "，".join(changes)


def _previous_trend_text(row: dict) -> str:
    if not row["previous"]:
        return ""
    changes = []
    if row["pass_change"] is not None:
        changes.append(f"成功率 {row['pass_change'] * 100:+.0f} 个百分点")
    for label, key in (("超时", "timeout_rate"), ("断流", "stream_break_rate")):
        current = row[key]
        previous = row["previous"].get(key)
        if current is not None and previous is not None and current != previous:
            changes.append(f"{label} {(current - previous) * 100:+.0f} 个百分点")
    return "，".join(changes)


def _rate_rows(rows: list[dict], family: str, online_multiplier: float) -> list[dict]:
    return [row for row in rows if any(
        group["model_family"] == family
        and group["online_multiplier"] is not None
        and float(group["online_multiplier"]) == online_multiplier
        for group in row["report_groups"])]


def _mapping_line(row: dict, family: str, online_multiplier: float) -> str:
    upstream = "未记录" if row["upstream_multiplier"] is None \
        else f"{float(row['upstream_multiplier']):g}x"
    return (f"- {row['channel']} · {family} · {upstream} → "
            f"现上线 · {family} · {online_multiplier:g}x")


def _anomaly_lines(row: dict) -> list[str]:
    report_groups = row["report_groups"]
    lines = [
        *[_mapping_line(row, group["model_family"], float(group["online_multiplier"]))
          for group in report_groups if group["online_multiplier"] is not None],
        f"  渠道：{row['channel']}",
        f"  异常：{'、'.join(row['reasons'])}",
    ]
    if not report_groups:
        upstream = "未记录" if row["upstream_multiplier"] is None \
            else f"{float(row['upstream_multiplier']):g}x"
        lines.insert(0, f"- {row['channel']} · {row['model_family']} · {upstream} → 现上线 · 未配置倍率")
    admission_trend = _admission_trend_text(row)
    if admission_trend:
        lines.append(f"  较准入：{admission_trend}")
    previous_trend = _previous_trend_text(row)
    if previous_trend:
        lines.append(f"  较上次：{previous_trend}")
    return lines


def build_summary(
    schedule: dict, run: dict, tasks: list[dict], complete: bool,
) -> tuple[str, list[int]]:
    rows = [_analyze_row(_task_metrics(task)) for task in tasks]
    anomalies = [row for row in rows if row["reasons"]]
    pending = [row for row in rows if row["status"] not in TERMINAL_STATUSES]
    title = "最终补充报告" if complete and run["initial_sent"] else "定时测试报告"
    finished = sum(t["status"] in TERMINAL_STATUSES for t in tasks)
    result = "暂无测试对象" if not rows else (
        "未全部完成" if pending else ("存在异常" if anomalies else "正常"))
    lines = [f"{title}｜{schedule['name']}｜{run['run_date']}",
             f"结果：{result}", f"测试进度：{finished}/{len(tasks)} 完成"]
    families = sorted({group["model_family"] for row in rows for group in row["report_groups"]})
    for family in families:
        lines.extend(["", f"【{family}】"])
        online_multipliers = sorted({float(group["online_multiplier"])
                                     for row in rows for group in row["report_groups"]
                                     if group["model_family"] == family
                                     and group["online_multiplier"] is not None})
        for online_multiplier in online_multipliers:
            cohort = _rate_rows(rows, family, online_multiplier)
            cohort_anomalies = [row for row in cohort if row["reasons"]]
            cohort_pending = [row for row in cohort if row["status"] not in TERMINAL_STATUSES]
            reasons = list(dict.fromkeys(
                reason for row in cohort_anomalies for reason in row["reasons"]))
            state = "未完成" if cohort_pending else (
                f"异常（{'、'.join(reasons)}）" if reasons else "正常")
            lines.append(f"{online_multiplier:g}x|{state}")
    ungrouped = [row for row in rows if not row["report_groups"]]
    if ungrouped:
        lines.extend(["", "【未配置上线倍率】"])
        for row in ungrouped:
            upstream = "未记录" if row["upstream_multiplier"] is None \
                else f"{float(row['upstream_multiplier']):g}x"
            lines.append(f"- {row['channel']} · {row['model_family']} · {upstream}")
    if anomalies:
        lines.extend(["", "【异常渠道】"])
        for row in anomalies:
            lines.extend(_anomaly_lines(row))
    if pending:
        lines.extend(["", f"未完成渠道：{'、'.join(row['name'] for row in pending)}；全部完成后将自动补发。"])
    warning_tasks = [task for task in tasks if _target_day_complete(
        task.get("target_id"), run["run_date"], run["id"])]
    warnings = _warning_targets(warning_tasks, run["run_date"]) if complete else []
    if warnings:
        names = [str((store.get("targets", target_id) or {}).get("name", target_id))
                 for target_id in warnings]
        lines.extend(["", f"⚠️ 连续三天降智警告：{'、'.join(names)}"])
    return "\n".join(lines), warnings


def _target_day_complete(target_id: int | None, run_date: str, current_run_id: int) -> bool:
    if not target_id:
        return False
    enabled_schedules = store.query(
        "SELECT id,report_minute FROM scheduled_tests WHERE enabled=1")
    if not enabled_schedules:
        return False
    final_minute = max(int(schedule["report_minute"]) for schedule in enabled_schedules)
    final_at = datetime.strptime(run_date, "%Y-%m-%d") + timedelta(minutes=final_minute)
    if datetime.now() < final_at:
        return False
    runs = store.query("SELECT * FROM scheduled_runs WHERE run_date=?", (run_date,))
    for run in runs:
        tasks = _run_tasks(run)
        if any(task.get("target_id") == target_id for task in tasks) \
                and run["id"] != current_run_id and run["status"] != "complete":
            return False
    return True


def _record_scores(run: dict, tasks: list[dict]) -> None:
    for task in tasks:
        metrics = _task_metrics(task)
        if not task.get("target_id"):
            continue
        if not store.query("SELECT task_id FROM scheduled_metric_samples WHERE task_id=?",
                           (task["id"],)):
            store.insert("scheduled_metric_samples", {
                "task_id": task["id"], "scheduled_run_id": run["id"],
                "target_id": task["target_id"],
                "finished_at": task.get("finished_at") or time.time(),
                "status": task["status"], "pass_rate": metrics["pass_rate"],
                "p95_latency": metrics["p95_latency"], "speed": metrics["speed"],
                "timeout_rate": metrics["timeout_rate"],
                "stream_break_rate": metrics["stream_break_rate"],
                "score": metrics["score"],
            })
        if metrics["score"] is None:
            continue
        if not store.query("SELECT task_id FROM scheduled_score_samples WHERE task_id=?",
                           (task["id"],)):
            store.insert("scheduled_score_samples", {
                "task_id": task["id"], "target_id": task["target_id"],
                "score_date": run["run_date"], "score": metrics["score"],
            })
        trend = store.query("SELECT * FROM inspect_trends WHERE target_id=?", (task["target_id"],))
        if not trend:
            store.insert("inspect_trends", {
                "target_id": task["target_id"], "baseline_score": metrics["score"],
                "warning_active": 0, "updated_at": time.time(),
            })


def _warning_targets(tasks: list[dict], score_date: str) -> list[int]:
    warnings = []
    for target_id in {task.get("target_id") for task in tasks if task.get("target_id")}:
        trend_rows = store.query("SELECT * FROM inspect_trends WHERE target_id=?", (target_id,))
        if not trend_rows:
            continue
        trend = trend_rows[0]
        days = store.query(
            "SELECT score_date,AVG(score) score FROM scheduled_score_samples "
            "WHERE target_id=? AND score_date<=? GROUP BY score_date ORDER BY score_date DESC LIMIT 3",
            (target_id, score_date),
        )
        threshold = trend["baseline_score"] - 0.15
        degraded = len(days) == 3 and all(day["score"] <= threshold for day in days)
        if len(days) == 3:
            parsed = [datetime.strptime(day["score_date"], "%Y-%m-%d").date() for day in days]
            degraded = degraded and (parsed[0] - parsed[1]).days == 1 \
                and (parsed[1] - parsed[2]).days == 1
        if degraded and not trend["warning_active"]:
            warnings.append(target_id)
        elif days and days[0]["score"] > threshold and trend["warning_active"]:
            store.update("inspect_trends", trend["id"], {
                "warning_active": 0, "updated_at": time.time()})
    return warnings


async def create_inspect_task(target: dict, auto: bool = False) -> int:
    """给一个渠道建巡检任务并入队。手动抽检与自动巡检共用这条路径。"""
    task_id = create_task_row("inspect", target)
    store.add_event(task_id, "自动巡检触发" if auto else "手动抽检触发", stage="调度")
    await runner.submit(task_id)
    return task_id


def create_task_row(
    kind: str, target: dict, include_hard: bool | None = None,
    task_options: dict | None = None,
) -> int:
    """建任务行，快照里只放脱敏后的配置。

    include_hard 只对带硬题的包有意义。它同时决定预估请求数与费用上限 ——
    跑硬题时上限要按硬题的量给，否则任务会在硬题跑到一半时撞上限停下。
    """
    pack = packs.get_pack(kind)
    want_hard = bool(include_hard)
    est = packs.estimate(kind, target["price_in"] or 2.0, target["price_out"] or 8.0,
                         include_hard=want_hard)
    snapshot = {
        "name": target["name"], "protocol": target["protocol"],
        "base_url": target["base_url"], "model": target["model"],
        "group_name": target["group_name"], "env": target["env"],
        "price_in": target["price_in"], "price_out": target["price_out"],
        "key_masked": mask_of(target),
        "pack": pack["name"], "pack_version": pack["version"],
        "include_hard": want_hard,
    }
    if task_options:
        snapshot.update(task_options)
    rounds = int((task_options or {}).get("inspect_rounds") or 1)
    specialty_estimate = specialty.estimate(
        list((task_options or {}).get("specialty_profiles") or []))
    total_requests = est["requests"] * rounds + specialty_estimate["requests"]
    specialty_cost = specialty_estimate["tokens"] / 1_000_000 * (target["price_out"] or 8.0)
    return store.insert("tasks", {
        "kind": kind, "target_id": target["id"], "target_name": target["name"],
        "pack_name": pack["name"], "pack_version": pack["version"],
        "status": "queued", "snapshot": store.dumps(snapshot),
        "progress": store.dumps({"done": 0, "total": total_requests}),
        "cost_limit": max((est["cost"] * rounds + specialty_cost) * 4, 0.5),
        "include_hard": 1 if want_hard else 0,
        "created_at": time.time(),
    })


def mask_of(target: dict) -> str:
    """快照里的 Key 一律脱敏，解密只发生在执行器内存中。"""
    from .security import decrypt
    if not target.get("key_enc"):
        return ""
    try:
        return mask(decrypt(target["key_enc"]))
    except Exception:
        return "***"
