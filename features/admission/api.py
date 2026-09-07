from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from shared.api import Selection, resolve
from shared.network import guarded_transport
from shared.redaction import EventRedactor
from shared.registry import RegistryError, normalize
from . import main as engine, storage


class CandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: SecretStr
    model: str = Field(min_length=1, max_length=160)
    protocol: Literal["openai", "anthropic"]


class CompareInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate: CandidateInput
    reference: Selection
    rounds: int = Field(default=1, ge=1, le=5)


class ReportEndpointInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    base_url: str = Field(min_length=1, max_length=1000)
    model: str = Field(min_length=1, max_length=160)
    protocol: Literal["openai", "anthropic"]
    name: str | None = Field(default=None, max_length=160)
    channel_id: int | None = Field(default=None, ge=1)
    multiplier: float | None = None


class ReportQuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    difficulty: str = Field(min_length=1, max_length=40)
    prompt: str = Field(max_length=20_000)


class ReportResponseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    round: int = Field(ge=1, le=5)
    question_id: str = Field(min_length=1, max_length=80)
    side: Literal["candidate", "reference"]
    content: str = Field(default="", max_length=200_000)
    reasoning: str = Field(default="", max_length=200_000)


class AdmissionReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    created_at: str = Field(min_length=1, max_length=80)
    status: Literal["completed", "canceled", "failed"]
    rounds: int = Field(ge=1, le=5)
    candidate: ReportEndpointInput
    reference: ReportEndpointInput
    questions: list[ReportQuestionInput] = Field(min_length=1, max_length=5)
    measurements: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    responses: list[ReportResponseInput] = Field(default_factory=list, max_length=50)
    summary: dict[str, Any] = Field(default_factory=dict)


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


@asynccontextmanager
async def lifespan(_app):
    storage.init()
    try:
        yield
    finally:
        storage.close()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.state.upstream_transport = None
app.get("/api/meta")(engine.meta)


class ExtractInput(BaseModel):
    text: str = Field(min_length=1, max_length=100_000, repr=False)


@app.post("/api/extract-channel")
async def extract_channel(body: ExtractInput):
    try:
        return engine.extract_channel_credentials(body.text)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(400, "无法提取候选端信息，请检查文本或手动填写") from exc


@app.post("/api/compare")
async def compare(body: CompareInput, request: Request):
    try:
        clean = normalize({"base_url": body.candidate.base_url,
                           "api_key": body.candidate.api_key.get_secret_value()})
    except RegistryError as exc:
        raise HTTPException(400, str(exc)) from exc
    candidate = {"base_url": clean["base_url"], "api_key": clean["api_key"],
                 "model": body.candidate.model, "protocol": body.candidate.protocol}
    reference = resolve(body.reference)
    run = engine.CompareRequest(candidate=engine.EndpointConfig(**candidate),
                                reference=engine.EndpointConfig(**reference), rounds=body.rounds)
    redactor = EventRedactor([candidate["api_key"], reference["api_key"]])

    async def stream() -> AsyncIterator[bytes]:
        total = len(engine.QUESTIONS) * body.rounds
        yield engine.encode_event({"type": "run_started", "question_count": len(engine.QUESTIONS),
                                   "rounds": body.rounds, "total_question_runs": total})
        timeout = httpx.Timeout(connect=20, read=240, write=20, pool=20)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False,
                transport=app.state.upstream_transport or guarded_transport()) as client:
            for number in range(1, body.rounds + 1):
                for index, question in enumerate(engine.QUESTIONS, 1):
                    if await request.is_disconnected():
                        return
                    yield engine.encode_event({"type": "question_started", "question_id": question["id"],
                                               "round": number, "index": index})
                    async for event in engine.run_question(run, question, client):
                        for safe in redactor.filter(event):
                            yield engine.encode_event({**safe, "round": number})
                    completed = (number - 1) * len(engine.QUESTIONS) + index
                    yield engine.encode_event({"type": "question_finished", "question_id": question["id"],
                        "round": number, "completed_question_runs": completed, "total_question_runs": total})
                    if completed < total:
                        await engine.wait_between_questions()
        yield engine.encode_event({"type": "run_finished"})

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/reports", status_code=201)
async def save_report(body: AdmissionReportInput):
    report = body.model_dump()
    if _contains_secret_field(report):
        raise HTTPException(status_code=400, detail="报告中不能包含密钥或密码字段")
    try:
        return storage.save_report(report)
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
    return report


@app.delete("/api/reports/{report_id}")
async def remove_report(report_id: int):
    if not storage.delete_report(report_id):
        raise HTTPException(status_code=404, detail="准入报告不存在")
    return {"deleted": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True))
