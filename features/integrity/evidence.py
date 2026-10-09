"""Explicit official-account or passive-summary imports; no file discovery.

Metadata change rules are independently expressed from pinned nerfed source
behavior. Imports and HMAC caller authentication do not prove evidence truth.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime

from .reference import validate_authorization
from .strategies import canonical_hash, get_strategy

EVENT_FIELDS = {"type", "turn_id", "timestamp", "model", "effort", "context_window"}
CATALOG_FIELDS = {"slug", "visibility", "context_window", "priority", "upgrade", "supported_reasoning_levels"}
OBSERVATION_FIELDS = {"observation_schema", "manifest_hash", "probe_id", "valid", "invalid_reason", "numbers",
                      "parsed_numbers", "minimum_numbers", "expected_count"}
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def _identifier(value: object, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value):
        raise ValueError("invalid evidence identifier")


def _validate_events(events: object, catalog: object) -> None:
    if not isinstance(events, list) or len(events) > 1000 or not isinstance(catalog, list) or len(catalog) > 300:
        raise ValueError("bounded explicit events/catalog required")
    for event in events:
        if not isinstance(event, dict) or set(event) - EVENT_FIELDS or event.get("type") not in {
            "turn_context", "thread_settings_applied", "token_count"}:
            raise ValueError("invalid evidence event schema")
        _identifier(event.get("turn_id"))
        timestamp = event.get("timestamp")
        if not isinstance(timestamp, str) or len(timestamp) > 40:
            raise ValueError("evidence timestamp required")
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timestamp timezone required")
        except ValueError as exc:
            raise ValueError("invalid evidence timestamp") from exc
        _identifier(event.get("model"), optional=True)
        if "effort" in event and event["effort"] not in EFFORTS and event["effort"] is not None:
            raise ValueError("invalid evidence effort")
        if "context_window" in event and event["context_window"] is not None and (
                type(event["context_window"]) is not int or not 1 <= event["context_window"] <= 10000000):
            raise ValueError("invalid evidence context window")
    seen = set()
    for item in catalog:
        if not isinstance(item, dict) or set(item) - CATALOG_FIELDS:
            raise ValueError("invalid evidence catalog schema")
        _identifier(item.get("slug"))
        if item["slug"] in seen:
            raise ValueError("duplicate catalog model")
        seen.add(item["slug"])
        if item.get("visibility") not in {None, "visible", "hidden"}:
            raise ValueError("invalid catalog visibility")
        for key in ("context_window", "priority"):
            if key in item and item[key] is not None and (type(item[key]) is not int or abs(item[key]) > 10000000):
                raise ValueError("invalid catalog numeric field")
        _identifier(item.get("upgrade"), optional=True)
        levels = item.get("supported_reasoning_levels", [])
        if not isinstance(levels, list) or any(level not in EFFORTS for level in levels):
            raise ValueError("invalid catalog effort levels")


def analyze_metadata(events: Sequence[dict], catalog: Sequence[dict], *, events_coverage_complete: bool = False) -> dict:
    _validate_events(events, catalog)
    catalog_by_id = {item["slug"]: item for item in catalog}
    settings, unique, contexts = {}, set(), []
    for event in events:
        digest = canonical_hash(event)
        if digest in unique:
            continue
        unique.add(digest)
        if event["type"] == "thread_settings_applied":
            settings[event["turn_id"]] = event
        else:
            contexts.append(event)
    changes, previous = [], {}
    for event in sorted(contexts, key=lambda item: datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))):
        for field in ("model", "effort", "context_window"):
            value = event.get(field)
            if value is None:
                continue
            if field in previous and previous[field] != value:
                old = previous[field]
                applied = settings.get(event["turn_id"], {}).get(field) == value
                evidence_status = "applied" if applied else "unrecorded" if events_coverage_complete else "unknown"
                direction = "unknown"
                if field == "effort":
                    direction = "down" if EFFORTS.index(value) < EFFORTS.index(old) else "up"
                elif field == "context_window":
                    direction = "down" if value < old else "up"
                else:
                    before, after = catalog_by_id.get(old, {}), catalog_by_id.get(value, {})
                    if before.get("upgrade") == value:
                        direction = "up"
                    elif after.get("upgrade") == old or after.get("visibility") == "hidden":
                        direction = "down"
                    elif type(before.get("priority")) is int and type(after.get("priority")) is int:
                        direction = "up" if after["priority"] > before["priority"] else "down" \
                            if after["priority"] < before["priority"] else "lateral"
                changes.append({"turn_id": event["turn_id"], "timestamp": event["timestamp"], "field": field,
                                "before": old, "after": value, "settings_evidence": evidence_status,
                                "direction": direction})
            previous[field] = value
    return {"status": "changes_observed" if changes else "no_change_observed" if events and events_coverage_complete else "unknown",
            "metadata_available": bool(contexts), "coverage_complete_declared": events_coverage_complete,
            "changes": changes, "unique_events": len(unique), "catalog_available": bool(catalog),
            "interpretation": "request_settings_only_not_actual_model_weights"}


def validate_account_evidence(data: dict) -> dict:
    fields = {"schema", "account_alias", "expected_model", "authorization", "provenance", "events_coverage_complete",
              "observations", "events", "catalog"}
    if not isinstance(data, dict) or not fields - {"observations"} <= set(data) or set(data) - fields \
            or data.get("schema") != "integrity-official-account-evidence/v1":
        raise ValueError("invalid official-account evidence schema")
    _identifier(data["account_alias"])
    _identifier(data["expected_model"])
    validate_authorization(data["authorization"])
    provenance = data["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {"source", "trusted", "complete"} \
            or provenance["source"] != "official_account" or type(provenance["trusted"]) is not bool \
            or type(provenance["complete"]) is not bool or type(data["events_coverage_complete"]) is not bool:
        raise ValueError("invalid official-account provenance declaration")
    _validate_events(data["events"], data["catalog"])
    observations = data.get("observations", [])
    if not isinstance(observations, list) or len(observations) > 3:
        raise ValueError("official-account observation ceiling exceeded")
    for item in observations:
        if not isinstance(item, dict) or set(item) - OBSERVATION_FIELDS or item.get("observation_schema") != "integrity-observation/v1":
            raise ValueError("body-free official-account projection required")
        if type(item.get("expected_count")) is not int or not 292 <= item["expected_count"] <= 332:
            raise ValueError("official-account expected count required")
        if not isinstance(item.get("numbers"), list) or len(item["numbers"]) > 10000:
            raise ValueError("bounded official-account numerical vector required")
        if any(type(value) is not int or not 1 <= value <= 355 for value in item["numbers"]):
            raise ValueError("invalid official-account numerical vector")
        if type(item.get("valid")) is not bool or item.get("invalid_reason") not in {None, "invalid_response",
                "incomplete_protocol", "invalid_finish_reason", "protocol_error", "invalid_text_type",
                "response_too_large", "insufficient_numbers"}:
            raise ValueError("invalid official-account projection validity")
    canonical_hash(data)
    return json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))


def analyze_account_evidence(data: dict) -> dict:
    from .scoring import score_strategy
    data = validate_account_evidence(data)
    result = {"scope": "official_account", "account_alias": data["account_alias"],
              "evidence_hash": canonical_hash(data), "evidence_authenticity": "not_verified",
              "identity_authenticated": False, "calibration_status": "unvalidated",
              "source_verdict": "UNKNOWN", "fingerprint": None,
              "metadata_status": "available" if any(e["type"] != "thread_settings_applied" for e in data["events"]) else "unavailable",
              "metadata": analyze_metadata(data["events"], data["catalog"],
                                           events_coverage_complete=data["events_coverage_complete"])}
    if not data["provenance"]["trusted"] or not data["provenance"]["complete"]:
        result.update(status="unknown", reason="untrusted_or_incomplete_evidence")
        return result
    observations = data.get("observations", [])
    if observations:
        result["fingerprint"] = score_strategy(get_strategy("nerfed"), observations, expected_model=data["expected_model"])
        result["source_verdict"] = result["fingerprint"]["source_verdict"]
    result["status"] = "analyzed" if observations or data["events"] else "unknown"
    return result


def analyze_passive_evidence(data: dict) -> dict:
    fields = {"schema", "client_alias", "authorization", "summary"}
    if not isinstance(data, dict) or set(data) != fields or data.get("schema") != "integrity-passive-summary/v1":
        raise ValueError("invalid passive-summary schema")
    _identifier(data["client_alias"])
    validate_authorization(data["authorization"])
    summary = data["summary"]
    allowed = {"requests", "completed", "failed", "unknown", "protocol", "model", "window_start", "window_end"}
    if not isinstance(summary, dict) or set(summary) - allowed:
        raise ValueError("passive counters only; evaluator outputs are not accepted")
    for key in ("requests", "completed", "failed", "unknown"):
        if type(summary.get(key)) is not int or not 0 <= summary[key] <= 1000000:
            raise ValueError("invalid passive-summary counters")
    if summary["completed"] + summary["failed"] + summary["unknown"] != summary["requests"]:
        raise ValueError("passive-summary denominator mismatch")
    _identifier(summary.get("protocol"))
    _identifier(summary.get("model"))
    for key in ("window_start", "window_end"):
        if not isinstance(summary.get(key), str) or len(summary[key]) > 40:
            raise ValueError("passive-summary window required")
        try:
            timestamp = datetime.fromisoformat(summary[key].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError("timezone required")
        except ValueError as exc:
            raise ValueError("invalid passive-summary window") from exc
    return {"scope": "passive_client", "client_alias": data["client_alias"], "summary": dict(summary),
            "evidence_hash": canonical_hash(data), "status": "imported_summary",
            "source_verdict": "UNKNOWN", "identity_authenticated": False, "evidence_authenticity": "not_verified"}


def validate_analysis_output(data: dict) -> dict:
    """Validate the entire account-analysis persistence contract; reject extra keys."""
    fields = {"scope", "account_alias", "evidence_hash", "evidence_authenticity", "identity_authenticated",
              "calibration_status", "source_verdict", "fingerprint", "metadata", "metadata_status", "status", "reason"}
    if not isinstance(data, dict) or not fields - {"reason"} <= set(data) or set(data) - fields:
        raise ValueError("invalid analysis output schema")
    _identifier(data["account_alias"])
    if data["scope"] != "official_account" or data["evidence_authenticity"] != "not_verified" \
            or data["identity_authenticated"] is not False or data["calibration_status"] != "unvalidated" \
            or data["source_verdict"] not in {"UNKNOWN", "UNLISTED", "SUSPICIOUS", "MISMATCH"} \
            or data["metadata_status"] not in {"available", "unavailable"} \
            or data["status"] not in {"unknown", "analyzed"} \
            or data.get("reason") not in {None, "untrusted_or_incomplete_evidence"}:
        raise ValueError("invalid analysis output semantics")
    if not isinstance(data["evidence_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["evidence_hash"]):
        raise ValueError("invalid analysis evidence hash")
    metadata = data["metadata"]
    metadata_fields = {"status", "metadata_available", "coverage_complete_declared", "changes", "unique_events",
                       "catalog_available", "interpretation"}
    if not isinstance(metadata, dict) or set(metadata) != metadata_fields \
            or metadata["status"] not in {"changes_observed", "no_change_observed", "unknown"} \
            or metadata["interpretation"] != "request_settings_only_not_actual_model_weights":
        raise ValueError("invalid metadata output schema")
    if any(type(metadata[key]) is not bool for key in ("metadata_available", "coverage_complete_declared", "catalog_available")) \
            or type(metadata["unique_events"]) is not int or not 0 <= metadata["unique_events"] <= 1000:
        raise ValueError("invalid metadata output counters")
    changes = metadata["changes"]
    if not isinstance(changes, list) or len(changes) > 3000:
        raise ValueError("invalid metadata output changes")
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"turn_id", "timestamp", "field", "before", "after",
                                                        "settings_evidence", "direction"}:
            raise ValueError("invalid metadata change schema")
        if change["field"] not in {"model", "effort", "context_window"} or change["settings_evidence"] not in {
                "applied", "unrecorded", "unknown"} or change["direction"] not in {"unknown", "down", "up", "lateral"}:
            raise ValueError("invalid metadata change semantics")
        for side in ("before", "after"):
            _validate_events([{"type": "turn_context", "turn_id": change["turn_id"], "timestamp": change["timestamp"],
                               change["field"]: change[side]}], [])
    fingerprint = data["fingerprint"]
    if fingerprint is not None:
        fields = {"strategy_id", "calibration_status", "asset_hash", "scorer_hash", "sampling_hash", "manifest_hash",
                  "identity_authenticated", "valid_answers", "invalid_answers", "requested_answers", "missing_answers",
                  "diagnostics", "expected_model", "prediction", "probability", "results", "calibration", "status",
                  "source_verdict", "p_expected", "fused_margin_sigma", "family_probabilities", "conditions"}
        required = fields - {"p_expected", "fused_margin_sigma", "family_probabilities"}
        if not isinstance(fingerprint, dict) or not required <= set(fingerprint) or set(fingerprint) - fields:
            raise ValueError("invalid fingerprint output schema")
        if fingerprint["strategy_id"] != "nerfed" or fingerprint["calibration_status"] != "unvalidated" \
                or fingerprint["identity_authenticated"] is not False or fingerprint["conditions"] is not None \
                or fingerprint["source_verdict"] not in {"UNKNOWN", "UNLISTED", "SUSPICIOUS", "MISMATCH"} \
                or fingerprint["status"] not in {"insufficient_data", "scored"}:
            raise ValueError("invalid fingerprint output semantics")
        for key in ("asset_hash", "scorer_hash", "sampling_hash", "manifest_hash"):
            if not isinstance(fingerprint[key], str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint[key]):
                raise ValueError("invalid fingerprint output hash")
        _identifier(fingerprint["expected_model"])
        _identifier(fingerprint["prediction"], optional=True)
        for key in ("valid_answers", "invalid_answers", "requested_answers", "missing_answers"):
            if type(fingerprint[key]) is not int or not 0 <= fingerprint[key] <= 3:
                raise ValueError("invalid fingerprint output counters")
        for key in ("probability", "p_expected", "fused_margin_sigma"):
            value = fingerprint.get(key)
            if value is not None and (type(value) not in {int, float} or not math.isfinite(value)):
                raise ValueError("invalid fingerprint output score")
        rows = fingerprint["results"]
        if not isinstance(rows, list) or len(rows) > 16:
            raise ValueError("invalid fingerprint result set")
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"model", "display_name", "family", "probability", "score"}:
                raise ValueError("invalid fingerprint result schema")
            _identifier(row["model"])
            _identifier(row["family"])
            if not isinstance(row["display_name"], str) or len(row["display_name"]) > 120:
                raise ValueError("invalid fingerprint display name")
            if any(type(row[key]) not in {int, float} or not math.isfinite(row[key]) for key in ("probability", "score")):
                raise ValueError("invalid fingerprint result score")
        families = fingerprint.get("family_probabilities", {})
        if not isinstance(families, dict) or len(families) > 16:
            raise ValueError("invalid fingerprint family set")
        for key, value in families.items():
            _identifier(key)
            if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1 + 1e-9:
                raise ValueError("invalid fingerprint family probability")
        calibration = fingerprint["calibration"]
        if calibration is not None and (not isinstance(calibration, dict) or set(calibration) != {"queries", "beta"}
                or calibration["queries"] not in {"1", "2", "3"} or type(calibration["beta"]) not in {int, float}
                or not math.isfinite(calibration["beta"])):
            raise ValueError("invalid fingerprint calibration")
        diagnostics = fingerprint["diagnostics"]
        if not isinstance(diagnostics, list) or len(diagnostics) > 3:
            raise ValueError("invalid fingerprint diagnostics")
        for item in diagnostics:
            if not isinstance(item, dict) or set(item) != {"probe_id", "valid", "invalid_reason", "parsed_numbers",
                                                          "minimum_numbers", "expected_count"}:
                raise ValueError("invalid fingerprint diagnostic schema")
            _identifier(item["probe_id"])
            if type(item["valid"]) is not bool or item["invalid_reason"] not in {None, "invalid_response",
                    "incomplete_protocol", "invalid_finish_reason", "protocol_error", "invalid_text_type",
                    "response_too_large", "insufficient_numbers"}:
                raise ValueError("invalid fingerprint diagnostic validity")
            if any(type(item[key]) is not int or not 0 <= item[key] <= 10000 for key in (
                    "parsed_numbers", "minimum_numbers", "expected_count")):
                raise ValueError("invalid fingerprint diagnostic counters")
    canonical_hash(data)
    return json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
