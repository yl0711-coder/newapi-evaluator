import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from shared.api import Selection
from shared.registry import Conflict, RegistryError, get_registry
from . import feishu, main as engine, reporting, storage
from .jobs import AdmissionManager
from features.token_usage.transport import run_paired_check
from features.protocol_admission.api import app as protocol_app


_feishu_tasks: set[asyncio.Task[None]] = set()


class CandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: SecretStr
    model: str = Field(min_length=1, max_length=160)
    protocol: engine.AdmissionProtocol

    @model_validator(mode="after")
    def enforce_model_protocol(self):
        self.protocol = engine.required_protocol(self.model) or self.protocol
        return self


class AdmissionSelection(Selection):
    protocol: engine.AdmissionProtocol

    @model_validator(mode="after")
    def enforce_model_protocol(self):
        self.protocol = engine.required_protocol(self.model) or self.protocol
        return self


class CompareInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate: CandidateInput
    reference: AdmissionSelection
    rounds: int = Field(default=2, ge=1, le=5)


class ReportEndpointInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    base_url: str = Field(min_length=1, max_length=1000)
    model: str = Field(min_length=1, max_length=160)
    protocol: engine.AdmissionProtocol
    name: str | None = Field(default=None, max_length=160)
    channel_id: int | None = Field(default=None, ge=1)
    multiplier: float | None = None


class ReportQuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    difficulty: str = Field(min_length=1, max_length=40)
    prompt: str = Field(max_length=20_000)
    variant_id: str = Field(default="original", max_length=80)


class ReportResponseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    round: int = Field(ge=1, le=5)
    question_id: str = Field(min_length=1, max_length=80)
    side: Literal["candidate", "reference"]
    content: str = Field(default="", max_length=200_000)
    reasoning: str = Field(default="", max_length=200_000)
    pair_id: str = Field(default="", max_length=160)
    round_role: Literal["warmup", "evaluated"] = "evaluated"
    variant_id: str = Field(default="original", max_length=80)
    prompt: str = Field(default="", max_length=20_000)


class AdmissionReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1, 2]
    created_at: str = Field(min_length=1, max_length=80)
    status: Literal["completed", "canceled", "failed"]
    rounds: int = Field(ge=1, le=5)
    run_id: str = Field(default="", max_length=80)
    warmup_rounds: int = Field(default=0, ge=0, le=1)
    candidate: ReportEndpointInput
    reference: ReportEndpointInput
    questions: list[ReportQuestionInput] = Field(min_length=1, max_length=6)
    measurements: list[dict[str, Any]] = Field(default_factory=list, max_length=60)
    responses: list[ReportResponseInput] = Field(default_factory=list, max_length=60)
    summary: dict[str, Any] = Field(default_factory=dict)
    pairs: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    token_usage: dict[str, Any] | None = None


def _contains_secret_field(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in {"api_key", "apikey", "authorization", "password", "secret", "token"}:
                return True
            if _contains_secret_field(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_field(item) for item in value)
    return False


async def _deliver_feishu_record(entry_id: int) -> None:
    writer = app.state.feishu_writer
    if writer is None:
        return
    try:
        entry = storage.begin_feishu_delivery(entry_id)
        if entry["status"] == "synced":
            return
        record_id = await writer.create_record(entry["fields"])
        storage.finish_feishu_delivery(entry_id, record_id)
    except (feishu.FeishuError, ValueError) as exc:
        storage.fail_feishu_delivery(entry_id, str(exc))
    except (KeyError, RuntimeError):
        return
    except Exception as exc:
        storage.fail_feishu_delivery(entry_id, f"飞书写入内部错误：{type(exc).__name__}")


def _schedule_feishu_record(entry_id: int) -> None:
    if app.state.feishu_writer is None:
        return
    task = asyncio.create_task(_deliver_feishu_record(entry_id))
    _feishu_tasks.add(task)
    task.add_done_callback(_finish_feishu_task)


def _finish_feishu_task(task: asyncio.Task[None]) -> None:
    _feishu_tasks.discard(task)
    try:
        task.result()
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_app):
    storage.init()
    storage.recover_feishu_deliveries()
    settings = feishu.FeishuSettings.from_env()
    app.state.feishu_settings = settings
    try:
        app.state.feishu_writer = feishu.BitableWriter(settings) if settings.configured else None
    except ValueError:
        app.state.feishu_writer = None
    for entry in storage.list_unsynced_feishu_records():
        _schedule_feishu_record(entry["id"])
    manager = await _manager()
    try:
        async with protocol_app.router.lifespan_context(protocol_app):
            yield
    finally:
        await manager.stop()
        app.state.admission_manager = None
        if _feishu_tasks:
            await asyncio.gather(*tuple(_feishu_tasks), return_exceptions=True)
        app.state.feishu_writer = None
        storage.close()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.state.upstream_transport = None
