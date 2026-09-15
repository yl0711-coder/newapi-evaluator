from __future__ import annotations

import math
from collections import Counter
from statistics import median
from typing import Any


SIDES = ("candidate", "reference")


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _ratio(candidate: Any, reference: Any) -> float | None:
    left = _number(candidate)
    right = _number(reference)
    if left is None or right is None or right <= 0:
        return None
    return round(left / right, 3)


def _difference(candidate: Any, reference: Any) -> int | None:
    left = _number(candidate)
    right = _number(reference)
    if left is None or right is None:
        return None
    return round(left - right)


def _error_category(measurement: dict[str, Any] | None) -> str:
    if not measurement:
        return "missing"
    return str(measurement.get("error_category") or measurement.get("status") or "unknown")


def _error_family(measurement: dict[str, Any] | None) -> str:
    category = _error_category(measurement)
    if category in {"connect_timeout", "read_timeout", "pool_timeout", "timeout", "total_timeout"}:
        return "timeout"
    if category.startswith("http_5"):
        return "http_5xx"
    return category


def _attribution(
    candidate: dict[str, Any] | None,
    reference: dict[str, Any] | None,
) -> tuple[str, str]:
    candidate_ok = bool(candidate and candidate.get("ok"))
    reference_ok = bool(reference and reference.get("ok"))
    if candidate_ok and reference_ok:
        return "normal", "双端均正常"
    if not candidate_ok and reference_ok:
        return "candidate", "候选端独有异常"
    if candidate_ok and not reference_ok:
        return "reference", "参照端独有异常"
    if candidate is None or reference is None:
        return "unknown", "证据不完整"
    if _error_family(candidate) == _error_family(reference):
        return "shared", "双端共同异常，不能归责候选端"
    return "separate", "双端分别出现不同异常"


def _answer_lengths(
    responses: dict[tuple[int, str, str], dict[str, Any]],
    round_number: int,
    question_id: str,
) -> tuple[int, int]:
    values = []
    for side in SIDES:
        response = responses.get((round_number, question_id, side), {})
        values.append(len(str(response.get("content") or "")))
    return values[0], values[1]


def build_pairs(report: dict[str, Any]) -> list[dict[str, Any]]:
    measurements: dict[tuple[int, str, str], dict[str, Any]] = {}
    for item in report.get("measurements", []):
        if not isinstance(item, dict):
            continue
        key = (int(item.get("round") or 0), str(item.get("question_id") or ""), str(item.get("side") or ""))
        measurements[key] = item
    responses: dict[tuple[int, str, str], dict[str, Any]] = {}
    for item in report.get("responses", []):
        if not isinstance(item, dict):
            continue
        key = (int(item.get("round") or 0), str(item.get("question_id") or ""), str(item.get("side") or ""))
        responses[key] = item

    question_lookup = {
        str(item.get("id")): item
        for item in report.get("questions", [])
        if isinstance(item, dict) and item.get("id")
    }
    keys = sorted({(round_number, question_id) for round_number, question_id, _side in measurements})
    pairs = []
    for round_number, question_id in keys:
        candidate = measurements.get((round_number, question_id, "candidate"))
        reference = measurements.get((round_number, question_id, "reference"))
        source = candidate or reference or {}
        round_role = str(source.get("round_role") or ("warmup" if report.get("warmup_rounds") and round_number <= int(report["warmup_rounds"]) else "evaluated"))
        attribution, attribution_label = _attribution(candidate, reference)
        candidate_length, reference_length = _answer_lengths(responses, round_number, question_id)
        started = [_number(item.get("request_started_ms")) if item else None for item in (candidate, reference)]
        start_skew = round(abs(started[0] - started[1])) if all(value is not None for value in started) else None
        first_answer_ratio = _ratio(
            candidate.get("first_answer_ms") if candidate else None,
            reference.get("first_answer_ms") if reference else None,
        )
        total_ratio = _ratio(
            candidate.get("total_ms") if candidate else None,
            reference.get("total_ms") if reference else None,
        )
        speed_label = "不可比较"
        if candidate and reference and candidate.get("ok") and reference.get("ok") and total_ratio is not None:
            if total_ratio > 1.2:
                speed_label = "候选端较慢"
            elif total_ratio < 0.833:
                speed_label = "候选端较快"
            else:
                speed_label = "耗时接近"
        question = question_lookup.get(question_id, {})
        candidate_response = responses.get((round_number, question_id, "candidate"), {})
        pair_id = str(source.get("pair_id") or f"{report.get('run_id', 'legacy')}:{round_number}:{question_id}")
        pairs.append(
            {
                "pair_id": pair_id,
                "round": round_number,
                "round_role": round_role,
                "question_id": question_id,
                "title": question.get("title") or question_id,
                "difficulty": question.get("difficulty") or "unknown",
                "variant_id": candidate_response.get("variant_id") or "original",
                "candidate_ok": bool(candidate and candidate.get("ok")),
                "reference_ok": bool(reference and reference.get("ok")),
                "candidate_status": str(candidate.get("status") or "missing") if candidate else "missing",
                "reference_status": str(reference.get("status") or "missing") if reference else "missing",
                "attribution": attribution,
                "attribution_label": attribution_label,
                "first_answer_difference_ms": _difference(
                    candidate.get("first_answer_ms") if candidate else None,
                    reference.get("first_answer_ms") if reference else None,
                ),
                "first_answer_ratio": first_answer_ratio,
                "total_difference_ms": _difference(
                    candidate.get("total_ms") if candidate else None,
                    reference.get("total_ms") if reference else None,
                ),
                "total_ratio": total_ratio,
                "speed_label": speed_label,
                "start_skew_ms": start_skew,
                "candidate_answer_chars": candidate_length,
                "reference_answer_chars": reference_length,
                "answer_length_ratio": _ratio(candidate_length, reference_length),
                "ability_review": "人工查看双方原始回答",
            }
        )
    return pairs


