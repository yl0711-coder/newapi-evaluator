from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .feishu import BitableWriter, FeishuError, FeishuSettings
from .groups import group_for_model, model_options
from .mapping import build_feishu_fields
from .storage import Store, StoreConflict, StoreError


ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = Path(
    os.environ.get("ADMISSION_FEISHU_FRAMEWORK_DATA_DIR", ROOT / "data")
).expanduser().resolve()


class RecordWriter(Protocol):
    async def create_record(self, fields: dict[str, str]) -> str: ...


class ParticipationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    channel: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=160)


def create_app(
    repository: Store | None = None,
    feishu_settings: FeishuSettings | None = None,
    record_writer: RecordWriter | None = None,
) -> FastAPI:
    store = repository or Store(DEFAULT_DATA_DIR / "framework.db")
    settings = feishu_settings or FeishuSettings.from_env()
    writer = record_writer
    if writer is None and settings.configured:
        writer = BitableWriter(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        store.init()
        try:
            yield
        finally:
            store.close()

    application = FastAPI(
        title="准入渠道与测试分组飞书记录",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.store = store

    @application.exception_handler(StoreError)
    async def store_error(_request, exc: StoreError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @application.exception_handler(StoreConflict)
    async def store_conflict(_request, exc: StoreConflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request, exc: RequestValidationError):
        errors = [
            {
                "type": item.get("type", "validation_error"),
                "loc": item.get("loc", ()),
                "msg": item.get("msg", "请求参数不合法"),
            }
            for item in exc.errors()
        ]
        return JSONResponse(
            {"detail": "请求参数校验失败", "errors": errors}, status_code=422
        )

    async def deliver(participation_id: int) -> dict:
        current = store.get(participation_id)
        if current is None:
            raise StoreError("准入参与记录不存在")
        if current["sync_status"] == "synced":
            return current
        if writer is None:
            raise HTTPException(status_code=409, detail="飞书多维表格尚未配置完整")
        sending = store.begin_delivery(participation_id)
        if sending["sync_status"] == "synced":
            return sending
        try:
            record_id = await writer.create_record(sending["fields"])
        except (FeishuError, ValueError) as exc:
            return store.mark_failed(participation_id, str(exc))
        return store.mark_synced(participation_id, record_id)

    @application.get("/api/meta")
    async def meta():
        return {
            "models": model_options(),
            "feishu": {
                "configured": settings.configured,
                "configuration_status": settings.configuration_status,
                "written_fields": [settings.channel_field, settings.group_field],
            },
        }

    @application.get("/api/health")
    async def health():
        store.init()
        return {
            "status": "ok",
            "feishu_configuration": settings.configuration_status,
        }

    @application.post("/api/participations", status_code=201)
    async def create_participation(body: ParticipationInput):
        try:
            test_group = group_for_model(body.model)
            fields = build_feishu_fields(
                body.channel,
                test_group,
                channel_field=settings.channel_field,
                group_field=settings.group_field,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        created = store.create(
            body.channel,
            test_group,
            fields,
            delivery_configured=settings.configured,
        )
        if writer is None:
            return created
        return await deliver(created["id"])

    @application.get("/api/participations")
    async def participations():
        return {"participations": store.list()}

    @application.post("/api/participations/{participation_id}/retry")
    async def retry(participation_id: int):
        return await deliver(participation_id)

    application.mount(
        "/", StaticFiles(directory=ROOT / "web", html=True), name="prototype-web"
    )
    return application


app = create_app()
