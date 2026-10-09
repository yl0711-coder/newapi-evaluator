"""Explicit, reference-gated active reviews using the shared durable executor."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Callable

from shared.registry import Conflict, RegistryError, get_registry
from features.model_coverage.catalog import Catalog
from .durable import IntegrityStore, default_pricing
from .execution import ProbeRequest, ResolvedTarget, execute_requests, estimate_input_tokens
from .strategies import canonical_json

logger = logging.getLogger(__name__)
METHODS = {"hlwy", "kbf"}
COOLDOWN_SECONDS = 7 * 86400
TIMEZONE = "Asia/Shanghai"
_lock = threading.RLock()
_monitor_resolver: Callable | None = None
_executor_task: asyncio.Task | None = None
_wake: asyncio.Event | None = None
EXECUTOR_ID = "review-" + secrets.token_hex(8)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$")


def _json(value):
    return canonical_json(value)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or re.search(r"(?i)https?://|sk-|bearer|api_key|token=", value):
        raise RegistryError("请使用安全标识，不要填写凭据、URL 或日志正文")
    return value


def _principal(value):
    if value not in {"workbench", "monitor"}:
        raise RegistryError("未知调用身份")
    return value


def _apis(method):
    if method == "kbf":
        from .reference import (validate_reference_package, reference_conditions, build_reference_strategy,
                                project_reference_observation, score_reference)
        return validate_reference_package, reference_conditions, build_reference_strategy, project_reference_observation, score_reference
    if method == "hlwy":
        from .hlwy import (validate_hlwy_reference, hlwy_conditions, build_hlwy_strategy,
                           project_hlwy_observation, score_hlwy)
        return validate_hlwy_reference, hlwy_conditions, build_hlwy_strategy, project_hlwy_observation, score_hlwy
    raise RegistryError("仅支持 HLwY 或 KBF 主动复核")


class ReviewService:
    def __init__(self, registry):
        self.registry = registry
        self.store = IntegrityStore(registry)
        with registry.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS integrity_review_references (
                package_hash TEXT PRIMARY KEY, method TEXT NOT NULL, package_json TEXT NOT NULL, created_at REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS integrity_reference_access (
                package_hash TEXT NOT NULL, principal TEXT NOT NULL, PRIMARY KEY(package_hash,principal))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS integrity_reviews (
                review_key TEXT PRIMARY KEY, submission_hash TEXT NOT NULL, cooldown_key TEXT NOT NULL,
                job_id TEXT NOT NULL DEFAULT '', principal TEXT NOT NULL, payload_json TEXT NOT NULL,
                created_at REAL NOT NULL)""")

    def import_reference(self, package, expected_sha256, *, principal="workbench", confirm_authorized=False):
        _principal(principal)
        if confirm_authorized is not True:
            raise RegistryError("请确认拥有该参考资产的使用权")
        if not isinstance(package, dict) or len(_json(package).encode()) > 2_000_000:
            raise RegistryError("参考 JSON 无效或超过 2 MB")
        schema = package.get("schema", package.get("schema_version", ""))
        method = "hlwy" if schema == "integrity-hlwy-reference/v1" else "kbf" if schema == "integrity-kbf-reference/v1" else ""
        if not method:
            raise RegistryError("参考 schema 不受支持")
        try:
            clean = _apis(method)[0](package, expected_sha256=expected_sha256)
        except ValueError as exc:
            raise RegistryError(str(exc)) from None
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT package_json FROM integrity_review_references WHERE package_hash=?", (expected_sha256,)).fetchone()
            if row and row[0] != _json(clean):
                raise Conflict("参考哈希已绑定其他内容")
            conn.execute("INSERT OR IGNORE INTO integrity_review_references VALUES(?,?,?,?)", (expected_sha256, method, _json(clean), time.time()))
            conn.execute("INSERT OR IGNORE INTO integrity_reference_access VALUES(?,?)", (expected_sha256, principal))
        return self.reference_metadata(expected_sha256, principal=principal)

    def _reference(self, package_hash, principal):
        with self.registry.connect() as conn:
            row = conn.execute("""SELECT r.* FROM integrity_review_references r JOIN integrity_reference_access a
                ON a.package_hash=r.package_hash WHERE r.package_hash=? AND a.principal=?""", (package_hash, principal)).fetchone()
        if row is None:
            raise RegistryError("缺少可用且获授权的参考；未发出任何上游请求")
        package = _apis(row["method"])[0](json.loads(row["package_json"]), expected_sha256=package_hash)
        return row["method"], package

    def reference_metadata(self, package_hash, *, principal="workbench"):
        method, package = self._reference(package_hash, principal)
        conditions = _apis(method)[1](package)
        return {"reference_hash": package_hash, "strategy_id": method, "reference_model": package["reference_model"],
                "conditions": conditions, "has_self_test": bool(package.get("self_test")),
                "sample_count": len(package.get("probes", package.get("observations", [])))}

    def references(self, *, principal="workbench"):
        with self.registry.connect() as conn:
            hashes = [r[0] for r in conn.execute("SELECT package_hash FROM integrity_reference_access WHERE principal=?", (_principal(principal),))]
        return [self.reference_metadata(h, principal=principal) for h in hashes]

    def choices(self):
        catalog = Catalog(self.registry)
        models = catalog.models()
        channels = []
        for public in self.registry.list():
            available, reason = False, "渠道已停用"
            if public["enabled"]:
                try:
                    self.registry.resolve(public["id"])
                    available, reason = True, ""
                except RegistryError:
                    reason = "凭据不可解密"
            bindings = [catalog.binding(public["id"], m["id"]) for m in models]
            channels.append({"id": public["id"], "name": public["name"], "status": public["status"],
                             "available": available, "reason": reason,
                             "models": [{"model": m["model"], "upstream_model": m["upstream_model"], "protocol": m["request_protocol"]} for m in bindings]})
        return channels

    def _target(self, channel_id, model, protocol):
        from .execution import resolve_registry_target
        return resolve_registry_target(self.registry, channel_id, model, protocol)

    def enqueue(self, *, principal, registry_channel_id, model, protocol, strategy_id, reference_hash,
                source_ref, incident_id="", idempotency_key, limits, budget_seconds, confirm_live,
                pricing=None, target_snapshot=None, conditions=None):
        _principal(principal)
        if confirm_live is not True:
            raise RegistryError("必须明确确认本次主动请求")
        _identity(source_ref); _identity(idempotency_key)
        incident_id = _identity(incident_id or source_ref)
        method, package = self._reference(reference_hash, principal)
        if method != strategy_id:
            raise RegistryError("参考与所选复核方法不一致")
        reference_conditions = _apis(method)[1](package)
        if conditions is None or _json(conditions) != _json(reference_conditions):
            raise RegistryError("条件漂移：provider、模型、协议、参数或预算与参考不一致；未发送请求")
        if conditions.get("protocol") != protocol or conditions.get("model") != model:
            raise RegistryError("目标模型或协议与参考条件不一致；未发送请求")
        target = self._target(registry_channel_id, model, protocol)
        if principal == "monitor":
            if target_snapshot is None or _monitor_resolver is None:
                raise RegistryError("Monitor 目标缺少服务端 Registry 快照")
            metadata = {"target_snapshot": target_snapshot, "model": model, "protocol": protocol,
                        "registry_channel_id": registry_channel_id, "principal": principal}
            target = _monitor_resolver(metadata)
            if not isinstance(target, ResolvedTarget) or target.snapshot.get("canonical_model") != model or target.snapshot["protocol"] != protocol or target.snapshot["registry_channel_id"] != registry_channel_id:
                raise RegistryError("Monitor 目标绑定或凭据失效")
        elif target_snapshot is not None:
            raise RegistryError("工作台不能覆盖服务端目标快照")
        manifest, _ = self._requests(package, method)
        if not isinstance(limits, dict) or type(budget_seconds) is not int or not 1 <= budget_seconds <= manifest.total_timeout_seconds:
            raise RegistryError("复核时间预算超出该策略上限")
        if not 1 <= limits.get("max_requests", 0) <= manifest.max_requests:
            raise RegistryError("请求预算超出策略上限")
        if any(type(limits.get(k)) is not int or limits[k] < 1 for k in ("max_requests", "max_input_tokens", "max_output_tokens")):
            raise RegistryError("请输入正整数请求与 token 预算")
        if set(limits) != {"max_requests", "max_input_tokens", "max_output_tokens"}:
            raise RegistryError("预算字段不完整")
        actual_budget = {**{k: limits[k] for k in ("max_requests", "max_input_tokens", "max_output_tokens")},
                         "total_timeout_seconds": budget_seconds}
        if actual_budget != reference_conditions["budget"]:
            raise RegistryError("条件漂移：实际请求、token 或时间预算与参考不一致；未发送请求")
        if pricing is None:
            try:
                pricing = default_pricing(model)
            except ValueError:
                pricing = None
        submission = {"principal": principal, "model": model, "protocol": protocol, "strategy_id": method,
                      "reference_hash": reference_hash, "source_ref": source_ref, "incident_id": incident_id,
                      "idempotency_key": idempotency_key, "conditions": conditions, "target_snapshot": target.snapshot,
                      "registry_channel_id": registry_channel_id, "limits": limits, "budget_seconds": budget_seconds,
                      "pricing": pricing}
        submission_hash = _hash(submission)
        cooldown_key = _hash({k: submission[k] for k in ("principal", "model", "protocol", "strategy_id", "reference_hash", "conditions", "target_snapshot")})
        review_key = "review:" + _hash({k: submission[k] for k in ("principal", "incident_id", "source_ref", "strategy_id", "reference_hash", "target_snapshot")})
        now = time.time()
        with _lock:
            with self.registry.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                key_match = conn.execute("SELECT * FROM integrity_reviews WHERE principal=? AND json_extract(payload_json,'$.idempotency_key')=?", (principal, idempotency_key)).fetchone()
                if key_match and key_match["submission_hash"] != submission_hash:
                    raise Conflict("同一幂等键不能改变参考、目标、条件或预算")
                row = conn.execute("SELECT * FROM integrity_reviews WHERE review_key=?", (review_key,)).fetchone()
                if row:
                    previous = json.loads(row["payload_json"])
                    if row["submission_hash"] != submission_hash and previous["idempotency_key"] == idempotency_key:
                        raise Conflict("同一幂等键不能改变参考、目标、条件或预算")
                else:
                    row = conn.execute("SELECT * FROM integrity_reviews WHERE principal=? AND cooldown_key=? AND created_at>? ORDER BY created_at DESC LIMIT 1", (principal, cooldown_key, now - COOLDOWN_SECONDS)).fetchone()
                if row:
                    payload = json.loads(row["payload_json"])
                    review_key = row["review_key"]
                else:
                    payload = {**submission, "created_at": now, "deadline": now + budget_seconds,
                               "budget_date": datetime.fromtimestamp(now, ZoneInfo(TIMEZONE)).date().isoformat(),
                               "manifest_hash": manifest.manifest_hash}
                    conn.execute("INSERT INTO integrity_reviews VALUES(?,?,?,'',?,?,?)", (review_key, submission_hash, cooldown_key, principal, _json(payload), now))
            result = self._ensure_job(review_key, payload)
        wake_executor()
        return self.get(result["job_id"], principal=principal)

    def _requests(self, package, method):
        manifest = _apis(method)[2](package, package["budget"]["max_requests"]) if method == "hlwy" else _apis(method)[2](package)
        parameters = package["parameters"]
        requests = []
        for spec in manifest.probes:
            probe = {"id": spec.probe_id, "name": spec.probe_id, "prompt": spec.prompt,
                     "system_prompt": parameters.get("system_prompt", ""), "stream": False,
                     "max_tokens": spec.max_output_tokens, "request_timeout_seconds": manifest.request_timeout_seconds}
            if parameters.get("wrapper"):
                raise RegistryError("当前执行器仅支持空 wrapper；未发送请求")
            sampling = parameters.get("sampling", {})
            if set(sampling) - {"temperature", "top_p", "seed", "provider", "anti_target"} or sampling.get("anti_target", False):
                raise RegistryError("当前执行器不支持该采样参数；未发送请求")
            for key in ("temperature", "top_p", "seed", "provider"):
                if key in sampling:
                    probe[key] = sampling[key]
            if parameters.get("thinking") not in {"", "none", "disabled"}:
                if package["protocol"] == "anthropic":
                    raise RegistryError("Messages 执行器不支持该 reasoning effort；未发送请求")
                probe["reasoning_effort"] = parameters["thinking"]
            elif parameters.get("thinking") in {"none", "disabled"}:
                probe["thinking"] = parameters["thinking"]
            from .transport import payload
            payload({"protocol": package["protocol"], "model": package["reference_model"]}, probe)
            requests.append(ProbeRequest(spec.probe_id, probe, estimate_input_tokens(probe), spec.max_output_tokens))
        return manifest, requests

    def _ensure_job(self, review_key, payload):
        method, package = self._reference(payload["reference_hash"], payload["principal"])
        manifest, requests = self._requests(package, method)
        if manifest.manifest_hash != payload["manifest_hash"]:
            raise RegistryError("策略或参考版本漂移")
        job = self.store.enqueue(idempotency_key=review_key, target_snapshot=payload["target_snapshot"],
            strategy={"strategy_id": method, "manifest_hash": manifest.manifest_hash,
                      "reference_hash": payload["reference_hash"], "source_ref": payload["source_ref"]},
            requests=requests, limits=payload["limits"], deadline=payload["deadline"],
            pricing=payload["pricing"], budget_scope="review", budget_key="active-review", budget_date=payload["budget_date"],
            timezone=TIMEZONE, plan_version=manifest.manifest_hash,
            principal=payload["principal"])
        with self.registry.connect() as conn:
            conn.execute("UPDATE integrity_reviews SET job_id=? WHERE review_key=?", (job["job_id"], review_key))
        return job

    def _payload(self, job_id, principal, administrative=False):
        _principal(principal)
        with self.registry.connect() as conn:
            row = conn.execute("SELECT * FROM integrity_reviews WHERE job_id=?", (job_id,)).fetchone()
        if row is None or (row["principal"] != principal and not (administrative and principal == "workbench")):
            raise KeyError("复核任务不存在")
        return json.loads(row["payload_json"])

    def get(self, job_id, *, principal, administrative=False):
        payload = self._payload(job_id, principal, administrative)
        job = self.store.public(job_id)
        method, package = self._reference(payload["reference_hash"], payload["principal"])
        _, _, _, _, score = _apis(method)
        result = score(package, self.store.job(job_id)["results"], conditions=payload["conditions"])
        return {**job, "task_id": job_id, "principal": payload["principal"], "source_ref": payload["source_ref"],
                "incident_id": payload["incident_id"], "reference_hash": payload["reference_hash"],
                "strategy_id": method, "created_at": payload["created_at"], "report": result,
                "executor": executor_status(), "comparison_conditions": payload["conditions"],
                "next_step": "先排除条件漂移。HLwY 仍有疑点时可手动创建 KBF；结果不认证身份或权重。"}

    def list(self, *, principal=None, administrative=False):
        with self.registry.connect() as conn:
            rows = conn.execute("SELECT job_id,principal FROM integrity_reviews WHERE job_id<>'' ORDER BY created_at DESC LIMIT 100").fetchall()
        caller = principal or "workbench"
        return [self.get(row[0], principal=caller, administrative=administrative) for row in rows
                if row[1] == caller or (administrative and caller == "workbench")]

    def cancel(self, job_id, *, principal, administrative=False):
        self._payload(job_id, principal, administrative)
        self.store.cancel(job_id)
        wake_executor()
        return self.get(job_id, principal=principal, administrative=administrative)

    def resume(self, job_id, *, principal, administrative=False):
        payload = self._payload(job_id, principal, administrative)
        if self._resolve(payload) is None:
            raise RegistryError("目标连接、映射或生产身份已变化，不能恢复原任务")
        self._requests(self._reference(payload["reference_hash"], payload["principal"])[1], payload["strategy_id"])
        self.store.resume(job_id)
        wake_executor()
        return self.get(job_id, principal=principal, administrative=administrative)

    def _resolve(self, payload):
        if payload["principal"] == "monitor":
            target = _monitor_resolver(payload) if _monitor_resolver else None
        else:
            target = self._target(payload["registry_channel_id"], payload["model"], payload["protocol"])
        if not isinstance(target, ResolvedTarget) or target.snapshot != payload["target_snapshot"]:
            return None
        return target

    async def run_pending(self, send=None, *, limit=20):
        if not executor_enabled():
            return 0
        if send is None:
            from .transport import send_probe
            send = send_probe
        count = 0
        self.store.recover_expired_leases()
        with self.registry.connect() as conn:
            orphan = conn.execute("SELECT review_key,payload_json FROM integrity_reviews WHERE job_id=''").fetchall()
        for row in orphan:
            self._ensure_job(row[0], json.loads(row[1]))
        with self.registry.connect() as conn:
            pending = conn.execute("SELECT r.job_id FROM integrity_reviews r JOIN integrity_jobs j ON j.job_id=r.job_id WHERE j.status='queued' ORDER BY r.created_at LIMIT ?", (limit,)).fetchall()
        for row in pending:
            job = self.store.claim(job_id=row[0], owner=EXECUTOR_ID)
            if job is None:
                continue
            session = self.store.session(job["job_id"], job["owner"])
            try:
                payload = self._payload(job["job_id"], job["principal"])
                method, package = self._reference(payload["reference_hash"], payload["principal"])
                manifest, requests = self._requests(package, method)
                if manifest.manifest_hash != payload["manifest_hash"]:
                    session.finish("rejected", "reference_changed", [])
                    continue
                project = _apis(method)[3]
                def projection(request, raw, started, finished):
                    observed = project(package, request.probe["id"], raw)
                    return {**observed, "status": raw.get("status", "completed"),
                            "input_tokens_reported": raw.get("input_tokens_reported"),
                            "output_tokens_reported": raw.get("output_tokens_reported"),
                        "reasoning_tokens_reported": raw.get("reasoning_tokens_reported"),
                            "duration_ms": max(0, (finished - started) * 1000)}
                await execute_requests(session, requests, lambda: self._resolve(payload), send, projection)
            except asyncio.CancelledError:
                raise
            except Exception:
                session.finish("failed", "review_executor_error", [])
                logger.error("active review failed: review_executor_error")
            count += 1
        return count


