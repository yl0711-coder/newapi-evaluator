"""Ordinary workbench endpoints. Keys and bodies are never accepted or returned."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from features.model_coverage.catalog import Catalog, model_name
from shared.registry import Conflict, RegistryError
from . import service
from .domain import LABELS, MAX_REPORTED_TOKENS, VERSION, protocol

router = APIRouter(prefix="/api/token-usage")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(Input):
    channel_id: int = Field(ge=1, strict=True)
    model: str = Field(min_length=1, max_length=160)
    protocol: str

    @field_validator("model")
    @classmethod
    def clean_model(cls, value):
        return model_name(value)

    @field_validator("protocol")
    @classmethod
    def clean_protocol(cls, value):
        return protocol(value)


class Binding(Input):
    model: str = Field(min_length=1, max_length=160)
    protocol: str
    reference_channel_id: int = Field(ge=1, strict=True)
    confirm_trusted: bool = Field(default=False, strict=True)
    threshold_config: dict | None = None


class Configuration(Target):
    enabled: bool = Field(default=False, strict=True)
    confirm_live: bool = Field(default=False, strict=True)
    config_id: int | None = Field(default=None, ge=1, strict=True)


class LogEvidence(Input):
    event_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    observed_at: str = Field(min_length=1, max_length=40)
    input_tokens_reported: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS, strict=True)
    reference_tokens_reported: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS, strict=True)
    first_input_tokens_reported: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS, strict=True)
    final_input_tokens_reported: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS, strict=True)
    source: Literal["monitor_log"] = "monitor_log"

    @field_validator("observed_at")
    @classmethod
    def clean_time(cls, value):
        from features.model_coverage.monitor import parse_time, ContractError
        try:
            parse_time(value, "observed_at")
        except ContractError:
            raise ValueError("invalid_timestamp") from None
        return value


class Manual(Target):
    confirm_live: bool = Field(default=False, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    log_evidence: LogEvidence | None = None


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (ValueError, RegistryError):
        raise HTTPException(422, "token_usage_contract_rejected") from None
    except KeyError:
        raise HTTPException(404, "trusted_reference_or_job_not_found") from None


@router.get("/meta")
async def meta():
    svc = service.get_service()
    return {"detection_version": VERSION, "labels": LABELS, "channels": svc.choices(),
            "models": Catalog(svc.registry).models(), "executor": svc.status()}


@router.get("/bindings")
async def bindings():
    return {"bindings": service.get_service().store.bindings()}


@router.post("/bindings")
async def save_binding(body: Binding):
    return call(service.get_service().save_binding, body.model, body.protocol, body.reference_channel_id,
                confirm_trusted=body.confirm_trusted, threshold_config=body.threshold_config)


@router.get("/configs")
async def configs():
    return {"configs": service.get_service().configs()}


@router.post("/configs")
async def save_config(body: Configuration):
    return call(service.get_service().save_config, **body.model_dump())


@router.post("/preview")
async def preview(body: Target):
    return call(service.get_service().preview, **body.model_dump())


@router.post("/manualcheck", status_code=202)
async def manualcheck(body: Manual):
    return call(service.get_service().submit, **body.model_dump(mode="json"))


@router.get("/status")
async def status():
    return service.executor_status()


@router.get("/results")
async def results():
    return {"jobs": service.get_service().store.list(administrative=True)}


@router.get("/results/{job_id}")
async def result(job_id: str):
    return call(service.get_service().store.get, job_id, administrative=True)


@router.post("/results/{job_id}/cancel", status_code=202)
async def cancel(job_id: str):
    return call(service.get_service().cancel, job_id, administrative=True)
