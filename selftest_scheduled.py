"""定时测试自测：计划幂等、3 轮并发、汇总和连续三日告警。"""
import asyncio
import time
from datetime import datetime, timedelta

from app import runner, scheduler, store

failed = []


def check(name, ok, detail=None):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    if not ok:
        failed.append((name, detail))


store.init()
now = time.time()
family_id = store.insert("model_families", {
    "name": "定时模型家族", "created_at": now, "updated_at": now})
store.insert("model_family_models", {
    "family_id": family_id, "model": "test-model", "display_name": "test-model",
    "enabled": 1, "sort_order": 10, "created_at": now, "updated_at": now})
platform_group_id = store.insert("platform_groups", {
    "family_id": family_id, "multiplier": 1.2,
    "created_at": now, "updated_at": now})
target_id = store.insert("targets", {
    "name": "定时模型", "protocol": "openai", "base_url": "https://example.com/v1",
    "model": "test-model", "key_enc": "", "upstream_multiplier": 0.7,
    "platform_group_id": platform_group_id,
    "recorded": 1, "status": "pending",
    "created_at": now, "updated_at": now,
})
unselected_target_id = store.insert("targets", {
    "name": "未勾选模型", "protocol": "openai", "base_url": "https://example.com/v1",
    "model": "unselected-model", "key_enc": "", "recorded": 1, "status": "pending",
    "created_at": now, "updated_at": now,
})
schedule_id = store.insert("scheduled_tests", {
    "name": "早间巡检", "report_minute": 540,
    "feishu_webhook_ids": "[]", "email_recipient_ids": "[]", "enabled": 1,
    "created_at": now, "updated_at": now,
})
store.insert("scheduled_test_targets", {"target_id": target_id, "created_at": now})
schedule = store.get("scheduled_tests", schedule_id)


