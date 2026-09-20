import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from shared.config import DATA_DIR
from shared.registry import get_registry
from .catalog import PROTOCOLS, CHECKS
from .models import PlanInput, StartInput, DiscoveryInput
from .report import ERRORS, html_report
from .service import Manager
from .storage import Store

WEB = Path(__file__).parent / "web"


async def parse(request, model):
    body = bytearray()
    async def receive():
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65_536:
                raise HTTPException(413, "请求内容超过 64 KiB")
    try:
        await asyncio.wait_for(receive(), 10)
        return model.model_validate_json(body)
    except asyncio.TimeoutError:
        raise HTTPException(408, "请求内容接收超时") from None
    except (ValidationError, ValueError):
        raise HTTPException(422, "字段格式或范围不符合要求，请检查渠道、模型和请求参数") from None


def create_app(directory=None, registry=None, transport_factory=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.manager = Manager(Store(directory or DATA_DIR / "protocol-admission"), registry or get_registry(), transport_factory)
        async with app.state.manager.lifespan():
            yield

    app = FastAPI(title="模型与协议检测", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(_, exc):
        return JSONResponse({"detail": "记录不存在"}, status_code=404)

    @app.get("/")
    async def home():
        return FileResponse(WEB / "index.html")

    @app.get("/api/meta")
    async def meta():
        return {"protocols": PROTOCOLS, "errors": ERRORS, "checks_per_model": len(CHECKS), "max_models": 5}

    @app.get("/api/models")
    async def cached_models(channel_id: int, request: Request):
        return request.app.state.manager.cached_models(channel_id)

    @app.post("/api/models")
    async def discover(request: Request):
        return await request.app.state.manager.discover(await parse(request, DiscoveryInput))

    @app.post("/api/preview")
    async def preview(request: Request):
        return request.app.state.manager.preview(await parse(request, PlanInput))

    @app.post("/api/runs")
    async def start(request: Request):
        return await request.app.state.manager.start(await parse(request, StartInput))

    @app.get("/api/runs")
    async def history(request: Request):
        return {"runs": [{"id": r["id"], "created_at": r["created_at"], "state": r["state"], "mode": r["config"]["mode"]} for r in request.app.state.manager.store.list()]}

    @app.get("/api/runs/{identifier}")
    async def run(identifier: str, request: Request):
        return request.app.state.manager.report(identifier)

    @app.post("/api/runs/{identifier}/stop")
    async def stop(identifier: str, request: Request):
        return await request.app.state.manager.stop(identifier)

    @app.get("/api/runs/{identifier}/export/{format}")
    async def export(identifier: str, format: str, request: Request):
        if format not in {"json", "html"}:
            raise HTTPException(404, "仅支持 JSON 和 HTML")
        report = request.app.state.manager.report(identifier)
        headers = {"Content-Disposition": f'attachment; filename="protocol-{report["id"]}.{format}"'}
        return JSONResponse(report, headers=headers) if format == "json" else HTMLResponse(html_report(report), headers=headers)

    app.mount("/static", StaticFiles(directory=WEB))
    return app


app = create_app()
