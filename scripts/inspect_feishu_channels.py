"""Inspect the sync contract and optional local metadata without credentials or requests."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.feishu_reader import FIELDS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    result = {"mode": "manual", "fields": list(FIELDS), "online_value": "已上线", "requires_key": True,
              "requires_baseurl": True, "requires_multiplier": True, "requests_sent": 0,
              "database_modified": False, "credentials_decrypted": False, "configured": False,
              "sqlite_coordination_files_possible": bool(args.data_dir)}
    if args.data_dir:
        path = args.data_dir.resolve() / "channels.db"
        if not path.is_file():
            parser.error("指定目录中不存在渠道库")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT value FROM registry_meta WHERE key='feishu_channel_sync'").fetchone()
            if row:
                value = json.loads(row[0])
                result.update(configured=bool(value.get("secret_enc")), version=value.get("version"),
                              source_fingerprint=hashlib.sha256(value.get("table_url", "").encode()).hexdigest(),
                              last_success=value.get("last_success"))
    result["fingerprint"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
