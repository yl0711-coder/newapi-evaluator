"""Read-only protocol configuration evidence; never decrypts keys or sends requests."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.protocol_admission.catalog import PROTOCOLS, CHECKS, fingerprint


def inspect(directory=None):
    channels = []
    if directory is not None:
        database = Path(directory).resolve() / "channels.db"
        if not database.is_file():
            raise ValueError("channel database missing")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
            for row in conn.execute("SELECT id,base_url,version,enabled FROM channels ORDER BY id"):
                channels.append({"alias": f"channel-{row[0]}", "masked_host": "host-" + hashlib.sha256((urlsplit(row[1]).hostname or "").encode()).hexdigest()[:12],
                                 "version": row[2], "enabled": bool(row[3])})
    value = {"channels": channels, "protocols": PROTOCOLS,
             "checks": {key: {"protocol": value[0], "path": "/v1" + value[1], "stream": value[2]} for key, value in CHECKS.items()},
             "checks_per_model": len(CHECKS), "max_models": 5}
    return {**value, "fingerprint": fingerprint(value), "extracted_at": int(time.time()), "requests_sent": 0, "secrets_read": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    try:
        value = inspect(args.data_dir)
    except (ValueError, sqlite3.Error, OSError):
        parser.error("cannot read requested protocol configuration")
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    main()
