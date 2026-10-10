"""Fixed, persisted connection retries; uncertain sends are never repeated."""
from __future__ import annotations

import json

from .execution import ProbeRequest, build_requests

POLICY = "connection-retries-v1"
MAX_RETRIES = 3
RETRYABLE = frozenset({"network_error", "timeout", "stream_break"})


def requests(manifest, config):
    result = []
    for request in build_requests(manifest, config):
        result.append(request)
        for retry in range(1, MAX_RETRIES + 1):
            result.append(ProbeRequest(f"{request.request_id}:retry-{retry}", request.probe,
                                       request.input_tokens_reserved, request.output_tokens_reserved))
    return result


def skip_reason(request, outputs):
    base, marker, retry = request.request_id.rpartition(":retry-")
    if not marker:
        return None
    previous_id = base if retry == "1" else f"{base}:retry-{int(retry) - 1}"
    previous = next((r for r in outputs if r["request_id"] == previous_id), None)
    if previous is None:
        return "retry_not_needed"
    return None if previous.get("status") in RETRYABLE else "retry_not_needed"


def summarize(attempts, planned):
    latest = {}
    for attempt in attempts:
        base, marker, retry = attempt["request_id"].rpartition(":retry-")
        base, rank = (base, int(retry)) if marker else (attempt["request_id"], 0)
        if base not in latest or rank > latest[base][0]:
            latest[base] = (rank, attempt)
    final = [a for _, a in latest.values()]
    outputs = [json.loads(a["result_json"]) for a in final if a["status"] == "completed"]
    valid = sum(bool(r.get("valid")) for r in outputs)
    return {"planned": planned, "attempted": len(final), "physical_attempts": len(attempts),
            "valid": valid, "invalid": len(outputs) - valid,
            "unknown": sum(a["status"] in {"unknown", "permitted"} for a in final),
            "not_run": max(0, planned - len(final)),
            "request_errors": sum(r.get("status") not in {"completed", "success", "ok"} for r in outputs),
            "retry_policy": POLICY, "max_retries": MAX_RETRIES, "outputs": outputs}


def final_outputs(results):
    by_probe = {}
    for result in results:
        probe = result.get("probe_id", result["request_id"].split(":retry-", 1)[0])
        previous = by_probe.get(probe)
        rank = int(result["request_id"].rsplit(":retry-", 1)[1]) if ":retry-" in result["request_id"] else 0
        previous_rank = (int(previous["request_id"].rsplit(":retry-", 1)[1])
                         if previous and ":retry-" in previous["request_id"] else 0)
        if previous is None or rank > previous_rank:
            by_probe[probe] = result
    return list(by_probe.values())