async def exercise():
    submitted = []
    original_submit = runner.submit

    async def fake_submit(task_id):
        submitted.append(task_id)

    runner.submit = fake_submit
    report_at = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=30)
    check("报告发送前 30 分钟进入测试窗口",
          scheduler.TEST_LEAD_MINUTES == 30, scheduler.TEST_LEAD_MINUTES)
    await scheduler._ensure_run(schedule, report_at)
    await scheduler._ensure_run(schedule, report_at)
    runner.submit = original_submit
    check("重复扫描只建一个任务", len(submitted) == 1, submitted)
    task = store.get("tasks", submitted[0])
    check("只为工作台已勾选模型建任务",
          task["target_id"] == target_id and task["target_id"] != unselected_target_id, task)
    snapshot = store.loads(task["snapshot"], {})
    progress = store.loads(task["progress"], {})
    check("定时任务保存 3 轮与 15 秒间隔",
          snapshot["inspect_rounds"] == 3 and snapshot["round_interval_seconds"] == 15, snapshot)
    check("一次定时任务只使用模型的唯一平台组",
          snapshot["scheduled_report_groups"] == [
              {"model_family": "定时模型家族", "online_multiplier": 1.2}]
          and snapshot["upstream_multiplier"] == 0.7, snapshot)
    check("进度总数是 18 请求", progress["total"] == 18, progress)

    original_one_step = runner._one_step
    calls = []

    async def fake_one_step(client, cfg, spec):
        calls.append(time.perf_counter())
        await asyncio.sleep(0.02)
        return {"step": spec["step"], "ok": True, "reason": "", "detail": "ok",
                "latency": 0.02, "first_token": 0.01,
                "usage": {"prompt": 1, "completion": 1}, "extra": {}}

    runner._one_step = fake_one_step
    started = time.perf_counter()
    results = await runner._run_repeated_inspect(
        object(), {}, task, [{"kind": "chat", "step": "A"}, {"kind": "chat", "step": "B"}],
        {"inspect_rounds": 3, "round_interval_seconds": 0.01},
    )
    runner._one_step = original_one_step
    check("三轮各两题全部执行", len(results) == 6, len(results))
    check("轮内请求并发", time.perf_counter() - started < 0.09)

    run = store.query("SELECT * FROM scheduled_runs WHERE scheduled_test_id=?", (schedule_id,))[0]
    sample_report = {"metrics": {
        "pass_rate": 5 / 6, "p95_latency": 1.2, "timeout_rate": 0,
        "stream_break_rate": 0, "capability_score": 5, "capability_total": 6,
        "performance": {"median_tokens_per_second": 22.0},
    }}
    store.update("tasks", task["id"], {"status": "success", "report": store.dumps(sample_report)})
    task = store.get("tasks", task["id"])
    summary, _ = scheduler.build_summary(schedule, run, [task], True)
    check("正常汇总按模型和上线倍率压缩展示", all(
        text in summary for text in ("结果：正常", "【定时模型家族】", "1.2x|正常"))
        and "速度最慢渠道" not in summary and "tok/s" not in summary, summary)

    codex_tasks = []
    for index, (model, online_multiplier) in enumerate((
        ("gpt-5.6-sol", 0.7), ("gpt-5.6-terra", 0.7),
        ("gpt-5.6-sol", 1.2), ("gpt-5.6-terra", 1.2),
    ), 2000):
        codex_tasks.append({
            "id": index, "target_id": index, "target_name": model,
            "status": "success", "created_at": now,
            "snapshot": store.dumps({
                "channel_name": f"渠道-{online_multiplier:g}", "model": model,
                "model_family": "Codex", "upstream_multiplier": online_multiplier,
                "scheduled_report_groups": [{
                    "model_family": "Codex",
                    "online_multiplier": online_multiplier,
                }],
            }),
            "report": store.dumps(sample_report),
        })
    codex_summary, _ = scheduler.build_summary(
        schedule, run, codex_tasks, False)
    check("Codex 两个实际模型按家族汇总并保留两个平台倍率", all(
        text in codex_summary for text in (
            "【Codex】", "0.7x|正常", "1.2x|正常"))
        and "【gpt-5.6-sol】" not in codex_summary
        and "【gpt-5.6-terra】" not in codex_summary
        and "【未配置上线倍率】" not in codex_summary,
        codex_summary)

    store.insert("scheduled_metric_samples", {
        "task_id": 999, "scheduled_run_id": run["id"], "target_id": target_id,
        "finished_at": now - 60, "status": "success", "pass_rate": 1.0,
        "p95_latency": 1.0, "speed": 21.0, "timeout_rate": 0,
        "stream_break_rate": 0, "score": 1.0,
    })
    store.insert("tasks", {
        "kind": "admission", "target_id": target_id, "target_name": "定时模型",
        "status": "success", "created_at": now - 3600, "finished_at": now - 3500,
        "report": store.dumps({"metrics": {
            "pass_rate": 1.0, "p95_latency": 1.0, "timeout_rate": 0,
            "stream_break_rate": 0, "capability_score": 6, "capability_total": 6,
            "performance": {"median_tokens_per_second": 30.0},
        }}),
    })
    degraded_report = {"metrics": {
        "pass_rate": 0.8, "p95_latency": 1.3, "timeout_rate": 0,
        "stream_break_rate": 0, "capability_score": 5, "capability_total": 6,
        "performance": {"median_tokens_per_second": 20.0},
    }}
    store.update("tasks", task["id"], {"report": store.dumps(degraded_report)})
    task = store.get("tasks", task["id"])
    abnormal_summary, _ = scheduler.build_summary(schedule, run, [task], True)
    check("异常汇总先列倍率状态再单列渠道并对比准入基线", all(text in abnormal_summary for text in (
        "结果：存在异常", "【定时模型家族】", "1.2x|异常（稳定性下降、速度缓慢）",
        "【异常渠道】",
        "定时模型 · 定时模型家族 · 0.7x → 现上线 · 定时模型家族 · 1.2x",
        "异常：稳定性下降、速度缓慢", "较准入：速度 -33%，P95 +30%",
        "较上次：成功率 -20 个百分点",
    )) and abnormal_summary.index("【异常渠道】") > abnormal_summary.index("1.2x|异常")
        and "1.2x|异常（稳定性下降、速度缓慢）\n- 定时模型" not in abnormal_summary,
        abnormal_summary)
    scheduler._record_scores(run, [task])
    check("定时指标快照持久化用于下次环比", bool(store.query(
        "SELECT task_id FROM scheduled_metric_samples WHERE task_id=?", (task["id"],))))

    trend = store.query("SELECT * FROM inspect_trends WHERE target_id=?", (target_id,))
    if trend:
        store.update("inspect_trends", trend[0]["id"], {
            "baseline_score": 1.0, "warning_active": 0, "updated_at": now})
    else:
        store.insert("inspect_trends", {
            "target_id": target_id, "baseline_score": 1.0,
            "warning_active": 0, "updated_at": now})
    for index, day in enumerate(("2026-08-23", "2026-08-24", "2026-08-25"), 100):
        store.insert("scheduled_score_samples", {
            "task_id": index, "target_id": target_id, "score_date": day, "score": 0.8})
    warnings = scheduler._warning_targets([task], "2026-08-25")
    check("连续三日低于基线 15 个百分点告警", warnings == [target_id], warnings)


asyncio.run(exercise())
print("\n失败项：" + ("无" if not failed else str(failed)))
raise SystemExit(1 if failed else 0)
