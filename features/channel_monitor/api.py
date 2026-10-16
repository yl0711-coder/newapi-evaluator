from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .service import get_service

router = APIRouter(prefix="/api/channel-monitor")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InstanceInput(Input):
    base_url: str = Field(min_length=1, max_length=1000)
    token: SecretStr = Field(default=SecretStr(""), max_length=4096)
    user_id: int | None = Field(default=None, gt=0)


class BindingInput(Input):
    remote_id: int = Field(gt=0)
    registry_id: int = Field(gt=0)
    model: str = Field(min_length=1, max_length=160)
    protocol: Literal["responses", "openai"]
    expected: str = Field(min_length=1, max_length=160)
    confirm_live: bool = False
    confirm_same_credential: bool = False


class PlanInput(Input):
    enabled: bool
    confirm_live: bool = False
    confirm_manage: bool = False
    confirm_inventory: bool = False


class CircuitInput(Input):
    revision: int = Field(ge=0)
    confirm_reset: bool = False


class LockInput(Input):
    locked: bool
    takeover: bool = False


def error(exc):
    return HTTPException(404 if isinstance(exc, KeyError) else 422,
                         "映射不存在" if isinstance(exc, KeyError) else str(exc))


@router.get("/meta")
async def meta():
    try:
        return get_service().meta()
    except ValueError as exc:
        raise error(exc) from None


@router.put("/instance")
async def instance(body: InstanceInput):
    try:
        return get_service().save_instance(body.base_url, body.token.get_secret_value().strip(), body.user_id)
    except ValueError as exc:
        raise error(exc) from None


@router.post("/bindings")
async def add_binding(body: BindingInput):
    try:
        return await get_service().add_binding(**body.model_dump())
    except (ValueError, KeyError) as exc:
        raise error(exc) from None


@router.put("/bindings/{binding_id}/lock")
async def lock(binding_id: int, body: LockInput):
    try:
        return get_service().store.lock(binding_id, **body.model_dump())
    except (ValueError, KeyError) as exc:
        raise error(exc) from None


@router.delete("/bindings/{binding_id}")
async def remove(binding_id: int):
    try:
        get_service().store.remove(binding_id)
        return {"removed": True}
    except (ValueError, KeyError) as exc:
        raise error(exc) from None


@router.put("/plan")
async def plan(body: PlanInput):
    service = get_service()
    try:
        result = service.enable(**body.model_dump())
        if not body.enabled:
            await service.stop()
        return result
    except ValueError as exc:
        raise error(exc) from None


@router.post("/run")
async def run():
    try:
        return await get_service().run_now()
    except ValueError as exc:
        raise error(exc) from None


@router.get("/history")
async def history():
    return get_service().store.history()


@router.post("/circuit/reset")
async def reset_circuit(body: CircuitInput):
    service = get_service()
    try:
        if body.confirm_reset is not True:
            raise ValueError("请确认已核对故障原因后解除熔断")
        return service.store.reset_circuit(body.revision, service.clock())
    except ValueError as exc:
        raise error(exc) from None


@router.get("/rounds/{round_id}/attempts")
async def attempts(round_id: int):
    return {"attempts": get_service().store.attempts(round_id)}
