"""Workbench-authenticated, asynchronous active review and explicit evidence import API."""
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from shared.registry import Conflict, RegistryError
from . import service

router = APIRouter(prefix="/api/integrity")
STATIC_DIR = Path(__file__).with_name("web")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReferenceInput(Input):
    package: dict
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirm_authorized: Literal[True]


class ReviewInput(Input):
    registry_channel_id: int = Field(ge=1)
    model: str = Field(min_length=1, max_length=160)
    protocol: Literal["openai", "responses", "anthropic"]
    strategy_id: Literal["hlwy", "kbf"]
    reference_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_ref: str = Field(min_length=1, max_length=200)
    incident_id: str = Field(default="", max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    limits: dict
    budget_seconds: int = Field(ge=1, le=86400)
    conditions: dict
    confirm_live: Literal[True]


class EvidenceInput(Input):
    evidence: dict
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirm_authorized: Literal[True]


class UnifiedInput(Input):
    registry_channel_id: int = Field(ge=1)
    model: Literal["gpt-6-astra", "gpt-6.1-sol"] = "gpt-6-astra"
    protocol: Literal["responses"] = "responses"
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirm_live: Literal[True]


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (RegistryError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None
    except KeyError:
        raise HTTPException(404, "任务或参考不存在") from None


@router.get("/meta")
async def meta():
    from .unified import get_service as unified_service, VERSION
    svc = service.get_service()
    return {"executor": service.executor_status(), "channels": call(svc.choices),
            "references": call(svc.references), "automatic_trigger": False,
            "methods": [{"id": "hlwy", "label": "HLwY 同条件数字分布复核", "max_attempts": 50, "max_retries": 0},
                        {"id": "kbf", "label": "KBF 知识边界补充复核", "max_retries": 0}],
            "audit_status": {"fpverify": "reference_gated_future_audit", "modivue": "explicit_summary_only"},
            "unified": {"version": VERSION, "channels": call(unified_service().choices),
                        "default_model": "gpt-6-astra", "max_requests": 8, "max_retries": 0}}


@router.get("/tests")
async def unified_tests():
    from .unified import get_service
    return {"tasks": call(get_service().list, administrative=True)}


@router.post("/tests", status_code=202)
async def create_unified_test(body: UnifiedInput):
    from .unified import get_service
    return call(get_service().submit, **body.model_dump(mode="json"))


@router.get("/tests/{job_id}")
async def unified_test(job_id: str):
    from .unified import get_service
    return call(get_service().get, job_id, administrative=True)


@router.post("/tests/{job_id}/cancel", status_code=202)
async def cancel_unified_test(job_id: str):
    from .unified import get_service
    return call(get_service().cancel, job_id, principal="workbench", administrative=True)


@router.post("/tests/{job_id}/resume", status_code=202)
async def resume_unified_test(job_id: str):
    from .unified import get_service
    return call(get_service().resume, job_id, principal="workbench", administrative=True)


@router.get("/tests/{job_id}/export")
async def export_unified_test(job_id: str):
    from .unified import get_service
    return JSONResponse(call(get_service().get, job_id, administrative=True),
                        headers={"Content-Disposition": 'attachment; filename="three-method-api.json"'})


@router.post("/references")
async def import_reference(body: ReferenceInput):
    return call(service.import_reference, body.package, body.expected_sha256,
                principal="workbench", confirm_authorized=body.confirm_authorized)


@router.get("/reviews")
async def reviews():
    return {"tasks": call(service.list_reviews, principal="workbench", administrative=True)}


@router.post("/reviews", status_code=202)
async def create_review(body: ReviewInput):
    return call(service.enqueue_review, principal="workbench", **body.model_dump(mode="json"))


@router.get("/reviews/{job_id}")
async def review(job_id: str):
    return call(service.get_review, job_id, principal="workbench", administrative=True)


@router.post("/reviews/{job_id}/cancel", status_code=202)
async def cancel_review(job_id: str):
    return call(service.cancel_review, job_id, principal="workbench", administrative=True)


@router.post("/reviews/{job_id}/resume", status_code=202)
async def resume_review(job_id: str):
    return call(service.resume_review, job_id, principal="workbench", administrative=True)


@router.get("/reviews/{job_id}/export")
async def export_review(job_id: str):
    task = call(service.get_review, job_id, principal="workbench", administrative=True)
    return JSONResponse(task, headers={"Content-Disposition": 'attachment; filename="integrity-review.json"'})


@router.post("/evidence", status_code=202)
async def import_evidence(body: EvidenceInput):
    from .monitor_adapter import submit_evidence
    return call(submit_evidence, body.evidence, principal="workbench", idempotency_key=body.idempotency_key,
                confirm_authorized=body.confirm_authorized)


@router.get("/evidence")
async def evidence_tasks():
    from .monitor_adapter import list_evidence
    return {"tasks": call(list_evidence, principal="workbench", administrative=True)}


@router.get("/evidence/{job_id}")
async def evidence_task(job_id: str):
    from .monitor_adapter import get_evidence
    return call(get_evidence, job_id, principal="workbench", administrative=True)


@router.post("/evidence/{job_id}/cancel", status_code=202)
async def cancel_evidence(job_id: str):
    from .monitor_adapter import cancel_evidence
    return call(cancel_evidence, job_id, principal="workbench", administrative=True)


@router.post("/evidence/{job_id}/resume", status_code=202)
async def resume_evidence(job_id: str):
    from .monitor_adapter import resume_evidence
    return call(resume_evidence, job_id, principal="workbench", administrative=True)


@router.get("/evidence/{job_id}/export")
async def export_evidence(job_id: str):
    from .monitor_adapter import get_evidence
    task = call(get_evidence, job_id, principal="workbench", administrative=True)
    return JSONResponse(task, headers={"Content-Disposition": 'attachment; filename="official-account-analysis.json"'})
