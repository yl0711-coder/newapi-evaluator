from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from features.admission import storage


def sanitized_snapshot(entry: dict) -> dict:
    parsed = urlsplit(entry["channel_url"])
    hostname = parsed.hostname or ""
    host_fingerprint = hashlib.sha256(hostname.encode()).hexdigest()[:12]
    configuration_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "channel_url": entry["channel_url"],
                "test_group": entry["test_group"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "submission_id": entry["id"],
        "channel_alias": f"candidate-{entry['id']}",
        "masked_host": f"{parsed.scheme}://host-{host_fingerprint}",
        "test_group": entry["test_group"],
        "sync_status": entry["status"],
        "written_field_count": len(entry["fields"]),
        "configuration_fingerprint": configuration_fingerprint,
        "extracted_at": int(time.time()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="输出准入飞书写入的脱敏验收快照")
    parser.add_argument("--entry-id", type=int)
    parser.add_argument("--latest", action="store_true")
    args = parser.parse_args()
    if bool(args.entry_id) == bool(args.latest):
        parser.error("请且仅请指定 --entry-id 或 --latest")
    try:
        entry = (
            storage.get_feishu_record(args.entry_id)
            if args.entry_id
            else storage.latest_feishu_record()
        )
        if entry is None:
            raise SystemExit("没有可提取的准入飞书记录")
        print(json.dumps(sanitized_snapshot(entry), ensure_ascii=False, indent=2))
    finally:
        storage.close()


if __name__ == "__main__":
    main()
