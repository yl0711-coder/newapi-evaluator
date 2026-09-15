"""Read public target metadata without initialising databases or sending requests."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from .models import Target, fingerprint


def inspect(directory, channel_id=None, protocol="openai", model="diagnosis-mock"):
    target = Target(mode="live" if channel_id else "mock", channel_id=channel_id, protocol=protocol, model=model)
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "mode": target.mode,
              "protocol": protocol, "model": model, "requests_sent": 0, "database_modified": False}
    if channel_id:
        path = Path(directory).expanduser().resolve() / "channels.db"
        if not path.is_file():
            raise ValueError("公共渠道库不存在")
        conn = sqlite3.connect(path.as_uri() + "?mode=ro")
        try:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute("SELECT id,version,base_url,enabled FROM channels WHERE id=?", (channel_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ValueError("渠道不存在")
        result.update(alias=f"渠道 #{row[0]}", version=row[1], enabled=bool(row[3]), host_masked="***",
                      host_fingerprint=fingerprint(urlsplit(row[2]).hostname or ""), endpoint_fingerprint=fingerprint(row[2]))
    else:
        result["alias"] = "本地 Mock"
    result["fingerprint"] = fingerprint({k: v for k, v in result.items() if k != "checked_at"})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--channel-id", type=int)
    parser.add_argument("--protocol", choices=("openai", "responses", "anthropic"), default="openai")
    parser.add_argument("--model", default="diagnosis-mock")
    args = parser.parse_args()
    try:
        result = inspect(args.data_dir, args.channel_id, args.protocol, args.model)
    except (ValueError, sqlite3.Error):
        parser.exit(2, "只读核对失败：请检查字段和渠道库结构。\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
