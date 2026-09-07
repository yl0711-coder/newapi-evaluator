from __future__ import annotations

import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from shared.registry import RegistryError, get_registry

from . import scheduler, storage, transport
from .config import TIMEZONE, WEB_DIR
from .egress import EgressDenied, validate_url
from .security import basic_auth_middleware


class ChannelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=80)
    registry_channel_id: int = Field(ge=1)
    model: str = Field(min_length=1, max_length=160)
    protocol: Literal["openai", "anthropic"] = "openai"
    enabled: bool = True


class ScheduleInput(BaseModel):
    id: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=80)
    daily_times: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default=TIMEZONE, min_length=1, max_length=80)
    channel_ids: list[int] = Field(min_length=1, max_length=100)
    rounds: int = Field(default=3, ge=1, le=10)
    round_interval_seconds: int = Field(default=15, ge=0, le=3600)
    notification_delay_seconds: int = Field(default=0, ge=0, le=86400)
    max_concurrency: int = Field(default=3, ge=1, le=10)
    min_success_rate: float = Field(default=0.95, ge=0, le=1)
    max_timeout_rate: float = Field(default=0.05, ge=0, le=1)
    max_stream_break_rate: float = Field(default=0, ge=0, le=1)
    max_p95_ms: int = Field(default=30000, ge=100, le=600000)
    speed_threshold_mode: Literal["fixed", "adaptive", "off"] = "fixed"
    speed_baseline_min_runs: int = Field(default=5, ge=3, le=30)
    speed_slow_ratio: float = Field(default=1.5, ge=1.1, le=5)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_schedule(self) -> "ScheduleInput":
        scheduler.parse_daily_times(self.daily_times)
        scheduler.timezone(self.timezone)
        self.channel_ids = list(dict.fromkeys(self.channel_ids))
        return self


class FeishuInput(BaseModel):
    webhook: str = Field(default="", max_length=1000)


class ReportGroupInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    family: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=40)
    registry_channel_ids: list[int] = Field(default_factory=list, max_length=100)
    always_normal: bool = False

    @model_validator(mode="after")
    def validate_group(self) -> "ReportGroupInput":
        self.registry_channel_ids = list(dict.fromkeys(self.registry_channel_ids))
        if not self.always_normal and not self.registry_channel_ids:
            raise ValueError("非免测分组至少需要一个公共渠道")
        return self


class ReportGroupsInput(BaseModel):
    groups: list[ReportGroupInput] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_unique_groups(self) -> "ReportGroupsInput":
        names = [(group.family, group.label) for group in self.groups]
        if len(names) != len(set(names)):
            raise ValueError("报告分组不能重名")
        return self


@asynccontextmanager
async def lifespan(_app: FastAPI):
    storage.init()
    await scheduler.start()
    yield
    await scheduler.stop()
    storage.close()


app = FastAPI(title="定时稳定性测试", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/api/health")
async def health() -> JSONResponse:
    scheduler_status = scheduler.status()
    database_ok = storage.health()
    state = "ok" if database_ok and scheduler_status["running"] else "degraded"
    return JSONResponse({"status": state, "database": database_ok, "scheduler": scheduler_status})


@app.get("/api/meta")
async def meta() -> JSONResponse:
    return JSONResponse({
        "test_pack": {"version": transport.INSPECT_VERSION, "requests_per_round": len(transport.PROBES)},
        "default_timezone": TIMEZONE,
    })


@app.get("/api/channels")
async def channels() -> JSONResponse:
    return JSONResponse({"channels": storage.list_channels()})


@app.get("/api/channel-inventory")
async def channel_inventory() -> JSONResponse:
    return JSONResponse({"inventory": storage.list_inventory()})


@app.post("/api/channels")
async def save_channel(body: ChannelInput) -> JSONResponse:
    try:
        get_registry().resolve(body.registry_channel_id)
        data = body.model_dump()
        channel_id = storage.upsert_channel(data)
    except (EgressDenied, RegistryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="渠道名称已经存在") from exc
    return JSONResponse({"id": channel_id})


@app.delete("/api/channels/{channel_id}")
async def remove_channel(channel_id: int) -> JSONResponse:
    if not storage.delete_channel(channel_id):
        raise HTTPException(status_code=404, detail="渠道不存在")
    return JSONResponse({"deleted": True})


@app.get("/api/schedules")
async def schedules() -> JSONResponse:
    return JSONResponse({"schedules": storage.list_schedules(), "scheduler": scheduler.status()})


@app.post("/api/schedules")
async def save_schedule(body: ScheduleInput) -> JSONResponse:
    existing_ids = {item["id"] for item in storage.list_channels(ids=body.channel_ids)}
    if existing_ids != set(body.channel_ids):
        raise HTTPException(status_code=400, detail="计划包含不存在的渠道")
    data = body.model_dump()
    data["daily_times"] = ",".join(scheduler.parse_daily_times(body.daily_times))
    try:
        schedule_id = storage.upsert_schedule(data)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="计划名称已经存在") from exc
    return JSONResponse({"id": schedule_id})


