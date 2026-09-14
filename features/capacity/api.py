from __future__ import annotations

import asyncio
import copy
import json
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from relay_lab.config import DATA_ROOT
from relay_lab.console import Console


STATIC = Path(__file__).resolve().parents[2] / "relay_lab" / "web"
DOWNLOADS = {"report.md", "summary.json", "results.jsonl", "fader-events.json"}
JOB_ID = re.compile(r"[a-f0-9]{32}")


def _console(app: FastAPI) -> Console:
    value = getattr(app.state, "console", None)
    if value is None:
        value = Console(DATA_ROOT / "console")
        app.state.console = value
    return value


def _job(console: Console, identifier: str):
    if not JOB_ID.fullmatch(identifier) or identifier not in console.jobs:
        raise HTTPException(404, "未找到测试任务")
    return console.jobs[identifier]


def _session(request: Request, console: Console) -> None:
    if not secrets.compare_digest(request.headers.get("X-Relay-UI", ""), console.token):
        raise HTTPException(403, "页面会话已失效，请刷新页面")


async def _body(request: Request) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(400, "请求格式无效")
    raw = await request.body()
    if not 0 < len(raw) <= 65_536:
        raise HTTPException(400, "请求格式无效")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "请求格式无效") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "请求格式无效")
    return value


def _safe_error(error: ValueError) -> str:
    message = str(error)
    prefixes = ("请", "已有", "并发", "每阶", "单请求", "号池", "Mock 账号", "长任务")
    return message if message.startswith(prefixes) else "接口地址或参数无效，请检查输入"


@asynccontextmanager
async def lifespan(app: FastAPI):
    console = _console(app)
    try:
        yield
    finally:
        for job in list(console.jobs.values()):
            console.cancel(job.id)
        threads = [job.thread for job in console.jobs.values() if job.thread and job.thread.is_alive()]
        for thread in threads:
            await asyncio.to_thread(thread.join, 30)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    )
    return response


@app.get("/")
async def page():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
async def state(request: Request):
    console = _console(request.app)
    with console.lock:
        jobs = [job.public(False) for job in console.jobs.values()]
    return {
        "service": "relay-lab-console",
        "ui_schema_version": 4,
        "csrf": console.token,
        "revision": console.server_revision,
        "jobs": sorted(jobs, key=lambda job: job["started_at"], reverse=True),
    }


@app.get("/api/jobs/{identifier}")
async def job_detail(identifier: str, request: Request):
    return _job(_console(request.app), identifier).public()


@app.get("/api/jobs/{identifier}/{filename}")
async def download(identifier: str, filename: str, request: Request):
    job = _job(_console(request.app), identifier)
    if filename not in DOWNLOADS:
        raise HTTPException(404, "未找到测试文件")
    path = job.output / filename
    if not path.is_file():
        raise HTTPException(404, "未找到测试文件")
    media_type = "application/json" if filename.endswith((".json", ".jsonl")) else "text/markdown"
    return FileResponse(path, media_type=media_type, filename=filename)


@app.post("/api/jobs", status_code=202)
async def create_job(request: Request):
    console = _console(request.app)
    _session(request, console)
    body = await _body(request)
    try:
        return console.create(copy.deepcopy(body)).public()
    except ValueError as error:
        raise HTTPException(400, _safe_error(error)) from error


@app.post("/api/jobs/{identifier}/faders")
async def adjust_faders(identifier: str, request: Request):
    console = _console(request.app)
    _session(request, console)
    job = _job(console, identifier)
    try:
        return console.adjust_faders(job.id, await _body(request)).public()
    except ValueError as error:
        raise HTTPException(400, _safe_error(error)) from error


@app.post("/api/jobs/{identifier}/stop")
async def stop_job(identifier: str, request: Request):
    console = _console(request.app)
    _session(request, console)
    await _body(request)
    return console.cancel(_job(console, identifier).id).public()


@app.exception_handler(HTTPException)
async def http_error(_request: Request, error: HTTPException):
    return JSONResponse({"error": error.detail}, status_code=error.status_code)


app.mount("/assets", StaticFiles(directory=STATIC))
