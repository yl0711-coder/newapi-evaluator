from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from features.daily_checks.domain import MT_ALIASES, identity_verdict
from features.integrity.strategies import canonical_hash, get_strategy, load_bank

VERSION = "newapi-channel-monitor-v3"
INTERVAL = 1800
MAX_ATTEMPTS = 5
QUALITY_ROUNDS = {"quality_failure": 2, "mt_mismatch": 3}
MIN_CHANNELS = 4
ROUND_WINDOW = 1100
RETRY_DELAYS = (0, 2, 4, 8, 16)
FLOOR_RECHECK_DELAY = 60
CIRCUIT_FAILURES = 2
RETRYABLE = frozenset({"network_error", "timeout", "empty_response", "stream_break",
                       "upstream_error", "rate_limited", "upstream_5xx", "auth_error", "http_error"})


def retryable(status):
    return status in RETRYABLE or bool(re.fullmatch(r"http_5\d\d", str(status)))


def contract():
    return {"version": VERSION, "interval_seconds": INTERVAL, "max_attempts_per_question": MAX_ATTEMPTS,
            "quality_rounds": dict(QUALITY_ROUNDS), "minimum_channels": MIN_CHANNELS,
            "modeltrace_after_completed_primary_candy": True, "modeltrace_probes_per_round": 3,
            "transport_failure_bypasses_minimum": False, "recovery_success_rounds": 1,
            "retry_delays_seconds": list(RETRY_DELAYS), "floor_recheck_delay_seconds": FLOOR_RECHECK_DELAY,
            "circuit_failure_channels": CIRCUIT_FAILURES, "circuit_scope": "all_automatic_disables",
            "circuit_release": "later_scheduled_round_all_unlocked_bindings_healthy_minimum_two",
            "floor_recheck": "one_connectivity_only_candy_after_exhausted_transport_before_disabling",
            "recovery_requires_fresh_feishu_online_inventory": True,
            "recovery_requires_correct_candy_and_matching_mt": True,
            "identity_authenticated": False, "calibration_status": "unvalidated",
            "candy_expected_answer": 21, "candy_scorer": "candy-final-integer-v1",
            "round_window_seconds": ROUND_WINDOW, "schedule_basis": "fixed_start_to_start",
            "missed_slots": "skip", "maximum_questions_per_binding": 5,
            "maximum_attempts_per_binding": 21, "supported_newapi_channel_types": [1]}


def endpoint(value):
    try:
        parts = urlsplit(value.strip().rstrip("/"))
        parts.port
    except (ValueError, AttributeError):
        raise ValueError("地址格式无效") from None
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment):
        raise ValueError("地址须为不含凭据、查询参数及片段的 HTTP 地址")
    return parts.geturl().rstrip("/")


def upstream_root(value):
    parts = urlsplit(endpoint(value))
    return (parts.scheme, parts.hostname.casefold(), parts.port,
            re.sub(r"/v1$", "", parts.path))


def next_due(now):
    return (int(now) // INTERVAL + 1) * INTERVAL


def candy_probe():
    questions = json.loads((Path(__file__).parents[1] / "admission/questions.json").read_text())
    source = next(q for q in questions if q["id"] == "hard-3")
    return {"id": "candy", "prompt": source["prompt"] + "\n最后一行严格使用：答案：<整数>。",
            "max_tokens": source["max_tokens"], "stream": False, "reasoning_effort": "low",
            "request_timeout_seconds": 60}


def grade_candy(text):
    # SOURCE: standalone scheduled runner's candy_answer_is_21 contract.
    if not isinstance(text, str):
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    line = lines[-1].strip("`* ")
    match = re.fullmatch(r"(?:(?:最终答案|答案)\s*(?:(?:是|为)\s*)?[:：]?\s*)?([0-9]+)\s*(?:颗|个)?[。.!！]?\s*", line)
    if match:
        return (match.group(1).lstrip("0") or "0") == "21"
    return None


def mt_verdict(score):
    if (score.get("valid_answers") != 3 or score.get("invalid_answers") != 0
            or identity_verdict(score, "completed", 0, 3, 3) == "无法判定"):
        return "unknown"
    expected = score.get("expected_model")
    if expected and score.get("prediction") == expected:
        return "match"
    return "mismatch" if score.get("source_verdict") == "MISMATCH" else "unknown"


def expected_models():
    return [m["id"] for m in load_bank(get_strategy("modeltrace"))["models"]]


def expected_model(value):
    return MT_ALIASES.get(value, value)


def condition_hash(snapshot, remote_hash, model, expected):
    return canonical_hash({"target": snapshot, "remote": remote_hash, "model": model, "expected": expected,
                           "contract": contract(), "candy": candy_probe(),
                           "modeltrace": get_strategy("modeltrace").manifest_hash})


def validate_mapping(remote, target, model):
    if (remote["type"] != 1 or remote["multi_key"] or remote["overrides"]
            or target.channel["protocol"] not in {"openai", "responses"}):
        raise ValueError("仅支持单密钥、无额外转换的 OpenAI 兼容渠道")
    if model not in remote["models"] or upstream_root(remote["base_url"]) != upstream_root(target.channel["base_url"]):
        raise ValueError("New API 渠道地址或模型与直测目标不一致")
    try:
        mapping = json.loads(remote["model_mapping"] or "{}")
    except (ValueError, TypeError):
        raise ValueError("New API 模型映射无效") from None
    if not isinstance(mapping, dict) or mapping.get(model, model) != target.channel["model"]:
        raise ValueError("New API 模型映射与直测目标不一致")


def protection(target, inventory):
    """Disabling a whole channel affects every model/group served by that channel."""
    deficient = []
    for group in target["groups"]:
        for model in target["models"]:
            remaining = sum(c["id"] != target["id"] and c["status"] == 1
                            and group in c["groups"] and model in c["models"] for c in inventory)
            if remaining < MIN_CHANNELS:
                deficient.append({"group": group, "model": model, "remaining": remaining})
    return deficient
