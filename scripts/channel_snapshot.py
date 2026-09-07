"""从测试机安全环境一键生成脱敏渠道快照。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = Path(tempfile.mkdtemp(prefix="newapi-evaluator-channel-snapshot-"))
sys.path.insert(0, str(ROOT))

# app.config 在 import 时创建运行目录。快照命令强制放到系统临时目录，
# 绝不触碰仓库或正式数据目录。
os.environ["TEST_DATA_DIR"] = str(RUNTIME_ROOT / "runtime")
os.environ["TEST_BACKUP_DIR"] = str(RUNTIME_ROOT / "runtime" / "backups")

from app.channel_snapshot import (  # noqa: E402
    SnapshotConfigError,
    build_snapshot,
    parse_snapshot_config,
)


def _inside_repository(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT)
        return True
    except ValueError:
        return False


def _config_text(config_path: str) -> str:
    if config_path:
        path = Path(config_path).expanduser().resolve()
        if _inside_repository(path):
            raise SnapshotConfigError("渠道配置文件必须位于仓库之外")
        try:
            if path.stat().st_size > 64 * 1024:
                raise SnapshotConfigError("渠道配置文件过大")
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SnapshotConfigError("无法读取渠道配置文件") from exc

    inline = os.getenv("CHANNEL_SNAPSHOT_INPUT", "").strip()
    if inline:
        return inline
    values = {
        "name": os.getenv("CHANNEL_SNAPSHOT_ALIAS", ""),
        "base_url": os.getenv("CHANNEL_SNAPSHOT_BASE_URL", ""),
        "model": os.getenv("CHANNEL_SNAPSHOT_MODEL", ""),
        "api_key": os.getenv("CHANNEL_SNAPSHOT_API_KEY", ""),
        "protocol": os.getenv("CHANNEL_SNAPSHOT_PROTOCOL", "openai"),
    }
    return json.dumps(values, ensure_ascii=False)


def _output_path(value: str) -> Path:
    if value:
        output = Path(value).expanduser().resolve()
    else:
        output = RUNTIME_ROOT / "channel-snapshot.json"
    if _inside_repository(output):
        raise SnapshotConfigError("渠道快照必须输出到仓库之外")
    return output


def _write_snapshot(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从本机安全配置提取脱敏渠道快照",
    )
    parser.add_argument(
        "--config",
        default=os.getenv("CHANNEL_SNAPSHOT_CONFIG", ""),
        help="仓库外的本地渠道配置文件；默认读取 CHANNEL_SNAPSHOT_CONFIG",
    )
    parser.add_argument(
        "--output",
        default=os.getenv("CHANNEL_SNAPSHOT_OUTPUT", ""),
        help="仓库外的快照路径；默认位于系统临时目录",
    )
    args = parser.parse_args()

    try:
        config = parse_snapshot_config(_config_text(args.config))
        timeout = float(os.getenv("CHANNEL_SNAPSHOT_TIMEOUT_SECONDS", "20"))
        snapshot = asyncio.run(build_snapshot(config, timeout_seconds=timeout))
        output = _output_path(args.output)
        _write_snapshot(output, snapshot)
    except (SnapshotConfigError, ValueError):
        print("渠道快照生成失败：本机安全配置无效或不完整", file=sys.stderr)
        return 1
    except Exception as exc:  # 绝不打印可能携带请求内容的异常文本
        print(f"渠道快照生成失败：{type(exc).__name__}", file=sys.stderr)
        return 1

    print(f"snapshot={output}")
    print(f"probe_status={snapshot['probe']['status']}")
    return 0 if snapshot["probe"]["status"] == "通过" else 2


if __name__ == "__main__":
    raise SystemExit(main())
