"""Atomic, hash-verified strategy assets and executor ceilings."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

ASSET_ROOT = Path(__file__).with_name("assets")
MANIFEST_SHA256: dict[str, str] = {'canary': 'c158bb58804d037c5b8d61da9d3cd3cb17f54682ea890215b6c275881b314044', 'health': '992b12f5dfd7d75ac7a46775cd6cab4609cd972e3088498875036ff4d4da1675', 'modeltrace': '17f1abaf4d995ba780ceb8ce56062ddeb4f915891d9129194794f00aa51bf97b', 'nerfed': '3ba9db6576088d46d0cedb79e42067d6da34d8c8d27e831c30ecf3d2916c5f24', 'nerfed-api': '8b4079790f9324e27159ddb9036e4b41697cb7cb849897fae96a1f548c93cef5', 'traceone': '0021a386451b101184d6ff82455a0b17885b938e0b9c81b0ee4d5ef915421b32'}


def canonical_json(value: object) -> str:
    """Stable JSON numbers across a browser parse/stringify round trip.

    JSON has one number type: Python's 1.0 and JavaScript's 1 must bind the
    same reference. Reject non-finite values rather than hashing invalid JSON.
    """
    def normalized(item):
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("non-finite canonical number")
            return int(item) if item.is_integer() else item
        if isinstance(item, dict):
            return {key: normalized(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalized(child) for child in item]
        return item
    return json.dumps(normalized(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class ProbeSpec:
    probe_id: str
    prompt: str
    expected_count: int = 0
    family: str = ""
    expected: str | int | None = None
    max_output_tokens: int = 2048
    system_prompt: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class StrategyManifest:
    strategy_id: str
    version: str
    method: str
    asset_hash: str
    scorer_hash: str
    sampling_hash: str
    manifest_hash: str
    probes: tuple[ProbeSpec, ...]
    normal_valid_answers: int
    minimum_valid_answers: int
    max_requests: int
    max_output_tokens: int
    request_timeout_seconds: int
    total_timeout_seconds: int
    calibration_status: str = "unvalidated"
    max_retries: int = 0
    execution_mode: str = "api"

    def to_dict(self, include_prompts: bool = False) -> dict:
        result = asdict(self)
        if not include_prompts:
            result["probes"] = [{key: value for key, value in item.items()
                                 if key not in {"prompt", "system_prompt", "expected"}}
                                for item in result["probes"]]
        return result


def _read_verified(relative: str, expected: str) -> bytes:
    path = ASSET_ROOT / relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ASSET_ROOT.resolve()):
        raise ValueError("invalid integrity asset path")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError("integrity asset hash mismatch: " + relative)
    return payload


def _bundle(strategy_id: str) -> tuple[dict, bytes]:
    if strategy_id not in MANIFEST_SHA256:
        raise ValueError("unknown integrity strategy")
    raw = _read_verified(strategy_id + "/manifest.json", MANIFEST_SHA256[strategy_id])
    bundle = json.loads(raw)
    for asset in bundle["assets"]:
        _read_verified(asset["path"], asset["sha256"])
    for source, expected in bundle["runtime_hashes"].items():
        if source not in {"scoring.py", "reference.py", "traceone.py", "hlwy.py", "evidence.py"}:
            raise ValueError("unknown scoring runtime")
        path = Path(__file__).with_name(source)
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("integrity scorer hash mismatch: " + source)
    return bundle, raw


def get_strategy(strategy_id: str) -> StrategyManifest:
    bundle, raw = _bundle(strategy_id)
    probes_data = json.loads(_read_verified(bundle["probes_path"], bundle["probes_sha256"]))
    probes = tuple(ProbeSpec(**item) for item in probes_data)
    if len({probe.probe_id for probe in probes}) != len(probes) or len(probes) != bundle["max_requests"]:
        raise ValueError("invalid fixed probe set")
    return StrategyManifest(
        strategy_id=strategy_id, version=bundle["version"], method=bundle["method"],
        asset_hash=canonical_hash(bundle["assets"]), scorer_hash=canonical_hash(bundle["runtime_hashes"]),
        sampling_hash=canonical_hash(bundle["sampling"]),
        manifest_hash=canonical_hash({"bundle": hashlib.sha256(raw).hexdigest(),
                                      "loader": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}),
        probes=probes, **{key: bundle[key] for key in (
            "normal_valid_answers", "minimum_valid_answers", "max_requests", "max_output_tokens",
            "request_timeout_seconds", "total_timeout_seconds", "max_retries", "execution_mode")})


def load_bank(manifest: StrategyManifest) -> dict:
    if manifest.strategy_id not in {"modeltrace", "nerfed", "nerfed-api", "traceone"}:
        raise ValueError("strategy has no identity bank")
    current = get_strategy(manifest.strategy_id)
    if current != manifest:
        raise ValueError("strategy manifest drift")
    bundle, _ = _bundle(manifest.strategy_id)
    bank = json.loads(_read_verified(bundle["bank_path"], bundle["bank_sha256"]))
    order = [item["id"] for item in bank["models"]]
    if bank["schema"] != "robust-number-fingerprint-bank" or len(order) != bundle["classes"]:
        raise ValueError("identity bank schema/classes mismatch")
    if order != bank["robust"]["model_order"]:
        raise ValueError("identity bank order mismatch")
    return bank


def list_strategies() -> list[dict]:
    return [get_strategy(strategy_id).to_dict() for strategy_id in MANIFEST_SHA256]


def load_artifact(manifest: StrategyManifest) -> dict:
    if manifest.strategy_id != "traceone" or get_strategy("traceone") != manifest:
        raise ValueError("invalid TraceOne manifest")
    bundle, _ = _bundle("traceone")
    return json.loads(_read_verified(bundle["artifact_path"], bundle["artifact_sha256"]))
