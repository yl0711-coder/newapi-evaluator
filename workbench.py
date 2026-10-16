from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
import importlib
import os

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from shared.access import install_access
from shared.api import router
from shared.feishu_channels import router as feishu_channels_router
from shared.config import WEB_DIR
from shared.registry import get_registry
from shared.scheduler_lock import scheduler_lock
from features.model_coverage import internal_api as monitor_internal
from features.model_coverage.api import router as model_coverage_router
from features.integrity.api import router as integrity_router, STATIC_DIR as INTEGRITY_STATIC
from features.integrity import service as integrity_service, monitor_adapter as integrity_monitor
from features.token_usage.api import router as token_usage_router
from features.token_usage.internal_api import internal_router as token_usage_internal_router
from features.token_usage import service as token_usage_service
from features.daily_checks.api import router as daily_checks_router, STATIC_DIR as DAILY_STATIC
from features.channel_monitor.api import router as channel_monitor_router

FEATURES = {
    "admission": ("准入测试", "features.admission.api"),
    "stability": ("定时稳定性测试", "features.stability.app.main"),
    "reasoning": ("非流式回答测试", "features.reasoning.api"),
    "capacity": ("中转站极限测试", "features.capacity.api"),
    "diagnosis": ("请求特征诊断", "features.diagnosis.api"),
    "image-quality": ("生图模型质量测试", "features.image_quality.api"),
    "volcengine-admission": ("火山新 Key 验收", "features.volcengine_admission.api"),
}


def create_app(mode: str | None = None):
    mode = mode or os.getenv("PLATFORM_APP", "all")
    if mode not in {"all", "channels", *FEATURES}:
        raise ValueError("未知启动模式")
    selected = list(FEATURES) if mode == "all" else ([mode] if mode in FEATURES else [])
    children = {name: importlib.import_module(FEATURES[name][1]).app for name in selected}
    include_integrity = mode in {"all", "stability"}
    include_token_usage = mode in {"all", "stability", "admission"}

    @asynccontextmanager
    async def lifespan(_app):
        get_registry()
        async with AsyncExitStack() as stack:
            if "stability" not in children:
                from features.stability.app import storage
                stack.callback(storage.close)
            if "stability" in children:
                from features.stability.app.config import DATA_DIR
                stack.enter_context(scheduler_lock(DATA_DIR))
                # The Monitor executor shares the single-scheduler lock, so only one process runs it.
                if await monitor_internal.start_executor():
                    stack.push_async_callback(monitor_internal.stop_executor)
                integrity_service.configure_monitor_resolver(integrity_monitor.resolve_monitor_target)
                if await integrity_service.start_executor():
                    stack.push_async_callback(integrity_service.stop_executor)
                await token_usage_service.start_executor()
                stack.push_async_callback(token_usage_service.stop_executor)
            if include_integrity and await integrity_monitor.start_offline_executor():
                stack.push_async_callback(integrity_monitor.stop_offline_executor)
            for child in children.values():
                await stack.enter_async_context(child.router.lifespan_context(child))
            yield

    app = FastAPI(title="模型测试工作台", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    install_access(app)
    app.include_router(router)
    app.include_router(feishu_channels_router)
    app.include_router(model_coverage_router)
    app.include_router(monitor_internal.internal_router)
    app.include_router(token_usage_internal_router)
    if include_token_usage:
        app.include_router(token_usage_router)
        app.mount("/token-usage", StaticFiles(directory=WEB_DIR.parent / "features/token_usage/web", html=True))
    if include_integrity:
        app.include_router(integrity_router)
        app.mount("/integrity", StaticFiles(directory=INTEGRITY_STATIC, html=True))
        app.include_router(daily_checks_router)
        app.mount("/daily-checks", StaticFiles(directory=DAILY_STATIC, html=True))
        app.include_router(channel_monitor_router)
        app.mount("/channel-monitor", StaticFiles(directory=WEB_DIR.parent / "features/channel_monitor/web", html=True))
    app.state.stability_available = "stability" in children

    @app.get("/api/platform")
    async def info():
        features = [{"id": name, "name": FEATURES[name][0], "url": f"/{name}/"} for name in selected]
        if "admission" in children:
            features.insert(0, {"id": "protocol-admission", "name": "模型与协议检测", "url": "/admission/protocol/"})
        if include_integrity:
            features.append({"id": "integrity", "name": "模型完整性复核", "url": "/integrity/"})
            features.append({"id": "daily-checks", "name": "已上线渠道每日检测", "url": "/daily-checks/"})
            features.append({"id": "channel-monitor", "name": "渠道自动启停", "url": "/channel-monitor/"})
        if include_token_usage:
            features.append({"id": "token-usage", "name": "Token 用量监控", "url": "/token-usage/"})
        return {"mode": mode, "features": features}

    @app.get("/api/health")
    async def health():
        state = {"status": "ok", "channels": len(get_registry().list()), "features": selected}
        state["integrity_executor"] = integrity_service.executor_status()
        state["evidence_executor"] = integrity_monitor.offline_executor_status()
        if include_token_usage:
            state["token_usage_executor"] = token_usage_service.executor_status()
        if "stability" in children:
            from features.stability.app import scheduler, storage, integrity
            state["scheduler"] = scheduler.status()
            state["scheduled_integrity_executor"] = integrity.executor_status()
            state["monitor_executor"] = monitor_internal.executor_status()
            from features.daily_checks.service import get_service as daily_service
            state["daily_checks_executor"] = daily_service().status()
            from features.channel_monitor.service import get_service as channel_monitor_service
            state["channel_monitor_executor"] = channel_monitor_service().status()
            if not storage.health() or not state["scheduler"]["running"]:
                state["status"] = "degraded"
        return state

    @app.get("/")
    async def home():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/channels/")
    async def channels_page():
        return FileResponse(WEB_DIR / "channels.html")

    app.mount("/assets", StaticFiles(directory=WEB_DIR))
    for name, child in children.items():
        child.add_exception_handler(RequestValidationError, app.exception_handlers[RequestValidationError])
        app.mount(f"/{name}", child)
    return app
