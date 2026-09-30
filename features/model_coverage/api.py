from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from shared.registry import Conflict, RegistryError, get_registry
from features.stability.app import scheduler
from .catalog import Catalog
from .discovery import fetch_models
from .production import ProductionCoverage
from .workflow import WorkflowQueue
from .monitor import MonitorStore
from . import service


router = APIRouter(prefix="/api/model-coverage")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ModelInput(Input):
    model: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=80)
    family: str = Field(min_length=1, max_length=40)
    protocol: Literal["openai", "anthropic", "responses"]


class MappingInput(Input):
    upstream_model: str = Field(min_length=1, max_length=160)
    protocol: Literal["openai", "anthropic", "responses"]


class FetchInput(Input):
    auth_kind: Literal["openai", "anthropic"] = "openai"
    confirm_live: Literal[True]


class Selection(Input):
    channel_id: int = Field(ge=1)
    model_id: int = Field(ge=1)


class Selections(Input):
    items: list[Selection] = Field(min_length=1, max_length=100)


class PlanInput(Selections):
    schedule_id: int = Field(ge=1)


class EnrollmentInput(PlanInput):
    preview_token: str = Field(min_length=64, max_length=64)
    confirm_live: Literal[True]


class VerificationInput(Selections):
    confirm_live: Literal[True]


class ProductionSnapshot(Input):
    source: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=160)
    generated_at: float = Field(gt=0)
    cursor: str = Field(default="", max_length=256)
    items: list[dict] = Field(min_length=1, max_length=10000)


class WorkflowTaskInput(Input):
    task_type: str = Field(min_length=1, max_length=80)
    payload: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=160)
    budget_seconds: int = Field(default=900, ge=1, le=86400)


class WorkflowEventInput(Input):
    event_id: str = Field(min_length=1, max_length=160)
    event_type: str = Field(min_length=1, max_length=80)
    payload: dict = Field(default_factory=dict)


def call(function, *args):
    try:
        return function(*args)
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except RegistryError as exc:
        raise HTTPException(400, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "渠道、模型或计划不存在") from exc


def require_stability(request):
    if not request.app.state.stability_available:
        raise HTTPException(409, "请在完整工作台或定时测试模式中执行此操作")


@router.get("")
async def overview(request: Request):
    result = service.coverage()
    result["production_coverage"] = ProductionCoverage(get_registry()).overview(
        [item for channel in result["channels"] for item in channel["models"]]
    )
    return {**result, "stability_available": request.app.state.stability_available}


@router.post("/production/import")
async def production_import(body: ProductionSnapshot):
    try:
        return ProductionCoverage(get_registry()).import_snapshot(body.model_dump(mode="json"))
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except RegistryError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/workflow/tasks")
async def workflow_tasks():
    return {"tasks": WorkflowQueue(get_registry()).list()}


@router.post("/workflow/tasks")
async def workflow_enqueue(body: WorkflowTaskInput):
    try:
        return WorkflowQueue(get_registry()).enqueue(**body.model_dump(mode="json"))
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except RegistryError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/workflow/tasks/{task_id}/cancel")
async def workflow_cancel(task_id: int):
    try:
        return WorkflowQueue(get_registry()).cancel(task_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/workflow/events", status_code=202)
async def workflow_event(body: WorkflowEventInput):
    try:
        return WorkflowQueue(get_registry()).enqueue(
            f"monitor:{body.event_type}", body.payload,
            idempotency_key=f"event:{body.event_id}", budget_seconds=900,
        )
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except RegistryError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/models")
async def add_model(body: ModelInput):
    return {"id": call(Catalog(get_registry()).add, body.model, body.label, body.family, body.protocol)}


@router.put("/channels/{channel_id}/models/{model_id}")
async def mapping(channel_id: int, model_id: int, body: MappingInput):
    call(Catalog(get_registry()).bind, channel_id, model_id, body.upstream_model, body.protocol)
    return {"saved": True}


@router.post("/channels/{channel_id}/fetch")
async def fetch(channel_id: int, body: FetchInput):
    call(get_registry().resolve, channel_id)
    return await fetch_models(get_registry(), channel_id, body.auth_kind)


@router.post("/enrollment/preview")
async def preview(body: PlanInput, request: Request):
    require_stability(request)
    return call(service.plan_preview, body.schedule_id, [item.model_dump() for item in body.items])


@router.post("/enrollment")
async def enroll(body: EnrollmentInput, request: Request):
    require_stability(request)
    return call(service.enroll, body.schedule_id, [item.model_dump() for item in body.items], body.preview_token)


@router.post("/verify", status_code=202)
async def verify(body: VerificationInput, request: Request):
    require_stability(request)
    run_id = call(service.verify_once, [item.model_dump() for item in body.items])
    await scheduler.tick()
    return {"run_id": run_id}


class IdentityInput(Input):
    channel_identity: str | None = Field(default=None, max_length=160)


@router.get("/monitor/identities")
async def monitor_identities():
    return {"identities": {str(k): v for k, v in MonitorStore(get_registry()).identities().items()}}


@router.put("/monitor/identities/{channel_id}")
async def monitor_bind_identity(channel_id: int, body: IdentityInput):
    try:
        return MonitorStore(get_registry()).bind_identity(channel_id, body.channel_identity or None)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except RegistryError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/monitor/jobs")
async def monitor_jobs(limit: int = 50):
    return {"jobs": MonitorStore(get_registry()).recent_jobs(limit)}
