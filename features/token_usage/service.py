"""Trusted model bindings and independent scheduled/active paired executor."""
from __future__ import annotations

import asyncio
import json
import math
import os
import secrets
import time
from contextlib import nullcontext

from features.integrity.execution import resolve_registry_target
from features.model_coverage.catalog import Catalog, model_name
from shared.registry import Conflict, RegistryError, get_registry
from shared.redaction import scrub
from shared.scheduler_lock import scheduler_lock
from .domain import INTERVAL_SECONDS, VERSION, classify_pair, condition_hash, digest, empty_result, observation, protocol, thresholds
from .storage import ScheduleStopped, Store, encode
from .transport import run_paired_check


class TokenUsageService:
    def __init__(self, registry, *, upstream_transport=None, clock=time.time):
        self.registry, self.clock = registry, clock
        self.store = Store(registry.directory / "token-usage")
        self.upstream_transport = upstream_transport
        self.tasks = {}
        self.loop_task = None
        self.active = False
        self.owner = "token-" + secrets.token_hex(8)
        self.lock = None

    def reference_for(self, model, wire_protocol):
        model, kind = model_name(model), protocol(wire_protocol)
        binding = self.store.binding(model, kind)
        current = resolve_registry_target(self.registry, binding["snapshot"]["registry_channel_id"], model, kind)
        if current.snapshot != binding["snapshot"]:
            raise Conflict("trusted_reference_changed")
        current.channel["_token_thresholds"] = binding["thresholds"]
        return current

    def admission_binding_for(self, model, wire_protocol):
        resolved = self.reference_for(model, wire_protocol)
        binding = self.store.binding(model, protocol(wire_protocol))
        return {"resolved": resolved, "revision": binding["revision"], "thresholds": binding["thresholds"],
                "condition_hash": condition_hash(binding["thresholds"])}

    def save_binding(self, model, wire_protocol, reference_channel_id, *, confirm_trusted=False, threshold_config=None):
        if confirm_trusted is not True:
            raise ValueError("trusted_reference_confirmation_required")
        model, kind = model_name(model), protocol(wire_protocol)
        reference = resolve_registry_target(self.registry, reference_channel_id, model, kind)
        return self.store.save_binding(model, kind, reference.snapshot, thresholds(threshold_config))

    def choices(self):
        rows = self.registry.list()
        keys = []
        for row in rows:
            if row["credential_status"] == "available":
                try:
                    keys.append(self.registry.get(row["id"], secret=True)["api_key"])
                except (KeyError, RegistryError):
                    pass  # A concurrent removal/replacement does not break choices.
        return [{"id": row["id"], "name": scrub(row["name"], keys), "enabled": row["enabled"], "credential_status": row["credential_status"]}
                for row in rows]

    def _spec(self, channel_id, model, wire_protocol, *, source, log_evidence=None):
        kind, model = protocol(wire_protocol), model_name(model)
        reference = self.reference_for(model, kind)
        candidate = resolve_registry_target(self.registry, channel_id, model, kind)
        if channel_id == reference.snapshot["registry_channel_id"]:
            raise ValueError("candidate_and_reference_must_differ")
        if candidate.snapshot["model"] != reference.snapshot["model"]:
            raise ValueError("paired_upstream_model_mismatch")
        binding = self.store.binding(model, kind)
        spec = {"candidate": candidate.snapshot, "reference": reference.snapshot, "binding_revision": binding["revision"],
                "model": model, "protocol": kind, "source": source, "condition_hash": condition_hash(binding["thresholds"]), "thresholds": binding["thresholds"],
                "log_evidence": log_evidence}
        spec["frozen_hash"] = digest({k: spec[k] for k in ("candidate", "reference", "binding_revision", "condition_hash")})
        # Display names belong to the historical record, not connection identity.
        paired_secrets = [candidate.channel["api_key"], reference.channel["api_key"]]
        spec["display_identity"] = {side: {"name": scrub(resolved.channel["name"], paired_secrets), "registry_channel_id": resolved.snapshot["registry_channel_id"],
                                          "model": resolved.snapshot["canonical_model"], "protocol": kind}
                                    for side, resolved in (("candidate", candidate), ("reference", reference))}
        return spec

    def preview(self, channel_id, model, protocol):
        spec = self._spec(channel_id, model, protocol, source="fixed_retest")
        return {"detection_version": VERSION, "target": spec["candidate"], "reference": spec["reference"],
                "condition_hash": spec["condition_hash"], "thresholds": spec["thresholds"], "frozen_hash": spec["frozen_hash"],
                "display_identity": spec["display_identity"],
                "initial_requests": 2, "max_requests": 4, "max_retries": 0, "interval_seconds": INTERVAL_SECONDS,
                "billing_status": "unverified"}

    def submit(self, channel_id, model, protocol, *, confirm_live=False, idempotency_key,
               principal="workbench", event_id=None, log_evidence=None, reference_channel_id=None, occurrence=None,
               display_names=None):
        if confirm_live is not True:
            raise ValueError("live_confirmation_required")
        daily = principal.startswith("daily:")
        if daily and (type(occurrence) not in {int, float} or not math.isfinite(occurrence)):
            raise ValueError("daily_occurrence_required")
        if not daily and occurrence is not None:
            raise ValueError("occurrence_requires_daily_principal")
        if display_names is not None and (not daily or set(display_names) != {"candidate", "reference"}
                or any(not isinstance(n, str) or len(n) > 160 for n in display_names.values())):
            raise ValueError("invalid_daily_display_names")
        # Event idempotency binds the submitted public payload, independently of later Registry drift.
        payload = {"channel_id": channel_id, "model": model, "protocol": protocol,
                               "reference_channel_id": reference_channel_id, "log_evidence": log_evidence,
                               "event_id": event_id, "confirm_live": confirm_live}
        if daily:
            payload["occurrence"] = occurrence
        payload_hash = digest(payload)
        submitted = self.store.submitted(principal=principal, idem=idempotency_key, event_id=event_id, payload_hash=payload_hash)
        if submitted is not None:
            return submitted
        source = "daily_scheduled" if daily else "monitor_active" if principal.startswith("monitor:") else "fixed_retest"
        spec = self._spec(channel_id, model, protocol, source=source, log_evidence=log_evidence)
        if display_names is not None:
            for side, name in display_names.items():
                spec["display_identity"][side]["name"] = name
        if reference_channel_id is not None and reference_channel_id != spec["reference"]["registry_channel_id"]:
            raise Conflict("trusted_reference_mismatch")
        return self.store.enqueue(spec, principal=principal, idem=idempotency_key, event_id=event_id,
                                  payload_hash=payload_hash, occurrence=occurrence)

    def configs(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM configs ORDER BY id").fetchall()
        return [{"id": row["id"], "enabled": bool(row["enabled"]), "next_due": row["next_due"],
                 "interval_seconds": INTERVAL_SECONDS, **json.loads(row["spec"])} for row in rows]

    def save_config(self, channel_id, model, protocol, *, enabled=False, confirm_live=False, config_id=None):
        if enabled and confirm_live is not True:
            raise ValueError("schedule_confirmation_required")
        if config_id is not None and not enabled:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT spec FROM configs WHERE id=?", (config_id,)).fetchone()
                if row is None:
                    raise KeyError("config_not_found")
                frozen = json.loads(row["spec"])
                if (channel_id, model, protocol) == (frozen["candidate"]["registry_channel_id"], frozen["model"], frozen["protocol"]):
                    # Stopping a plan must remain possible after its connection,
                    # mapping or trusted reference has become invalid.
                    db.execute("UPDATE configs SET enabled=0 WHERE id=?", (config_id,))
                    return self._paused_config(db, config_id)
        spec = self._spec(channel_id, model, protocol, source="scheduled")
        next_due = (int(self.clock() // INTERVAL_SECONDS) + 1) * INTERVAL_SECONDS
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if config_id is None:
                cur = db.execute("INSERT INTO configs(spec,enabled,next_due,created_at) VALUES(?,?,?,?)",
                                 (encode(spec), int(enabled), next_due, self.clock()))
                config_id = cur.lastrowid
            else:
                if not db.execute("SELECT id FROM configs WHERE id=?", (config_id,)).fetchone():
                    raise KeyError("config_not_found")
                db.execute("UPDATE configs SET spec=?,enabled=?,next_due=? WHERE id=?", (encode(spec), int(enabled), next_due, config_id))
        return next(row for row in self.configs() if row["id"] == config_id)

    @staticmethod
    def _paused_config(db, config_id):
        row = db.execute("SELECT * FROM configs WHERE id=?", (config_id,)).fetchone()
        return {"id": row["id"], "enabled": False, "next_due": row["next_due"], "interval_seconds": INTERVAL_SECONDS, **json.loads(row["spec"])}

    def schedule_tick(self):
        now = self.clock()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM configs WHERE enabled=1 AND next_due<=?", (now,)).fetchall()
            # Advance first; one occurrence at the current half-hour, never a historical burst.
            for row in rows:
                db.execute("UPDATE configs SET next_due=? WHERE id=?", ((int(now // INTERVAL_SECONDS) + 1) * INTERVAL_SECONDS, row["id"]))
        for row in rows:
            spec = json.loads(row["spec"])
            occurrence = int(now // INTERVAL_SECONDS) * INTERVAL_SECONDS
            self.store.enqueue(spec, principal="scheduled", idem=f"config-{row['id']}:{occurrence}", config_id=row["id"], occurrence=occurrence, now=now)
        return len(rows)

    def _resolve_frozen(self, spec):
        reference = self.reference_for(spec["model"], spec["protocol"])
        binding = self.store.binding(spec["model"], spec["protocol"])
        candidate = resolve_registry_target(self.registry, spec["candidate"]["registry_channel_id"], spec["model"], spec["protocol"])
        if reference.snapshot != spec["reference"] or binding["revision"] != spec["binding_revision"]:
            raise Conflict("reference_changed")
        if candidate.snapshot != spec["candidate"]:
            raise Conflict("target_changed")
        return ({**candidate.channel, "canonical_model": spec["candidate"]["canonical_model"]},
                {**reference.channel, "canonical_model": spec["reference"]["canonical_model"]})

    async def execute_one(self, *, active=None, scheduled_only=False, active_only=False, job_id=None, permit_context=None,
                          comparison_previous=None, previous_occurrence=None):
        row = self.store.claim(self.owner, active=self.active if active is None else active, scheduled_only=scheduled_only, active_only=active_only, job_id=job_id)
        if row is None:
            return None
        job_id, spec = row["id"], row["spec"]
        initial_requests_sent = sum(a["state"] in {"sending", "confirmed", "unknown"}
                                    for a in self.store.get(job_id, administrative=True)["attempts"])
        task = asyncio.current_task()
        self.tasks[job_id] = task
        previous = None
        control_reason = None
        if row["config_id"] is not None:
            with self.store.connect() as db:
                older = db.execute("SELECT result FROM jobs WHERE config_id=? AND occurrence<? AND result IS NOT NULL ORDER BY occurrence DESC LIMIT 1", (row["config_id"], row["occurrence"])).fetchone()
            previous = json.loads(older[0]) if older else None
        elif spec["source"] == "daily_scheduled":
            previous = comparison_previous

        def contextual(result):
            if result.get("pairs"):
                first = result["pairs"][0]
                score = classify_pair(first["candidate"], first["reference"], previous=previous,
                                      source=spec["source"], frozen_hash=spec["frozen_hash"], occurrence=row["occurrence"], log_evidence=spec["log_evidence"], threshold_config=spec["thresholds"],
                                      previous_occurrence=previous_occurrence if spec["source"] == "daily_scheduled" else None)
                result = {**result, **{k: v for k, v in score.items() if k not in {"pairs", "requests_sent"}}}
            return {**result, "source": spec["source"], "target": spec["candidate"], "reference": spec["reference"],
                    "display_identity": spec.get("display_identity"),
                    "condition_hash": spec["condition_hash"], "thresholds": spec["thresholds"],
                    "frozen_hash": spec["frozen_hash"], "occurrence": row["occurrence"], "log_evidence": spec["log_evidence"]}

        def missed_window():
            return row["config_id"] is not None and row["occurrence"] < int(self.clock() // INTERVAL_SECONDS) * INTERVAL_SECONDS

        def stopped(reason):
            result = empty_result(reason)
            attempts = self.store.get(job_id, administrative=True)["attempts"]
            sent = sum(a["state"] in {"sending", "confirmed", "unknown"} for a in attempts)
            result.update(requests_sent=sent, new_requests_sent=sent - initial_requests_sent)
            if reason == "missed_scheduled_window":
                result["label"] = "已错过定时采样窗口，未补发请求；输入 Token 用量无法判定"
            return contextual(result)

        async def permit(side, index):
            nonlocal control_reason
            if missed_window():
                control_reason = "missed_scheduled_window"
                raise RuntimeError("missed_scheduled_window")
            self._resolve_frozen(spec)
            try:
                with permit_context(spec) if permit_context is not None else nullcontext():
                    self.store.permit(job_id, self.owner, side, index)
            except ScheduleStopped as exc:
                control_reason = str(exc)
                raise

        async def confirmed(side, index, result):
            self.store.attempt(job_id, self.owner, side, index, result)

        async def publish(result):
            self.store.publish(job_id, self.owner, contextual(result))

        async def heartbeat():
            while True:
                await asyncio.sleep(20)
                self.store.heartbeat(job_id, self.owner)

        heart = asyncio.create_task(heartbeat())
        try:
            reason = self.store.schedule_stop_reason(row["config_id"], spec)
            if reason or missed_window():
                self.store.publish(job_id, self.owner, stopped(reason or "missed_scheduled_window"), terminal=True)
                return self.store.get(job_id, administrative=True)
            candidate, reference = self._resolve_frozen(spec)
            restored = {}
            for attempt in self.store.get(job_id, administrative=True)["attempts"]:
                if attempt["state"] == "unknown":
                    restored[attempt["step_id"]] = observation([], status="unknown")
                elif attempt["state"] == "confirmed":
                    restored[attempt["step_id"]] = attempt["result"]
            result = await run_paired_check(candidate, reference, previous=previous, before_send=permit,
                                           on_attempt=confirmed, on_result=publish, upstream_transport=self.upstream_transport,
                                           resume_attempts=restored)
            self.store.publish(job_id, self.owner, contextual(result), terminal=True)
        except asyncio.CancelledError:
            self.store.publish(job_id, self.owner, stopped("cancelled_unknown"), terminal=True)
            if not self.store.get(job_id, administrative=True)["cancellation_requested"]:
                raise
        except (ValueError, RegistryError, KeyError, RuntimeError):
            reason = control_reason or self.store.schedule_stop_reason(row["config_id"], spec) or ("missed_scheduled_window" if missed_window() else "target_or_reference_changed_or_execution_stopped")
            self.store.publish(job_id, self.owner, stopped(reason), terminal=True)
        finally:
            heart.cancel()
            await asyncio.gather(heart, return_exceptions=True)
            self.tasks.pop(job_id, None)
        return self.store.get(job_id, administrative=True)

    def cancel(self, job_id, *, principal="workbench", administrative=False):
        result = self.store.cancel(job_id, principal=principal, administrative=administrative)
        task = self.tasks.get(job_id)
        if task:
            task.cancel()
        return result

    async def start(self, *, active=None):
        if self.loop_task:
            return
        self.active = os.getenv("EVAL_TOKEN_USAGE_EXECUTOR", "off") == "live" if active is None else bool(active)
        self.lock = scheduler_lock(self.store.directory)
        self.lock.__enter__()
        async def scheduled():
            while True:
                self.schedule_tick()
                await self.execute_one(scheduled_only=True)
                await asyncio.sleep(0.2)
        async def active_jobs():
            while True:
                if self.active:
                    await self.execute_one(active=True, active_only=True)
                await asyncio.sleep(0.2)
        async def loop():
            children = [asyncio.create_task(scheduled()), asyncio.create_task(active_jobs())]
            try:
                await asyncio.gather(*children)
            finally:
                for child in children:
                    child.cancel()
                await asyncio.gather(*children, return_exceptions=True)
        self.loop_task = asyncio.create_task(loop())

    async def stop(self):
        if self.loop_task:
            self.loop_task.cancel()
            await asyncio.gather(self.loop_task, return_exceptions=True)
            self.loop_task = None
        if self.lock:
            self.lock.__exit__(None, None, None)
            self.lock = None

    def status(self):
        return {"running": bool(self.loop_task and not self.loop_task.done()), "active_enabled": self.active,
                "scheduled_enabled": sum(row["enabled"] for row in self.configs()), "interval_seconds": INTERVAL_SECONDS,
                "detection_version": VERSION, "max_inflight_requests": 2, "billing_status": "unverified"}


_service = None


def get_service():
    global _service
    registry = get_registry()
    if _service is None or _service.registry is not registry:
        if _service is not None and _service.loop_task is not None:
            raise RuntimeError("token_usage_registry_changed_while_running")
        _service = TokenUsageService(registry)
    return _service


async def start_executor():
    await get_service().start()


async def stop_executor():
    if _service:
        await _service.stop()


def executor_status():
    return get_service().status()
