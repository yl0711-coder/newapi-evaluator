from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from shared.registry import Conflict, RegistryError, get_registry
from features.stability.app import scheduler
from .catalog import Catalog
from .discovery import fetch_models
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
    return {**service.coverage(), "stability_available": request.app.state.stability_available}


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
