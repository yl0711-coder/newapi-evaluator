"""渠道快照自测：使用假上游验证一键执行、稳定指纹与彻底脱敏。"""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


TEST_ROOT = Path(tempfile.mkdtemp(prefix="channel-snapshot-selftest-"))
os.environ["TEST_DATA_DIR"] = str(TEST_ROOT / "runtime")
os.environ["TEST_BACKUP_DIR"] = str(TEST_ROOT / "runtime" / "backups")
os.environ["TEST_EGRESS_ALLOWLIST"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from mock_upstream import app as mock_app  # noqa: E402


ROOT = Path(__file__).resolve().parent
GOOD_KEY = "sk-test-good-key-123"
PRIVATE_KEY = "sk-test-private-key-456"
fails: list[str] = []


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


PORT = available_port()
UPSTREAM = f"http://127.0.0.1:{PORT}"


def check(name: str, condition: bool, extra: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {extra}"))
    if not condition:
        fails.append(name)


def serve() -> None:
    server = uvicorn.Server(uvicorn.Config(
        mock_app, host="127.0.0.1", port=PORT, log_level="error",
    ))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(60):
        try:
            httpx.get(f"{UPSTREAM}/v1/models", timeout=1, trust_env=False)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError("假上游未启动")


def run_snapshot(env_updates: dict[str, str], output: Path | None = None) -> tuple[subprocess.CompletedProcess[str], Path]:
    env = {**os.environ, **env_updates}
    for name in (
        "CHANNEL_SNAPSHOT_CONFIG", "CHANNEL_SNAPSHOT_INPUT", "CHANNEL_SNAPSHOT_ALIAS",
        "CHANNEL_SNAPSHOT_BASE_URL", "CHANNEL_SNAPSHOT_MODEL",
        "CHANNEL_SNAPSHOT_API_KEY", "CHANNEL_SNAPSHOT_PROTOCOL", "CHANNEL_SNAPSHOT_OUTPUT",
    ):
        if name not in env_updates:
            env.pop(name, None)
    command = [sys.executable, "scripts/channel_snapshot.py"]
    if output is not None:
        command.extend(["--output", str(output)])
    result = subprocess.run(
        command, cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    path_text = next(
        (line.removeprefix("snapshot=") for line in result.stdout.splitlines()
         if line.startswith("snapshot=")),
        str(output or ""),
    )
    return result, Path(path_text)


def load(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    return json.loads(text), text


def main() -> int:
    print("=" * 50)
    print("渠道快照 v1")
    serve()

    common = {
        "CHANNEL_SNAPSHOT_ALIAS": "mock-channel",
        "CHANNEL_SNAPSHOT_BASE_URL": UPSTREAM,
        "CHANNEL_SNAPSHOT_MODEL": "gpt-4o-mini",
        "CHANNEL_SNAPSHOT_API_KEY": GOOD_KEY,
        "CHANNEL_SNAPSHOT_PROTOCOL": "openai",
    }
    first_run, first_path = run_snapshot(common)
    first, first_text = load(first_path)
    check("无参数单命令可生成快照", first_run.returncode == 0, first_run.stderr)
    check("假上游模型探测通过",
          first["probe"]["status"] == "通过"
          and first["channel"]["model_count"] == 3, first)
    check("快照不含凭据或认证头",
          GOOD_KEY not in first_text and "Authorization" not in first_text
          and "Bearer" not in first_text, first_text)
    mode = stat.S_IMODE(first_path.stat().st_mode)
    check("快照写在仓库外且权限受限",
          ROOT not in first_path.parents and (os.name == "nt" or mode == 0o600),
          {"path": str(first_path), "mode": oct(mode)})

    second_path = TEST_ROOT / "repeat.json"
    second_run, _ = run_snapshot(common, second_path)
    second, _ = load(second_path)
    check("相同非敏感配置产生稳定指纹",
          second_run.returncode == 0
          and first["channel"]["stable_id"] == second["channel"]["stable_id"]
          and first["channel"]["configuration_fingerprint"]
          == second["channel"]["configuration_fingerprint"])

    config_path = TEST_ROOT / "local-channel.json"
    config_path.write_text(json.dumps({
        "name": "file-channel", "base_url": UPSTREAM, "model": "gpt-4o-mini",
        "api_key": GOOD_KEY, "protocol": "openai",
    }), encoding="utf-8")
    file_output = TEST_ROOT / "from-file.json"
    file_run, _ = run_snapshot(
        {"CHANNEL_SNAPSHOT_CONFIG": str(config_path)}, file_output,
    )
    file_snapshot, file_text = load(file_output)
    check("仓库外本地配置复用现有导入格式",
          file_run.returncode == 0
          and file_snapshot["channel"]["alias"] == "file-channel"
          and GOOD_KEY not in file_text, file_run.stderr)

    failure_output = TEST_ROOT / "awaiting.json"
    failure_run, _ = run_snapshot({
        **common,
        "CHANNEL_SNAPSHOT_ALIAS": f"unsafe-{PRIVATE_KEY}",
        "CHANNEL_SNAPSHOT_API_KEY": PRIVATE_KEY,
    }, failure_output)
    failure, failure_text = load(failure_output)
    check("真实上游失败生成待确认证据", failure_run.returncode == 2
          and failure["probe"]["status"] == "待确认"
          and bool(failure["probe"]["error_summary"]), failure)
    check("失败证据不含响应正文、客户参数或凭据",
          all(secret not in failure_text + failure_run.stdout + failure_run.stderr
              for secret in (PRIVATE_KEY, "alice", "customer=", "token=", "invalid key")),
          failure_text + failure_run.stdout + failure_run.stderr)

    query_output = TEST_ROOT / "query-redaction.json"
    query_run, _ = run_snapshot({
        **common,
        "CHANNEL_SNAPSHOT_BASE_URL": f"{UPSTREAM}/customer/alice?token={PRIVATE_KEY}",
        "CHANNEL_SNAPSHOT_API_KEY": PRIVATE_KEY,
    }, query_output)
    query_snapshot, query_text = load(query_output)
    check("失败证据只保留脱敏主机信息",
          query_run.returncode == 2
          and query_snapshot["channel"]["base_url_masked"] == UPSTREAM
          and all(secret not in query_text for secret in (
              PRIVATE_KEY, "alice", "customer", "token=", "Not Found",
          )), query_text)

    anthropic_output = TEST_ROOT / "anthropic.json"
    anthropic_run, _ = run_snapshot({
        **common,
        "CHANNEL_SNAPSHOT_PROTOCOL": "anthropic",
        "CHANNEL_SNAPSHOT_MODEL": "claude-sonnet-5",
    }, anthropic_output)
    anthropic, _ = load(anthropic_output)
    check("复用 Anthropic 协议认证头与模型解析",
          anthropic_run.returncode == 0
          and anthropic["channel"]["protocol"] == "anthropic"
          and anthropic["channel"]["model_count"] == 3, anthropic)

    print("-" * 50)
    print("失败项：无" if not fails else "失败项：" + repr(fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
