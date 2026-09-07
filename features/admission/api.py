from pathlib import Path
from typing import AsyncIterator, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from shared.api import Selection, resolve
from shared.network import guarded_transport
from shared.redaction import EventRedactor
from shared.registry import RegistryError, normalize
from . import main as engine


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


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
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


app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True))
