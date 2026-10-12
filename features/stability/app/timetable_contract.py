"""Half-hour defaults on a ten-minute grid; no database or credentials."""
from __future__ import annotations
import re
from datetime import datetime

from zoneinfo import ZoneInfo

VERSION = "layered-integrity-v3"
LEGACY_VERSIONS = ("layered-integrity-v1", "layered-integrity-v2")
METHODS = ("modeltrace", "nerfed")
GRID = [f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in range(0, 60, 10)]
DEFAULT_TIMES = GRID[::3]
DEFAULTS = {"modeltrace_times": DEFAULT_TIMES, "nerfed_times": DEFAULT_TIMES, "reasoning_effort": "low"}
WINDOWS = {"modeltrace": 600, "nerfed": 600}


def upgrade_config(config, version):
    empty = version == "layered-integrity-v2" and all(config.get(method + "_times") == [] for method in ("modeltrace", "canary"))
    times = [] if empty else DEFAULT_TIMES
    return {**{k: config[k] for k in ("registry_channel_ids", "target_bindings", "channel_names", "channel_multipliers") if k in config},
            "channel_names": dict(config.get("channel_names", {})),
            "channel_multipliers": dict(config.get("channel_multipliers", {})),
            "modeltrace_times": list(times), "nerfed_times": list(times), "reasoning_effort": "low",
            "legacy_plan": {"version": version, "config": config}}


def validate_config(value):
    allowed = set(DEFAULTS) | {"registry_channel_ids", "target_bindings", "channel_names", "channel_multipliers", "legacy_plan"}
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
    if config["modeltrace_times"] != config["nerfed_times"]:
        raise ValueError("MT 与 Nerfed 必须在同一十分钟时刻采样")
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
    for clock in config["modeltrace_times"]:
        due = local_epoch(day, clock, zone)
        if due is None:
            continue
        methods = list(METHODS)
        for channel_id in config["registry_channel_ids"]:
            for method in methods:
                key = f"sampling-v3:{schedule['id']}:{int(due)}:{channel_id}:{method}"
                window = WINDOWS[method]
                rows.append({"slot_key": key, "slot": f"{clock}-{method}", "schedule_id": schedule["id"],
                             "budget_date": str(day), "clock": clock, "due": due, "deadline": due + window,
                             "registry_channel_id": channel_id, "method": method, "model": "gpt-6-astra",
                             "protocol": "responses", "dependency": None, "priority": 0})
    return rows
