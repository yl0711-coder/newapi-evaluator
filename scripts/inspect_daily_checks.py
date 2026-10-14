"""Zero-side-effect inspection of the daily scheduling and reporting contract."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.daily_checks.domain import contract, fingerprint


if __name__ == "__main__":
    value = contract()
    print(json.dumps({**value, "fingerprint": fingerprint(value), "requests_sent": 0,
                      "database_opened": False, "credentials_read": False, "notifications_sent": 0},
                     ensure_ascii=False, indent=2))
