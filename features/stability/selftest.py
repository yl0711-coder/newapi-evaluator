from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path


_temp = tempfile.TemporaryDirectory(prefix="stability-selftest-")
os.environ["STABILITY_DATA_DIR"] = _temp.name
os.environ["PLATFORM_DATA_DIR"] = str(Path(_temp.name) / "registry")
os.environ["STABILITY_EGRESS_ALLOWLIST"] = "127.0.0.1"

import httpx  # noqa: E402

from app import scheduler, storage, transport  # noqa: E402


failures: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(f"  {'OK  ' if condition else 'FAIL'} {name}")
    if not condition:
        failures.append(f"{name}: {detail}")


def channel_input() -> dict[str, object]:
    return {
        "name": "自测渠道",
        "base_url": "http://127.0.0.1:9876/v1",
        "model": "selftest-model",
        "protocol": "openai",
        "api_key": "sk-selftest-never-store-plain",
        "enabled": True,
    }


def schedule_input(channel_id: int) -> dict[str, object]:
    return {
        "name": "自测计划",
        "daily_times": "09:00,18:00",
        "timezone": "Asia/Shanghai",
        "channel_ids": [channel_id],
        "rounds": 3,
        "round_interval_seconds": 15,
        "notification_delay_seconds": 3300,
        "max_concurrency": 1,
        "min_success_rate": 0.95,
        "max_timeout_rate": 0.05,
        "max_stream_break_rate": 0,
        "max_p95_ms": 30000,
        "speed_threshold_mode": "adaptive",
        "speed_baseline_min_runs": 5,
        "speed_slow_ratio": 1.5,
        "enabled": True,
    }


