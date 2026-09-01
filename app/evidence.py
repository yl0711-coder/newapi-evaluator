"""Per-metric evidence eligibility for model-channel evaluations."""
from typing import Literal

Eligibility = Literal["eligible", "ineligible", "censored", "unknown"]


def assess(*, completion_status: str, attribution: str,
           grade_status: str, completion_policy: str = "answer_sufficient") -> dict[str, Eligibility]:
    result: dict[str, Eligibility] = {
        "ability": "ineligible",
        "stability": "unknown",
        "performance": "unknown",
        "cost": "unknown",
        "risk": "unknown",
    }
    if attribution == "client_network_suspect" or completion_status == "client_interrupted":
        result.update(stability="ineligible", performance="ineligible")
        return result
    if attribution == "client_load_suspect":
        if completion_status in {"completed", "completed_with_truncation"}:
            result.update(
                ability="eligible" if grade_status in {"passed", "failed", "partial"}
                else "ineligible",
                stability="eligible", performance="ineligible", cost="eligible", risk="eligible")
        return result
    if completion_status == "upstream_error" or attribution == "upstream_suspect":
        result.update(stability="eligible", performance="censored", cost="eligible", risk="eligible")
        return result
    if completion_status == "timeout":
        result.update(stability="unknown", performance="censored")
        return result
    if completion_status in {"empty", "protocol_error"}:
        result.update(stability="eligible", performance="censored", cost="eligible", risk="eligible")
        return result
    if completion_status == "completed_with_truncation":
        result.update(stability="eligible", performance="eligible", cost="eligible", risk="eligible")
        if grade_status in {"passed", "failed", "partial"} and completion_policy == "answer_sufficient":
            result["ability"] = "eligible"
        return result
    if completion_status == "completed":
        result.update(stability="eligible", performance="eligible", cost="eligible", risk="eligible")
        if grade_status in {"passed", "failed", "partial"}:
            result["ability"] = "eligible"
        return result
    return result


def attribution_for(*, completion_status: str, reason: str = "") -> str:
    if completion_status == "client_interrupted":
        return "client_network_suspect"
    if completion_status == "upstream_error" or reason in {"上游错误", "限流"}:
        return "upstream_suspect"
    if completion_status in {"timeout", "protocol_error"}:
        return "mixed_or_unknown"
    return "valid"