def configure_monitor_resolver(resolver):
    global _monitor_resolver
    _monitor_resolver = resolver


def get_service():
    return ReviewService(get_registry())


def import_reference(*args, **kwargs):
    return get_service().import_reference(*args, **kwargs)


def enqueue_review(**kwargs):
    return get_service().enqueue(**kwargs)


def get_review(job_id, **kwargs):
    return get_service().get(job_id, **kwargs)


def list_reviews(**kwargs):
    return get_service().list(**kwargs)


def cancel_review(job_id, **kwargs):
    return get_service().cancel(job_id, **kwargs)


def resume_review(job_id, **kwargs):
    return get_service().resume(job_id, **kwargs)


def executor_enabled():
    return os.environ.get("EVAL_INTEGRITY_EXECUTOR", "off") == "live"


def executor_status():
    return {"enabled": executor_enabled(), "running": bool(_executor_task and not _executor_task.done()),
            "mode": "live" if executor_enabled() else "off", "trigger": "explicit_manual_or_authenticated_monitor"}


def wake_executor():
    if _wake is not None:
        _wake.set()


async def _loop():
    while True:
        try:
            await get_service().run_pending()
            from .unified import get_service as unified_service
            await unified_service().run_pending()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("active review consumer failed")
        try:
            await asyncio.wait_for(_wake.wait(), 5)
        except asyncio.TimeoutError:
            pass
        _wake.clear()


async def start_executor():
    global _executor_task, _wake
    if not executor_enabled():
        return False
    get_service().store.recover_expired_leases()
    if not _executor_task or _executor_task.done():
        _wake = asyncio.Event()
        _executor_task = asyncio.create_task(_loop(), name="integrity-active-review")
    return True


async def stop_executor():
    global _executor_task, _wake
    if _executor_task:
        _executor_task.cancel()
        await asyncio.gather(_executor_task, return_exceptions=True)
    _executor_task, _wake = None, None
