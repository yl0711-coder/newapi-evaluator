#!/usr/bin/env python3
"""报告页的受保护运行控制 API；不返回凭据或请求正文。"""
from __future__ import annotations

import argparse
import hmac
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException
import uvicorn

import hourly_channel_diagnostic as diagnostic
import manage


ROOT = Path(__file__).resolve().parent


class ControlError(Exception):
    """可安全显示给控制台用户的控制错误。"""


class ProcessController:
    def __init__(self, state_dir: Path, credentials: Path):
        self.state_dir = state_dir.resolve()
        self.credentials = credentials.resolve()
        self.config_path = self.state_dir / "config.json"
        self.data_dir = self.state_dir / "data"
        self.report_path = self.state_dir / "reports/report.html"
        self.log_path = self.state_dir / "reports/worker.log"
        self._process: subprocess.Popen[str] | None = None
        self._started_at: float | None = None
        self._last_exit_code: int | None = None
        self._lock = threading.RLock()

    def _reap(self) -> None:
        if self._process is not None and self._process.poll() is not None:
            self._last_exit_code = self._process.returncode
            self._process = None
            self._started_at = None

    def _config(self) -> dict[str, Any]:
        try:
            return diagnostic.load_config(self.config_path)
        except (OSError, ValueError, SystemExit, TypeError) as exc:
            raise ControlError("配置文件无效，无法启动") from exc

    def _validate_start(self, config: dict[str, Any]) -> None:
        enabled = [channel for channel in config.get("channels", []) if channel.get("enabled", True)]
        if not enabled:
            raise ControlError("没有启用渠道，请先点击“启用全部渠道”")
        try:
            credentials = diagnostic.load_credentials(self.credentials)
        except (OSError, ValueError) as exc:
            raise ControlError("凭据文件无效或权限不是 0600/0400") from exc
        missing = [channel.get("name", "unnamed") for channel in enabled
                   if not diagnostic.channel_credentials(channel, credentials)[0]]
        if missing:
            raise ControlError(f"有 {len(missing)} 个启用渠道缺少凭据")

    def enable_all(self) -> None:
        with self._lock:
            try:
                manage.configure_enabled(self.state_dir, [], True)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, SystemExit) as exc:
                raise ControlError("启用渠道失败，请检查状态目录权限和配置") from exc

    def _launch(self, command_name: str) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, "-B", str(ROOT / "hourly_channel_diagnostic.py"), command_name,
                   "--config", str(self.config_path), "--credentials", str(self.credentials),
                   "--data-dir", str(self.data_dir), "--output", str(self.report_path),
                   "--confirm-live"]
        try:
            log = self.log_path.open("a", encoding="utf-8")
            self._process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                             start_new_session=True, text=True)
            log.close()
        except OSError as exc:
            if "log" in locals():
                log.close()
            raise ControlError("无法启动检测进程") from exc
        self._started_at = time.time()
        self._last_exit_code = None

    def start(self) -> None:
        with self._lock:
            self._reap()
            if self._process is not None:
                return
            config = self._config()
            self._validate_start(config)
            self._launch("daemon")

    def run_once(self) -> None:
        with self._lock:
            self._reap()
            if self._process is not None:
                raise ControlError("检测进程正在运行，请先等待本轮完成")
            config = self._config()
            self._validate_start(config)
            self._launch("run-once")

    def stop(self) -> None:
        with self._lock:
            self._reap()
            if self._process is None:
                return
            self._process.terminate()
            try:
                self._process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
            self._last_exit_code = 0
            self._process = None
            self._started_at = None

    def _last_run(self) -> dict[str, Any] | None:
        db_path = self.data_dir / "diagnostic.sqlite3"
        if not db_path.exists():
            return None
        try:
            with sqlite3.connect(db_path) as connection:
                row = connection.execute(
                    "SELECT id, started_at, finished_at, mock, status FROM runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return {"id": row[0], "started_at": row[1], "finished_at": row[2],
                "mode": "mock" if row[3] else "live", "status": row[4]}

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._reap()
            config = self._config()
            channels = config.get("channels", [])
            state = "running" if self._process is not None else "stopped"
            if self._process is None and self._last_exit_code not in (None, 0):
                state = "failed"
            return {
                "state": state,
                "pid": self._process.pid if self._process is not None else None,
                "started_at": self._started_at,
                "last_exit_code": self._last_exit_code,
                "total_channels": len(channels),
                "enabled_channels": sum(bool(channel.get("enabled", True)) for channel in channels),
                "test_models": config.get("test_models", list(diagnostic.DEFAULT_TEST_MODELS)),
                "requests_per_channel": (int(config["rounds"]) * 6 + int(config["juice_runs"]) * 5)
                * len(config["test_models"]),
                "last_run": self._last_run(),
            }


def create_app(controller: ProcessController, token: str) -> FastAPI:
    app = FastAPI(title="小时渠道诊断控制服务", docs_url=None, redoc_url=None)

    def authorize(value: str | None) -> None:
        if not token:
            raise HTTPException(status_code=503, detail="控制服务未配置令牌")
        if not value or not hmac.compare_digest(value, token):
            raise HTTPException(status_code=403, detail="控制令牌无效")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        try:
            return controller.status()
        except ControlError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/enable-all")
    def enable_all(x_control_token: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(x_control_token)
        try:
            controller.enable_all()
            return controller.status()
        except ControlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/start")
    def start(x_control_token: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(x_control_token)
        try:
            controller.start()
            return controller.status()
        except ControlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/run-once")
    def run_once(x_control_token: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(x_control_token)
        try:
            controller.run_once()
            return controller.status()
        except ControlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/stop")
    def stop(x_control_token: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(x_control_token)
        try:
            controller.stop()
            return controller.status()
        except ControlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8098, type=int)
    args = parser.parse_args()
    app = create_app(ProcessController(args.state_dir, args.credentials), os.environ.get("DIAGNOSTIC_CONTROL_TOKEN", ""))
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
