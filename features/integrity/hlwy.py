"""HLwY source-compatible distribution comparison (LGPL-2.1).

Adapted from hanlinwenyuan/hlwy-ai-checker at
8629b9353fdeaa1a0fc6b36eaf0460fbba1d44b3. Modified 2026-10-08:
stdlib pure functions, authorized immutable references, condition/coverage gates,
fixed 50-attempt ceiling, no adaptive retransmission or identity/purity claim.
Complete corresponding source is this file; see assets/hlwy/LICENSE and NOTICE.md.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence

from .reference import reference_conditions, runtime_hash, validate_endpoint, validate_reference_metadata
from .strategies import ProbeSpec, StrategyManifest, canonical_hash

SCHEMA = "integrity-hlwy-reference/v1"
ALGORITHM_VERSION = "hlwy-8629b935-v1"
PROMPT = "请从1到355之间随机选择一个数字，只输出这个数字，不要有任何其他内容。"
PROMPT_HASH = hashlib.sha256(PROMPT.encode()).hexdigest()


def hlwy_reference_hash(data: Mapping) -> str:
    return canonical_hash({key: value for key, value in data.items() if key != "package_hash"})


def hlwy_conditions(data: Mapping) -> dict:
    return reference_conditions(data)


def validate_hlwy_reference(data: dict, *, expected_sha256: str) -> dict:
    fields = {"schema", "authorization", "reference_model", "provider", "endpoint", "protocol", "parameters", "budget",
              "algorithm_version", "prompt_hash", "scorer_hash", "observations", "package_hash"}
    if not isinstance(data, dict) or not fields - {"package_hash"} <= set(data) or set(data) - fields \
            or data.get("schema") != SCHEMA:
        raise ValueError("invalid HLwY reference schema")
    digest = hlwy_reference_hash(data)
    if digest != expected_sha256 or data.get("package_hash", digest) != digest:
        raise ValueError("HLwY reference hash mismatch")
    validate_reference_metadata(data)
    if data["algorithm_version"] != ALGORITHM_VERSION or data["prompt_hash"] != PROMPT_HASH \
            or data["scorer_hash"] != runtime_hash("hlwy.py"):
        raise ValueError("HLwY prompt/scorer version mismatch")
    parameters = data["parameters"]
    sampling = parameters["sampling"]
    if parameters["max_output_tokens"] != 256 or sampling.get("temperature") != 1 \
            or sampling.get("anti_target") is not False:
        raise ValueError("HLwY frozen sampling mismatch")
    observations = data["observations"]
    if not isinstance(observations, list) or not 1 <= len(observations) <= 50 \
            or any(value is not None and (type(value) is not int or not 1 <= value <= 355) for value in observations):
        raise ValueError("invalid HLwY reference observations")
    if data["budget"]["max_requests"] != len(observations):
        raise ValueError("HLwY budget must bind reference attempt count")
    result = json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
    result["package_hash"] = digest
    return result


def build_hlwy_strategy(package: dict, attempts: int = 50) -> StrategyManifest:
    package = validate_hlwy_reference(package, expected_sha256=hlwy_reference_hash(package))
    if type(attempts) is not int or not 1 <= attempts <= 50 or attempts != package["budget"]["max_requests"]:
        raise ValueError("HLwY same-condition normal attempt budget required")
    probes = tuple(ProbeSpec(f"hlwy-{index + 1:03d}", PROMPT, max_output_tokens=256,
                             system_prompt=package["parameters"]["system_prompt"]) for index in range(attempts))
    return StrategyManifest("hlwy", ALGORITHM_VERSION, "single-number-distribution-similarity", PROMPT_HASH,
                            package["scorer_hash"], canonical_hash(hlwy_conditions(package)), package["package_hash"], probes,
                            attempts, math.ceil(attempts * 0.5), attempts, 256, 30,
                            package["budget"]["total_timeout_seconds"])


def project_hlwy_observation(package: dict, probe_id: str, raw: Mapping) -> dict:
    from .scoring import project_observation
    return project_observation(build_hlwy_strategy(package, package["budget"]["max_requests"]), probe_id, raw)


def distribution_statistics(numbers: Sequence[int]) -> dict:
    if not numbers or any(type(value) is not int or not 1 <= value <= 355 for value in numbers):
        raise ValueError("valid HLwY integers required")
    counts = [0] * 355
    for number in numbers:
        counts[number - 1] += 1
    mode = max(range(355), key=lambda index: counts[index]) + 1
    mean = sum(numbers) / len(numbers)
    return {"distribution": [count / len(numbers) for count in counts], "mode": mode,
            "mean": mean, "std_dev": math.sqrt(sum((value - mean) ** 2 for value in numbers) / len(numbers)),
            "median": sorted(numbers)[len(numbers) // 2], "min": min(numbers), "max": max(numbers),
            "unique": len(set(numbers)), "mode_count": counts[mode - 1]}


def compare_distributions(target_numbers: Sequence[int], reference_numbers: Sequence[int]) -> dict:
    target, reference = distribution_statistics(target_numbers), distribution_statistics(reference_numbers)
    left, right = target["distribution"], reference["distribution"]
    cosine = sum(a * b for a, b in zip(left, right)) / math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    divergence = 0.0
    for p_raw, q_raw in zip(left, right):
        p, q = p_raw + 1e-10, q_raw + 1e-10
        middle = (p + q) / 2
        divergence += (p * math.log(p / middle) + q * math.log(q / middle)) / 2
    mode_score = max(0, 1 - abs(target["mode"] - reference["mode"]) / 50)
    return {"cosine_similarity": cosine, "js_divergence": divergence, "mode_score": mode_score,
            "overall_score": mode_score * 0.5 + cosine * math.exp(-divergence) * 0.5,
            "target_statistics": {key: value for key, value in target.items() if key != "distribution"},
            "reference_statistics": {key: value for key, value in reference.items() if key != "distribution"}}


def score_hlwy(package: dict, outputs: Sequence[Mapping], *, conditions: Mapping) -> dict:
    from .scoring import _observations
    package = validate_hlwy_reference(package, expected_sha256=hlwy_reference_hash(package))
    manifest = build_hlwy_strategy(package, package["budget"]["max_requests"])
    observations = _observations(manifest, outputs)
    numbers = [item["number"] for item in observations if item["valid"]]
    reference = [value for value in package["observations"] if value is not None]
    total = manifest.max_requests
    result = {"strategy_id": "hlwy", "source_verdict": "UNDETERMINED", "calibration_status": "unvalidated",
              "identity_authenticated": False, "algorithm_version": ALGORITHM_VERSION,
              "reference_hash": package["package_hash"], "scorer_hash": manifest.scorer_hash,
              "sampling_hash": manifest.sampling_hash, "provider_binding_status": "declared/unverified",
              "reference_endpoint_fingerprint": validate_endpoint(package["endpoint"]),
              "target_total": total, "target_attempted": len(observations), "target_valid": len(numbers),
              "target_invalid": total - len(numbers), "target_coverage": len(numbers) / total,
              "reference_total": total, "reference_valid": len(reference), "reference_invalid": total - len(reference),
              "reference_coverage": len(reference) / total, "comparison": None,
              "conditions_hash": canonical_hash(conditions)}
    if conditions != hlwy_conditions(package):
        result.update(status="rejected", reason="reference_conditions_mismatch")
        return result
    if len(numbers) / total < 0.5 or len(reference) / total < 0.5:
        result.update(status="undetermined", reason="insufficient_coverage")
        return result
    result.update(status="scored", source_verdict="BEHAVIORAL_COMPARISON", comparison=compare_distributions(numbers, reference),
                  interpretation="exploratory_similarity_not_identity_or_equivalence")
    return result
