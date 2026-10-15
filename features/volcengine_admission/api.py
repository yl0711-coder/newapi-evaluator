"""Parent-authenticated workbench surface; no arbitrary upstream or filesystem path."""
from contextlib import asynccontextmanager
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from shared.config import DATA_DIR
from . import catalog
from .models import AuthInput, CleanupInput, PreviewInput, ResumeInput, RunInput
from .service import AdmissionService
from .storage import Conflict

WEB_DIR = Path(__file__).parent / "web"


def create_app(*, data_dir=None, transport=None, poll_interval=5, poll_window=3600):
    @asynccontextmanager
    async def lifespan(application):
        yield
        if application.state.service is not None:
            await application.state.service.close()

    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    application.state.service = None

    def service():
        if application.state.service is None:
            application.state.service = AdmissionService(data_dir or DATA_DIR / "volcengine-admission", transport=transport,
                                                         poll_interval=poll_interval, poll_window=poll_window)
        return application.state.service

    application.state.get_service = service

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        # FastAPI's default error body may contain a rejected credential or prompt input.
        return JSONResponse(status_code=422, content={"detail": "输入字段格式无效，请核对请求参数"})

    @application.exception_handler(Conflict)
    async def conflict_error(_request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    def error(exc):
        if isinstance(exc, KeyError):
            return HTTPException(404, "验收记录或本地样片不存在")
        return HTTPException(409 if isinstance(exc, Conflict) else 422, str(exc))

    @application.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    @application.get("/api/meta")
    async def meta():
        return catalog.meta()

    @application.get("/api/capacity")
    async def capacity():
        return service().capacity()

    @application.post("/api/preview")
    async def preview(body: PreviewInput):
        try:
            return service().preview(body)
        except ValueError as exc:
            raise error(exc) from None

    @application.post("/api/auth")
    async def auth(body: AuthInput):
        try:
            return await service().auth(body)
        except ValueError as exc:
            raise error(exc) from None

    @application.post("/api/runs")
    async def create(body: RunInput):
        try:
            return await service().create(body)
        except ValueError as exc:
            raise error(exc) from None

    @application.get("/api/runs")
    async def runs():
        return {"runs": service().runs()}

    @application.get("/api/runs/{run_id}")
    async def detail(run_id: str):
        try:
            return service().detail(run_id)
        except KeyError as exc:
            raise error(exc) from None

    @application.post("/api/runs/{run_id}/stop")
    async def stop(run_id: str):
        try:
            return await service().stop(run_id)
        except KeyError as exc:
            raise error(exc) from None

    @application.post("/api/runs/{run_id}/resume")
    async def resume(run_id: str, body: ResumeInput):
        try:
            return await service().resume(run_id, body)
        except (ValueError, KeyError) as exc:
            raise error(exc) from None

    @application.get("/api/runs/{run_id}/report")
    async def report(run_id: str):
        try:
            body = json.dumps(service().detail(run_id), ensure_ascii=False, indent=2, allow_nan=False)
            return Response(body, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="volcengine-{run_id}.json"',
                                                                          "Cache-Control": "private, no-store"})
        except KeyError as exc:
            raise error(exc) from None

    @application.post("/api/runs/{run_id}/cleanup")
    async def cleanup(run_id: str, body: CleanupInput):
        try:
            return await service().cleanup(run_id, body)
        except (ValueError, KeyError) as exc:
            raise error(exc) from None

    @application.get("/api/runs/{run_id}/media/{media_id}")
    async def media(run_id: str, media_id: str, download: bool = False):
        try:
            path, content_type = service().store.media(run_id, media_id)
            return FileResponse(path, media_type=content_type, filename=path.name if download else None,
                                headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
        except KeyError as exc:
            raise error(exc) from None

    application.mount("/assets", StaticFiles(directory=WEB_DIR, check_dir=False))
    return application


app = create_app()


def get_service():
    return app.state.get_service()
