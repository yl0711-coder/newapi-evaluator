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
    contract = {"schema_version": "eval-integrity-inspect/v1", "strategies": strategies,
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
