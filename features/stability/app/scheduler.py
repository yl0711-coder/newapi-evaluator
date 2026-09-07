from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, time as clock_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from shared.network import guarded_transport

from . import storage, transport
from .config import CATCHUP_HOURS, MAX_ACTIVE_RUNS, RETENTION_DAYS, TIMEZONE
from .security import scrub


POLL_SECONDS = 15
LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30
WEBHOOK_KEY = "feishu_webhook"

logger = logging.getLogger(__name__)
_loop_task: asyncio.Task[None] | None = None
_active: dict[int, asyncio.Task[None]] = {}
_notifications: dict[int, asyncio.Task[None]] = {}
_last_tick_at: float | None = None
_last_retention_at: float | None = None


def parse_daily_times(value: str) -> list[str]:
    output: list[str] = []
    for part in value.replace("，", ",").split(","):
        item = part.strip()
        if not item:
            continue
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item):
            raise ValueError(f"无效执行时刻：{item}；请使用 HH:MM")
        if item not in output:
            output.append(item)
    if not output:
        raise ValueError("至少填写一个执行时刻")
    return sorted(output)


def timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区：{name}") from exc


def ensure_due_runs(now_epoch: float | None = None) -> int:
    now_epoch = time.time() if now_epoch is None else now_epoch
    created = 0
    for schedule in storage.list_schedules(enabled_only=True):
        zone = timezone(schedule.get("timezone") or TIMEZONE)
        now_local = datetime.fromtimestamp(now_epoch, zone)
        for day_offset in (0, -1):
            target_date = (now_local + timedelta(days=day_offset)).date()
            for item in parse_daily_times(schedule["daily_times"]):
                hour, minute = (int(value) for value in item.split(":"))
                due = datetime.combine(target_date, clock_time(hour, minute), zone)
                due_epoch = due.timestamp()
                if due_epoch > now_epoch or due_epoch < now_epoch - CATCHUP_HOURS * 3600:
                    continue
                if due_epoch < float(schedule.get("created_at") or 0):
                    continue
                if storage.create_run(schedule, due_epoch) is not None:
                    created += 1
    return created


def _thresholds(snapshot: dict[str, Any]) -> dict[str, Any]:
    output = {key: snapshot[key] for key in (
        "min_success_rate", "max_timeout_rate", "max_stream_break_rate", "max_p95_ms"
    )}
    if snapshot.get("speed_threshold_mode", "fixed") != "fixed":
        output["max_p95_ms"] = 10**15
    return output


