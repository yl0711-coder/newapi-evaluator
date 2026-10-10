"""One sequential reserve/send/commit loop for integrity consumers and Monitor adapters.

Prompts and answers live only in memory. A session owns persistence, target verification,
and lease fencing; transports must invoke ``before_send`` at the final outbound boundary.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, Callable
from .strategies import canonical_hash


class ExecutionStopped(RuntimeError):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


@dataclass(frozen=True)
class ProbeRequest:
    request_id: str
    probe: dict[str, Any]
    input_tokens_reserved: int
    output_tokens_reserved: int
    identity_hash: str = ""

    def manifest(self) -> dict[str, Any]:
        digest = self.identity_hash or canonical_hash(self.probe)
        return {"request_id": self.request_id, "identity_hash": digest,
                "input_tokens_reserved": self.input_tokens_reserved,
                "output_tokens_reserved": self.output_tokens_reserved}


@dataclass(frozen=True)
class ResolvedTarget:
    channel: dict[str, Any]
    snapshot: dict[str, Any]


def resolve_registry_target(registry, channel_id, model, protocol, *, require_online=False, conn=None):
    from shared.registry import RegistryError
    from features.model_coverage.catalog import Catalog, model_name, validate_protocol
    if type(channel_id) is not int or channel_id < 1:
        raise RegistryError("请选择精确的公共渠道 ID")
    model = model_name(model)
    if conn is None:
        Catalog(registry)
        with registry.connect() as current:
            return resolve_registry_target(registry, channel_id, model, protocol,
                                           require_online=require_online, conn=current)
    row = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if row is None:
        raise RegistryError("公共渠道不存在")
    channel = registry.public(row)
    try:
        channel["api_key"] = registry._cipher.decrypt(row["key_enc"].encode()).decode()
    except Exception:
        raise RegistryError("渠道密钥缺失或无法解密，请替换密钥") from None
    registry.validate_connection(channel, require_online=require_online)
    row = conn.execute("""SELECT c.*, COALESCE(b.upstream_model,c.model) AS upstream_model,
        COALESCE(b.protocol,c.protocol) AS request_protocol FROM model_catalog c
        LEFT JOIN channel_model_bindings b ON b.model_id=c.id AND b.channel_id=? WHERE c.model=?""",
                       (channel_id, model)).fetchone()
    if row is None:
        raise RegistryError("请选择已登记的模型")
    binding = dict(row)
    if binding["request_protocol"] != protocol:
        raise RegistryError("协议与当前模型映射不一致")
    upstream = model_name(binding["upstream_model"])
    if channel["api_key"] in upstream:
        raise RegistryError("模型 ID 不能包含凭据")
    validate_protocol(protocol, model, upstream)
    snapshot = {"registry_channel_id": channel_id, "connection_fingerprint": registry.connection_fingerprint(channel),
                "model": upstream, "canonical_model": model, "protocol": protocol,
                "mapping_revision": canonical_hash(binding)}
    if require_online:
        snapshot["registry_status"] = "online"
    return ResolvedTarget({**channel, "model": upstream, "protocol": protocol}, snapshot)


def estimate_input_tokens(probe: dict[str, Any]) -> int:
    """Byte-based accounting estimate; reported tokenizer usage can be higher."""
    content = {k: probe[k] for k in ("prompt", "system_prompt") if k in probe}
    return len(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()) + 256


def build_requests(manifest, config, *, prefix=""):
    """Fixed API probes used by both daily slots and the unified manual task."""
    requests = []
    for spec in manifest.probes:
        probe = {"id": spec.probe_id, "name": spec.probe_id, "prompt": spec.prompt,
                 "system_prompt": spec.system_prompt, "stream": False,
                 "max_tokens": spec.max_output_tokens,
                 "request_timeout_seconds": manifest.request_timeout_seconds,
                 "reasoning_effort": config["reasoning_effort"]}
        requests.append(ProbeRequest(prefix + spec.probe_id, probe,
                                     estimate_input_tokens(probe), spec.max_output_tokens))
    return requests


async def execute_requests(session, requests: list[ProbeRequest], resolve_target: Callable,
                           send: Callable, project_result: Callable, *, stop_when: Callable | None = None,
                           skip_request: Callable | None = None, yield_after: int | None = None) -> str:
    """Execute immutable request identities once, preserving every confirmed/unknown attempt.

    ``resolve_target()`` returns a fresh ResolvedTarget (sync or async).
    ``send(channel, probe, *, before_send)`` calls the hook immediately before HTTP.
    ``project_result(request, raw, started, finished)`` returns only derived public values.
    The session validates that projection before committing it atomically with its attempt.
    """
    status, reason, skipped = "completed", "", []
    index = 0
    sent = 0
    prior = session.existing_requests() if yield_after is not None else None
    try:
        for index, request in enumerate(requests):
            existing = prior.get(request.request_id) if prior is not None else session.existing(request)
            if existing:
                if existing["identity_hash"] != request.manifest()["identity_hash"]:
                    raise ExecutionStopped("rejected", "request_identity_changed")
                continue
            skip_reason = skip_request(request, session.results()) if skip_request else None
            if skip_reason:
                skipped.append({"request_id": request.request_id, "reason": skip_reason})
                continue
            if stop_when is not None and stop_when(session.results()):
                break
            session.check()
            target = resolve_target()
            if inspect.isawaitable(target):
                target = await target
            if not isinstance(target, ResolvedTarget):
                raise ExecutionStopped("rejected", "target_unavailable")
            attempt: dict[str, Any] = {}
            started = time.time()

            async def before_send():
                if attempt:
                    raise ExecutionStopped("failed", "attempt_already_permitted")
                fresh = resolve_target()
                if inspect.isawaitable(fresh):
                    fresh = await fresh
                if not isinstance(fresh, ResolvedTarget) or fresh.snapshot != target.snapshot:
                    raise ExecutionStopped("rejected", "target_changed")
                attempt.update(session.reserve(request, fresh))

            async def request_task():
                # A two-argument sender is useful for isolated mocks; production has the hook.
                if "before_send" in inspect.signature(send).parameters:
                    return await send(target.channel, request.probe, before_send=before_send)
                await before_send()
                return await send(target.channel, request.probe)

            task = asyncio.create_task(request_task())
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=min(.1, session.LEASE_SECONDS / 3))
                    if not task.done():
                        if time.time() >= session.deadline:
                            raise ExecutionStopped("expired", "deadline_exceeded")
                        # Cancellation also stops local waiting for an in-flight request.
                        # Its permitted attempt remains unknown and is never resent.
                        session.check()
                        if session.heartbeat() == "lost":
                            raise ExecutionStopped("lost", "executor_lease_lost")
                raw = await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if not attempt:
                state = raw.get("status") if isinstance(raw, dict) else None
                reason = {"egress_denied": "eval_egress_denied", "network_error": "transport_connect",
                          "timeout": "transport_timeout"}.get(state, "transport_without_permit")
                raise ExecutionStopped("rejected" if state == "egress_denied" else "failed", reason)
            projection = project_result(request, raw, started, time.time())
            if not session.complete(request, attempt, projection):
                raise ExecutionStopped("lost", "executor_lease_lost")
            sent += 1
            if yield_after is not None and sent >= yield_after and any(r.request_id not in prior for r in requests[index + 1:]):
                session.check()
                return "yielded" if session.pause() else "lost"
        session.check()
    except ExecutionStopped as exc:
        status, reason = exc.status, exc.reason
        skipped += [{"request_id": r.request_id, "reason": reason} for r in requests[index:]
                   if not session.existing(r)]
    except (Exception, asyncio.CancelledError) as exc:
        reason = "executor_stopped" if isinstance(exc, asyncio.CancelledError) else "executor_error"
        session.finish("partially_completed" if session.results() else "failed", reason,
                       [{"request_id": r.request_id, "reason": reason} for r in requests[index:]])
        raise
    if status == "yielded" and yield_after is not None:
        return "yielded" if session.pause() else "lost"
    if status == "lost":
        session.recover()
        return "lost"
    if status == "expired" and session.results():
        status = "partially_completed"
    if status == "completed" and skipped:
        status, reason = "partially_completed", "health_failed_or_unmeasured"
    if not session.finish(status, reason, skipped):
        return "lost"
    return session.status()