@app.delete("/api/schedules/{schedule_id}")
async def remove_schedule(schedule_id: int) -> JSONResponse:
    if not storage.delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="计划不存在")
    return JSONResponse({"deleted": True})


@app.post("/api/schedules/{schedule_id}/run", status_code=202)
async def run_schedule(schedule_id: int) -> JSONResponse:
    schedule = storage.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="计划不存在")
    if storage.has_active_run(schedule_id):
        raise HTTPException(status_code=409, detail="该计划已有等待或运行中的任务")
    run_id = storage.create_run(schedule, time.time(), source="manual")
    if run_id is None:
        raise HTTPException(status_code=409, detail="该计划刚刚已经触发")
    await scheduler.tick()
    return JSONResponse({"run_id": run_id, "status": "pending"}, status_code=202)


@app.get("/api/runs")
async def runs(limit: int = Query(default=50, ge=1, le=200)) -> JSONResponse:
    return JSONResponse({"runs": storage.list_runs(limit)})


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: int) -> JSONResponse:
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return JSONResponse(run)


@app.get("/api/settings/feishu")
async def feishu_status() -> JSONResponse:
    return JSONResponse({"configured": bool(storage.get_setting(scheduler.WEBHOOK_KEY))})


@app.put("/api/settings/feishu")
async def save_feishu(body: FeishuInput) -> JSONResponse:
    webhook = body.webhook.strip()
    if webhook:
        parsed = urlsplit(webhook)
        if parsed.scheme != "https" or parsed.hostname != "open.feishu.cn" or not parsed.path.startswith("/open-apis/bot/"):
            raise HTTPException(status_code=400, detail="请输入飞书群机器人的 HTTPS Webhook")
    storage.set_setting(scheduler.WEBHOOK_KEY, webhook)
    return JSONResponse({"configured": bool(webhook)})


@app.get("/api/settings/report-groups")
async def report_groups() -> JSONResponse:
    return JSONResponse({"groups": storage.list_report_groups()})


@app.put("/api/settings/report-groups")
async def save_report_groups(body: ReportGroupsInput) -> JSONResponse:
    existing_ids = {item["id"] for item in storage.list_inventory()}
    requested_ids = {
        channel_id
        for group in body.groups
        for channel_id in group.registry_channel_ids
    }
    missing = sorted(requested_ids - existing_ids)
    if missing:
        raise HTTPException(status_code=400, detail=f"报告分组包含不存在的公共渠道：{missing}")
    storage.replace_report_groups([group.model_dump() for group in body.groups])
    return JSONResponse({"groups": storage.list_report_groups()})


@app.post("/api/settings/feishu/test")
async def test_feishu() -> JSONResponse:
    webhook = storage.get_setting(scheduler.WEBHOOK_KEY)
    if not webhook:
        raise HTTPException(status_code=400, detail="尚未配置飞书 Webhook")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = await client.post(webhook, json={
                "msg_type": "text", "content": {"text": "定时稳定性测试：通知通道已就绪。"}
            })
            response.raise_for_status()
            data = response.json()
            if data.get("code", data.get("StatusCode", 0)) != 0:
                raise RuntimeError("飞书返回失败状态")
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail="飞书通知测试失败") from exc
    return JSONResponse({"sent": True})


@app.get("/", include_in_schema=False)
async def home() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
