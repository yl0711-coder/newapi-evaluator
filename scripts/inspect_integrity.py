"""Read immutable strategy contracts without opening databases or credentials."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from features.integrity.strategies import list_strategies

    strategies = list_strategies()
    from features.stability.app.timetable_contract import DEFAULTS, VERSION, build_slots
    from datetime import date
    config = {**DEFAULTS, "registry_channel_ids": [1]}
    graph = build_slots({"id": 1, "layered_config": config, "timezone": "Asia/Shanghai"}, date(2026, 10, 8))
    timetable = {"plan_version": VERSION, "defaults": DEFAULTS, "channels": 1,
                 "daily_request_formula": "N * (192*len(canary_times) + 3*len(modeltrace_times) + len(union))",
                 "default_requests_per_channel": sum({"canary": 192, "modeltrace": 3, "health": 1}[r["method"]] for r in graph),
                 "occurrences": len(graph), "windows_seconds": {"canary": 3600, "modeltrace": 600},
                 "both_methods_empty_allowed": True, "dst": "skip_gap_first_fold", "inflight": 1}
    contract = {"schema_version": "eval-integrity-inspect/v1", "strategies": strategies, "timetable": timetable,
                "requests_sent": 0, "databases_opened": 0, "credentials_read": False,
                "api_calibration": "unvalidated", "evidence_type": "source"}
    contract["configuration_fingerprint"] = hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    contract["fingerprint"] = contract["configuration_fingerprint"]
    print(json.dumps(contract, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
