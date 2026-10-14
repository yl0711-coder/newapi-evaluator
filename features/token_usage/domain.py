"""The shared input-token contract for admission, schedules and Monitor."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

VERSION = "input-token-paired-v1"
INTERVAL_SECONDS = 1800
MAX_REQUESTS = 4
MAX_REPORTED_TOKENS = 2**63 - 1
LABELS = {"high_risk": "输入 Token 用量上报显著偏高；实际计费待核实",
          "minor_anomaly": "连续两轮输入 Token 用量上报偏高；实际计费待核实",
          "pending_confirmation": "输入 Token 用量略高，等待下一轮同条件定时确认",
          "normal": "固定输入双端用量未发现异常",
          "not_reproduced": "固定输入未复现日志异常，日志证据保留",
          "unable_to_determine": "输入 Token 用量无法判定"}
PROTOCOL_ALIASES = {"openai": "openai", "chat": "openai", "chat_completions": "openai",
                    "responses": "responses", "anthropic": "anthropic", "messages": "anthropic"}
# Independent synthetic fixture, without customer or upstream content.
FIXED_INPUT = "Read the synthetic inventory below. Reply only OK.\n" + "\n".join(
    f"record-{i:04d}: amber birch cobalt delta elm fern granite harbor ivory juniper kiln linen moss north oak pearl quartz reed silver timber umber violet willow xenon yellow zinc."
    for i in range(192))
PARAMETERS = {"stream": True, "max_tokens": 32}
ASTRA_PARAMETERS = {"stream": True, "max_tokens": 4096, "reasoning_effort": "low"}
PARAMETER_POLICY = {"default": PARAMETERS, "gpt-6-astra": ASTRA_PARAMETERS}
DEFAULT_THRESHOLDS = {"minor_ratio": 1.10, "minor_difference": 128}


def thresholds(value=None):
    value = dict(DEFAULT_THRESHOLDS if value is None else value)
    if set(value) != set(DEFAULT_THRESHOLDS):
        raise ValueError("invalid_threshold_fields")
    ratio, difference = value["minor_ratio"], value["minor_difference"]
    if type(ratio) not in {float, int} or not 1.01 <= ratio < 2 or type(difference) is not int or not 1 <= difference <= 4096:
        raise ValueError("invalid_minor_thresholds")
    return {"minor_ratio": float(ratio), "minor_difference": difference}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


CONDITION_HASH = digest({"version": VERSION, "input_sha256": hashlib.sha256(FIXED_INPUT.encode()).hexdigest(),
                         "parameter_policy": PARAMETER_POLICY, "thresholds": DEFAULT_THRESHOLDS})


def condition_hash(custom=None):
    return digest({"version": VERSION, "input_sha256": hashlib.sha256(FIXED_INPUT.encode()).hexdigest(),
                   "parameter_policy": PARAMETER_POLICY, "thresholds": thresholds(custom)})


def protocol(value):
    if not isinstance(value, str) or value.lower() not in PROTOCOL_ALIASES:
        raise ValueError("unsupported_protocol")
    return PROTOCOL_ALIASES[value.lower()]


def tokens(value):
    return value if type(value) is int and 0 <= value <= MAX_REPORTED_TOKENS else None


def normalize_usage(usage, wire_protocol):
    """Return reported counts only; missing fields are never token estimates."""
    if not isinstance(usage, dict):
        return None
    kind = protocol(wire_protocol)
    names = ("prompt_tokens", "input_tokens") if kind == "openai" else ("input_tokens",)
    supplied = [usage[name] for name in names if name in usage]
    if not supplied:
        return None
    raw = tokens(supplied[0])
    invalid = any(tokens(value) is None for value in supplied) or len(set(str(v) for v in supplied)) > 1
    creation = read = cached = None
    normalized = raw
    if kind == "anthropic":
        creation = tokens(usage.get("cache_creation_input_tokens"))
        read = tokens(usage.get("cache_read_input_tokens"))
        if "cache_creation_input_tokens" in usage or "cache_read_input_tokens" in usage:
            invalid = invalid or creation is None or read is None
            normalized = raw + creation + read if raw is not None and creation is not None and read is not None else None
            invalid = invalid or (normalized is not None and tokens(normalized) is None)
    else:
        details = usage.get("prompt_tokens_details") if kind == "openai" else usage.get("input_tokens_details")
        if isinstance(details, dict) and "cached_tokens" in details:
            cached = tokens(details["cached_tokens"])
            invalid = invalid or cached is None or raw is None or cached > raw
        # OpenAI input usage already includes cached tokens.
    return {"input_tokens": raw, "cache_creation_input_tokens": creation,
            "cache_read_input_tokens": read, "cached_tokens": cached,
            "normalized_input_tokens": None if invalid else normalized, "invalid": bool(invalid)}


def observation(trace, *, status, terminal=False, transport_status=None):
    first, final = (trace[0], trace[-1]) if trace else (None, None)
    values = {row["normalized_input_tokens"] for row in trace if row["normalized_input_tokens"] is not None}
    conflict = len(values) > 1
    invalid = any(row["invalid"] or (row["normalized_input_tokens"] is not None and tokens(row["normalized_input_tokens"]) is None) for row in trace)
    count = tokens(final["normalized_input_tokens"]) if final else None
    return {"status": status, "transport_status": transport_status or status, "terminal": terminal,
            "valid": status == "completed" and terminal and not conflict and not invalid and count is not None and count > 0,
            "normalized_input_tokens": count, "first_usage": first, "final_usage": final,
            "input_usage_delta": final["input_tokens"] - first["input_tokens"] if first and final and first["input_tokens"] is not None and final["input_tokens"] is not None else None,
            "usage_trace": trace, "usage_conflict": conflict, "usage_invalid": invalid,
            "source": "upstream_reported_usage"}


def empty_result(reason, *, source="fixed_retest", log_evidence=None):
    return {"detection_version": VERSION, "condition_hash": CONDITION_HASH, "status": "unable_to_determine",
            "severity": "unknown", "conclusion": "unable_to_determine", "label": LABELS["unable_to_determine"], "reason": reason,
            "ratio": None, "difference": None, "candidate_input_tokens": None, "reference_input_tokens": None,
            "billing_status": "unverified", "source": source, "pairs": [], "requests_sent": 0,
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "log_evidence": log_evidence, "minor_streak": 0}


def classify_pair(candidate, reference, *, previous=None, source="fixed_retest", frozen_hash=None,
                  occurrence=None, log_evidence=None, threshold_config=None, previous_occurrence=None):
    configured = thresholds(threshold_config)
    current_hash = condition_hash(configured)
    result = empty_result("invalid_usage_or_terminal", source=source, log_evidence=log_evidence)
    result.update(frozen_hash=frozen_hash, occurrence=occurrence, condition_hash=current_hash, thresholds=configured)
    def usable(obs):
        count = tokens(obs.get("normalized_input_tokens"))
        return obs.get("valid") and count is not None and count > 0

    if not usable(candidate) or not usable(reference):
        for side, obs in (("reference", reference), ("candidate", candidate)):
            if usable(obs):
                continue
            if obs.get("usage_conflict"):
                result.update(reason="input_usage_changed_within_request", conflicting_side=side,
                              label="同一请求的输入用量上报发生变化，双端对照无法可靠判定；实际计费待核实")
            elif not obs.get("terminal"):
                result.update(reason=f"{side}_terminal_missing", label=f"{'参照端' if side == 'reference' else '候选端'}未完整结束，输入 Token 用量无法判定")
            elif obs.get("usage_invalid") or (obs.get("normalized_input_tokens") is not None and tokens(obs["normalized_input_tokens"]) is None):
                result.update(reason=f"{side}_usage_invalid", label=f"{'参照端' if side == 'reference' else '候选端'}输入用量字段无效，无法可靠判定；实际计费待核实")
            elif obs.get("normalized_input_tokens") == 0:
                result.update(reason=f"{side}_input_usage_zero", label=f"{'参照端' if side == 'reference' else '候选端'}输入用量上报为零，无法可靠判定；实际计费待核实")
            else:
                result.update(reason=f"{side}_input_usage_missing", label=f"{'参照端' if side == 'reference' else '候选端'}未返回输入用量，无法可靠判定；实际计费待核实")
            break
        return result
    c, r = candidate["normalized_input_tokens"], reference["normalized_input_tokens"]
    ratio, difference = c / r, c - r
    numerator, denominator = Decimal(str(configured["minor_ratio"])).as_integer_ratio()
    result.update(ratio=ratio, difference=difference, candidate_input_tokens=c, reference_input_tokens=r)
    if c >= 2 * r and difference >= 1024:
        result.update(status="completed", severity="high_risk", conclusion="high_risk", reason="major_input_token_anomaly")
    elif c * denominator >= r * numerator and difference >= configured["minor_difference"]:
        adjacent = (occurrence is not None and (
            source == "scheduled" and previous_occurrence is None and previous and previous.get("occurrence") == occurrence - INTERVAL_SECONDS
            or source == "daily_scheduled" and previous_occurrence is not None and previous_occurrence < occurrence
            and previous and previous.get("occurrence") == previous_occurrence))
        successive = (source in {"scheduled", "daily_scheduled"} and adjacent and previous and previous.get("source") == source
                      and previous.get("conclusion") in {"pending_confirmation", "minor_anomaly"}
                      and frozen_hash is not None and previous.get("frozen_hash") == frozen_hash
                      and previous.get("condition_hash") == current_hash)
        result.update(status="completed" if successive else "pending", severity="warning" if successive else "pending",
                      conclusion="minor_anomaly" if successive else "pending_confirmation",
                      reason="successive_scheduled_minor" if successive else "minor_requires_successive_scheduled_round",
                      minor_streak=(previous.get("minor_streak", 1) + 1) if successive else 1)
    else:
        result.update(status="completed", severity="normal", conclusion="not_reproduced" if log_evidence else "normal",
                      reason="fixed_input_did_not_reproduce" if log_evidence else "within_threshold")
    result["label"] = LABELS[result["conclusion"]]
    return result
