"""Side-effect-free inspect-config: imports no Registry or executor."""
from __future__ import annotations

import argparse
import hashlib
import json

from .domain import CONDITION_HASH, DEFAULT_THRESHOLDS, FIXED_INPUT, INTERVAL_SECONDS, VERSION, protocol


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["inspect-config"])
    parser.add_argument("--protocol", default="responses", choices=["openai", "responses", "anthropic"])
    args = parser.parse_args()
    result = {"detection_version": VERSION, "protocol": protocol(args.protocol), "condition_hash": CONDITION_HASH,
              "fingerprint": CONDITION_HASH,
              "sample_sha256": hashlib.sha256(FIXED_INPUT.encode()).hexdigest(), "minor_thresholds": DEFAULT_THRESHOLDS,
              "major_thresholds": {"ratio": 2, "difference": 1024}, "initial_requests": 2, "max_requests": 4,
              "max_retries": 0, "interval_seconds": INTERVAL_SECONDS, "rounds_per_day": 48,
              "initial_requests_per_target_day": 96, "max_requests_per_target_day": 192,
              "trusted_reference": "explicit_saved_model_protocol_registry_binding_required",
              "billing_status": "unverified", "requests_sent": 0, "database_opened": False}
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
