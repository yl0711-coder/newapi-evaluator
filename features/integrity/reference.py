"""Authorized user-owned KBF references and independently implemented statistics.

No KBF source or public reference answers are embedded. Binomial and confidence
bound formulas are implemented independently from their mathematical definitions.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from .strategies import ProbeSpec, StrategyManifest, canonical_hash

SCHEMA = "integrity-kbf-reference/v1"
SCORER_VERSION = "independent-kbf-binomial-cp99-v1"
PARAMETER_KEYS = {"system_prompt", "wrapper", "thinking", "max_output_tokens", "retry_policy", "sampling"}


def runtime_hash(filename: str) -> str:
    return hashlib.sha256(Path(__file__).with_name(filename).read_bytes()).hexdigest()


def reference_hash(data: Mapping) -> str:
    return canonical_hash({key: value for key, value in data.items() if key != "package_hash"})


def validate_authorization(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {"authorized", "basis", "source"} or value["authorized"] is not True:
        raise ValueError("explicit reference authorization required")
    for key in ("basis", "source"):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 240:
            raise ValueError("authorization provenance required")


def validate_endpoint(value: object) -> str:
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError("reference endpoint required")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None \
                or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("credential-free endpoint required")
        parsed.port
    except (ValueError, TypeError) as exc:
        raise ValueError("credential-free endpoint required") from exc
    return hashlib.sha256(value.rstrip("/").encode()).hexdigest()


def validate_parameters(parameters: object) -> None:
    if not isinstance(parameters, dict) or set(parameters) != PARAMETER_KEYS:
        raise ValueError("complete reference parameters required")
    if any(not isinstance(parameters[key], str) or len(parameters[key]) > 4000
           for key in ("system_prompt", "wrapper", "thinking")):
        raise ValueError("invalid prompt/thinking parameters")
    cap = parameters["max_output_tokens"]
    if type(cap) is not int or not 1 <= cap <= 8192:
        raise ValueError("invalid output token limit")
    if parameters["retry_policy"] != {"max_retries": 0}:
        raise ValueError("reference automatic retries must be disabled")
    if not isinstance(parameters["sampling"], dict):
        raise ValueError("actual sampling parameters required")
    canonical_hash(parameters)


def validate_reference_metadata(data: Mapping) -> None:
    validate_authorization(data.get("authorization"))
    for key in ("reference_model", "provider", "protocol"):
        value = data.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,120}", value):
            raise ValueError("invalid reference " + key)
    validate_endpoint(data.get("endpoint"))
    validate_parameters(data.get("parameters"))
    budget = data.get("budget")
    if not isinstance(budget, dict) or set(budget) != {"max_requests", "max_input_tokens", "max_output_tokens",
                                                    "total_timeout_seconds"}:
        raise ValueError("complete reference budget required")
    if any(type(value) is not int or value <= 0 for value in budget.values()):
        raise ValueError("invalid reference budget")
    if "openrouter" in data["provider"].lower() or "openrouter" in urlsplit(data["endpoint"]).hostname.lower():
        provider = data["parameters"]["sampling"].get("provider")
        if not isinstance(provider, dict) or provider.get("allow_fallbacks") is not False \
                or not isinstance(provider.get("order"), list) or len(provider["order"]) != 1:
            raise ValueError("OpenRouter needs one pinned provider and no fallback")


def reference_conditions(data: Mapping) -> dict:
    return {"provider": data["provider"], "protocol": data["protocol"], "model": data["reference_model"],
            "parameters": data["parameters"], "budget": data["budget"]}


def validate_reference_package(data: dict, *, expected_sha256: str) -> dict:
    allowed = {"schema", "authorization", "reference_model", "provider", "endpoint", "protocol", "parameters",
               "budget", "probe_hash", "scorer_version", "scorer_hash", "probes", "self_test", "package_hash"}
    required = allowed - {"self_test", "package_hash"}
    if not isinstance(data, dict) or not required <= set(data) or set(data) - allowed or data.get("schema") != SCHEMA:
        raise ValueError("invalid KBF reference schema")
    digest = reference_hash(data)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "") or digest != expected_sha256 \
            or data.get("package_hash", digest) != digest:
        raise ValueError("KBF reference hash mismatch")
    validate_reference_metadata(data)
    if data["scorer_version"] != SCORER_VERSION or data["scorer_hash"] != runtime_hash("reference.py"):
        raise ValueError("KBF scorer version/hash mismatch")
    probes = data["probes"]
    if not isinstance(probes, list) or not 1 <= len(probes) <= 256:
        raise ValueError("KBF selected probe set required")
    seen = set()
    for probe in probes:
        if not isinstance(probe, dict) or set(probe) != {"probe_id", "prompt", "expected"}:
            raise ValueError("invalid KBF probe schema")
        if not isinstance(probe["probe_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", probe["probe_id"]) \
                or probe["probe_id"] in seen:
            raise ValueError("invalid/duplicate KBF probe ID")
        seen.add(probe["probe_id"])
        if not isinstance(probe["prompt"], str) or not 1 <= len(probe["prompt"]) <= 12000 \
                or type(probe["expected"]) is not int or not 1 <= probe["expected"] <= 4:
            raise ValueError("invalid KBF probe content")
    if data["probe_hash"] != canonical_hash(probes):
        raise ValueError("KBF probe hash mismatch")
    if data["budget"]["max_requests"] != len(probes):
        raise ValueError("KBF budget must bind selected probe count")
    self_test = data.get("self_test")
    if self_test is not None:
        if not isinstance(self_test, dict) or set(self_test) != {"reference_model", "conditions", "outcomes"}:
            raise ValueError("invalid KBF self-test schema")
        if self_test["reference_model"] != data["reference_model"] or self_test["conditions"] != reference_conditions(data):
            raise ValueError("KBF self-test conditions mismatch")
        outcomes = self_test["outcomes"]
        if not isinstance(outcomes, dict) or set(outcomes) != seen \
                or any(value is not None and type(value) is not bool for value in outcomes.values()):
            raise ValueError("KBF self-test outcome set mismatch")
    result = json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
    result["package_hash"] = digest
    return result


def build_reference_strategy(package: dict) -> StrategyManifest:
    package = validate_reference_package(package, expected_sha256=reference_hash(package))
    parameters = package["parameters"]
    probes = tuple(ProbeSpec(item["probe_id"], item["prompt"], expected=item["expected"],
                             max_output_tokens=parameters["max_output_tokens"],
                             system_prompt=parameters["system_prompt"]) for item in package["probes"])
    return StrategyManifest("kbf", SCORER_VERSION, "knowledge-boundary-binomial", package["probe_hash"],
                            package["scorer_hash"], canonical_hash(reference_conditions(package)), package["package_hash"],
                            probes, len(probes), max(1, math.ceil(len(probes) * 0.5)), len(probes),
                            parameters["max_output_tokens"], 60, package["budget"]["total_timeout_seconds"])


def project_reference_observation(package: dict, probe_id: str, raw: Mapping) -> dict:
    from .scoring import project_observation
    return project_observation(build_reference_strategy(package), probe_id, raw)


def binomial_upper_tail(trials: int, errors: int, probability: float) -> float:
    if type(trials) is not int or type(errors) is not int or not 0 <= errors <= trials \
            or not 0 <= probability <= 1:
        raise ValueError("invalid binomial parameters")
    if errors == 0 or probability == 1:
        return 1.0
    if probability == 0:
        return 0.0
    terms = [math.lgamma(trials + 1) - math.lgamma(k + 1) - math.lgamma(trials - k + 1)
             + k * math.log(probability) + (trials - k) * math.log1p(-probability)
             for k in range(errors, trials + 1)]
    peak = max(terms)
    return min(1.0, math.exp(peak) * sum(math.exp(value - peak) for value in terms))


def clopper_pearson_upper(errors: int, trials: int, confidence: float = 0.99) -> float:
    if type(trials) is not int or type(errors) is not int or not 0 <= errors <= trials or trials <= 0 \
            or not 0 < confidence < 1:
        raise ValueError("invalid confidence-bound parameters")
    if errors == trials:
        return 1.0
    if errors == 0:
        return -math.expm1(math.log1p(-confidence) / trials)
    lower, upper = 0.0, 1.0
    # P[Bin(n,p) <= k] = 1-confidence; monotone inversion of exact binomial CDF.
    for _ in range(80):
        middle = (lower + upper) / 2
        cdf = binomial_upper_tail(trials, trials - errors, 1 - middle)
        if cdf > 1 - confidence:
            lower = middle
        else:
            upper = middle
    return (lower + upper) / 2


def score_reference(package: dict, outputs: Sequence[Mapping], *, conditions: Mapping) -> dict:
    from .scoring import _observations
    package = validate_reference_package(package, expected_sha256=reference_hash(package))
    manifest = build_reference_strategy(package)
    observations = _observations(manifest, outputs)
    by_id = {item["probe_id"]: item for item in observations}
    values = [by_id.get(probe.probe_id, {}).get("correct")
              if by_id.get(probe.probe_id, {}).get("valid") else None for probe in manifest.probes]
    total, valid = len(values), sum(value is not None for value in values)
    errors = sum(value is False for value in values)
    result = {"strategy_id": "kbf", "source_verdict": "UNKNOWN", "calibration_status": "unvalidated",
              "identity_authenticated": False, "reference_hash": package["package_hash"],
              "scorer_hash": manifest.scorer_hash, "sampling_hash": manifest.sampling_hash,
              "provider_binding_status": "declared/unverified", "reference_endpoint_fingerprint": validate_endpoint(package["endpoint"]),
              "target_total": total, "target_valid": valid, "target_invalid": total - valid,
              "target_coverage": valid / total, "target_errors": errors, "reference_total": total,
              "reference_valid": None, "reference_invalid": None, "reference_coverage": None,
              "p_value": None, "p0": None, "alpha": 0.05, "confidence": 0.99,
              "conditions_hash": canonical_hash(conditions)}
    if conditions != reference_conditions(package):
        result.update(status="rejected", reason="reference_conditions_mismatch")
        return result
    self_test = package.get("self_test")
    if self_test is None:
        result.update(status="unknown", reason="missing_reference_self_test")
        return result
    self_values = list(self_test["outcomes"].values())
    self_valid = sum(value is not None for value in self_values)
    self_errors = sum(value is False for value in self_values)
    result.update(reference_valid=self_valid, reference_invalid=total - self_valid,
                  reference_coverage=self_valid / total, reference_errors=self_errors)
    if valid / total < 0.5 or self_valid / total < 0.5:
        result.update(status="undetermined", source_verdict="UNDETERMINED", reason="insufficient_coverage")
        return result
    p0 = clopper_pearson_upper(self_errors, self_valid)
    p_value = binomial_upper_tail(valid, errors, p0)
    result.update(status="scored", p0=p0, p_value=p_value,
                  source_verdict="DIFF" if p_value < 0.05 else "SAME",
                  interpretation="difference_detected" if p_value < 0.05 else "no_significant_difference_detected")
    return result
