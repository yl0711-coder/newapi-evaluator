"""Monitor Registry targets and durable, offline official-account evidence analysis."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time

from shared.registry import Conflict, RegistryError, get_registry
from .durable import IntegrityStore
from .execution import ProbeRequest, ResolvedTarget, execute_requests

logger = logging.getLogger(__name__)
_executor_task = None
_wake = None
EXECUTOR_ID = "evidence-" + secrets.token_hex(8)


def resolve_monitor_target(metadata):
    from .execution import resolve_registry_target
    snapshot = metadata.get("target_snapshot")
    if not isinstance(snapshot, dict) or not snapshot.get("canonical_model"):
        return None
    try:
        target = resolve_registry_target(get_registry(), snapshot["registry_channel_id"],
                                         snapshot["canonical_model"], snapshot["protocol"])
        return target if target.snapshot == snapshot else None
    except (KeyError, RegistryError):
        return None


def monitor_target_snapshot(registry_channel_id, model, protocol):
    from .execution import resolve_registry_target
    from features.model_coverage.monitor import ContractError
    try:
        return resolve_registry_target(get_registry(), registry_channel_id, model, protocol).snapshot
    except (KeyError, RegistryError):
        raise ContractError(422, "target_connection_invalid", "Registry channel, credentials or model mapping are invalid") from None


def _owned(job_id, principal, administrative=False):
    if principal not in {"monitor", "workbench"}:
        raise RegistryError("unknown principal")
    store = IntegrityStore(get_registry())
    job = store.public(job_id)
    if job["execution_mode"] != "offline" or (job["principal"] != principal and not (principal == "workbench" and administrative)):
        raise KeyError("evidence job not found")
    return store, job


def submit_evidence(evidence, *, principal, idempotency_key, confirm_authorized=False, deadline=None):
    if confirm_authorized is not True or principal not in {"monitor", "workbench"}:
        raise RegistryError("explicit evidence authorization is required")
    try:
        job = IntegrityStore(get_registry()).enqueue_offline(evidence=evidence, principal=principal, idempotency_key=idempotency_key, deadline=deadline)
    except Conflict:
        raise
    except ValueError:
        raise RegistryError("official-account evidence failed the whitelist or idempotency contract") from None
    if _wake is not None:
        _wake.set()
    return get_evidence(job["job_id"], principal=principal)


def get_evidence(job_id, *, principal, administrative=False):
    _, job = _owned(job_id, principal, administrative)
    return {**job, "outbound_requests": 0, "scope": "official_account", "executor": offline_executor_status(),
            "evidence_authenticity": "not_verified"}


def list_evidence(*, principal, administrative=False):
    if principal not in {"monitor", "workbench"}:
        raise RegistryError("unknown principal")
    jobs = IntegrityStore(get_registry()).list_jobs(principal=None if principal == "workbench" and administrative else principal)
    return [get_evidence(j["job_id"], principal=principal, administrative=administrative) for j in jobs if j["execution_mode"] == "offline"]


def cancel_evidence(job_id, *, principal, administrative=False):
    store, _ = _owned(job_id, principal, administrative)
    store.cancel(job_id)
    return get_evidence(job_id, principal=principal, administrative=administrative)


def resume_evidence(job_id, *, principal, administrative=False):
    store, _ = _owned(job_id, principal, administrative)
    store.resume(job_id)
    if _wake is not None:
        _wake.set()
    return get_evidence(job_id, principal=principal, administrative=administrative)


async def run_offline_pending(*, limit=20):
    from .evidence import analyze_account_evidence
    store, count = IntegrityStore(get_registry()), 0
    while count < limit:
        job = store.claim(owner=EXECUTOR_ID, execution_mode="offline")
        if job is None:
            break
        snapshot = job["target_snapshot"]
        evidence = store.offline_evidence(job["job_id"])
        request = ProbeRequest("evidence-analysis", {"id": "evidence-analysis", "evidence_hash": snapshot["evidence_hash"]}, 0, 0)
        async def analyze(_channel, _probe, *, before_send):
            # This hook authorizes a local computation; this sender has no network capability.
            await before_send()
            return await asyncio.to_thread(analyze_account_evidence, evidence)
        try:
            await execute_requests(store.session(job["job_id"], job["owner"]), [request],
                lambda: ResolvedTarget({}, snapshot), analyze, lambda _request, raw, _started, _finished: raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("offline evidence analysis failed: executor_error")
        count += 1
    return count


async def _loop():
    while True:
        await run_offline_pending()
        try:
            await asyncio.wait_for(_wake.wait(), 15)
        except asyncio.TimeoutError:
            pass
        _wake.clear()


async def start_offline_executor():
    global _executor_task, _wake
    if _executor_task is None or _executor_task.done():
        IntegrityStore(get_registry()).recover_expired_leases()
        _wake = asyncio.Event()
        _executor_task = asyncio.create_task(_loop(), name="official-account-evidence-analysis")
    return True


async def stop_offline_executor():
    global _executor_task, _wake
    if _executor_task is not None:
        _executor_task.cancel()
        await asyncio.gather(_executor_task, return_exceptions=True)
    _executor_task, _wake = None, None


def offline_executor_status():
    return {"mode": "offline_analysis", "enabled": True, "running": bool(_executor_task and not _executor_task.done()),
            "outbound_requests": 0, "evidence_authenticity": "not_verified"}