def _apply_speed_threshold(
    channel_id: int, model: str, summary: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    mode = str(snapshot.get("speed_threshold_mode") or "fixed")
    current_p95 = summary.get("p95_success_latency_ms")
    if mode != "adaptive":
        return {
            **summary,
            "speed_assessment": {
                "mode": mode,
                "status": "fixed" if mode == "fixed" else "disabled",
                "current_p95_ms": current_p95,
                "threshold_p95_ms": snapshot.get("max_p95_ms") if mode == "fixed" else None,
            },
        }

    required = int(snapshot.get("speed_baseline_min_runs") or 5)
    ratio = float(snapshot.get("speed_slow_ratio") or 1.5)
    baseline = storage.channel_latency_baseline(channel_id, model)
    baseline_p95 = baseline.get("median_p95_latency_ms")
    sample_count = int(baseline.get("sample_count") or 0)
    threshold = round(float(baseline_p95) * ratio) if baseline_p95 is not None else None
    status = "collecting" if sample_count < required else "unavailable"
    reasons = list(summary.get("reasons") or [])
    if sample_count >= required and threshold is not None and current_p95 is not None:
        status = "slow" if float(current_p95) > threshold else "normal"
        if status == "slow":
            reasons.append("P95 延迟高于历史中位数")
    output = {
        **summary,
        "reasons": reasons,
        "verdict": "fail" if reasons or not summary.get("total") else "pass",
        "speed_assessment": {
            "mode": "adaptive",
            "status": status,
            "historical_runs": sample_count,
            "required_runs": required,
            "baseline_median_p95_ms": baseline_p95,
            "threshold_p95_ms": threshold,
            "current_p95_ms": current_p95,
            "slow_ratio": ratio,
        },
    }
    return output


async def _measure_channel(run_id: int, channel: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    rounds = int(snapshot["rounds"])
    interval = float(snapshot["round_interval_seconds"])
    results: list[dict[str, Any]] = []
    timeout = httpx.Timeout(connect=20, read=180, write=20, pool=20)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False, transport=guarded_transport()) as client:
        async def run_round(round_number: int) -> list[dict[str, Any]]:
            if round_number > 1 and interval:
                await asyncio.sleep(interval * (round_number - 1))
            batch = await asyncio.gather(
                *(transport.run_probe(client, channel, probe) for probe in transport.PROBES),
                return_exceptions=True,
            )
            round_results: list[dict[str, Any]] = []
            for probe, value in zip(transport.PROBES, batch):
                if isinstance(value, Exception):
                    value = {
                        "probe_id": probe["id"], "probe_name": probe["name"], "ok": False,
                        "status": "platform_error", "latency_ms": None, "ttft_ms": None,
                        "tokens_per_second": None, "output_tokens": None, "finish_reason": "",
                        "error": scrub(type(value).__name__, channel["api_key"]),
                    }
                result = {
                    **value,
                    "channel_id": channel["id"],
                    "channel_name": channel["name"],
                    "model": channel["model"],
                    "round_number": round_number,
                }
                storage.add_probe_result(run_id, result)
                round_results.append(result)
            storage.extend_lease(run_id, LEASE_SECONDS)
            return round_results

        for round_results in await asyncio.gather(*(run_round(number) for number in range(1, rounds + 1))):
            results.extend(round_results)
    summary = transport.summarize(results, _thresholds(snapshot))
    summary = _apply_speed_threshold(channel["id"], channel["model"], summary, snapshot)
    return {
        "channel_id": channel["id"],
        "registry_channel_id": channel.get("registry_channel_id"),
        "channel_name": channel["name"],
        "model": channel["model"],
        **summary,
    }


def _report_problem(channels: list[dict[str, Any]]) -> str:
    reasons = [str(reason) for channel in channels for reason in channel.get("reasons", [])]
    failures = {
        str(status)
        for channel in channels
        for status, count in (channel.get("failures") or {}).items()
        if count
    }
    labels: list[str] = []
    if "P95 延迟超过阈值" in reasons or "P95 延迟高于历史中位数" in reasons:
        labels.append("速度缓慢")
    if "超时率超过阈值" in reasons or "timeout" in failures:
        labels.append("请求超时")
    if "断流率超过阈值" in reasons or "stream_break" in failures:
        labels.append("流式中断")
    if "auth_error" in failures:
        labels.append("鉴权失败")
    if "rate_limited" in failures:
        labels.append("请求限流")
    if failures & {"network_error", "egress_denied"}:
        labels.append("连接失败")
    if not labels:
        labels.append("测试失败")
    return "、".join(dict.fromkeys(labels))


def _format_ms(value: Any) -> str:
    if value is None:
        return "-"
    number = float(value)
    return f"{number / 1000:.2f} 秒" if number >= 1000 else f"{number:.0f} ms"


def _detail_label(channel: dict[str, Any]) -> str:
    name = str(channel.get("channel_name") or f"渠道 #{channel.get('registry_channel_id', '-')}")
    model = str(channel.get("model") or "")
    return name if not model or model in name else f"{name} / {model}"


def _is_slow(channel: dict[str, Any]) -> bool:
    assessment = channel.get("speed_assessment") or {}
    return assessment.get("status") == "slow" or any(
        reason in {"P95 延迟超过阈值", "P95 延迟高于历史中位数"}
        for reason in channel.get("reasons", [])
    )


def _is_unstable(channel: dict[str, Any]) -> bool:
    speed_reasons = {"P95 延迟超过阈值", "P95 延迟高于历史中位数"}
    return bool(channel.get("failures")) or any(
        reason not in speed_reasons for reason in channel.get("reasons", [])
    )


def _render_channel_details(channels: list[dict[str, Any]]) -> list[str]:
    slow = [channel for channel in channels if _is_slow(channel)]
    unstable = [channel for channel in channels if _is_unstable(channel)]
    lines = ["", "速度缓慢渠道："]
    if not slow:
        lines.append("无")
    for channel in slow:
        speed = channel.get("speed_assessment") or {}
        parts = [f"P95 {_format_ms(speed.get('current_p95_ms') or channel.get('p95_latency_ms'))}"]
        if speed.get("baseline_median_p95_ms") is not None:
            parts.extend((
                f"历史中位数 {_format_ms(speed['baseline_median_p95_ms'])}",
                f"慢速线 {_format_ms(speed.get('threshold_p95_ms'))}",
            ))
        parts.append(f"TTFT P95 {_format_ms(channel.get('p95_ttft_ms'))}")
        token_speed = channel.get("p50_tokens_per_second")
        if token_speed is not None:
            parts.append(f"输出中位速度 {float(token_speed):.1f} tok/s")
        lines.append(f"- {_detail_label(channel)}：" + "；".join(parts))

    lines.append("不稳定渠道：")
    if not unstable:
        lines.append("无")
    for channel in unstable:
        total = int(channel.get("total") or 0)
        completed = int(channel.get("completed") or 0)
        timeouts = int(channel.get("timeout_count") or 0)
        streams = int(channel.get("streaming_total") or 0)
        breaks = int(channel.get("stream_break_count") or 0)
        detail = (
            f"- {_detail_label(channel)}：成功 {completed}/{total}（{float(channel.get('pass_rate') or 0):.1%}）；"
            f"超时 {timeouts}/{total}（{float(channel.get('timeout_rate') or 0):.1%}）；"
            f"断流 {breaks}/{streams}（{float(channel.get('stream_break_rate') or 0):.1%}）；"
            f"P95 {_format_ms(channel.get('p95_latency_ms'))}"
        )
        failures = channel.get("failures") or {}
        if failures:
            labels = {
                "timeout": "超时", "stream_break": "断流", "auth_error": "鉴权失败",
                "rate_limited": "限流", "network_error": "网络错误",
                "upstream_5xx": "上游 5xx", "content_mismatch": "内容不符",
                "empty_response": "空响应", "invalid_response": "无效响应",
            }
            detail += "；失败类型 " + "、".join(
                f"{labels.get(str(status), status)}×{int(count)}"
                for status, count in failures.items() if count
            )
        lines.append(detail)

    collecting = [
        channel for channel in channels
        if (channel.get("speed_assessment") or {}).get("status") == "collecting"
    ]
    if collecting:
        required = max(
            int((channel.get("speed_assessment") or {}).get("required_runs") or 0)
            for channel in collecting
        )
        minimum = min(
            int((channel.get("speed_assessment") or {}).get("historical_runs") or 0)
            for channel in collecting
        )
        lines.append(
            f"速度基线：采样中，{len(collecting)} 个目标尚未积累 {required} 次稳定历史"
            f"（当前最少 {minimum}/{required}）"
        )
    return lines


def _render_group_notification(run: dict[str, Any], summary: dict[str, Any]) -> str | None:
    groups = (run.get("snapshot") or {}).get("report_groups") or []
    if not groups:
        return None
    by_registry: dict[int, list[dict[str, Any]]] = {}
    for channel in summary.get("channels", []):
        registry_id = channel.get("registry_channel_id")
        if registry_id is not None:
            by_registry.setdefault(int(registry_id), []).append(channel)

    lines: list[str] = []
    current_family = ""
    for group in groups:
        family = str(group["family"])
        if family != current_family:
            if lines:
                lines.append("")
            lines.append(f"【{family}】")
            current_family = family
        if group.get("always_normal"):
            result = "正常"
        else:
            expected = {int(value) for value in group.get("registry_channel_ids", [])}
            found = expected & set(by_registry)
            matched = [channel for registry_id in expected for channel in by_registry.get(registry_id, [])]
            if found != expected:
                result = "异常（未测试）"
            elif matched and all(channel.get("verdict") == "pass" for channel in matched):
                result = "正常"
            else:
                result = f"异常（{_report_problem(matched)}）"
        lines.append(f"{group['label']}|{result}")
    lines.extend(_render_channel_details(summary.get("channels", [])))
    return "\n".join(lines)


def render_notification(run: dict[str, Any], summary: dict[str, Any]) -> str:
    grouped = _render_group_notification(run, summary)
    if grouped is not None:
        return grouped
    status = "通过" if summary["verdict"] == "pass" else "异常"
    lines = [
        f"【定时稳定性测试】{run['schedule_name']}｜{status}",
        f"完成 {summary['completed']}/{summary['total']} 次请求｜成功率 {summary['pass_rate']:.1%}",
        f"超时率 {summary['timeout_rate']:.1%}｜断流率 {summary['stream_break_rate']:.1%}",
        f"P95 {summary['p95_latency_ms'] if summary['p95_latency_ms'] is not None else '-'} ms",
    ]
    for channel in summary.get("channels", []):
        label = "通过" if channel["verdict"] == "pass" else "异常"
        reasons = "、".join(channel["reasons"])
        lines.append(f"- {channel['channel_name']} / {channel['model']}：{label}" + (f"（{reasons}）" if reasons else ""))
    lines.extend(_render_channel_details(summary.get("channels", [])))
    return "\n".join(lines)


async def notify(run_id: int, run: dict[str, Any], summary: dict[str, Any]) -> None:
    webhook = storage.get_setting(WEBHOOK_KEY)
    if not webhook:
        storage.update_notification(run_id, "disabled")
        return
    error = ""
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
                response = await client.post(
                    webhook,
                    json={"msg_type": "text", "content": {"text": render_notification(run, summary)}},
                )
                response.raise_for_status()
                data = response.json()
                if data.get("code", data.get("StatusCode", 0)) != 0:
                    raise RuntimeError("飞书返回失败状态")
            storage.update_notification(run_id, "sent")
            return
        except Exception as exc:
            error = type(exc).__name__
            if attempt < 3:
                await asyncio.sleep(attempt * 2)
    storage.update_notification(run_id, "failed", error)


async def execute_run(run_id: int) -> None:
    run = storage.get_run(run_id)
    if not run:
        return
    async def keep_lease() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            storage.extend_lease(run_id, LEASE_SECONDS)

    heartbeat = asyncio.create_task(keep_lease(), name=f"stability-lease-{run_id}")
    try:
        snapshot = run["snapshot"]
        storage.clear_probe_results(run_id)
        channels = [item for item in storage.list_channels(
            include_secrets=True, ids=[int(value) for value in snapshot["channel_ids"]]
        ) if item["enabled"] and item.get("api_key")]
        if len(channels) != len(snapshot["channel_ids"]):
            summary = {**transport.summarize([], _thresholds(snapshot)), "channels": []}
            storage.finish_run(run_id, "failed", summary, "计划中的部分渠道已停用或缺少配置，请检查巡检目标")
            return
        if not channels:
            summary = {**transport.summarize([], _thresholds(snapshot)), "channels": []}
            storage.finish_run(run_id, "failed", summary, "计划没有可用渠道")
            return
        semaphore = asyncio.Semaphore(int(snapshot["max_concurrency"]))

        async def worker(channel: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await _measure_channel(run_id, channel, snapshot)

        channel_summaries = await asyncio.gather(*(worker(channel) for channel in channels))
        detail = storage.get_run(run_id)
        all_results = (detail or {}).get("results", [])
        overall = transport.summarize(all_results, _thresholds(snapshot))
        failed_channels = [channel for channel in channel_summaries if channel.get("verdict") == "fail"]
        if failed_channels:
            overall["verdict"] = "fail"
            overall["reasons"] = list(dict.fromkeys([
                *overall.get("reasons", []),
                *(reason for channel in failed_channels for reason in channel.get("reasons", [])),
            ]))
        summary = {**overall, "channels": channel_summaries}
        storage.finish_run(run_id, "completed", summary)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        summary = {**transport.summarize([], _thresholds(snapshot)), "channels": []}
        storage.finish_run(run_id, "failed", summary, scrub(type(exc).__name__))
        logger.exception("run %s failed", run_id)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def tick(now_epoch: float | None = None) -> None:
    global _last_retention_at, _last_tick_at
    now_epoch = time.time() if now_epoch is None else now_epoch
    if _last_retention_at is None or now_epoch - _last_retention_at >= 3600:
        storage.prune_run_history(now_epoch, RETENTION_DAYS)
        _last_retention_at = now_epoch
    ensure_due_runs(now_epoch)
    storage.recover_expired_runs(now_epoch)
    available = max(0, MAX_ACTIVE_RUNS - len(_active))
    for run in storage.pending_runs(available):
        run_id = int(run["id"])
        if not storage.claim_run(run_id, LEASE_SECONDS):
            continue
        task = asyncio.create_task(execute_run(run_id), name=f"stability-run-{run_id}")
        _active[run_id] = task
        task.add_done_callback(lambda _task, value=run_id: _active.pop(value, None))
    for run in storage.pending_notifications(now_epoch):
        run_id = int(run["id"])
        if run_id in _notifications or not storage.claim_notification(run_id):
            continue
        task = asyncio.create_task(
            notify(run_id, run, run["summary"]), name=f"stability-notify-{run_id}"
        )
        _notifications[run_id] = task
        task.add_done_callback(lambda _task, value=run_id: _notifications.pop(value, None))
    _last_tick_at = time.time()


async def _loop() -> None:
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduler tick failed")
        await asyncio.sleep(POLL_SECONDS)


async def start() -> None:
    global _last_retention_at, _loop_task
    storage.recover_all_running()
    storage.recover_sending_notifications()
    _last_retention_at = None
    if not _loop_task or _loop_task.done():
        _loop_task = asyncio.create_task(_loop(), name="stability-scheduler")


async def stop() -> None:
    global _loop_task
    if _loop_task:
        _loop_task.cancel()
        await asyncio.gather(_loop_task, return_exceptions=True)
    for task in list(_active.values()):
        task.cancel()
    await asyncio.gather(*_active.values(), return_exceptions=True)
    _active.clear()
    for task in list(_notifications.values()):
        task.cancel()
    await asyncio.gather(*_notifications.values(), return_exceptions=True)
    _notifications.clear()
    _loop_task = None


def status() -> dict[str, Any]:
    return {
        "running": bool(_loop_task and not _loop_task.done()),
        "active_runs": len(_active),
        "active_notifications": len(_notifications),
        "last_tick_at": _last_tick_at,
        "retention_days": RETENTION_DAYS,
    }
