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
from shared.config import WEB_DIR
from shared.registry import get_registry
from shared.scheduler_lock import scheduler_lock

FEATURES = {
    "admission": ("准入测试", "features.admission.api"),
    "stability": ("定时稳定性测试", "features.stability.app.main"),
    "reasoning": ("非流式回答测试", "features.reasoning.api"),
}


def create_app(mode: str | None = None):
    mode = mode or os.getenv("PLATFORM_APP", "all")
    if mode not in {"all", "channels", *FEATURES}:
        raise ValueError("未知启动模式")
    selected = list(FEATURES) if mode == "all" else ([mode] if mode in FEATURES else [])
    children = {name: importlib.import_module(FEATURES[name][1]).app for name in selected}

    @asynccontextmanager
    async def lifespan(_app):
        get_registry()
        async with AsyncExitStack() as stack:
            if "stability" in children:
                from features.stability.app.config import DATA_DIR
                stack.enter_context(scheduler_lock(DATA_DIR))
            for child in children.values():
                await stack.enter_async_context(child.router.lifespan_context(child))
            yield

    app = FastAPI(title="模型测试工作台", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    install_access(app)
    app.include_router(router)

    @app.get("/api/platform")
    async def info():
        return {"mode": mode, "features": [{"id": name, "name": FEATURES[name][0], "url": f"/{name}/"}
                                           for name in selected]}

    @app.get("/api/health")
    async def health():
        state = {"status": "ok", "channels": len(get_registry().list()), "features": selected}
        if "stability" in children:
            from features.stability.app import scheduler, storage
            state["scheduler"] = scheduler.status()
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
