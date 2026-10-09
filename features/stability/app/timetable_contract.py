"""Pure ten-minute configuration and DST occurrence graph; no database or credentials."""
from __future__ import annotations
import re
from datetime import datetime

from zoneinfo import ZoneInfo

VERSION = "layered-integrity-v2"
METHODS = ("canary", "modeltrace")
GRID = [f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in range(0, 60, 10)]
DEFAULTS = {"canary_times": GRID[::6], "modeltrace_times": GRID,
            "reasoning_effort": "low", "baseline_id": None, "baseline_ids": {}}
WINDOWS = {"modeltrace": 600, "canary": 3600}


def validate_config(value):
    allowed = set(DEFAULTS) | {"registry_channel_ids", "target_bindings", "channel_names", "channel_multipliers"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("十分钟计划配置含未知字段")
    config = {**DEFAULTS, **value}
    for method in METHODS:
        key = method + "_times"
        times = config[key]
        if (not isinstance(times, list) or len(times) > 144 or
                any(not isinstance(t, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5]0", t) for t in times)
                or len(times) != len(set(times))):
            raise ValueError("时刻须为不重复的 HH:MM 十分钟刻度")
        config[key] = sorted(times)
    if config["reasoning_effort"] != "low":
        raise ValueError("十分钟计划固定 Astra / Responses / low")
    if config["baseline_id"] is not None and (type(config["baseline_id"]) is not int or config["baseline_id"] < 1):
        raise ValueError("可信参照 ID 无效")
    ids = config["baseline_ids"]
    if (not isinstance(ids, dict) or any(not isinstance(k, str) or not re.fullmatch(r"[1-9][0-9]*", k) or type(v) is not int or v < 1 for k, v in ids.items())):
        raise ValueError("每渠道可信参照须为 Registry ID 到正整数 baseline ID 的映射")
    config["baseline_ids"] = dict(ids)
    return config


def local_epoch(day, clock, zone):
    local = datetime.fromisoformat(f"{day}T{clock}:00")
    first = local.replace(tzinfo=zone, fold=0)
    stamp = first.timestamp()
    # Round-trip excludes DST gaps; fold=0 selects only the first repeated time.
    return stamp if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == local else None


def build_slots(schedule, day):
    config, zone = schedule["layered_config"], ZoneInfo(schedule["timezone"])
    rows = []
    for clock in sorted(set(config["canary_times"]) | set(config["modeltrace_times"])):
        due = local_epoch(day, clock, zone)
        if due is None:
            continue
        methods = [m for m in METHODS if clock in config[m + "_times"]]
        for channel_id in config["registry_channel_ids"]:
            health_key = f"timetable:{schedule['id']}:{int(due)}:{channel_id}:health"
            for method in ["health", *methods]:
                key = health_key if method == "health" else f"timetable:{schedule['id']}:{int(due)}:{channel_id}:{method}"
                window = min(WINDOWS[m] for m in methods) if method == "health" else WINDOWS[method]
                rows.append({"slot_key": key, "slot": f"{clock}-{method}", "schedule_id": schedule["id"],
                             "budget_date": str(day), "clock": clock, "due": due, "deadline": due + window,
                             "registry_channel_id": channel_id, "method": method, "model": "gpt-6-astra",
                             "protocol": "responses", "dependency": None if method == "health" else health_key,
                             "priority": 0 if method == "modeltrace" or method == "health" and "modeltrace" in methods else 1 if method == "health" else 2})
    return rows
