"""/internal/v1 routes for Monitor only. Every request must carry a valid Monitor signature."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from shared.network import guarded_transport
from shared.registry import RegistryError, get_registry
from features.stability.app import transport
from features.stability.app.egress import EgressDenied, validate_url_sync
from . import monitor, service
from .monitor import SCHEMA_VERSION, ContractError, MonitorStore

logger = logging.getLogger(__name__)
internal_router = APIRouter(prefix="/internal/v1")
DNS_TIMEOUT_SECONDS = 5
DNS_WORKERS = 2
_dns_pool = ThreadPoolExecutor(max_workers=DNS_WORKERS, thread_name_prefix="monitor-egress")
_dns_limiter: asyncio.Semaphore | None = None
_dns_loop: asyncio.AbstractEventLoop | None = None


def _error(exc: ContractError, request_id: str, *, schema_version=SCHEMA_VERSION) -> JSONResponse:
    return JSONResponse({"schema_version": schema_version, "error": {"code": exc.code, "message": exc.message,
                         "retryable": exc.retryable, "request_id": request_id}},
                        status_code=exc.status, headers={**exc.headers, "X-Request-Id": request_id})


async def _authenticate(request: Request) -> bytes:
    """Verify the Monitor HMAC signature, timestamp window and single-use nonce; return the raw body."""
    try:
        creds = monitor.credentials()
    except RuntimeError:
        logger.error("Monitor credential misconfigured")
        raise ContractError(503, "monitor_access_misconfigured", "Monitor credential is misconfigured", retryable=False) from None
    if creds is None:
        raise ContractError(503, "monitor_access_disabled", "Monitor internal API is not enabled", retryable=False)
    key_id, secret = creds
    headers = request.headers
    if headers.get("x-nexus-client") != monitor.CLIENT:
        raise ContractError(403, "client_not_allowed", "only Monitor may call this API")
    if not hmac.compare_digest(headers.get("x-nexus-key-id", "").encode(), key_id.encode()):
        raise ContractError(401, "invalid_credentials", "unknown key id")
    timestamp, nonce, signature = headers.get("x-nexus-timestamp", ""), headers.get("x-nexus-nonce", ""), headers.get("x-nexus-signature", "")
    try:
        signed_at = monitor.parse_time(timestamp, "X-Nexus-Timestamp")
    except ContractError:
        raise ContractError(401, "invalid_signature", "timestamp is missing or invalid") from None
    if abs(time.time() - signed_at) > monitor.SIGNATURE_WINDOW_SECONDS:
        raise ContractError(401, "signature_expired", "timestamp is outside the allowed window")
    if not 16 <= len(nonce) <= 128 or not nonce.isascii() or not nonce.replace("-", "").replace("_", "").isalnum():
        raise ContractError(401, "invalid_signature", "nonce is missing or invalid")
    body = await _bounded_body(request)
    # Sign exactly what was sent on the wire: raw (undecoded) path plus the raw query string.
    raw_path = request.scope.get("raw_path") or request.url.path.encode()
    query = request.scope.get("query_string", b"")
    path_qs = raw_path.decode("latin-1") + (("?" + query.decode("latin-1")) if query else "")
    expected = monitor.sign(secret, request.method, path_qs, timestamp, nonce, body)
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        raise ContractError(401, "invalid_signature", "signature does not match")
    # Nonce is recorded only after the signature is valid, so forged requests cannot burn nonces.
    if not MonitorStore(get_registry()).remember_nonce(key_id, nonce, time.time()):
        raise ContractError(401, "replayed_request", "nonce was already used")
    return body


async def _bounded_body(request: Request) -> bytes:
    """Read at most MAX_BODY_BYTES; oversized requests are refused before they are buffered."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > monitor.MAX_BODY_BYTES):
        raise ContractError(413, "request_too_large", "request body is too large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > monitor.MAX_BODY_BYTES:
            raise ContractError(413, "request_too_large", "request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _json_body(raw: bytes) -> dict:
    try:
        value = json.loads(raw or b"null")
    except ValueError:
        raise ContractError(400, "invalid_request", "body must be JSON") from None
    return monitor.check_schema(value)


def _eval_models() -> dict[int, list[dict]]:
    return {row["channel_id"]: row["models"] for row in service.coverage()["channels"]}


def resolve_channel(channel_id: int) -> dict | None:
    """Enabled Eval channel with its key for the executor; None if deleted or disabled."""
    try:
        channel = get_registry().get(channel_id, secret=True)
    except (KeyError, RegistryError):
        return None
    return channel if channel["enabled"] else None


def _public_channel(channel_id: int) -> dict | None:
    channel = resolve_channel(channel_id)
    return None if channel is None else {k: v for k, v in channel.items() if k != "api_key"}


def egress_check(channel: dict, protocol: str) -> None:
    try:
        validate_url_sync(transport.endpoint_url(channel["base_url"], protocol))
    except EgressDenied:
        raise ContractError(422, "egress_denied", "target URL violates the Eval egress policy") from None


async def _bounded_egress(channel: dict, protocol: str) -> None:
    global _dns_limiter, _dns_loop
    loop = asyncio.get_running_loop()
    if _dns_loop is not loop:
        _dns_limiter, _dns_loop = asyncio.Semaphore(DNS_WORKERS), loop
    limiter = _dns_limiter
    try:
        await asyncio.wait_for(limiter.acquire(), DNS_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise ContractError(503, "egress_check_timeout", "target validation is busy", retryable=True) from None
    future = _dns_pool.submit(egress_check, channel, protocol)

    def release(_future):
        try:
            loop.call_soon_threadsafe(limiter.release)
        except RuntimeError:
            pass

    future.add_done_callback(release)
    task = asyncio.wrap_future(future, loop=loop)
    task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
    try:
        await asyncio.wait_for(asyncio.shield(task), DNS_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        # A running libc resolver cannot be terminated; its slot stays held until it returns.
        raise ContractError(503, "egress_check_timeout", "target validation timed out", retryable=True) from None


async def _handle(request: Request, action, *, schema_version=SCHEMA_VERSION):
    request_id = "eval-" + secrets.token_hex(8)
    try:
        raw = await _authenticate(request)
        status, payload = await action(raw)
    except ContractError as exc:
        # Log code and path only: never headers, bodies or channel data.
        logger.info("monitor internal %s -> %s", request.method, exc.code)
        return _error(exc, request_id, schema_version=schema_version)
    except RegistryError:
        return _error(ContractError(400, "invalid_request", "request was rejected by validation"), request_id, schema_version=schema_version)
    except Exception:
        logger.error("monitor internal request failed: eval_unavailable")
        return _error(ContractError(503, "eval_unavailable", "Eval could not process the request", retryable=True,
                                    headers={"Retry-After": "30"}), request_id, schema_version=schema_version)
    return JSONResponse(payload, status_code=status, headers={"X-Request-Id": request_id})


def _idempotency_key(request: Request) -> str:
    value = request.headers.get("idempotency-key", "")
    if not monitor._IDENTITY.fullmatch(value):
        raise ContractError(400, "idempotency_key_required", "write requests need a valid Idempotency-Key header")
    return value


@internal_router.put("/production-inventory/{inventory_version}")
async def production_inventory(inventory_version: str, request: Request):
    async def action(raw):
        _idempotency_key(request)
        body = _json_body(raw)
        store = MonitorStore(get_registry())
        return 200, store.import_inventory(inventory_version, body, await asyncio.to_thread(_eval_models))
    return await _handle(request, action)


@internal_router.post("/probe-jobs")
async def create_probe_job(request: Request):
    async def action(raw):
        key = _idempotency_key(request)
        body = _json_body(raw)
        store = MonitorStore(get_registry())
        replay = store.replay_job(body, key)
        if replay:
            return 200, replay
        validated = store._validate_job(body, _public_channel, lambda _channel, _protocol: None)
        await _bounded_egress(validated["_channel"], validated["protocol"])
        response, created = store.create_job(body, key, _public_channel, lambda _channel, _protocol: None, validated_job=validated)
        if created:
            wake_executor()
        return (201 if created else 200), response
    return await _handle(request, action)


@internal_router.get("/probe-jobs/{job_id}")
async def get_probe_job(job_id: str, request: Request):
    async def action(_raw):
        return 200, MonitorStore(get_registry()).job(job_id)
    return await _handle(request, action)


@internal_router.post("/probe-jobs/{job_id}/cancel")
async def cancel_probe_job(job_id: str, request: Request):
    async def action(_raw):
        key = _idempotency_key(request)
        return 202, MonitorStore(get_registry()).cancel(job_id, key)
    return await _handle(request, action)


def _limit(request: Request) -> int:
    value = request.query_params.get("limit", "200")
    if not value.isdigit():
        raise ContractError(400, "invalid_field", "limit must be an integer between 1 and 200")
    return int(value)


@internal_router.get("/probe-results")
async def probe_results(request: Request):
    async def action(_raw):
        return 200, MonitorStore(get_registry()).results(request.query_params.get("cursor", ""), _limit(request))
    return await _handle(request, action)


@internal_router.get("/probe-events")
async def probe_events(request: Request):
    async def action(_raw):
        return 200, MonitorStore(get_registry()).events(request.query_params.get("cursor", ""), _limit(request))
    return await _handle(request, action)


def _integrity_body(raw):
    try:
        body = json.loads(raw)
    except ValueError:
        raise ContractError(400, "invalid_request", "body must be JSON") from None
    if not isinstance(body, dict):
        raise ContractError(400, "invalid_request", "body must be an object")
    if body.get("strategy") in {"nerfed", "is-gpt-nerfed"} or body.get("job_type") in {"nerfed", "is-gpt-nerfed"}:
        raise ContractError(422, "strategy_contract_changed", "nerfed now analyzes explicit official-account evidence with schema_version 2.0")
    if body.get("schema_version") != "2.0":
        raise ContractError(400, "schema_version_unsupported", "integrity jobs require schema_version 2.0")
    return body


def _integrity_call(function, *args, **kwargs):
    from shared.registry import Conflict
    try:
        return function(*args, **kwargs)
    except Conflict:
        raise ContractError(409, "idempotency_conflict", "task identity or conditions changed") from None
    except KeyError:
        raise ContractError(404, "job_not_found", "integrity task or reference not found") from None
    except (ValueError, RegistryError):
        raise ContractError(422, "integrity_contract_rejected", "target, reference, evidence or execution conditions are invalid") from None


@internal_router.post("/integrity-jobs")
async def create_integrity_job(request: Request):
    async def action(raw):
        from features.integrity import service as reviews, monitor_adapter as adapter
        from features.integrity.api import ReviewInput
        from pydantic import ValidationError
        key, body = _idempotency_key(request), _integrity_body(raw)
        if body.get("job_type") == "nerfed-evidence-analysis":
            if set(body) != {"schema_version", "job_type", "evidence", "confirm_authorized"}:
                raise ContractError(400, "invalid_field", "evidence jobs accept only explicit whitelisted evidence")
            job = _integrity_call(adapter.submit_evidence, body["evidence"], principal="monitor", idempotency_key=key,
                                  confirm_authorized=body["confirm_authorized"])
        elif body.get("job_type") == "nerfed-api":
            from features.integrity.unified import get_service
            if set(body) != {"schema_version", "job_type", "target", "confirm_live"}:
                raise ContractError(400, "invalid_field", "nerfed-api requires explicit target and confirmation")
            target = body["target"]
            if not isinstance(target, dict) or set(target) != {"channel_identity", "inventory_version", "model", "protocol"}:
                raise ContractError(400, "invalid_field", "explicit production target is required")
            if any(not isinstance(target[k], str) or not target[k] or len(target[k]) > 160 for k in target):
                raise ContractError(400, "invalid_field", "target identifiers are invalid")
            snapshot = adapter.monitor_target_snapshot(identity=target["channel_identity"], inventory_version=target["inventory_version"],
                model=target["model"], protocol=target["protocol"])
            job = _integrity_call(get_service().submit, registry_channel_id=snapshot["registry_channel_id"],
                model=target["model"], protocol=target["protocol"], idempotency_key=key,
                confirm_live=body["confirm_live"], principal="monitor", kind="nerfed-api-v1", target_snapshot=snapshot)
        elif body.get("job_type") == "active-review":
            if set(body) != {"schema_version", "job_type", "target", "review"}:
                raise ContractError(400, "invalid_field", "active review fields are invalid")
            target = body["target"]
            if not isinstance(target, dict) or set(target) != {"channel_identity", "inventory_version", "model", "protocol"}:
                raise ContractError(400, "invalid_field", "explicit production target is required")
            if any(not isinstance(target[k], str) or not target[k] or len(target[k]) > 160 for k in target) or target["protocol"] not in {"openai", "responses", "anthropic"}:
                raise ContractError(400, "invalid_field", "target identifiers or protocol are invalid")
            if not isinstance(body["review"], dict) or set(body["review"]) != {
                    "strategy_id", "reference_hash", "source_ref", "incident_id", "limits", "budget_seconds", "conditions", "confirm_live"}:
                raise ContractError(400, "invalid_field", "review fields are invalid; target and idempotency come from the server")
            snapshot = adapter.monitor_target_snapshot(**{ "identity": target["channel_identity"],
                "inventory_version": target["inventory_version"], "model": target["model"], "protocol": target["protocol"]})
            try:
                validated = ReviewInput.model_validate({**body["review"], "registry_channel_id": snapshot["registry_channel_id"],
                    "model": target["model"], "protocol": target["protocol"], "idempotency_key": key})
            except (ValidationError, TypeError):
                raise ContractError(400, "invalid_field", "review fields or budgets are invalid") from None
            reviews.configure_monitor_resolver(adapter.resolve_monitor_target)
            job = _integrity_call(reviews.enqueue_review, principal="monitor", target_snapshot=snapshot,
                                  **validated.model_dump(mode="json"))
        else:
            raise ContractError(422, "job_type_unsupported", "supported types: active-review, nerfed-api, nerfed-evidence-analysis")
        return 202, {"schema_version": "2.0", "job": job}
    return await _handle(request, action, schema_version="2.0")


def _integrity_job(job_id, operation="get"):
    from features.integrity import service as reviews, monitor_adapter as adapter
    from features.integrity.durable import IntegrityStore
    job = _integrity_call(IntegrityStore(get_registry()).public, job_id)
    if job["principal"] != "monitor":
        raise ContractError(404, "job_not_found", "integrity task not found")
    if job["strategy"].get("strategy_id") == "nerfed-api-v1":
        from features.integrity.unified import get_service
        function = {"get": get_service().get, "cancel": get_service().cancel, "resume": get_service().resume}[operation]
        return _integrity_call(function, job_id, principal="monitor")
    operations = {"get": (adapter.get_evidence, reviews.get_review),
                  "cancel": (adapter.cancel_evidence, reviews.cancel_review),
                  "resume": (adapter.resume_evidence, reviews.resume_review)}
    function = operations[operation][0 if job["execution_mode"] == "offline" else 1]
    return _integrity_call(function, job_id, principal="monitor")


@internal_router.get("/integrity-jobs")
async def list_integrity_jobs(request: Request):
    async def action(_raw):
        from features.integrity import service as reviews, monitor_adapter as adapter
        from features.integrity.unified import get_service
        return 200, {"schema_version": "2.0", "jobs": _integrity_call(reviews.list_reviews, principal="monitor") +
                    _integrity_call(adapter.list_evidence, principal="monitor") +
                    _integrity_call(get_service().list, principal="monitor")}
    return await _handle(request, action, schema_version="2.0")


@internal_router.get("/integrity-jobs/{job_id}")
@internal_router.get("/integrity-jobs/{job_id}/result")
async def get_integrity_job(job_id: str, request: Request):
    async def action(_raw):
        return 200, {"schema_version": "2.0", "job": _integrity_job(job_id)}
    return await _handle(request, action, schema_version="2.0")


@internal_router.post("/integrity-jobs/{job_id}/cancel")
async def cancel_integrity_job(job_id: str, request: Request):
    async def action(_raw):
        _idempotency_key(request)
        return 202, {"schema_version": "2.0", "job": _integrity_job(job_id, "cancel")}
    return await _handle(request, action, schema_version="2.0")


@internal_router.post("/integrity-jobs/{job_id}/resume")
async def resume_integrity_job(job_id: str, request: Request):
    async def action(_raw):
        _idempotency_key(request)
        return 202, {"schema_version": "2.0", "job": _integrity_job(job_id, "resume")}
    return await _handle(request, action, schema_version="2.0")


# ---- background executor --------------------------------------------------------------------
POLL_SECONDS = 15
_executor_task: asyncio.Task | None = None
# Per-process lease owner: results and terminal states are only written under this identity.
EXECUTOR_ID = "executor-" + secrets.token_hex(8)
_wake: asyncio.Event | None = None


async def send_probe(channel: dict, probe: dict, *, before_send=None) -> dict:
    """One upstream request through the guarded transport and the process-wide request limiter."""
    from features.stability.app.scheduler import probe_semaphore
    timeout = httpx.Timeout(connect=20, read=180, write=20, pool=20)
    async with probe_semaphore():
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False,
                                     transport=guarded_transport()) as client:
            return await transport.run_probe(client, channel, probe, before_send=before_send)


async def run_pending(send=send_probe, *, limit: int = 20) -> int:
    """Claim and execute due jobs one at a time; returns how many jobs ran."""
    store, count = MonitorStore(get_registry()), 0
    while count < limit:
        job = await asyncio.to_thread(store.claim, EXECUTOR_ID)
        if job is None:
            break
        try:
            await monitor.execute_job(store, job, resolve_channel, send)
        except Exception:
            # The job is already ended as executor_error; keep draining the rest of the queue.
            logger.error("monitor job execution failed: executor_error")
        count += 1
    return count


def wake_executor() -> None:
    if _wake is not None:
        _wake.set()


async def _loop() -> None:
    while True:
        try:
            await run_pending()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("monitor executor tick failed")
        try:
            await asyncio.wait_for(_wake.wait(), POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
        _wake.clear()


async def start_executor() -> bool:
    """Start only when EVAL_MONITOR_EXECUTOR=live; otherwise jobs stay queued and expire."""
    global _executor_task, _wake
    if not monitor.executor_enabled():
        return False
    MonitorStore(get_registry()).recover_expired_leases()
    _wake = asyncio.Event()
    if not _executor_task or _executor_task.done():
        _executor_task = asyncio.create_task(_loop(), name="monitor-executor")
    return True


async def stop_executor() -> None:
    global _executor_task, _wake
    if _executor_task:
        _executor_task.cancel()
        await asyncio.gather(_executor_task, return_exceptions=True)
    _executor_task, _wake = None, None


def executor_status() -> dict:
    return {"enabled": monitor.executor_enabled(), "running": bool(_executor_task and not _executor_task.done())}
