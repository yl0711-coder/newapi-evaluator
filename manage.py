#!/usr/bin/env python3
"""部署后初始化、导入与启停渠道配置；这些命令都不发送渠道请求。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

import hourly_channel_diagnostic as diagnostic
from channel_catalog import parse_document


def write_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.close()
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def initialize(state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (state / "config.json").exists():
        write_private(state / "config.json", {"timezone": "Asia/Shanghai", "rounds": 5, "juice_runs": 5,
                                             "test_models": list(diagnostic.DEFAULT_TEST_MODELS),
                                             "data_dir": "data", "channels": []})
    if not (state / "credentials.json").exists():
        write_private(state / "credentials.json", {"credentials": {}})
    (state / "reports").mkdir(exist_ok=True, mode=0o755)
    refresh(state)


def refresh(state: Path) -> None:
    config = diagnostic.load_config(state / "config.json")
    diagnostic.build_report(Path(config["data_dir"]) / "diagnostic.sqlite3",
                            state / "reports/report.html", config["timezone"], config["channels"], config["test_models"])


def import_document(source: Path, state: Path, default_model: str | None = None) -> dict:
    channels, incoming = parse_document(source.read_text(encoding="utf-8"), default_model)
    initialize(state)
    config = diagnostic.load_config(state / "config.json")
    saved = diagnostic.load_credentials(state / "credentials.json")
    by_id = {c.get("id", c["name"]): c for c in config["channels"]}
    for channel in channels:
        previous = by_id.get(channel["id"])
        if previous:
            channel["enabled"] = bool(previous.get("enabled")) and channel["model_confirmed"]
        by_id[channel["id"]] = channel
    config["channels"] = list(by_id.values())
    config["data_dir"] = "data"
    write_private(state / "credentials.json", {"credentials": {**saved, **incoming}})
    write_private(state / "config.json", config)
    refresh(state)
    return {"imported": len(channels), "total": len(config["channels"]),
            "pending_models": sum(not c["model_confirmed"] for c in channels)}


def configure_enabled(state: Path, ids: list[str], enabled: bool,
                      model_for_missing: str | None = None) -> None:
    config = diagnostic.load_config(state / "config.json")
    selected = [c for c in config["channels"] if not ids or c.get("id") in ids]
    if not selected or set(ids) - {c.get("id") for c in selected}:
        raise ValueError("渠道 ID 不存在")
    for channel in selected:
        if channel.get("model_confirmed") is False and enabled and model_for_missing:
            channel.update(model=model_for_missing, model_confirmed=True)
        channel["enabled"] = enabled
    config["data_dir"] = "data"
    write_private(state / "config.json", config)
    refresh(state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "import", "inspect", "enable", "disable", "report"))
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--default-model")
    parser.add_argument("--model-for-missing")
    parser.add_argument("--id", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    state = args.state_dir.resolve()
    if state.is_relative_to(Path(__file__).resolve().parent):
        parser.error("私有状态目录必须位于源码目录外")
    try:
        if args.command == "inspect":
            diagnostic.inspect_config(diagnostic.load_config(state / "config.json"))
            return 0
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with diagnostic.run_lock(state / "data/scheduler.sqlite3"), diagnostic.run_lock(state / "data/diagnostic.sqlite3"):
            if args.command == "init":
                initialize(state)
            elif args.command == "import":
                if not args.source:
                    parser.error("import 需要 --source")
                print(json.dumps(import_document(args.source, state, args.default_model), ensure_ascii=False))
            elif args.command in ("enable", "disable"):
                if not args.id and not args.all:
                    parser.error("请选择 --id 或 --all")
                configure_enabled(state, args.id, args.command == "enable", args.model_for_missing)
            else:
                refresh(state)
        print("操作完成；未发送渠道请求。")
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        # 只传播本程序生成的配置错误，不回显输入文档或解析器上下文。
        print(f"操作失败（{type(exc).__name__}）；检查命令、文件权限、模型确认及巡检是否已停止。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
