"""Read-only configuration inspection; no credential lookup or network calls."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError
from .engine import Settings, inspect_settings


def main():
    parser = argparse.ArgumentParser(description="生图测试只读配置检查")
    parser.add_argument("command", choices=["inspect-config"])
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = Settings.model_validate_json(args.config.read_bytes())
    except (OSError, ValidationError):
        print(json.dumps({"status": "invalid_config"}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "valid", "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                      "network_requested": False, **inspect_settings(config)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
