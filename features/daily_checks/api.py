from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, SecretStr, Field

from shared.feishu_notify import validate_webhook
from shared.registry import RegistryError, Conflict
from . import domain, reporting
from .service import get_service

STATIC_DIR = Path(__file__).parent / "web"
router = APIRouter(prefix="/api/daily-checks")


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family: Literal["GPT", "Claude"]
    model: str = Field(min_length=1, max_length=160)
    protocol: Literal["openai", "responses", "anthropic"]
    reference_channel_id: int = Field(strict=True, gt=0)


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profiles: list[Profile] = Field(default_factory=list, max_length=2)
    notify: bool = True
    enabled: bool = False
    confirm_live: bool = False
    confirm_notify: bool = False
    confirm_trusted: bool = False
    preview_fingerprint: str | None = Field(default=None, max_length=64)


class WebhookInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    webhook: SecretStr = Field(max_length=1000)


class RetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_notify: bool = False


def error(exc):
    status = 409 if isinstance(exc, Conflict) else 404 if isinstance(exc, KeyError) else 422
    detail = "请求的批次或可信参照不存在" if isinstance(exc, KeyError) else str(exc)
    return HTTPException(status, detail)


@router.get("/meta")
async def meta():
    service = get_service()
    try:
        configured = bool(service.webhook())
    except ValueError:
        configured = False
    models = [{**m, "label": domain.display(m["label"]), "test_families": domain.families(m["family"])}
              for m in service.catalog.models() if domain.families(m["family"])]
    channels = [{**c, "name": domain.display(c["name"])} for c in service.token.choices()]
    return {**domain.contract(), "reasons": domain.REASONS, "models": models, "channels": channels, "plan": service.store.plan(),
            "notification_configured": configured, "executor": service.status()}


@router.post("/preview")
async def preview(body: PlanInput):
    try:
        return get_service().preview([p.model_dump() for p in body.profiles], notify=body.notify)
    except (ValueError, KeyError) as exc:
        raise error(exc) from None


@router.put("/plan")
async def save(body: PlanInput):
    try:
        return get_service().save(**{**body.model_dump(), "profiles": [p.model_dump() for p in body.profiles]})
    except (ValueError, KeyError) as exc:
        raise error(exc) from None


@router.post("/pause")
async def pause():
    service = get_service()
    plan = service.store.pause(service.clock())
    await service.tick()
    return plan


@router.put("/notification")
async def notification(body: WebhookInput):
    from features.stability.app import storage, scheduler
    webhook = body.webhook.get_secret_value().strip()
    try:
        if webhook:
            validate_webhook(webhook)
    except ValueError as exc:
        raise error(exc) from None
    storage.set_setting(scheduler.WEBHOOK_KEY, webhook)
    return {"configured": bool(webhook)}


@router.get("/batches")
async def batches():
    service = get_service()
    return {"batches": [{"id": row["id"], "status": row["status"], "start_at": row["start_at"], "report_at": row["report_at"],
                          "coverage": row["report"]["coverage"] if row["report"] else None,
                          "notifications": service.store.messages(row["id"])} for row in service.store.batches()],
            "executor": service.status()}


@router.get("/batches/{batch_id}")
async def detail(batch_id: str):
    try:
        return get_service().detail(batch_id)
    except KeyError as exc:
        raise error(exc) from None


@router.post("/batches/{batch_id}/cancel")
async def cancel(batch_id: str):
    try:
        return await get_service().cancel(batch_id)
    except KeyError as exc:
        raise error(exc) from None


@router.post("/batches/{batch_id}/retry-notification")
async def retry(batch_id: str, body: RetryInput):
    if body.confirm_notify is not True:
        raise HTTPException(422, "请确认重新发送未送达或未确认的报告片段；未确认片段可能重复送达")
    service = get_service()
    try:
        count = service.store.retry_notifications(batch_id)
        await service.tick()
        return {"retried_parts": count}
    except KeyError as exc:
        raise error(exc) from None


@router.get("/batches/{batch_id}/export")
async def export(batch_id: str, format: Literal["json", "markdown"] = "json"):
    try:
        report = get_service().report(batch_id)
    except KeyError as exc:
        raise error(exc) from None
    body = domain.encode(report) if format == "json" else reporting.markdown(report)
    return Response(body, media_type="application/json" if format == "json" else "text/markdown")
