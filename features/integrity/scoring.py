"""Behavioral scoring, derived from pinned MIT ModelTrace/nerfed/TraceOne.

Modified for Eval: transport invalidation, body-free observations, atomic bundles,
explicit unknowns and complete denominators. See NOTICE.md for source attribution.
Statistical formulas do not authenticate model weights or API routing.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence

from .strategies import StrategyManifest, canonical_hash, get_strategy, load_bank

VALUE_MAX = 355
OBSERVATION_SCHEMA = "integrity-observation/v1"
CONDITION_FIELDS = ("provider", "protocol", "model", "parameters", "budget")


def parse_numbers(text: str) -> list[int]:
    runs, current, previous_end = [], [], 0
    for match in re.finditer(r"\d+", text):
        if current and any(character.isalpha() for character in text[previous_end:match.start()]):
            runs.append(current)
            current = []
        # Values outside the fixed 1..355 bank are irrelevant. Avoid converting
        # unbounded digit runs, which can otherwise raise before projection.
        digits = match.group().lstrip("0") or "0"
        value = int(digits) if len(digits) <= 3 else 0
        if 1 <= value <= VALUE_MAX:
            current.append(value)
        previous_end = match.end()
    if current:
        runs.append(current)
    return max(runs, key=len) if runs else []


def count_numbers(numbers: Sequence[int]) -> list[int]:
    counts = [0] * VALUE_MAX
    for value in numbers:
        if type(value) is not int or not 1 <= value <= VALUE_MAX:
            raise ValueError("invalid fingerprint integer")
        counts[value - 1] += 1
    return counts


def standardize(values: Sequence[float]) -> list[float]:
    center = sum(values) / len(values)
    scale = max(math.sqrt(sum((value - center) ** 2 for value in values) / len(values)), 1e-12)
    return [(value - center) / scale for value in values]


def hellinger_feature(counts: Sequence[int]) -> list[float]:
    total = sum(counts) + 0.5 * len(counts)
    return [math.sqrt((count + 0.5) / total) for count in counts]


def ordered_block_feature(numbers: Sequence[int]) -> list[float]:
    pieces, start = [], 0
    quotient, remainder = divmod(len(numbers), 4)
    for index in range(4):
        size = quotient + (index < remainder)
        counts = [0.5] * 16
        for value in numbers[start:start + size]:
            counts[min(15, (16 * (value - 1)) // 355)] += 1
        pieces.extend(math.sqrt(count / (size + 8)) for count in counts)
        start += size
    digits = [0.5] * 10
    for value in numbers:
        digits[value % 10] += 1
    pieces.extend(math.sqrt(count / (len(numbers) + 5)) for count in digits)
    return pieces


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("fingerprint dimensions mismatch")
    return sum(a * b for a, b in zip(left, right))


def _normalized(values: Sequence[float]) -> list[float]:
    scale = max(math.sqrt(_dot(values, values)), 1e-12)
    return [value / scale for value in values]


def project_nuisance(values: Sequence[float], basis: Sequence[Sequence[float]]) -> list[float]:
    # Simultaneous projection matches the upstream orthonormal SVD basis contract.
    projections = [_dot(values, vector) for vector in basis]
    return [value - sum(coefficient * vector[index] for coefficient, vector in zip(projections, basis))
            for index, value in enumerate(values)]


def _feature_scores(feature: Sequence[float], artifact: dict) -> list[float]:
    values = [(value - center) / scale for value, center, scale in
              zip(feature, artifact["feature_mean"], artifact["feature_scale"])]
    return standardize([_dot(_normalized(project_nuisance(values, artifact["nuisance_basis"])), centroid)
                        for centroid in artifact["centroids"]])


def robust_score_numbers(numbers: Sequence[int], bank: dict) -> dict:
    marginal = _feature_scores(hellinger_feature(count_numbers(numbers)), bank["robust"]["hellinger"])
    artifact = bank["robust"].get("ordered_blocks")
    if not artifact or not artifact.get("weight"):
        return {"marginal": marginal, "ordered": marginal, "fused": marginal}
    values = [(value - center) / scale for value, center, scale in
              zip(ordered_block_feature(numbers), artifact["feature_mean"], artifact["feature_scale"])]
    unit = _normalized(values)
    template = standardize([max(_dot(unit, environment[index])
                                for environment in artifact["environment_centroids"])
                            for index in range(len(artifact["centroids"]))])
    nuisance = _feature_scores(ordered_block_feature(numbers), artifact)
    ordered = standardize([0.5 * a + 0.5 * b for a, b in zip(template, nuisance)])
    weight = float(artifact["weight"])
    return {"marginal": marginal, "ordered": ordered,
            "fused": [(1 - weight) * a + weight * b for a, b in zip(marginal, ordered)]}


def softmax(values: Sequence[float]) -> list[float]:
    peak = max(values)
    weights = [math.exp(value - peak) for value in values]
    total = sum(weights)
    return [weight / total for weight in weights]


def source_verdict(expected_model: str, results: Sequence[dict], valid_answers: int) -> dict:
    if not expected_model:
        return {"source_verdict": "UNKNOWN", "p_expected": None, "fused_margin_sigma": None}
    expected = next((item for item in results if item["model"] == expected_model), None)
    if expected is None:
        return {"source_verdict": "UNLISTED", "p_expected": None, "fused_margin_sigma": None}
    top = results[0]
    margin = top["score"] - expected["score"]
    mismatch = (top["model"] != expected_model and valid_answers >= 2
                and top["probability"] + 1e-9 >= 0.8
                and expected["probability"] <= 0.2 + 1e-9 and margin + 1e-9 >= 0.5)
    return {"source_verdict": "MISMATCH" if mismatch else "SUSPICIOUS",
            "p_expected": expected["probability"], "fused_margin_sigma": margin}


def _invalid_reason(raw: Mapping) -> str | None:
    if raw.get("valid") is False:
        return "invalid_response"
    if raw.get("status", "completed") not in {"completed", "success", "ok"}:
        return "incomplete_protocol"
    if raw.get("finish_reason") in {"length", "max_tokens", "max_output_tokens", "tool_calls", "tool_use",
                                   "content_filter", "error", "cancelled", "interrupted"}:
        return "invalid_finish_reason"
    if raw.get("truncated") or raw.get("tool_calls") or raw.get("error"):
        return "protocol_error"
    return None


def _extract_answer(text: str) -> str | None:
    stripped = text.strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped or None
    except (ValueError, RecursionError):
        return None
    if isinstance(payload, dict) and set(payload) == {"answer"}:
        answer = payload["answer"]
        return str(answer).strip() if answer is not None else None
    if isinstance(payload, (str, int, float)) and not isinstance(payload, bool):
        return str(payload).strip()
    return None


def project_observation(manifest: StrategyManifest, probe_id: str, raw: Mapping) -> dict:
    probe = next((item for item in manifest.probes if item.probe_id == probe_id), None)
    if probe is None:
        raise ValueError("unknown probe ID")
    reason = _invalid_reason(raw)
    text = raw.get("text", "")
    if not isinstance(text, str):
        reason = reason or "invalid_text_type"
        text = ""
    if len(text) > 100000:
        reason = reason or "response_too_large"
        text = ""
    observation = {"observation_schema": OBSERVATION_SCHEMA, "manifest_hash": manifest.manifest_hash,
                   "probe_id": probe_id, "valid": reason is None, "invalid_reason": reason}
    if manifest.strategy_id in {"modeltrace", "nerfed", "nerfed-api"}:
        numbers = parse_numbers(text) if reason is None else []
        minimum = max(80, math.ceil(probe.expected_count * 0.55))
        if len(numbers) < minimum:
            reason = reason or "insufficient_numbers"
        if len(numbers) > 4096:
            reason = reason or "too_many_numbers"
        observation.update(numbers=numbers if reason is None else [], parsed_numbers=len(numbers),
                           minimum_numbers=minimum, expected_count=probe.expected_count)
    elif manifest.strategy_id == "canary":
        answer = _extract_answer(text) if reason is None else None
        if answer is None:
            reason = reason or "unparseable_answer"
        observation["correct"] = None if reason else answer == str(probe.expected).strip()
    elif manifest.strategy_id == "traceone":
        from .traceone import parse_grid_response
        parsed = parse_grid_response(text) if reason is None else parse_grid_response("")
        if not parsed["analyzable"]:
            reason = reason or "unanalyzable_grid"
        observation.update(parsed)
        if reason:
            observation["numbers"] = []
            # A malformed grid can contain thousands of rows. Keep its failure
            # category and parsed count, without persisting an unbounded vector.
            observation["row_counts"] = parsed["row_counts"][:9]
    elif manifest.strategy_id == "hlwy":
        match = re.search(r"\d+", text) if reason is None else None
        digits = (match.group().lstrip("0") or "0") if match else ""
        number = int(digits) if digits and len(digits) <= 3 else None
        if number is None or not 1 <= number <= 355:
            reason = reason or "unparseable_answer"
        observation["number"] = None if reason else number
    elif manifest.strategy_id == "kbf":
        answer = _extract_answer(text) if reason is None else None
        choice = int(answer) if answer is not None and re.fullmatch(r"[1-4]", answer) else None
        if choice is None:
            reason = reason or "unparseable_answer"
        observation.update(choice=None if reason else choice, correct=None if reason else choice == probe.expected)
    elif manifest.strategy_id == "health":
        if not text.strip():
            reason = reason or "empty_answer"
    else:
        raise ValueError("unknown projection strategy")
    observation.update(valid=reason is None, invalid_reason=reason)
    return observation


def _observations(manifest: StrategyManifest, outputs: Sequence[Mapping]) -> list[dict]:
    if len(outputs) > manifest.max_requests:
        raise ValueError("request ceiling exceeded")
    observations, seen = [], set()
    for index, output in enumerate(outputs):
        probe_id = output.get("probe_id", output.get("item_id"))
        if probe_id is None and manifest.strategy_id in {"modeltrace", "nerfed", "nerfed-api"}:
            probe_id = manifest.probes[index].probe_id
        if probe_id in seen:
            raise ValueError("duplicate probe observation")
        seen.add(probe_id)
        if output.get("observation_schema") == OBSERVATION_SCHEMA:
            if output.get("manifest_hash") != manifest.manifest_hash:
                raise ValueError("observation manifest drift")
            probe = next((item for item in manifest.probes if item.probe_id == probe_id), None)
            if probe is None or type(output.get("valid")) is not bool:
                raise ValueError("invalid observation projection")
            if manifest.strategy_id in {"modeltrace", "nerfed", "nerfed-api", "traceone"}:
                numbers = output.get("numbers", [])
                if not isinstance(numbers, list) or len(numbers) > 4096:
                    raise ValueError("invalid projected numbers")
                count_numbers(numbers)
                if manifest.strategy_id == "traceone":
                    counts = output.get("row_counts")
                    if not isinstance(counts, list) or len(counts) > 10 or any(type(value) is not int or value < 0 for value in counts):
                        raise ValueError("invalid projected grid rows")
                    analyzable = len(counts) == 9 and all(25 <= value <= 45 for value in counts) \
                        and 280 <= len(numbers) <= 350 and sum(counts) == len(numbers)
                    if output["valid"] and (not analyzable or output.get("analyzable") is not True):
                        raise ValueError("unanalyzable projected grid")
                    if type(output.get("format_compliant")) is not bool or not isinstance(output.get("format_errors"), list):
                        raise ValueError("invalid projected format diagnostics")
                else:
                    expected_count = output.get("expected_count", probe.expected_count) if manifest.execution_mode == "import_only" else probe.expected_count
                    if type(expected_count) is not int or not 292 <= expected_count <= 333:
                        raise ValueError("invalid projected expected count")
                    if output["valid"] and len(numbers) < max(80, math.ceil(expected_count * 0.55)):
                        raise ValueError("insufficient projected numbers")
            elif manifest.strategy_id in {"canary", "kbf"}:
                if output.get("correct") is not None and type(output["correct"]) is not bool:
                    raise ValueError("invalid projected outcome")
                if output["valid"] and type(output.get("correct")) is not bool:
                    raise ValueError("valid projected outcome required")
                if manifest.strategy_id == "kbf" and output["valid"] and (
                        type(output.get("choice")) is not int or not 1 <= output["choice"] <= 4
                        or output["correct"] != (output["choice"] == probe.expected)):
                    raise ValueError("invalid projected KBF choice")
            elif manifest.strategy_id == "hlwy" and output["valid"] and (
                    type(output.get("number")) is not int or not 1 <= output["number"] <= 355):
                raise ValueError("invalid projected HLwY number")
            observations.append(dict(output))
        else:
            observations.append(project_observation(manifest, probe_id, output))
    return observations


def _base(manifest: StrategyManifest) -> dict:
    return {"strategy_id": manifest.strategy_id, "calibration_status": "unvalidated",
            "asset_hash": manifest.asset_hash, "scorer_hash": manifest.scorer_hash,
            "sampling_hash": manifest.sampling_hash, "manifest_hash": manifest.manifest_hash,
            "identity_authenticated": False}


def _score_identity(manifest: StrategyManifest, observations: Sequence[dict], expected_model: str) -> dict:
    bank = load_bank(manifest)
    valid = [observation for observation in observations if observation["valid"]]
    diagnostics = [{key: item.get(key) for key in ("probe_id", "valid", "invalid_reason", "parsed_numbers",
                                                  "minimum_numbers", "expected_count")} for item in observations]
    result = {**_base(manifest), "valid_answers": len(valid),
              "invalid_answers": len(observations) - len(valid), "requested_answers": len(observations),
              "missing_answers": len(manifest.probes) - len(observations), "diagnostics": diagnostics,
              "expected_model": expected_model, "prediction": None, "probability": None,
              "results": [], "calibration": None, "status": "insufficient_data",
              "source_verdict": "UNLISTED" if expected_model and expected_model not in
              {model["id"] for model in bank["models"]} else "SUSPICIOUS"}
    if not valid:
        return result
    score_vectors = [robust_score_numbers(item["numbers"], bank)["fused"] for item in valid]
    combined = [sum(vector[index] for vector in score_vectors) / len(valid)
                for index in range(len(bank["models"]))]
    key = str(len(valid))
    beta = float(bank["calibration"][key]["beta"])
    probabilities = softmax([beta * value for value in combined])
    rows = [{"model": model["id"], "display_name": model.get("display_name", model["id"]),
             "family": model.get("family", "models"), "probability": probabilities[index],
             "score": combined[index]} for index, model in enumerate(bank["models"])]
    rows.sort(key=lambda item: item["probability"], reverse=True)
    family_probabilities = {family: sum(row["probability"] for row in rows if row["family"] == family)
                            for family in dict.fromkeys(row["family"] for row in rows)}
    result.update(results=rows, prediction=rows[0]["model"], probability=rows[0]["probability"],
                  calibration={"queries": key, "beta": beta}, family_probabilities=family_probabilities,
                  status="scored" if len(valid) >= manifest.minimum_valid_answers else "insufficient_data",
                  **source_verdict(expected_model, rows, len(valid)))
    return result


def one_sided_mcnemar(regressions: int, improvements: int) -> float:
    if type(regressions) is not int or type(improvements) is not int or min(regressions, improvements) < 0:
        raise ValueError("invalid discordant counts")
    count = regressions + improvements
    return sum(math.comb(count, value) for value in range(regressions, count + 1)) / 2 ** count


def holm_bonferroni(p_values: Sequence[float], alpha: float = 0.05) -> list[dict]:
    if not 0 < alpha < 1 or any(not math.isfinite(value) or not 0 <= value <= 1 for value in p_values):
        raise ValueError("invalid Holm parameters")
    results = [{} for _ in p_values]
    running, stopped = 0.0, False
    for rank, (index, value) in enumerate(sorted(enumerate(p_values), key=lambda pair: pair[1])):
        factor = len(p_values) - rank
        reject = not stopped and value <= alpha / factor
        stopped = stopped or not reject
        running = max(running, factor * value)
        results[index] = {"p_value": value, "adjusted_p_value": min(1.0, running), "reject": reject}
    return results


def compare_paired_outcomes(baseline: Mapping[str, bool | None], current: Mapping[str, bool | None],
                            *, alpha: float = 0.05, minimum_effect: float = 0.05) -> dict:
    if not 0 < alpha < 1 or not 0 <= minimum_effect <= 1:
        raise ValueError("invalid paired-test parameters")
    if any(value is not None and type(value) is not bool for value in (*baseline.values(), *current.values())):
        raise ValueError("invalid paired outcomes")
    if set(baseline) != set(current):
        return {"status": "invalid_comparison", "reason": "item_set_mismatch"}
    count = len(current)
    regressions = sum(baseline[item] is True and current[item] is not True for item in current)
    improvements = sum(baseline[item] is not True and current[item] is True for item in current)
    baseline_accuracy = sum(value is True for value in baseline.values()) / count if count else None
    current_accuracy = sum(value is True for value in current.values()) / count if count else None
    loss = baseline_accuracy - current_accuracy if count else None
    p_value = one_sided_mcnemar(regressions, improvements)
    status = "insufficient_data" if not count else (
        "degraded" if loss >= minimum_effect and p_value <= alpha else
        "inconclusive" if loss >= minimum_effect else "no_detected_degradation")
    return {"status": status, "paired_items": count, "baseline_accuracy": baseline_accuracy,
            "current_accuracy": current_accuracy, "accuracy_loss": loss, "regressions": regressions,
            "improvements": improvements, "invalid_baseline_items": sum(value is None for value in baseline.values()),
            "invalid_current_items": sum(value is None for value in current.values()),
            "one_sided_p_value": p_value, "alpha": alpha, "minimum_effect": minimum_effect,
            "invalid_policy": "fail", "complete_pairing": True}


def validate_conditions(conditions: Mapping | None) -> dict:
    if not isinstance(conditions, Mapping) or any(key not in conditions for key in CONDITION_FIELDS):
        raise ValueError("complete sampling conditions required")
    if any(not isinstance(conditions[key], str) or not conditions[key].strip()
           for key in ("provider", "protocol", "model")):
        raise ValueError("provider/protocol/model required")
    if not isinstance(conditions["parameters"], Mapping) or not isinstance(conditions["budget"], Mapping):
        raise ValueError("sampling parameters and budget required")
    canonical_hash(conditions)
    return dict(conditions)


def _score_canary(manifest: StrategyManifest, observations: Sequence[dict], baseline: dict | None,
                  conditions: Mapping | None) -> dict:
    by_id = {item["probe_id"]: item for item in observations}
    outcomes = {probe.probe_id: by_id[probe.probe_id].get("correct")
                if probe.probe_id in by_id and by_id[probe.probe_id]["valid"] else None for probe in manifest.probes}
    correct = sum(value is True for value in outcomes.values())
    result = {**_base(manifest), "outcomes": outcomes, "score": correct / len(outcomes),
              "correct": correct, "total": len(outcomes), "invalid": sum(value is None for value in outcomes.values()),
              "valid_answers": sum(value is not None for value in outcomes.values()),
              "requested_answers": len(observations), "status": "current_only" if len(observations) == len(outcomes) else "incomplete",
              "not_run": len(outcomes) - len(observations), "conditions": conditions,
              "comparison": None}
    if baseline is None:
        return result
    if result["not_run"]:
        result.update(status="incomplete", comparison={"reason": "unattempted_items"})
        return result
    try:
        current_conditions = validate_conditions(conditions)
        baseline_conditions = validate_conditions(baseline.get("conditions"))
    except ValueError:
        result.update(status="invalid_comparison", comparison={"reason": "missing_sampling_conditions"})
        return result
    if current_conditions != baseline_conditions:
        result.update(status="invalid_comparison", comparison={"reason": "sampling_conditions_mismatch"})
        return result
    if any(baseline.get(key) != result[key] for key in ("asset_hash", "scorer_hash", "sampling_hash", "manifest_hash")):
        result.update(status="invalid_comparison", comparison={"reason": "asset_or_scorer_mismatch"})
        return result
    old = baseline.get("outcomes")
    if not isinstance(old, dict) or set(old) != set(outcomes):
        result.update(status="invalid_comparison", comparison={"reason": "item_set_mismatch"})
        return result
    overall = compare_paired_outcomes(old, outcomes)
    families = []
    for family in sorted({probe.family for probe in manifest.probes}):
        ids = [probe.probe_id for probe in manifest.probes if probe.family == family]
        families.append({"family": family, **compare_paired_outcomes(
            {key: old[key] for key in ids}, {key: outcomes[key] for key in ids})})
    corrections = holm_bonferroni([item["one_sided_p_value"] for item in families])
    for item, correction in zip(families, corrections):
        item.update(holm_adjusted_p_value=correction["adjusted_p_value"], holm_reject=correction["reject"],
                    multiplicity_adjusted_status="degraded" if correction["reject"] and item["accuracy_loss"] >= 0.05
                    else "inconclusive" if item["accuracy_loss"] >= 0.05 else "no_detected_degradation")
    result.update(status=overall["status"], comparison={"overall": overall, "families": families,
                  "primary_test": "exact one-sided McNemar", "secondary_tests": "Holm-Bonferroni"})
    return result


def score_strategy(manifest: StrategyManifest, outputs: Sequence[Mapping], *, expected_model: str = "",
                   baseline: dict | None = None, conditions: Mapping | None = None) -> dict:
    if get_strategy(manifest.strategy_id) != manifest:
        raise ValueError("strategy manifest drift")
    observations = _observations(manifest, outputs)
    if manifest.strategy_id in {"modeltrace", "nerfed", "nerfed-api"}:
        result = _score_identity(manifest, observations, expected_model)
        result["conditions"] = conditions
        return result
    if manifest.strategy_id == "traceone":
        from .traceone import score_traceone
        result = score_traceone(manifest, observations, expected_model)
        result["conditions"] = conditions
        return result
    if manifest.strategy_id == "health":
        valid = sum(item["valid"] for item in observations)
        return {**_base(manifest), "status": "completed" if valid else "incomplete", "valid_answers": valid,
                "invalid_answers": len(observations) - valid, "requested_answers": len(observations),
                "scope": "tested_model_protocol_only", "conditions": conditions}
    return _score_canary(manifest, observations, baseline, conditions)