async def protocol_checks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["stream"]:
            stream = "\n".join((
                'data: {"model":"selftest-model","choices":[{"delta":{"content":"101,102"},"finish_reason":null}]}',
                'data: {"model":"selftest-model","choices":[{"delta":{"content":",103"},"finish_reason":"stop"}]}',
                'data: {"usage":{"prompt_tokens":10,"completion_tokens":3},"choices":[]}',
                "data: [DONE]",
                "",
            ))
            return httpx.Response(200, text=stream, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={
            "model": "selftest-model",
            "choices": [{"message": {"content": "391"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
        })

    channel = channel_input()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        math_result = await transport.run_probe(client, channel, transport.PROBES[4])
        stream_result = await transport.run_probe(client, channel, transport.PROBES[1])
    check("非流式能力题可判分", math_result["ok"] and math_result["usage_complete"], math_result)
    check("流式完成标记和 usage 可解析", stream_result["ok"] and stream_result["usage_complete"], stream_result)


async def round_checks(run_id: int, channel_id: int, snapshot: dict[str, object]) -> None:
    original = transport.run_probe
    starts: list[tuple[str, float]] = []
    active = 0
    peak_active = 0

    async def fake_probe(_client: object, _channel: dict, probe: dict) -> dict[str, object]:
        nonlocal active, peak_active
        starts.append((probe["id"], time.perf_counter()))
        active += 1
        peak_active = max(peak_active, active)
        try:
            await asyncio.sleep(0.08)
            return {
                "probe_id": probe["id"], "probe_name": probe["name"], "ok": True,
                "status": "completed", "latency_ms": 80, "ttft_ms": 10,
                "tokens_per_second": 20.0, "output_tokens": 10, "finish_reason": "stop",
                "actual_model": "selftest-model", "usage_complete": True,
                "model_mismatch": False, "error": "",
            }
        finally:
            active -= 1

    fast_snapshot = {**snapshot, "rounds": 3, "round_interval_seconds": 0.02}
    transport.run_probe = fake_probe
    started = time.perf_counter()
    try:
        summary = await scheduler._measure_channel(
            run_id,
            {**channel_input(), "id": channel_id},
            fast_snapshot,
        )
    finally:
        transport.run_probe = original
    connect_starts = [value for probe_id, value in starts if probe_id == "connect"]
    check("三轮共执行 18 个请求", len(starts) == 18 and summary["total"] == 18, len(starts))
    check(
        "巡检题目全局最多并发 2 个",
        peak_active == scheduler.MAX_CONCURRENT_PROBES == 2,
        peak_active,
    )
    check(
        "并发受限时三轮仍完整执行",
        len(connect_starts) == 3 and connect_starts == sorted(connect_starts),
        connect_starts,
    )


async def main() -> None:
    check("测试包沿用 ins-v2", transport.INSPECT_VERSION == "ins-v2")
    check("每轮固定 6 个请求", len(transport.PROBES) == 6)
    check("请求体保持原版单条 user 消息", len(transport.payload(channel_input(), transport.PROBES[0])["messages"]) == 1)
    check("OpenAI URL 补全", transport.endpoint_url("https://api.example.com/v1", "openai").endswith("/v1/chat/completions"))
    check("Anthropic URL 补全", transport.endpoint_url("https://api.example.com", "anthropic").endswith("/v1/messages"))
    check("时间列表去重并排序", scheduler.parse_daily_times("18:00，09:00,18:00") == ["09:00", "18:00"])
    try:
        scheduler.parse_daily_times("09:00,bad")
    except ValueError:
        invalid_rejected = True
    else:
        invalid_rejected = False
    check("非法执行时刻会被拒绝", invalid_rejected)

    storage.init()
    inventory_id = storage.add_inventory({
        "name": "inventory.example.com",
        "base_url": "https://inventory.example.com/v1/",
        "scope": "codex",
        "multiplier": 0.25,
        "api_key": "sk-inventory-selftest-plain",
        "source_kind": "selftest",
    })
    inventory = storage.list_inventory()
    check(
        "渠道资料只返回脱敏密钥",
        inventory_id is not None and len(inventory) == 1
        and "api_key" not in inventory[0] and "***" in inventory[0]["key_masked"],
        inventory,
    )
    duplicate_inventory = storage.add_inventory({
        "name": "inventory.example.com",
        "base_url": "https://inventory.example.com/v1",
        "scope": "codex",
        "multiplier": 0.25,
        "api_key": "sk-inventory-selftest-plain",
        "source_kind": "selftest",
    })
    check("相同渠道资料不会重复导入", duplicate_inventory is None)
    channel_id = storage.upsert_channel(channel_input())
    stored = storage.get_channel(channel_id, include_secret=True) or {}
    check("API Key 可解密读取", stored.get("api_key") == channel_input()["api_key"])
    check(
        "数据库不保存明文 API Key",
        b"sk-selftest-never-store-plain" not in Path(storage.DB_PATH).read_bytes(),
    )

    schedule_id = storage.upsert_schedule(schedule_input(channel_id))
    schedule = storage.get_schedule(schedule_id) or {}
    due = 1_800_000_000.0
    run_id = storage.create_run(schedule, due)
    duplicate = storage.create_run(schedule, due)
    check("相同计划时刻幂等", run_id is not None and duplicate is None)
    assert run_id is not None
    check("任务可领取", storage.claim_run(run_id))
    await round_checks(run_id, channel_id, schedule)
    detail = storage.get_run(run_id) or {}
    check("逐请求证据已持久化", len(detail.get("results", [])) == 18)
    check("运行中任务可在重启后恢复", storage.recover_all_running() == 1)

    thresholds = {
        "min_success_rate": 0.95,
        "max_timeout_rate": 0.05,
        "max_stream_break_rate": 0,
        "max_p95_ms": 1000,
    }
    failed_result = {
        "probe_id": "performance-1", "ok": False, "status": "stream_break",
        "latency_ms": 1500, "ttft_ms": 100, "tokens_per_second": None,
    }
    failed_summary = transport.summarize([failed_result], thresholds)
    check(
        "阈值异常会判失败",
        failed_summary["verdict"] == "fail" and len(failed_summary["reasons"]) >= 2,
        failed_summary,
    )
    timeout_summary = transport.summarize([{
        **failed_result, "status": "timeout", "stream_break": True,
    }], thresholds)
    check(
        "流式超时同时计入超时率和断流率",
        timeout_summary["timeout_rate"] == 1 and timeout_summary["stream_break_rate"] == 1,
        timeout_summary,
    )
    await protocol_checks()
    storage.close()


try:
    asyncio.run(main())
finally:
    storage.close()
    _temp.cleanup()

print("\n失败项：" + ("无" if not failures else "；".join(failures)))
raise SystemExit(1 if failures else 0)
