import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from shared.config import DATA_DIR
from shared.registry import get_registry
from .models import CaseImport, PlanInput, StartInput
from .report import markdown, report
from .service import Manager
from .storage import Store

WEB = Path(__file__).parent / "web"


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        data = bytearray()

        async def read_body():
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return False
                data.extend(message.get("body", b""))
                if len(data) > 262_144:
                    raise OverflowError
                if not message.get("more_body"):
                    return True

        try:
            if not await asyncio.wait_for(read_body(), 10):
                return
        except OverflowError:
            return await JSONResponse({"detail": "导入内容超过 256 KiB"}, status_code=413)(scope, receive, send)
        except asyncio.TimeoutError:
            return await JSONResponse({"detail": "请求体接收超时"}, status_code=408)(scope, receive, send)
        delivered = False

        async def buffered():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(data), "more_body": False}
            return await receive()

        await self.app(scope, buffered, send)


async def parse(request, model):
    try:
        return model.model_validate_json(await request.body())
    except ValidationError:
        raise HTTPException(422, "字段格式或范围不符合要求；仅接受页面列出的指标字段") from None


def create_app(directory=None, registry=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.manager = Manager(Store(directory or DATA_DIR / "diagnosis"), registry or get_registry())
        async with app.state.manager.lifespan():
            yield

    app = FastAPI(title="请求特征诊断", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(BodyLimit)

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        # Only local, fixed contract errors escape the manager. Provider errors never reach this handler.
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(_, exc):
        return JSONResponse({"detail": "记录不存在"}, status_code=404)

    @app.get("/")
    async def home():
        return FileResponse(WEB / "index.html")

    @app.get("/api/config")
    async def config(request: Request):
        manager = request.app.state.manager
        return {"channels": [
            {"id": c["id"], "alias": f"渠道 #{c['id']}", "version": c["version"], "enabled": c["enabled"]}
            for c in manager.registry.list()]}

    @app.get("/api/cases")
    async def cases(request: Request):
        return request.app.state.manager.store.cases()

    @app.post("/api/cases")
    async def import_cases(request: Request):
        body = await parse(request, CaseImport)
        return request.app.state.manager.store.import_cases(body.cases)

    @app.delete("/api/cases/{identifier}")
    async def delete_case(identifier: str, request: Request):
        request.app.state.manager.store.delete_case(identifier)
        return {"deleted": True}

    @app.post("/api/preview")
    async def preview(request: Request):
        return request.app.state.manager.preview(await parse(request, PlanInput))

    @app.post("/api/runs")
    async def start(request: Request):
        return await request.app.state.manager.start(await parse(request, StartInput))

    @app.get("/api/runs")
    async def runs(request: Request):
        return request.app.state.manager.store.runs()

    @app.get("/api/runs/{identifier}")
    async def run(identifier: str, request: Request):
        return report(request.app.state.manager.store.run(identifier))

    @app.post("/api/runs/{identifier}/stop")
    async def stop(identifier: str, request: Request):
        return await request.app.state.manager.stop(identifier)

    @app.delete("/api/runs/{identifier}")
    async def delete_run(identifier: str, request: Request):
        manager = request.app.state.manager
        if manager.store.run(identifier)["state"] == "running":
            raise HTTPException(409, "运行中不能删除")
        manager.store.delete_run(identifier)
        return {"deleted": True}

    @app.get("/api/runs/{identifier}/export/{format}")
    async def export(identifier: str, format: str, request: Request):
        value = report(request.app.state.manager.store.run(identifier))
        if format not in ("json", "md"):
            raise HTTPException(404, "仅支持 JSON 或 Markdown")
        headers = {"Content-Disposition": f'attachment; filename="diagnosis-{value["run"]["id"]}.{format}"'}
        return JSONResponse(value, headers=headers) if format == "json" else PlainTextResponse(markdown(value), headers=headers)

    app.mount("/static", StaticFiles(directory=WEB))
    return app


app = create_app()
