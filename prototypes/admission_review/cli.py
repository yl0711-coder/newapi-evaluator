from __future__ import annotations

import argparse
import hashlib
import json
import time
from typing import Any
from urllib.parse import urlsplit

from .api import DEFAULT_DATA_DIR
from .storage import Store


def sanitized_snapshot(run: dict[str, Any]) -> dict[str, Any]:
    parsed = urlsplit(run["channel_url"])
    hostname = parsed.hostname or ""
    host_fingerprint = hashlib.sha256(hostname.encode()).hexdigest()[:12]
    configuration = {
        "url": run["channel_url"],
        "model": run["model"],
        "protocol": run["protocol"],
    }
    fingerprint = hashlib.sha256(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "run_id": run["id"],
        "channel_alias": f"candidate-{run['id']}",
        "protocol": run["protocol"],
        "masked_host": f"{parsed.scheme}://host-{host_fingerprint}",
        "models": [run["model"]],
        "model_count": 1,
        "credential_supplied": run["credential_supplied"],
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
        run = store.get_run(args.run_id) if args.run_id else store.latest_run()
        if run is None:
            raise SystemExit("没有可提取的准入测试记录")
        print(json.dumps(sanitized_snapshot(run), ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