app.state.feishu_settings = feishu.FeishuSettings()
app.state.feishu_writer = None
app.state.admission_manager = None
app.state.token_usage_check = run_paired_check
app.get("/api/meta")(engine.meta)


@app.exception_handler(RequestValidationError)
async def validation_error(_request, _exc):
    return JSONResponse({"detail": "请求字段格式无效"}, status_code=422)


class ExtractInput(BaseModel):
    text: str = Field(min_length=1, max_length=100_000, repr=False)


@app.post("/api/extract-channel")
async def extract_channel(body: ExtractInput):
    try:
        return engine.extract_channel_credentials(body.text)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(400, "无法提取候选端信息，请检查文本或手动填写") from exc


def _submission_record(base_url: str, model: str) -> None:
    test_group = feishu.group_for_model(model)
    try:
        fields = feishu.build_fields(base_url, test_group, app.state.feishu_settings)
    except ValueError:
        fields = {feishu.DEFAULT_CHANNEL_FIELD: feishu.channel_group_value(base_url),
                  feishu.DEFAULT_GROUP_FIELD: test_group}
    entry = storage.enqueue_feishu_record(base_url, test_group, fields,
                                          delivery_configured=app.state.feishu_writer is not None)
    _schedule_feishu_record(entry["id"])


async def _manager() -> AdmissionManager:
    manager = app.state.admission_manager
    if manager is not None and (manager.store.path != storage.DB_PATH or manager.registry is not get_registry()
                                or not manager.running):
        await manager.stop()
        manager = None
    if manager is None:
        manager = AdmissionManager(storage.DB_PATH, get_registry(), transport=app.state.upstream_transport,
                                   token_check=app.state.token_usage_check, on_submit=_submission_record)
        await manager.start()
        app.state.admission_manager = manager
    manager.transport = app.state.upstream_transport
    manager.token_check = app.state.token_usage_check
    return manager


def _job_error(exc):
    if isinstance(exc, KeyError):
        return HTTPException(404, "准入任务不存在")
    if isinstance(exc, Conflict):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


async def _enqueue(body: CompareInput, request: Request):
    try:
        manager = await _manager()
        job = await manager.enqueue(body, idempotency_key=request.headers.get("Idempotency-Key"))
        return manager, job
    except (RegistryError, KeyError) as exc:
        raise _job_error(exc) from None


@app.post("/api/jobs", status_code=202)
async def create_job(body: CompareInput, request: Request):
    _manager_instance, job = await _enqueue(body, request)
    return job


@app.get("/api/jobs")
async def list_jobs():
    return {"jobs": (await _manager()).list()}


@app.get("/api/jobs/{task_id}")
async def job_detail(task_id: int):
    try:
        return (await _manager()).public(task_id)
    except KeyError as exc:
        raise _job_error(exc) from None


@app.get("/api/jobs/{task_id}/events")
async def job_events(task_id: int, after: int = 0):
    if after < 0:
        raise HTTPException(400, "事件游标必须为非负整数")
    try:
        return (await _manager()).store.events(task_id, after)
    except KeyError as exc:
        raise _job_error(exc) from None


@app.post("/api/jobs/{task_id}/cancel")
async def cancel_job(task_id: int):
    try:
        return await (await _manager()).cancel(task_id)
    except KeyError as exc:
        raise _job_error(exc) from None


class CredentialsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: SecretStr


@app.post("/api/jobs/{task_id}/credentials")
async def job_credentials(task_id: int, body: CredentialsInput):
    try:
        return (await _manager()).supply(task_id, body.api_key.get_secret_value())
    except (RegistryError, KeyError) as exc:
        raise _job_error(exc) from None


@app.post("/api/compare")
async def compare(body: CompareInput, request: Request):
    manager, job = await _enqueue(body, request)
    return StreamingResponse(manager.subscribe(job["id"], request), media_type="application/x-ndjson")


@app.post("/api/reports", status_code=201)
async def save_report(body: AdmissionReportInput):
    report = body.model_dump()
    if _contains_secret_field(report):
        raise HTTPException(status_code=400, detail="报告中不能包含密钥或密码字段")
    report = reporting.finalize_report(report)
    try:
        saved = storage.save_report(report)
        return {**report, **saved}
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc


@app.get("/api/reports")
async def reports():
    return {"reports": storage.list_reports()}


@app.get("/api/reports/{report_id}")
async def report_detail(report_id: int):
    report = storage.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="准入报告不存在")
    return reporting.finalize_report(report)


@app.get("/api/reports/{report_id}/ordinary")
async def ordinary_report(report_id: int):
    report = storage.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="准入报告不存在")
    return reporting.public_report(report)


@app.delete("/api/reports/{report_id}")
async def remove_report(report_id: int):
    if not storage.delete_report(report_id):
        raise HTTPException(status_code=404, detail="准入报告不存在")
    return {"deleted": True}


app.mount("/protocol", protocol_app)
app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True))
