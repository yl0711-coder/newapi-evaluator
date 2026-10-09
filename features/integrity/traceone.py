"""MIT-attributed TraceOne v12 optimized inference, adapted to stdlib Python.

Source: wangchao0708/TraceOne 9e0705944153d7a4f01fffa82db854bf68661764.
Modifications: body-free parser projections, protocol gating, standard-library
matrix operations, and explicit ordinary-API unvalidated conclusions. No fitting,
old v7 artifact, or bank outer-guard veto is used. See NOTICE.md.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Sequence


def parse_grid_response(text: str) -> dict:
    try:
        payload = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return {"numbers": [], "row_counts": [], "format_compliant": False,
                "format_errors": ["invalid_json"], "analyzable": False, "parsed_numbers": 0}
    if isinstance(payload, dict) and set(payload) == {"numbers"}:
        payload = payload["numbers"]
    if not isinstance(payload, list):
        return {"numbers": [], "row_counts": [], "format_compliant": False,
                "format_errors": ["missing_grid_array"], "analyzable": False, "parsed_numbers": 0}
    numbers, counts, errors = [], [], []
    if len(payload) != 9:
        errors.append("wrong_row_count")
    observed = 0
    rows_are_arrays = True
    for row_index, row in enumerate(payload):
        if not isinstance(row, list):
            rows_are_arrays = False
            counts.append(0)
            errors.append("row_not_array")
            continue
        observed += len(row)
        if len(row) != 35:
            errors.append("row_wrong_count")
        valid_row = []
        for value in row:
            if type(value) is not int:
                errors.append("item_not_integer")
            elif not 1 <= value <= 355:
                errors.append("item_out_of_range")
            else:
                valid_row.append(value)
        counts.append(len(valid_row))
        numbers.extend(valid_row)
    if observed != 315:
        errors.append("wrong_count")
    analyzable = len(payload) == 9 and rows_are_arrays and all(25 <= count <= 45 for count in counts) \
        and 280 <= len(numbers) <= 350
    return {"numbers": numbers, "row_counts": counts, "format_compliant": not errors,
            # These are reason categories, not one unbounded diagnostic per item.
            # Keep every distinct reason while fitting the durable projection contract.
            "format_errors": list(dict.fromkeys(errors)), "analyzable": analyzable, "parsed_numbers": len(numbers)}


def _split(values: Sequence[int], count: int) -> list[list[int]]:
    quotient, remainder = divmod(len(values), count)
    blocks, start = [], 0
    for index in range(count):
        size = quotient + (index < remainder)
        blocks.append(list(values[start:start + size]))
        start += size
    return blocks


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def sequence_structure(numbers: Sequence[int]) -> list[float]:
    if not 280 <= len(numbers) <= 350:
        raise ValueError("TraceOne needs 280-350 integers")
    blocks = [list(numbers)] + _split(numbers, 9) + [list(numbers[:count]) for count in (70, 140, 210, 280)]
    result = []
    for block in blocks:
        counts = Counter(block)
        mean = _mean(block)
        delta = [right - left for left, right in zip(block, block[1:])]
        result.extend([len(counts) / len(block), max(counts.values()),
                       sum(value ** 2 for value in counts.values()) / len(block), mean / 355,
                       math.sqrt(_mean([(value - mean) ** 2 for value in block])) / 355,
                       _mean([abs(value) for value in delta]) / 355,
                       _mean([value > 0 for value in delta]), _mean([abs(value) <= 10 for value in delta]),
                       _mean([value % 2 == 0 for value in block]), _mean([value % 5 == 0 for value in block]),
                       _mean([value % 10 == 7 for value in block])])
    counts = Counter(numbers)
    result.extend(sum(value >= count for value in counts.values()) for count in range(2, 7))
    result.extend(sum(value == count for value in counts.values()) for count in range(1, 6))
    mean = _mean(numbers)
    for lag in range(1, 9):
        pairs = list(zip(numbers[:-lag], numbers[lag:]))
        result.extend([_mean([left == right for left, right in pairs]),
                       _mean([abs(left - right) for left, right in pairs]) / 355,
                       _mean([(left - mean) * (right - mean) for left, right in pairs]) / 355 ** 2])
    return result


def js_similarity(counts: Sequence[int], reference_counts: Sequence[int]) -> float:
    total = sum(counts)
    if not total:
        return 0.0
    reference_total = sum(reference_counts) + 0.5 * 355
    divergence = 0.0
    for count, reference_count in zip(counts, reference_counts):
        p, q = count / total, (reference_count + 0.5) / reference_total
        middle = (p + q) / 2
        divergence += ((p * math.log(p / middle) if p else 0) + q * math.log(q / middle)) / 2
    return 1 - math.sqrt(max(0.0, divergence) / math.log(2))


def feature_groups(numbers: Sequence[int], bank: dict) -> dict[str, list[float]]:
    from .scoring import count_numbers, robust_score_numbers
    components = robust_score_numbers(numbers, bank)
    counts = count_numbers(numbers)
    entries = {model["id"]: model for model in bank["models"]}
    similarities = [js_similarity(counts, entries[model]["counts"]) for model in bank["robust"]["model_order"]]
    blocks = _split(numbers, 9)
    digits, positions = [], []
    for block in blocks:
        digit_count, position_count = [0] * 10, [0] * 8
        for number in block:
            digit_count[number % 10] += 1
            position_count[min((number - 1) * 8 // 355, 7)] += 1
        digits.extend(value / len(block) for value in digit_count)
        positions.extend(value / len(block) for value in position_count)
    bins = [min((number - 1) * 6 // 355, 5) for number in numbers]
    transitions = [0] * 36
    for left, right in zip(bins, bins[1:]):
        transitions[left * 6 + right] += 1
    return {"bank": components["fused"] + components["marginal"] + similarities,
            "raw": [count / len(numbers) for count in counts], "structure": sequence_structure(numbers),
            "order": digits + [count / (len(numbers) - 1) for count in transitions] + positions}


def ridge_predict(feature: Sequence[float], artifact: dict) -> list[float]:
    mean, scale, multiplier, weights = (artifact[key] for key in (
        "feature_mean", "feature_scale", "feature_multiplier", "weights"))
    if not len(feature) == len(mean) == len(scale) == len(multiplier) == len(weights):
        raise ValueError("TraceOne ridge dimensions mismatch")
    standardized = [(value - center) / divisor * factor for value, center, divisor, factor in
                    zip(feature, mean, scale, multiplier)]
    return [sum(value * weights[row][column] for row, value in enumerate(standardized)) + target_mean
            for column, target_mean in enumerate(artifact["target_mean"])]


def support_evaluate(feature: Sequence[float], winner: int, margin: float, artifact: dict) -> dict:
    support = artifact["support"]
    if len(feature) != len(support["feature_mean"]) or len(feature) != len(support["precision"]):
        raise ValueError("TraceOne support dimensions mismatch")
    residual = [(value - center) / divisor - centroid for value, center, divisor, centroid in zip(
        feature, support["feature_mean"], support["feature_scale"], support["centroids"][winner])]
    precision = support["precision"]
    distance = sum(left * sum(value * right for value, right in zip(row, residual))
                   for left, row in zip(residual, precision))
    threshold = float(support["thresholds"][winner])
    calibration = support["calibration_distances"][winner]
    tolerance = 1e-10 * max(1, abs(distance))
    p_value = (1 + sum(value >= distance - tolerance for value in calibration)) / (len(calibration) + 1)
    margin_passed = margin >= artifact["minimum_margin"] - 1e-12
    support_passed = distance <= threshold + 1e-10 * max(1, abs(distance), abs(threshold))
    return {"support_passed": margin_passed and support_passed, "support_distance": distance,
            "support_threshold": threshold, "support_p_value": p_value, "margin_passed": margin_passed,
            "support_path": "optimized_distance" if margin_passed and support_passed else
            "optimized_margin" if not margin_passed else "optimized_rejected"}


def score_traceone(manifest, observations: Sequence[dict], expected_model: str) -> dict:
    from .scoring import _base
    from .strategies import load_artifact, load_bank
    bank, artifact = load_bank(manifest), load_artifact(manifest)
    if artifact["bank_model_order"] != bank["robust"]["model_order"] or artifact["classifier"] != "ridge":
        raise ValueError("TraceOne atomic artifact mismatch")
    valid = [item for item in observations if item["valid"]]
    result = {**_base(manifest), "valid_answers": len(valid), "invalid_answers": len(observations) - len(valid),
              "requested_answers": len(observations), "missing_answers": 1 - len(observations),
              "status": "insufficient_data", "source_verdict": "UNLISTED" if expected_model and expected_model not in
              artifact["models"] else "UNKNOWN", "expected_model": expected_model,
              "prediction": None, "probability": None, "adapter_scores": {}, "adapter_margin": None,
              "support_passed": None, "support_distance": None, "support_threshold": None, "support_p_value": None,
              "diagnostics": [{key: item.get(key) for key in ("probe_id", "valid", "invalid_reason", "parsed_numbers",
                              "row_counts", "format_compliant", "format_errors", "analyzable")} for item in observations]}
    if not valid:
        return result
    numbers = valid[0]["numbers"]
    groups = feature_groups(numbers, bank)
    feature = [value for name in ("bank", "raw", "structure", "order") if artifact["group_weights"].get(name, 0) > 0
               for value in groups[name]]
    scores = ridge_predict(feature, artifact)
    order = sorted(range(len(scores)), key=lambda index: (scores[index], index))
    winner = order[-1]
    margin = scores[winner] - scores[order[-2]]
    structure = groups["structure"]
    support_feature = groups["bank"] + structure[:11] + structure[11:110:11] + structure[154:159]
    support = support_evaluate(support_feature, winner, margin, artifact)
    label = artifact["models"][winner] if support["support_passed"] else None
    source = "UNLISTED" if expected_model and expected_model not in artifact["models"] else (
        "UNKNOWN" if not label or not expected_model else "MATCH" if label == expected_model else "MISMATCH")
    result.update(status="scored" if label else "unknown", prediction=label,
                  adapter_scores=dict(zip(artifact["models"], scores)), adapter_margin=margin,
                  source_verdict=source, **support)
    return result
