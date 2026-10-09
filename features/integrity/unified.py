"""One durable API task with independent TraceOne/ModelTrace/nerfed samples.

The official-account evidence task remains separate. Only frozen API probes are
executed here; no upstream executable, Codex file, catalog loader or session is used.
"""
from __future__ import annotations

import asyncio
import time

from shared.registry import Conflict, RegistryError, get_registry
from features.model_coverage.catalog import Catalog
from .durable import IntegrityStore, default_pricing
from .execution import ResolvedTarget, build_requests, execute_requests
from .scoring import project_observation, score_strategy
from .strategies import canonical_hash, get_strategy, load_bank
from . import service

VERSION = "three-method-api-v1"
METHODS = ("traceone", "modeltrace", "nerfed-api")
CONFIG = {"reasoning_effort": "low"}


class UnifiedService:
    def __init__(self, registry):
        self.registry = registry
        self.store = IntegrityStore(registry)

    def target(self, channel_id, model, protocol):
        return service.ReviewService(self.registry)._target(channel_id, model, protocol)

    def choices(self):
        catalog = Catalog(self.registry)
        models = [m for m in catalog.models() if m["model"] in {"gpt-6-astra", "gpt-6.1-sol"}]
        rows = []
        for channel in self.registry.list():
            entries = []
            for model in models:
                mapped = catalog.binding(channel["id"], model["id"])
                reason = ""
                try:
                    self.target(channel["id"], model["model"], mapped["request_protocol"])
                except RegistryError as exc:
                    reason = str(exc)
                entries.append({"model": model["model"], "upstream_model": mapped["upstream_model"],
                                "protocol": mapped["request_protocol"], "eligible": not reason, "reason": reason})
            rows.append({"id": channel["id"], "name": channel["name"], "status": channel["status"],
                         "credential_status": channel["credential_status"], "models": entries,
                         "available": any(m["eligible"] for m in entries)})
        return rows

    def _layout(self, model, kind):
        methods = METHODS if kind == VERSION else ("nerfed-api",)
        manifests = {m: get_strategy(m) for m in ("health", *methods)}
        unsupported = {m for m in methods if m == "modeltrace" and
                       model not in {r["id"] for r in load_bank(manifests[m])["models"]}}
        requests = [r for m, manifest in manifests.items() if m not in unsupported
                    for r in build_requests(manifest, CONFIG, prefix=m + ":")]
        return manifests, unsupported, requests

    def submit(self, *, registry_channel_id, model="gpt-6-astra", protocol="responses",
               idempotency_key, confirm_live, principal="workbench", kind=VERSION, target_snapshot=None):
        if confirm_live is not True:
            raise RegistryError("必须明确确认本次最多8次主动请求")
        if model not in {"gpt-6-astra", "gpt-6.1-sol"} or protocol != "responses":
            raise RegistryError("首版三方法套餐支持 Astra / 6.1 Sol 的 Responses、low 条件")
        service._principal(principal); service._identity(idempotency_key)
        if kind not in {VERSION, "nerfed-api-v1"}:
            raise RegistryError("统一任务类型不受支持")
        if principal == "monitor":
            from .monitor_adapter import resolve_monitor_target
            target = resolve_monitor_target({"target_snapshot": target_snapshot})
            if not isinstance(target, ResolvedTarget) or target.snapshot["registry_channel_id"] != registry_channel_id or target.snapshot["canonical_model"] != model or target.snapshot["protocol"] != protocol:
                raise RegistryError("Monitor 目标绑定失效")
        else:
            if target_snapshot is not None:
                raise RegistryError("工作台不能覆盖目标快照")
            target = self.target(registry_channel_id, model, protocol)
        manifests, _, requests = self._layout(model, kind)
        strategy = {"strategy_id": kind, "version": VERSION, "expected_model": model,
                    **{m + "_hash": manifest.manifest_hash for m, manifest in manifests.items()}}
        limits = {"max_requests": len(requests), "max_input_tokens": sum(r.input_tokens_reserved for r in requests),
                  "max_output_tokens": sum(r.output_tokens_reserved for r in requests)}
        key = "unified:" + canonical_hash({"principal": principal, "key": idempotency_key})
        with service._lock:
            previous = self.store.job_by_key(key)
            if previous:
                if previous["target_snapshot"] != target.snapshot or previous["strategy"] != strategy:
                    raise Conflict("同一幂等键不能改变目标、条件或版本")
                return self.get(previous["job_id"], principal=principal)
            job = self.store.enqueue(idempotency_key=key, target_snapshot=target.snapshot, strategy=strategy,
                requests=requests, limits=limits, deadline=time.time() + 600,
                pricing=default_pricing(model), principal=principal, budget_scope="review",
                budget_key=key, plan_version=canonical_hash(strategy),
                daily_limits=limits)
        service.wake_executor()
        return self.get(job["job_id"], principal=principal)

    def _owned(self, job_id, principal, administrative=False):
        service._principal(principal)
        job = self.store.public(job_id)
        if job["strategy"].get("strategy_id") not in {VERSION, "nerfed-api-v1"} or (
                job["principal"] != principal and not (administrative and principal == "workbench")):
            raise KeyError("统一任务不存在")
        return job

    def _resolve(self, job):
        if job["principal"] == "monitor":
            from .monitor_adapter import resolve_monitor_target
            return resolve_monitor_target(job)
        old = job["target_snapshot"]
        try:
            target = self.target(old["registry_channel_id"], job["strategy"]["expected_model"], old["protocol"])
            return target if target.snapshot == old else None
        except RegistryError:
            return None

    def get(self, job_id, *, principal="workbench", administrative=False):
        job = self._owned(job_id, principal, administrative)
        model = job["strategy"]["expected_model"]
        manifests, unsupported, requests = self._layout(model, job["strategy"]["strategy_id"])
        reports = []
        with self.registry.connect() as conn:
            attempts = [dict(r) for r in conn.execute("SELECT * FROM integrity_attempts WHERE job_id=?", (job_id,))]
        health = next((r for r in job["results"] if r["request_id"].startswith("health:")), None)
        for method, manifest in manifests.items():
            if method == "health":
                continue
            actual = [a for a in attempts if a["request_id"].startswith(method + ":")]
            outputs = [r for r in job["results"] if r["request_id"].startswith(method + ":")]
            unknown = sum(a["status"] == "unknown" for a in actual)
            not_run = manifest.max_requests - len(actual)
            reason = "model_not_in_bank" if method in unsupported else "" if not not_run else (
                "health_failed_or_unmeasured" if (health and not health["valid"]) or any(a["request_id"].startswith("health:") and a["status"] == "unknown" for a in attempts)
                else job["reason"] or "pending")
            conditions = {"provider": "registry:" + str(job["target_snapshot"]["registry_channel_id"]),
                          "model": model, "protocol": job["target_snapshot"]["protocol"],
                          "connection_fingerprint": job["target_snapshot"]["connection_fingerprint"],
                          "effort": "low", "stream": False, "retry": 0,
                          "max_output_tokens": manifest.max_output_tokens}
            try:
                score = score_strategy(manifest, outputs, expected_model=model, conditions=conditions)
            except ValueError:
                score = {"status": "unknown", "reason": "scorer_or_asset_changed", "identity_authenticated": False}
            status = "unsupported" if method in unsupported else "unknown" if unknown else "skipped" if reason == "health_failed_or_unmeasured" else "not_run" if not actual else "completed" if len(outputs) == manifest.max_requests else "incomplete"
            reports.append({"method": method, "label": {"traceone":"TraceOne", "modeltrace":"ModelTrace", "nerfed-api":"is-gpt-nerfed (API)"}[method],
                "version": manifest.version, "status": status, "reason": reason, "planned": manifest.max_requests,
                "attempted": len(actual), "valid": sum(bool(o["valid"]) for o in outputs),
                "invalid": sum(not o["valid"] for o in outputs), "unknown": unknown, "not_run": not_run,
                "duration_ms": sum(o.get("duration_ms") or 0 for o in outputs),
                "usage": {k: sum(o[k] for o in outputs) if outputs and all(o.get(k) is not None for o in outputs) else None
                          for k in ("input_tokens_reported", "output_tokens_reported", "reasoning_tokens_reported")},
                "calibration_status": "unvalidated", "metadata_status": "unavailable",
                "score": score, "conditions": conditions,
                "limits": {"max_requests": manifest.max_requests, "max_retries": 0,
                           "request_timeout_seconds": manifest.request_timeout_seconds},
                "limitation": "普通API行为线索，未校准；相关数字指纹不能多票认证身份或实际权重。"})
        return {**job, "task_id": job_id, "reports": reports, "health": health,
                "executor": service.executor_status(), "task_version": job["strategy"]["strategy_id"]}

    def list(self, *, principal="workbench", administrative=False):
        return [self.get(j["job_id"], principal=principal, administrative=administrative)
                for j in self.store.list_jobs(principal=None if administrative and principal == "workbench" else principal)
                if j["strategy"].get("strategy_id") in {VERSION, "nerfed-api-v1"}]

    def cancel(self, job_id, **kwargs):
        self._owned(job_id, **kwargs)
        self.store.cancel(job_id)
        service.wake_executor()
        return self.get(job_id, **kwargs)

    def resume(self, job_id, **kwargs):
        job = self._owned(job_id, **kwargs)
        manifests, _, _ = self._layout(job["strategy"]["expected_model"], job["strategy"]["strategy_id"])
        if self._resolve(job) is None or any(job["strategy"][m + "_hash"] != manifest.manifest_hash for m, manifest in manifests.items()):
            raise RegistryError("目标或策略发生变化，不能恢复原任务")
        self.store.resume(job_id)
        service.wake_executor()
        return self.get(job_id, **kwargs)

    async def run_pending(self, send=None):
        if not service.executor_enabled():
            return 0
        if send is None:
            from .transport import send_probe
            send = send_probe
        count = 0
        self.store.recover_expired_leases()
        for job in self.list(administrative=True):
            if job["status"] != "queued":
                continue
            claimed = self.store.claim(job_id=job["job_id"], owner=service.EXECUTOR_ID)
            if not claimed:
                continue
            session = self.store.session(job["job_id"], claimed["owner"])
            try:
                manifests, _, requests = self._layout(job["strategy"]["expected_model"], job["strategy"]["strategy_id"])
                if any(job["strategy"][m + "_hash"] != manifest.manifest_hash for m, manifest in manifests.items()):
                    session.finish("rejected", "strategy_changed", [])
                    continue
                def project(request, raw, started, finished):
                    manifest = manifests[request.request_id.split(":", 1)[0]]
                    return {**project_observation(manifest, request.probe["id"], raw),
                            "status": raw.get("status", "invalid_response"),
                            **{k: raw.get(k) for k in ("input_tokens_reported", "output_tokens_reported", "reasoning_tokens_reported")},
                            "duration_ms": max(0, (finished - started) * 1000)}
                def skip(request, outputs):
                    if request.request_id.startswith("health:"):
                        return None
                    health = next((r for r in outputs if r["request_id"].startswith("health:")), None)
                    return None if health and health["valid"] else "health_failed_or_unmeasured"
                await execute_requests(session, requests, lambda: self._resolve(job), send, project, skip_request=skip)
            except asyncio.CancelledError:
                raise
            except Exception:
                session.finish("failed", "unified_executor_error", [])
            count += 1
        return count


def get_service():
    return UnifiedService(get_registry())
