"""Monitor signed active jobs and persisted result cursor; no workbench credentials."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field, ValidationError

from features.model_coverage.internal_api import _handle, _idempotency_key
from features.model_coverage.monitor import ContractError
from shared.registry import Conflict, RegistryError
from .api import Input, LogEvidence, Target
from . import service

internal_router = APIRouter(prefix="/internal/v1")


class MonitorTarget(Input):
    registry_channel_id: int = Field(ge=1, strict=True)
    model: str = Field(min_length=1, max_length=160)
    protocol: str


class JobInput(Input):
    schema_version: Literal["1.0"]
    target: MonitorTarget
    reference_channel_id: int | None = Field(default=None, ge=1, strict=True)
    event_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    confirm_live: bool = Field(default=False, strict=True)
    log_evidence: LogEvidence | None = None


def principal(request):
    return "monitor:" + request.headers["x-nexus-key-id"]


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Conflict:
        raise ContractError(409, "idempotency_conflict", "event identity or frozen conditions changed") from None
    except KeyError:
        raise ContractError(404, "job_or_reference_not_found", "job or trusted reference not found") from None
    except (ValueError, RegistryError):
        raise ContractError(422, "token_usage_contract_rejected", "paired token usage conditions are invalid") from None


@internal_router.post("/token-usage-jobs")
async def create_job(request: Request):
    async def action(raw):
        key = _idempotency_key(request)
        try:
            body = JobInput.model_validate_json(raw)
        except ValidationError:
            raise ContractError(400, "invalid_field", "strict token usage fields are required") from None
        value = body.model_dump(mode="json")
        target = value.pop("target")
        target["channel_id"] = target.pop("registry_channel_id")
        value.pop("schema_version")
        job = call(service.get_service().submit, **target, **value, idempotency_key=key, principal=principal(request))
        return 202, {"schema_version": "1.0", **job}
    return await _handle(request, action)


@internal_router.get("/token-usage-jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    async def action(raw):
        return 200, {"schema_version": "1.0", **call(service.get_service().store.get, job_id, principal=principal(request))}
    return await _handle(request, action)


@internal_router.get("/token-usage-jobs/{job_id}/result")
async def get_result(job_id: str, request: Request):
    async def action(raw):
        job = call(service.get_service().store.get, job_id, principal=principal(request))
        return 200, {"schema_version": "1.0", "job_id": job_id, "status": job["status"], "result": job["result"]}
    return await _handle(request, action)


@internal_router.post("/token-usage-jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    async def action(raw):
        _idempotency_key(request)
        if raw not in (b"", b"{}"):
            raise ContractError(400, "invalid_field", "cancel accepts an empty object")
        return 202, {"schema_version": "1.0", **call(service.get_service().cancel, job_id, principal=principal(request))}
    return await _handle(request, action)


@internal_router.get("/token-usage-events")
async def events(request: Request):
    async def action(raw):
        if set(request.query_params) - {"after", "limit"}:
            raise ContractError(400, "invalid_field", "unknown cursor query field")
        try:
            after, limit = int(request.query_params.get("after", "0")), int(request.query_params.get("limit", "100"))
        except ValueError:
            raise ContractError(400, "invalid_field", "cursor must be an integer") from None
        if after < 0 or not 1 <= limit <= 100:
            raise ContractError(400, "invalid_field", "cursor bounds are invalid")
        return 200, {"schema_version": "1.0", **service.get_service().store.events(principal(request), after, limit)}
    return await _handle(request, action)
