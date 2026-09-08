from __future__ import annotations

import argparse
import hashlib
import json
import time
from typing import Any

from .api import DEFAULT_DATA_DIR
from .storage import Store


def sanitized_snapshot(participation: dict[str, Any]) -> dict[str, Any]:
    configuration = {
        "channel": participation["channel_name"],
        "test_group": participation["test_group"],
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            configuration, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return {
        "participation_id": participation["id"],
        "channel_alias": f"candidate-{participation['id']}",
        "test_group": participation["test_group"],
        "sync_status": participation["sync_status"],
        "written_field_count": len(participation["fields"]),
        "configuration_fingerprint": fingerprint,
        "extracted_at": int(time.time()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="输出准入渠道的脱敏验收快照")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--latest", action="store_true")
    args = parser.parse_args()
    if bool(args.run_id) == bool(args.latest):
        parser.error("请且仅请指定 --run-id 或 --latest")
    store = Store(DEFAULT_DATA_DIR / "framework.db")
    try:
        participation = store.get(args.run_id) if args.run_id else store.latest()
        if participation is None:
            raise SystemExit("没有可提取的准入参与记录")
        print(json.dumps(sanitized_snapshot(participation), ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
