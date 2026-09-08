from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .mapping import FEISHU_FIELD_MAPPINGS
from .security import normalize_channel_url, validate_safe_summary
from .storage import FlowError, Store


ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = Path(
    os.environ.get("ADMISSION_FEISHU_FRAMEWORK_DATA_DIR", ROOT / "data")
).expanduser().resolve()


class StartRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    channel_name: str = Field(min_length=1, max_length=160)
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: SecretStr = Field(min_length=1, max_length=4096, repr=False)
    model: str = Field(min_length=1, max_length=160)
    protocol: Literal["openai", "anthropic"]


class TestResultInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["completed", "failed"]
    summary: dict[str, Any] = Field(default_factory=dict)


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["qualified", "not_qualified"]
    reviewer: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)


def create_app(repository: Store | None = None) -> FastAPI:
    store = repository or Store(DEFAULT_DATA_DIR / "framework.db")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        store.init()
        try:
            yield
        finally:
            store.close()

    application = FastAPI(
        title="准入人工确认与飞书记录框架",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.store = store

    @application.exception_handler(FlowError)
    async def flow_error(_request, exc: FlowError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request, exc: RequestValidationError):
        # FastAPI's default response includes the rejected input value.  That
        # is unsafe for credential fields, so report only location and reason.
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

    @application.get("/api/meta")
    async def meta():
        return {
            "framework": True,
            "feishu_delivery_enabled": False,
            "feishu_field_mapping_count": len(FEISHU_FIELD_MAPPINGS),
            "states": ["testing", "awaiting_review", "reviewed"],
        }

    @application.get("/api/health")
    async def health():
        store.init()
        return {"status": "ok", "feishu_delivery": "not_configured"}

    @application.post("/api/runs", status_code=201)
    async def start_run(body: StartRunInput):
        try:
            safe_url = normalize_channel_url(body.base_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # The credential is deliberately consumed only as a presence signal in
        # this framework.  The future evaluator adapter will keep it in memory.
        supplied = bool(body.api_key.get_secret_value())
        return store.create_run({
            "channel_name": body.channel_name,
            "base_url": safe_url,
            "model": body.model,
            "protocol": body.protocol,
            "credential_supplied": supplied,
        })

    @application.get("/api/runs")
    async def list_runs():
        return {"runs": store.list_runs()}

    @application.get("/api/runs/{run_id}")
    async def get_run(run_id: int):
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="准入测试记录不存在")
        return run

    @application.post("/api/runs/{run_id}/test-result")
    async def finish_test(run_id: int, body: TestResultInput):
        try:
            validate_safe_summary(body.summary)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return store.finish_test(run_id, body.outcome, body.summary)

    @application.post("/api/runs/{run_id}/review")
    async def review(run_id: int, body: ReviewInput):
        return store.review(run_id, body.decision, body.reviewer, body.note)

    @application.get("/api/outbox")
    async def outbox():
        return {
            "delivery_enabled": False,
            "field_mapping_count": len(FEISHU_FIELD_MAPPINGS),
            "jobs": store.list_outbox(),
        }

    application.mount(
        "/", StaticFiles(directory=ROOT / "web", html=True), name="prototype-web"
    )
    return application


app = create_app()
