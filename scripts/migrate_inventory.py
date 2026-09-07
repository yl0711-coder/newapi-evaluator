"""Read a legacy inventory without modifying its database or encryption key."""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.fernet import Fernet
from shared.registry import get_registry


def migrate(source_db: Path, source_key: Path, registry=None):
    registry = registry or get_registry()
    cipher = Fernet(source_key.read_bytes())
    connection = sqlite3.connect(source_db.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in connection.execute("SELECT * FROM channel_inventory ORDER BY id")]
        operational = {name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                       for name in ("channels", "schedules", "runs")}
    finally:
        connection.close()
    records = [{**row, "api_key": cipher.decrypt(row["api_key_enc"].encode()).decode()} for row in rows]
    result = registry.import_records(records)
    for original, channel_id in zip(records, result["ids"]):
        restored = registry.get(channel_id, secret=True)
        if any(restored[field] != original[field] for field in ("name", "base_url", "scope", "multiplier", "api_key")):
            raise RuntimeError("渠道迁移校验失败")
    return {"source_records": len(rows), "added": result["added"], "skipped": result["skipped"],
            "verified": len(records), "legacy_operational_counts": operational}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="将旧定时工具的渠道资料迁入公共库，不启动测试")
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--source-key", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(migrate(args.source_db, args.source_key))
    except Exception as exc:
        raise SystemExit(f"迁移失败 ({type(exc).__name__})，请检查数据库及配套密钥；源文件未被修改") from None
