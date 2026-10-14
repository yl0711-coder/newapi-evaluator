from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import time

import httpx

from features.integrity.durable import IntegrityStore
from features.integrity.execution import ExecutionStopped, build_requests, execute_requests, resolve_registry_target
from features.integrity.scoring import project_observation, score_strategy
from features.integrity.strategies import get_strategy
from features.integrity.transport import send_probe
from features.model_coverage.catalog import Catalog, validate_protocol
from features.token_usage.service import get_service as get_token_service
from shared.feishu_notify import send_text, validate_webhook
from shared.registry import get_registry, RegistryError, Conflict
from . import domain
from .reporting import notification_parts
from .storage import Store


class DailyChecks:
    def __init__(self, registry, *, token_service=None, sender=send_probe, notification_transport=None, clock=time.time):
        self.registry = registry
        self.catalog = Catalog(registry)
        self.store = Store(registry)
        self.integrity = IntegrityStore(registry)
        self.token = token_service or get_token_service()
        self.sender, self.clock = sender, clock
        self.notification_transport = notification_transport
        self.worker = self.notification = None
        self.started = False
        self.last_error = ""
        self.last_prune = None

    def _profiles(self, profiles):
        models = {m["model"]: m for m in self.catalog.models()}
        clean = []
        for row in profiles:
            family, model, protocol = row["family"], row["model"], row["protocol"]
            if family not in {"GPT", "Claude"} or model not in models or family not in domain.families(models[model]["family"]):
                raise RegistryError("请选择模型目录中对应 GPT / Claude 家族的确切模型")
            if any(r["family"] == family for r in clean):
                raise RegistryError("每个模型家族只能配置一次")
            validate_protocol(protocol, model)
            reference_id = row.get("reference_channel_id")
            if type(reference_id) is not int or reference_id < 1:
                raise RegistryError("请为每个已配置模型选择可信参照渠道")
            resolve_registry_target(self.registry, reference_id, model, protocol)
            clean.append({"family": family, "model": model, "protocol": protocol, "reference_channel_id": reference_id})
        return sorted(clean, key=lambda r: r["family"])

    def _inventory(self, profiles):
        selected = {p["family"]: p for p in profiles}
        items = []
        channels = self.registry.list()
        secrets = self._display_secrets(channels)
        for channel in channels:
            if channel["status"] != "online":
                continue
            for family in domain.families(channel["scope"]) or ["未识别"]:
                profile = selected.get(family)
                reason = ""
                if not channel["enabled"]:
                    reason = "channel_disabled"
                elif channel["credential_status"] != "available":
                    reason = "credential_unavailable"
                elif family == "未识别":
                    reason = "family_unrecognized"
                elif not profile:
                    reason = "family_not_configured"
                item = {"channel_id": channel["id"], "channel_name": domain.display(channel["name"], secrets),
                        "multiplier": channel["multiplier"], "family": family, "model": profile["model"] if profile else "",
                        "protocol": profile["protocol"] if profile else "", "target": None,
                        "profile": profile, "mt_reason": reason, "usage_reason": reason}
                if not reason:
                    try:
                        target = resolve_registry_target(self.registry, channel["id"], profile["model"], profile["protocol"], require_online=True)
                        item["target"] = target.snapshot
                    except (KeyError, RegistryError):
                        item.update(mt_reason="target_unavailable", usage_reason="target_unavailable")
                    if channel["id"] == profile["reference_channel_id"]:
                        item["usage_reason"] = "reference_is_candidate"
                items.append(item)
        return sorted(items, key=lambda r: (r["channel_name"].casefold(), r["channel_id"], r["family"]))

    def _display_secrets(self, channels=None):
        secrets = []
        channels = self.registry.list() if channels is None else channels
        for row in channels:
            if row["credential_status"] == "available":
                try:
                    secrets.append(self.registry.get(row["id"], secret=True)["api_key"])
                except (KeyError, RegistryError):
                    pass
        return secrets

    def preview(self, profiles, *, notify=True):
        clean = self._profiles(profiles)
        items = self._inventory(clean)
        for item in items:
            if item["usage_reason"]:
                continue
            try:
                reference = resolve_registry_target(self.registry, item["profile"]["reference_channel_id"], item["model"], item["protocol"])
                if reference.snapshot["model"] != item["target"]["model"]:
                    item["usage_reason"] = "paired_conditions_mismatch"
            except (KeyError, RegistryError):
                item["usage_reason"] = "trusted_reference_missing"
        initial = sum((0 if i["mt_reason"] else 3) + (0 if i["usage_reason"] else 2) for i in items)
        maximum = sum((0 if i["mt_reason"] else 3) + (0 if i["usage_reason"] else 4) for i in items)
        public = [{k: i[k] for k in ("channel_id", "channel_name", "family", "model", "protocol", "multiplier", "mt_reason", "usage_reason")} for i in items]
        return {**domain.contract(), "channels": public, "online_channels": len({i["channel_id"] for i in items}),
                "initial_requests_per_batch": initial, "max_requests_per_batch": maximum,
                "initial_requests_per_day": initial * 4, "max_requests_per_day": maximum * 4,
                "fingerprint": domain.fingerprint({"profiles": clean, "items": items, "notify": notify,
                    "references": [resolve_registry_target(self.registry, p["reference_channel_id"], p["model"], p["protocol"]).snapshot for p in clean]}),
                "window_capacity_guaranteed": False}

    def save(self, profiles, *, enabled=False, notify=True, confirm_live=False, confirm_notify=False,
             confirm_trusted=False, preview_fingerprint=None):
        clean = self._profiles(profiles)
        preview = self.preview(clean, notify=notify)
        if enabled:
            if not clean or confirm_live is not True:
                raise RegistryError("启用须配置模型并明确确认定时真实请求")
            if notify and confirm_notify is not True:
                raise RegistryError("启用飞书推送须明确确认发送报告")
            if preview_fingerprint != preview["fingerprint"]:
                raise Conflict("预览已变化，请重新预览后启用")
            if notify and not self.webhook():
                raise RegistryError("请先在页面保存飞书机器人 Webhook")
        prepared = []
        for profile in clean:
            reference = resolve_registry_target(self.registry, profile["reference_channel_id"], profile["model"], profile["protocol"])
            try:
                binding = self.token.store.binding(profile["model"], profile["protocol"])
            except KeyError:
                binding = None
            if binding is None or binding["snapshot"] != reference.snapshot:
                if confirm_trusted is not True:
                    raise RegistryError("请明确确认所选渠道为该模型和协议的可信参照")
                binding = None
            prepared.append((profile, binding))
        frozen = []
        for profile, binding in prepared:
            if binding is None:
                binding = self.token.save_binding(profile["model"], profile["protocol"], profile["reference_channel_id"], confirm_trusted=True)
            frozen.append({**profile, "binding_revision": binding["revision"]})
        spec = {"version": domain.VERSION, "profiles": frozen, "notify": notify}
        return self.store.save_plan(spec, enabled, self.clock())

    @staticmethod
    def webhook():
        from features.stability.app import storage, scheduler
        value = storage.get_setting(scheduler.WEBHOOK_KEY)
        return validate_webhook(value) if value else ""

    def _layout(self, item):
        manifest = get_strategy("modeltrace")
        config = {"reasoning_effort": None if item["protocol"] == "anthropic" else "low"}
        return manifest, build_requests(manifest, config, prefix="modeltrace:")

    def _guard(self, db, batch_id):
        row = db.execute("SELECT b.status,b.report_at,b.revision,p.enabled,p.revision AS current_revision FROM daily_check_batches b LEFT JOIN daily_check_plan p ON p.id=1 WHERE b.id=?", (batch_id,)).fetchone()
        if row is None or row["status"] != "collecting":
            raise ExecutionStopped("cancelled", "batch_cancelled")
        if self.clock() >= row["report_at"]:
            raise ExecutionStopped("expired", "report_deadline")
        if not row["enabled"] or row["revision"] != row["current_revision"]:
            raise ExecutionStopped("cancelled", "plan_changed")

    def _resolve_item(self, item, *, conn=None):
        target = resolve_registry_target(self.registry, item["channel_id"], item["model"], item["protocol"], require_online=True, conn=conn)
        if target.snapshot != item["target"]:
            raise RegistryError("target_changed")
        return target

    @contextmanager
    def _token_permit(self, item, spec):
        # Lock order Registry -> Token matches plan cancellation. Keep the final
        # online/plan/connection check and the paired permit under this fence.
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._guard(db, item["batch_id"])
            candidate = self._resolve_item(item, conn=db)
            reference = resolve_registry_target(self.registry, spec["reference"]["registry_channel_id"], item["model"], item["protocol"], conn=db)
            if ({k: v for k, v in candidate.snapshot.items() if k != "registry_status"} != spec["candidate"]
                    or reference.snapshot != spec["reference"]):
                raise RegistryError("target_or_reference_changed")
            binding = self.token.store.binding(item["model"], item["protocol"])
            if binding["revision"] != item["profile"]["binding_revision"]:
                raise RegistryError("reference_changed")
            yield

    def _enqueue_mt(self, item, batch):
        manifest, requests = self._layout(item)
        key = f"daily-mt:{batch['id']}:{item['channel_id']}:{item['family']}"
        job = self.integrity.job_by_key(key)
        if job is None:
            self._resolve_item(item)
            job = self.integrity.enqueue(idempotency_key=key, target_snapshot=item["target"],
                strategy={"strategy_id": domain.VERSION, "manifest_hash": manifest.manifest_hash}, requests=requests,
                limits={"max_requests": 3, "max_input_tokens": sum(r.input_tokens_reserved for r in requests),
                        "max_output_tokens": sum(r.output_tokens_reserved for r in requests)},
                deadline=batch["report_at"], not_before=batch["start_at"], budget_key=batch["id"],
                principal="daily", plan_version=batch["revision"])
        self.store.update_item(item, "mt", job_id=job["job_id"])
        return job, manifest, requests

    async def _run_mt(self, item, batch):
        job, manifest, requests = self._enqueue_mt(item, batch)
        if job["strategy"]["manifest_hash"] != manifest.manifest_hash:
            self.store.update_item(item, "mt", reason="strategy_changed")
            self.integrity.cancel(job["job_id"])
            return
        if (job["status"] in {"partially_completed", "failed"} and job["reason"] == "executor_stopped"
                and not job["cancel_requested"] and self.clock() < batch["report_at"]):
            self.integrity.resume(job["job_id"])
        claimed = self.integrity.claim(job["job_id"], owner="daily-checks")
        if claimed is None:
            return
        session = self.integrity.session(job["job_id"], claimed["owner"], reserve_guard=lambda db, *_: self._guard(db, batch["id"]))
        def project(request, raw, started, finished):
            return {**project_observation(manifest, request.probe["id"], raw), "status": raw.get("status", "invalid_response"),
                    **{k: raw.get(k) for k in ("latency_ms", "input_tokens_reported", "output_tokens_reported", "reasoning_tokens_reported")},
                    "started_at": started, "finished_at": finished}
        await execute_requests(session, requests, lambda: self._resolve_item(item), self.sender, project, record_connect_failure=True)

    async def _run_usage(self, item, batch):
        binding = self.token.store.binding(item["model"], item["protocol"])
        if binding["revision"] != item["profile"]["binding_revision"]:
            raise RegistryError("reference_changed")
        display_keys = self._display_secrets()
        names = {"candidate": item["channel_name"], "reference": domain.display(
            self.registry.get(item["profile"]["reference_channel_id"])["name"], display_keys)}
        job = self.token.submit(item["channel_id"], item["model"], item["protocol"], confirm_live=True,
                                idempotency_key=f"{batch['id']}:{item['channel_id']}:{item['family']}",
                                principal="daily:" + batch["id"], reference_channel_id=item["profile"]["reference_channel_id"],
                                occurrence=batch["start_at"], display_names=names)
        self.store.update_item(item, "usage", job_id=job["job_id"])
        previous_due = domain.previous_start(batch["start_at"])
        with self.registry.connect() as db:
            old = db.execute("SELECT i.usage_job_id FROM daily_check_items i JOIN daily_check_batches b ON b.id=i.batch_id WHERE b.start_at=? AND i.channel_id=? AND i.family=?", (previous_due, item["channel_id"], item["family"])).fetchone()
        previous = self.token.store.get(old[0], administrative=True)["result"] if old and old[0] else None
        await self.token.execute_one(job_id=job["job_id"], permit_context=lambda spec: self._token_permit(item, spec),
                                     comparison_previous=previous, previous_occurrence=previous_due)

    async def execute(self):
        for batch in reversed(self.store.batches(collecting=True)):
            for item in self.store.items(batch["id"]):
                with self.registry.connect() as db:
                    try:
                        self._guard(db, batch["id"])
                    except ExecutionStopped:
                        return
                for method in ("mt", "usage"):
                    if item[method + "_reason"]:
                        continue
                    try:
                        if method == "mt":
                            await self._run_mt(item, batch)
                        else:
                            await self._run_usage(item, batch)
                    except asyncio.CancelledError:
                        raise
                    except (KeyError, ValueError, RuntimeError):
                        self.store.update_item(item, method, reason="execution_failed")
                    await asyncio.sleep(0)

    def report(self, batch_id):
        batch = self.store.batch(batch_id)
        if batch["report"] is not None:
            return batch["report"]
        rows = []
        display_secrets = self._display_secrets()
        for item in self.store.items(batch_id):
            mt_job = self.integrity.job(item["mt_job_id"]) if item["mt_job_id"] else None
            mt_outputs = mt_job["results"] if mt_job else []
            unknown = mt_job["consumed"]["unknown_requests"] if mt_job else 0
            mt_observations = [*mt_outputs, *({"status": "unknown"} for _ in range(unknown))]
            score = {}
            if mt_job:
                try:
                    score = score_strategy(get_strategy("modeltrace"), mt_outputs,
                                           expected_model=domain.MT_ALIASES.get(item["model"], item["model"]))
                except (ValueError, KeyError):
                    score = {"status": "unknown", "reason": "strategy_changed"}
            mt = {"label": domain.identity_verdict(score, mt_job["status"] if mt_job else "not_run", unknown, 3, len(mt_outputs)),
                  "job_id": item["mt_job_id"], "status": mt_job["status"] if mt_job else "not_run",
                  "reason": item["mt_reason"] or (mt_job["reason"] if mt_job else "pending"),
                  "score": score, "calibration_status": "unvalidated", "identity_authenticated": False}
            usage_job = self.token.store.get(item["usage_job_id"], administrative=True) if item["usage_job_id"] else None
            result = usage_job["result"] if usage_job else None
            if result and result.get("display_identity"):
                result = {**result, "display_identity": {side: {**identity, "name": domain.display(identity.get("name"), display_secrets)}
                    for side, identity in result["display_identity"].items()}}
            usage = {"label": result["label"] if result else "输入 Token 用量无法判定",
                     "job_id": item["usage_job_id"], "status": usage_job["status"] if usage_job else "not_run",
                     "reason": item["usage_reason"] or (result.get("reason", "") if result else "pending"),
                     "conclusion": result.get("conclusion") if result else "unable_to_determine",
                     "billing_status": "unverified", "evidence": result}
            attempted = {side: [a["result"] or {"status": "unknown"} for a in usage_job["attempts"]
                                if a["step_id"].startswith("token:" + side + ":") and a["state"] not in {"not_sent"}]
                         if usage_job else [] for side in ("candidate", "reference")}
            usage_planned = 2 if result and result.get("severity") == "high_risk" else 1
            if item["usage_reason"] == "reference_is_candidate":
                usage_planned = 0
            fully_measured = (len(mt_outputs) == 3 and not unknown and all(o["status"] == "completed" for o in mt_outputs)
                              and (usage_planned == 0 or result is not None and result.get("conclusion") != "unable_to_determine"
                                   and usage_job["status"] == "completed"))
            rows.append({**{k: item[k] for k in ("channel_id", "channel_name", "family", "model", "protocol", "multiplier")},
                         "modeltrace": mt, "usage": usage, "fully_measured": fully_measured,
                         "performance": {"modeltrace": domain.performance(mt_observations, 3),
                                         **{"usage_" + side: domain.performance(attempted[side], usage_planned) for side in attempted}}})
        rows.sort(key=lambda r: (r["channel_name"].casefold(), r["channel_id"], r["family"]))
        return {"version": domain.VERSION, "batch_id": batch_id, "start_at": batch["start_at"], "report_at": batch["report_at"],
                "created_at": batch["created_at"], "timezone": domain.TIMEZONE, "status": batch["status"], "channels": rows,
                "coverage": {"rows": len(rows), "online_channels": len({r["channel_id"] for r in rows}),
                             "fully_measured_rows": sum(r["fully_measured"] for r in rows)},
                "inventory_source": "last_manual_registry_sync", "billing_status": "unverified", "identity_authenticated": False}

    async def _stop_batch(self, batch, reason):
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        for item in self.store.items(batch["id"]):
            for method in ("mt", "usage"):
                job_id = item[method + "_job_id"]
                if job_id:
                    if method == "mt":
                        self.integrity.cancel(job_id)
                    else:
                        self.token.cancel(job_id, administrative=True)
                if not item[method + "_reason"] and not job_id:
                    self.store.update_item(item, method, reason=reason)
        report = self.report(batch["id"])
        report["status"] = "finalized" if reason == "report_deadline" else "cancelled"
        report["finalized_at"] = self.clock()
        self.store.finish(batch["id"], report, notification_parts(report) if batch["spec"]["notify"] else [], self.clock(), status=report["status"])

    async def cancel(self, batch_id):
        batch = self.store.batch(batch_id)
        if batch["status"] == "collecting":
            await self._stop_batch(batch, "batch_cancelled")
        return self.detail(batch_id)

    def detail(self, batch_id):
        return {"report": self.report(batch_id), "notifications": self.store.messages(batch_id)}

    async def deliver(self):
        while row := self.store.claim_message(self.clock()):
            try:
                webhook = self.webhook()
                if not webhook:
                    self.store.message_result(row, "disabled", "webhook_missing")
                    continue
                await send_text(webhook, row["text"], transport=self.notification_transport)
            except asyncio.CancelledError:
                self.store.message_result(row, "unknown", "delivery_unconfirmed")
                raise
            except httpx.HTTPStatusError:
                self.store.message_result(row, "failed", "http_rejected")
            except httpx.HTTPError:
                self.store.message_result(row, "unknown", "delivery_unconfirmed")
            except (ValueError, KeyError):
                self.store.message_result(row, "failed", "delivery_rejected")
            else:
                self.store.message_result(row, "sent")
            await asyncio.sleep(.25)

    async def tick(self, now=None):
        now = self.clock() if now is None else now
        if not self.started:
            self.store.recover_notifications()
            self.started = True
        plan = self.store.plan()
        if plan["enabled"]:
            for slot in domain.slots(now):
                if not plan["updated_at"] <= slot["start_at"] <= now or self.store.has_batch(slot["report_at"]):
                    continue
                items = self._inventory(plan["profiles"])
                if now >= slot["report_at"]:
                    for item in items:
                        item.update(mt_reason=item["mt_reason"] or "start_window_missed", usage_reason=item["usage_reason"] or "start_window_missed")
                self.store.create_batch(slot, plan, items, now)
        for batch in self.store.batches(collecting=True):
            reason = "report_deadline" if now >= batch["report_at"] else "plan_changed" if not plan["enabled"] or plan["revision"] != batch["revision"] else None
            if reason:
                await self._stop_batch(batch, reason)
        if self.last_prune is None or now - self.last_prune >= 3600:
            from features.stability.app.config import RETENTION_DAYS
            self.store.prune(now - RETENTION_DAYS * 86400)
            self.last_prune = now
        for task in (self.worker, self.notification):
            if task is not None and task.done() and not task.cancelled() and task.exception() is not None:
                self.last_error = "execution_failed"
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self.execute(), name="daily-checks")
        if self.notification is None or self.notification.done():
            self.notification = asyncio.create_task(self.deliver(), name="daily-reports")

    async def stop(self):
        for task in (self.worker, self.notification):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.worker, self.notification) if t), return_exceptions=True)
        self.worker = self.notification = None
        self.started = False

    def status(self):
        return {"enabled": self.store.plan()["enabled"], "active": bool(self.worker and not self.worker.done()),
                "report_times": list(domain.REPORT_TIMES), "timezone": domain.TIMEZONE,
                "notification_active": bool(self.notification and not self.notification.done()), "last_error": self.last_error}


_service = None


def get_service():
    global _service
    registry = get_registry()
    if _service is None or _service.registry is not registry:
        _service = DailyChecks(registry)
    return _service