def _identity(endpoint: dict[str, Any], measurements: list[dict[str, Any]]) -> dict[str, Any]:
    requested = str(endpoint.get("model") or "")
    values: set[str] = set()
    for item in measurements:
        if item.get("actual_model"):
            values.add(str(item["actual_model"]))
        for value in item.get("actual_model_values") or []:
            if value:
                values.add(str(value))
    actual = sorted(values)
    if not actual:
        status, label = "unverified", "身份未验证"
    elif len(actual) > 1:
        status, label = "mixed", "响应模型字段不一致"
    elif actual[0].casefold() == requested.casefold():
        status, label = "matched", "响应模型字段匹配"
    else:
        status, label = "mismatch", "响应模型字段不一致"
    return {"status": status, "label": label, "requested_model": requested, "actual_models": actual}


def build_summary(report: dict[str, Any], pairs: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [pair for pair in pairs if pair["round_role"] == "evaluated"]
    counts = Counter(pair["attribution"] for pair in evaluated)
    valid = [pair for pair in evaluated if pair["attribution"] == "normal"]
    comparable = [pair for pair in valid if pair["total_ratio"] is not None]
    candidate_slower = sum(pair["total_ratio"] > 1.2 for pair in comparable)
    candidate_faster = sum(pair["total_ratio"] < 0.833 for pair in comparable)
    length_ratios = [pair["answer_length_ratio"] for pair in comparable if pair["answer_length_ratio"] is not None]
    median_length_ratio = median(length_ratios) if length_ratios else None
    required_repetition = max(2, math.ceil(len(comparable) * 0.6)) if comparable else 0
    if not comparable:
        speed = {"status": "insufficient", "label": "速度证据不足", "comparable_pairs": 0}
    elif candidate_slower >= required_repetition:
        speed = {"status": "candidate_slower", "label": "候选端存在持续单边延迟", "comparable_pairs": len(comparable)}
    elif candidate_faster >= required_repetition:
        if median_length_ratio is not None and median_length_ratio < 0.7:
            speed = {"status": "candidate_faster_shorter", "label": "候选端更快但回答明显更短，需结合能力判断", "comparable_pairs": len(comparable)}
        else:
            speed = {"status": "candidate_faster", "label": "候选端整体较快", "comparable_pairs": len(comparable)}
    else:
        speed = {"status": "similar", "label": "未发现持续单边速度偏差", "comparable_pairs": len(comparable)}
    ratios = [pair["total_ratio"] for pair in comparable]
    speed["median_total_ratio"] = round(median(ratios), 3) if ratios else None
    speed["median_answer_length_ratio"] = round(median_length_ratio, 3) if median_length_ratio is not None else None

    if not evaluated:
        stability = {"status": "insufficient", "label": "稳定性证据不足"}
    elif counts["candidate"]:
        stability = {"status": "candidate_issue", "label": "存在候选端独有异常"}
    elif counts["shared"] or counts["separate"] or counts["unknown"]:
        stability = {"status": "insufficient", "label": "存在共同或不可归因异常"}
    else:
        stability = {"status": "similar", "label": "本轮未发现候选端独有异常"}

    if counts["candidate"]:
        overall = {"status": "candidate_deviation", "label": "存在候选端单边运行偏差"}
    elif not evaluated or not valid:
        overall = {"status": "insufficient", "label": "证据不足，建议人工复核"}
    elif speed["status"] == "candidate_slower":
        overall = {"status": "candidate_deviation", "label": "存在候选端单边运行偏差"}
    elif counts["shared"] or counts["separate"] or counts["unknown"]:
        overall = {"status": "insufficient", "label": "本轮有效对照不足"}
    else:
        overall = {"status": "runtime_similar", "label": "本轮运行表现基本接近"}

    explanations = [
        "能力不由系统自动打分，请直接查看逐题双方回答后决定是否准入。",
        f"有效轮共 {len(evaluated)} 个题对，其中 {len(valid)} 个双端均正常。",
    ]
    if counts["candidate"]:
        explanations.append(f"发现 {counts['candidate']} 个候选端独有异常。")
    elif counts["shared"]:
        explanations.append(f"发现 {counts['shared']} 个双端共同异常，不直接归责候选端。")
    else:
        explanations.append("本轮未发现候选端独有异常。")
    explanations.append(speed["label"] + "。")

    measured = [item for item in report.get("measurements", []) if item.get("round_role") == "evaluated"]
    identities = {
        side: _identity(report.get(side, {}), [item for item in measured if item.get("side") == side])
        for side in SIDES
    }
    return {
        "overall": overall,
        "ability": {"status": "manual", "label": "查看逐题回答后人工判断"},
        "speed": speed,
        "stability": stability,
        "counts": {
            "candidate_only": counts["candidate"],
            "reference_only": counts["reference"],
            "shared": counts["shared"],
            "separate": counts["separate"],
            "unknown": counts["unknown"],
            "both_normal": counts["normal"],
        },
        "evidence": {
            "test_rounds": int(report.get("rounds") or 0),
            "warmup_rounds": int(report.get("warmup_rounds") or 0),
            "evaluated_rounds": len({pair["round"] for pair in evaluated}),
            "evaluated_pairs": len(evaluated),
            "valid_pairs": len(valid),
            "confidence": "中" if valid else "低",
        },
        "model_identity": identities,
        "explanations": explanations[:5],
    }


def finalize_report(report: dict[str, Any]) -> dict[str, Any]:
    result = dict(report)
    result.setdefault("warmup_rounds", 0)
    pairs = build_pairs(result)
    result["pairs"] = pairs
    result["summary"] = build_summary(result, pairs)
    return result


def public_report(report: dict[str, Any]) -> dict[str, Any]:
    finalized = finalize_report(report)
    def public_endpoint(value: Any) -> dict[str, Any]:
        endpoint = value if isinstance(value, dict) else {}
        return {key: endpoint.get(key) for key in ("name", "model", "protocol") if endpoint.get(key) is not None}
    return {
        "version": finalized.get("version"),
        "created_at": finalized.get("created_at"),
        "status": finalized.get("status"),
        "rounds": finalized.get("rounds"),
        "warmup_rounds": finalized.get("warmup_rounds", 0),
        "candidate": public_endpoint(finalized.get("candidate")),
        "reference": public_endpoint(finalized.get("reference")),
        "summary": finalized.get("summary", {}),
        "pairs": [
            {key: value for key, value in pair.items() if key not in {"pair_id"}}
            for pair in finalized.get("pairs", [])
            if pair.get("round_role") == "evaluated"
        ],
    }
