"""Durable integrity jobs; unknown requests retain attempt/token caps and never retry.

The registry database is used for atomic connection checks and request reservations.
Only public strategy identities, request hashes/caps and typed scoring projections persist.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING
from typing import Any
from zoneinfo import ZoneInfo

from .execution import ExecutionStopped, ProbeRequest, ResolvedTarget, budget_allows, estimate_input_tokens

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")
SNAPSHOT_FIELDS = {"registry_channel_id", "connection_fingerprint", "model", "protocol", "target_id",
                   "mapping_revision", "channel_identity", "inventory_version", "production_source", "production_version", "production_hash"}
PROJECTION_FIELDS = {"request_id", "status", "valid", "invalid_reason", "vector", "parsed", "correct", "item_id",
                     "family", "input_tokens_reported", "output_tokens_reported", "reasoning_tokens_reported", "latency_ms", "duration_ms",
                     "started_at", "finished_at", "calibration_status", "metadata_status", "observation_schema",
                     "manifest_hash", "probe_id", "numbers", "parsed_numbers", "minimum_numbers", "expected_count",
                     "analyzable", "row_counts", "format_compliant", "format_errors", "choice", "number", "reservation_exceeded"}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or re.search(r"(?i)https?://|sk-|bearer|api_key|token=", value):
        raise ValueError("invalid public identity")
    return value


def clean_projection(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - PROJECTION_FIELDS:
        raise ValueError("result projection contains unsupported fields")
    clean = {}
    for key, item in value.items():
        if item is None:
            clean[key] = None
        elif key in {"vector", "parsed", "numbers", "row_counts"}:
            if not isinstance(item, list) or len(item) > 4096 or any(type(x) not in {int, float} or not math.isfinite(x) for x in item):
                raise ValueError("parsed projection must be a finite numeric vector")
            clean[key] = item
        elif key == "format_errors":
            if not isinstance(item, list) or len(item) > 100:
                raise ValueError("invalid format summaries")
            clean[key] = [_identifier(x) for x in item]
        elif key in {"valid", "correct", "analyzable", "format_compliant", "reservation_exceeded"}:
            if type(item) is not bool:
                raise ValueError("invalid boolean projection")
            clean[key] = item
        elif key.endswith("tokens_reported") or key in {"parsed_numbers", "minimum_numbers", "expected_count", "choice", "number"}:
            if type(item) is not int or item < 0:
                raise ValueError("invalid token projection")
            clean[key] = item
        elif key in {"latency_ms", "duration_ms", "started_at", "finished_at"}:
            if type(item) not in {int, float} or not math.isfinite(item) or item < 0:
                raise ValueError("invalid time projection")
            clean[key] = item
        else:
            clean[key] = _identifier(item)
    return clean


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid USD estimate")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid USD estimate")
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def clean_pricing(value):
    if value is None:
        return None
    fields = {"input_usd_per_million", "output_usd_per_million", "source", "pricing_hash"}
    if not isinstance(value, dict) or set(value) - fields or not fields - {"pricing_hash"} <= set(value):
        raise ValueError("pricing_unavailable")
    clean = {"input_usd_per_million": str(Decimal(str(value["input_usd_per_million"]))),
             "output_usd_per_million": str(Decimal(str(value["output_usd_per_million"]))),
             "source": _identifier(value["source"])}
    if any(_money(clean[k]) <= 0 for k in ("input_usd_per_million", "output_usd_per_million")):
        raise ValueError("pricing_unavailable")
    digest = hashlib.sha256(_json(clean).encode()).hexdigest()
    if value.get("pricing_hash", digest) != digest:
        raise ValueError("pricing_hash_mismatch")
    return {**clean, "pricing_hash": digest}


def default_pricing(model: str):
    prices = {"gpt-6-astra": (10, 50), "gpt-6.1-sol": (2, 10)}
    if model not in prices:
        return None
    input_price, output_price = prices[model]
    return clean_pricing({"input_usd_per_million": input_price, "output_usd_per_million": output_price,
                          "source": "configured_estimate"})


def estimated_cost_micro(pricing, input_tokens, output_tokens):
    amount = Decimal(input_tokens) * Decimal(pricing["input_usd_per_million"]) + Decimal(output_tokens) * Decimal(pricing["output_usd_per_million"])
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


def clean_snapshot(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - SNAPSHOT_FIELDS:
        raise ValueError("invalid target snapshot fields")
    required = {"registry_channel_id", "connection_fingerprint", "model", "protocol"}
    if not required <= set(value) or type(value["registry_channel_id"]) is not int or value["registry_channel_id"] < 1:
        raise ValueError("invalid target snapshot")
    if not _SHA.fullmatch(value["connection_fingerprint"]) or value["protocol"] not in {"openai", "responses", "anthropic"}:
        raise ValueError("invalid target connection identity")
    return {k: v if k == "registry_channel_id" else None if v is None else _identifier(str(v)) for k, v in value.items()}


class IntegrityStore:
    LEASE_SECONDS = 120

    def __init__(self, registry):
        self.registry = registry
        with registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS integrity_jobs (
                job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
                target_json TEXT NOT NULL, strategy_json TEXT NOT NULL, manifest_json TEXT NOT NULL,
                limits_json TEXT NOT NULL, not_before REAL NOT NULL, deadline REAL NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0,
                owner TEXT, lease_until REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                skipped_json TEXT NOT NULL DEFAULT '[]')""")
            conn.execute("""CREATE TABLE IF NOT EXISTS integrity_attempts (
                job_id TEXT NOT NULL, request_id TEXT NOT NULL, identity_hash TEXT NOT NULL,
                attempt_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, input_cap INTEGER NOT NULL,
                output_cap INTEGER NOT NULL, result_json TEXT, created_at REAL NOT NULL, completed_at REAL,
                PRIMARY KEY(job_id,request_id))""")
            for table, additions in {
                "integrity_jobs": {"pricing_json": "TEXT", "budget_scope": "TEXT", "budget_key": "TEXT",
                    "budget_date": "TEXT", "timezone": "TEXT", "daily_limit_micro": "INTEGER", "plan_version": "TEXT",
                    "principal": "TEXT NOT NULL DEFAULT 'workbench'"},
                "integrity_attempts": {"reserved_cost_micro": "INTEGER NOT NULL DEFAULT 0",
                    "reported_cost_micro": "INTEGER", "charged_cost_micro": "INTEGER NOT NULL DEFAULT 0",
                    "pricing_hash": "TEXT", "plan_version": "TEXT"},
            }.items():
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                for name, definition in additions.items():
                    if name not in columns:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            conn.execute("""CREATE TABLE IF NOT EXISTS integrity_daily_budgets (
                budget_scope TEXT NOT NULL, budget_key TEXT NOT NULL, budget_date TEXT NOT NULL, timezone TEXT NOT NULL,
                limit_micro INTEGER NOT NULL, charged_micro INTEGER NOT NULL DEFAULT 0,
                reserved_micro INTEGER NOT NULL DEFAULT 0, reported_micro INTEGER NOT NULL DEFAULT 0,
                attempted_requests INTEGER NOT NULL DEFAULT 0, input_tokens_reserved INTEGER NOT NULL DEFAULT 0,
                output_tokens_reserved INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(budget_scope,budget_key,budget_date,timezone))""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(integrity_daily_budgets)")}
            for field in ("max_requests", "max_input_tokens", "max_output_tokens"):
                if field not in columns:
                    conn.execute(f"ALTER TABLE integrity_daily_budgets ADD COLUMN {field} INTEGER")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(integrity_jobs)")}
            if "daily_limits_json" not in columns:
                conn.execute("ALTER TABLE integrity_jobs ADD COLUMN daily_limits_json TEXT")
            if "execution_mode" not in columns:
                conn.execute("ALTER TABLE integrity_jobs ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'api'")
            conn.execute("""CREATE TABLE IF NOT EXISTS integrity_offline_evidence (
                job_id TEXT PRIMARY KEY, evidence_json TEXT NOT NULL)""")

    def enqueue(self, *, idempotency_key: str, target_snapshot: dict, strategy: dict,
                requests: list[ProbeRequest] | None = None, request_manifest: list[dict] | None = None,
                limits: dict, deadline: float, not_before: float | None = None, pricing: dict | None = None,
                budget_scope: str = "daily", budget_key: str = "layered-default", budget_date: str | None = None,
                timezone: str = "Asia/Shanghai", plan_version: str = "v1",
                principal: str = "workbench", daily_limits: dict | None = None) -> dict:
        key, target = _identifier(idempotency_key), clean_snapshot(target_snapshot)
        if not isinstance(strategy, dict) or not strategy or any(not isinstance(k, str) or not isinstance(v, (str, int)) for k, v in strategy.items()):
            raise ValueError("strategy must contain public identities and hashes")
        strategy = {_identifier(k): _identifier(str(v)) for k, v in strategy.items()}
        manifest = [r.manifest() for r in requests] if requests is not None else request_manifest
        if not isinstance(manifest, list) or not 1 <= len(manifest) <= 1000:
            raise ValueError("invalid request manifest")
        seen = set()
        for item in manifest:
            if set(item) != {"request_id", "identity_hash", "input_tokens_reserved", "output_tokens_reserved"}:
                raise ValueError("invalid request manifest fields")
            request_id = _identifier(item["request_id"])
            if request_id in seen or not _SHA.fullmatch(item["identity_hash"]):
                raise ValueError("duplicate or invalid request identity")
            seen.add(request_id)
            if any(type(item[k]) is not int or item[k] < 1 for k in ("input_tokens_reserved", "output_tokens_reserved")):
                raise ValueError("invalid token reservation")
        if not isinstance(limits, dict) or set(limits) - {"max_requests", "max_input_tokens", "max_output_tokens"} or not {"max_requests", "max_input_tokens", "max_output_tokens"} <= set(limits):
            raise ValueError("invalid hard budget fields")
        if any(type(limits[k]) is not int or limits[k] < 1 for k in ("max_requests", "max_input_tokens", "max_output_tokens")) or limits["max_requests"] > 1000:
            raise ValueError("invalid hard budgets")
        pricing = clean_pricing(pricing)
        if budget_scope not in {"daily", "reference", "review"}:
            raise ValueError("invalid budget scope")
        budget_key, plan_version, principal = map(_identifier, (budget_key, plan_version, principal))
        daily_limits = daily_limits or {"max_requests": 230 if budget_scope == "daily" else 1000,
                                        "max_input_tokens": 1_000_000, "max_output_tokens": 1_000_000}
        if set(daily_limits) != {"max_requests", "max_input_tokens", "max_output_tokens"} or any(type(v) is not int or v < 1 for v in daily_limits.values()):
            raise ValueError("invalid daily limits")
        tz = ZoneInfo(timezone)
        budget_date = budget_date or datetime.now(tz).date().isoformat()
        date.fromisoformat(budget_date)
        now = time.time()
        start = now if not_before is None else not_before
        if type(deadline) not in {int, float} or not math.isfinite(deadline) or deadline <= start:
            raise ValueError("invalid execution deadline")
        identity = {"target": target, "strategy": strategy, "manifest": manifest, "limits": limits,
                    "deadline": deadline, "not_before": not_before, "pricing": pricing, "budget_scope": budget_scope,
                    "budget_key": budget_key, "budget_date": budget_date, "timezone": timezone,
                    "plan_version": plan_version, "principal": principal,
                    "daily_limits": daily_limits}
        digest = hashlib.sha256(_json(identity).encode()).hexdigest()
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT * FROM integrity_jobs WHERE idempotency_key=?", (key,)).fetchone()
            if previous:
                if previous["request_hash"] != digest:
                    raise ValueError("idempotency_conflict")
                return self._job(conn, previous)
            job_id = "integrity-" + secrets.token_hex(16)
            conn.execute("INSERT INTO integrity_jobs(job_id,idempotency_key,request_hash,target_json,strategy_json,manifest_json,limits_json,not_before,deadline,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)",
                         (job_id, key, digest, _json(target), _json(strategy), _json(manifest), _json(limits), start, deadline, now, now))
            conn.execute("UPDATE integrity_jobs SET pricing_json=?,budget_scope=?,budget_key=?,budget_date=?,timezone=?,daily_limit_micro=?,plan_version=?,principal=? WHERE job_id=?",
                         (_json(pricing), budget_scope, budget_key, budget_date, timezone, None, plan_version, principal, job_id))
            conn.execute("UPDATE integrity_jobs SET daily_limits_json=? WHERE job_id=?", (_json(daily_limits), job_id))
            return self._job(conn, conn.execute("SELECT * FROM integrity_jobs WHERE job_id=?", (job_id,)).fetchone())

    def _job(self, conn, row):
        attempts = [dict(a) for a in conn.execute("SELECT * FROM integrity_attempts WHERE job_id=? ORDER BY created_at,request_id", (row["job_id"],))]
        results = [json.loads(a["result_json"]) for a in attempts if a["result_json"] is not None]
        return {"job_id": row["job_id"], "idempotency_key": row["idempotency_key"], "status": row["status"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "reason": row["reason"], "owner": row["owner"], "lease_until": row["lease_until"],
                "deadline": row["deadline"], "cancel_requested": bool(row["cancel_requested"]),
                "target_snapshot": json.loads(row["target_json"]), "strategy": json.loads(row["strategy_json"]),
                "request_manifest": json.loads(row["manifest_json"]), "limits": json.loads(row["limits_json"]),
                "results": results, "skipped": json.loads(row["skipped_json"]), "principal": row["principal"],
                "execution_mode": row["execution_mode"],
                "reservation_exceeded": any(r.get("reservation_exceeded") is True for r in results),
                "pricing": json.loads(row["pricing_json"]) if row["pricing_json"] else None,
                "budget_scope": row["budget_scope"], "budget_key": row["budget_key"], "budget_date": row["budget_date"],
                "timezone": row["timezone"], "plan_version": row["plan_version"],
                "fees": {"currency": "USD", "basis": "configured_estimate", "stopping_limit": None,
                         "estimated_usd": sum(a["reported_cost_micro"] for a in attempts) / 1e6
                             if attempts and all(a["reported_cost_micro"] is not None for a in attempts) else None,
                         "known_estimated_usd": sum(a["reported_cost_micro"] or 0 for a in attempts) / 1e6
                             if any(a["reported_cost_micro"] is not None for a in attempts) else None,
                         "reported_requests": sum(a["reported_cost_micro"] is not None for a in attempts),
                         "unknown_requests": sum(a["reported_cost_micro"] is None for a in attempts),
                         "pricing_status": "configured_estimate" if row["pricing_json"] and json.loads(row["pricing_json"]) else "unknown"},
                "consumed": {"requests": len(attempts), "input_tokens_reserved": sum(a["input_cap"] for a in attempts),
                             "output_tokens_reserved": sum(a["output_cap"] for a in attempts),
                             "unknown_requests": sum(a["status"] == "unknown" for a in attempts)}}

    def enqueue_offline(self, *, evidence: dict, idempotency_key: str, principal: str, deadline: float | None = None):
        from .evidence import validate_account_evidence
        evidence = validate_account_evidence(evidence)
        principal, key = _identifier(principal), _identifier(idempotency_key)
        digest = hashlib.sha256(_json({"evidence": evidence, "principal": principal}).encode()).hexdigest()
        now = time.time()
        deadline = now + 60 if deadline is None else deadline
        if type(deadline) not in {int, float} or not math.isfinite(deadline) or not now < deadline <= now + 3600:
            raise ValueError("invalid offline deadline")
        request = ProbeRequest("evidence-analysis", {"id": "evidence-analysis", "evidence_hash": digest}, 0, 0)
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM integrity_jobs WHERE idempotency_key=?", ("offline:" + principal + ":" + key,)).fetchone()
            if existing:
                if existing["request_hash"] != digest or existing["execution_mode"] != "offline":
                    from shared.registry import Conflict
                    raise Conflict("idempotency_conflict")
                return self._job(conn, existing)
            job_id = "integrity-" + secrets.token_hex(16)
            target = {"target_scope": "official_account", "account_alias": evidence["account_alias"], "evidence_hash": digest}
            conn.execute("INSERT INTO integrity_jobs(job_id,idempotency_key,request_hash,target_json,strategy_json,manifest_json,limits_json,not_before,deadline,status,created_at,updated_at,principal,execution_mode) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?,?,'offline')",
                         (job_id, "offline:" + principal + ":" + key, digest, _json(target), _json({"strategy_id": "nerfed-evidence-analysis", "schema_version": "2.0"}),
                          _json([request.manifest()]), _json({"max_requests": 1, "max_input_tokens": 0, "max_output_tokens": 0}), now, deadline, now, now, principal))
            conn.execute("INSERT INTO integrity_offline_evidence VALUES(?,?)", (job_id, _json(evidence)))
            return self._job(conn, conn.execute("SELECT * FROM integrity_jobs WHERE job_id=?", (job_id,)).fetchone())

    def offline_evidence(self, job_id):
        with self.registry.connect() as conn:
            row = conn.execute("SELECT evidence_json FROM integrity_offline_evidence WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("offline evidence not found")
        return json.loads(row[0])

    def job(self, job_id):
        with self.registry.connect() as conn:
            row = conn.execute("SELECT * FROM integrity_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("integrity job not found")
            return self._job(conn, row)

    def job_by_key(self, key):
        with self.registry.connect() as conn:
            row = conn.execute("SELECT * FROM integrity_jobs WHERE idempotency_key=?", (key,)).fetchone()
            return self._job(conn, row) if row else None

    def public(self, job_id):
        return {k: v for k, v in self.job(job_id).items() if k not in {"owner", "lease_until"}}

    def list_jobs(self, *, principal=None, limit=100):
        with self.registry.connect() as conn:
            rows = conn.execute("SELECT * FROM integrity_jobs" + (" WHERE principal=?" if principal else "") + " ORDER BY created_at DESC LIMIT ?",
                                (principal, min(200, max(1, int(limit)))) if principal else (min(200, max(1, int(limit))),)).fetchall()
            return [{k: v for k, v in self._job(conn, row).items() if k not in {"owner", "lease_until"}} for row in rows]

    def resume(self, job_id):
        self.recover_expired_leases()
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM integrity_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("integrity job not found")
            if row["deadline"] <= time.time():
                raise ValueError("deadline_exceeded")
            if row["status"] not in {"cancelled", "failed", "partially_completed"}:
                raise ValueError("job_not_resumable")
            conn.execute("UPDATE integrity_jobs SET status='queued',reason='explicit_resume',cancel_requested=0,owner=NULL,lease_until=NULL,updated_at=? WHERE job_id=?", (time.time(), job_id))
        return self.public(job_id)

    def daily_budget(self, *, budget_scope="daily", budget_key="layered-default", budget_date=None, timezone="Asia/Shanghai"):
        budget_date = budget_date or datetime.now(ZoneInfo(timezone)).date().isoformat()
        with self.registry.connect() as conn:
            row = conn.execute("SELECT * FROM integrity_daily_budgets WHERE budget_scope=? AND budget_key=? AND budget_date=? AND timezone=?", (budget_scope, budget_key, budget_date, timezone)).fetchone()
        if row is None:
            return None
        public = {k: row[k] for k in ("budget_scope", "budget_key", "budget_date", "timezone", "attempted_requests", "input_tokens_reserved", "output_tokens_reserved", "max_requests", "max_input_tokens", "max_output_tokens")}
        public["cost_stopping_limit"] = None
        return public

    def recover_expired_leases(self):
        now = time.time()
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT job_id FROM integrity_jobs WHERE status='running' AND (lease_until IS NULL OR lease_until<=?)", (now,)).fetchall()
            for row in rows:
                conn.execute("UPDATE integrity_attempts SET status='unknown' WHERE job_id=? AND status='permitted'", (row[0],))
                conn.execute("UPDATE integrity_jobs SET status='queued',owner=NULL,lease_until=NULL,reason='lease_recovered',updated_at=? WHERE job_id=?", (now, row[0]))
            conn.execute("UPDATE integrity_jobs SET status='expired',reason='deadline_exceeded',updated_at=? WHERE status='queued' AND deadline<=?", (now, now))
            return len(rows)

    def claim(self, job_id: str | None = None, owner: str = "", *, execution_mode: str = "api"):
        owner = _identifier(owner)
        # Fencing identity belongs to one claim, even if the process/worker name is
        # reused after expiry. A stale session must never complete the newer lease.
        owner = "claim:" + hashlib.sha256(owner.encode()).hexdigest()[:24] + ":" + secrets.token_hex(16)
        self.recover_expired_leases()
        now = time.time()
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM integrity_jobs WHERE status='queued' AND not_before<=? AND deadline>? AND execution_mode=?" + (" AND job_id=?" if job_id else "") + " ORDER BY created_at LIMIT 1",
                               (now, now, execution_mode, job_id) if job_id else (now, now, execution_mode)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE integrity_jobs SET status='running',owner=?,lease_until=?,updated_at=? WHERE job_id=?", (owner, now + self.LEASE_SECONDS, now, row["job_id"]))
        return self.job(row["job_id"])

    def session(self, job_id: str, owner: str):
        return IntegritySession(self, job_id, owner)

    def cancel(self, job_id):
        with self.registry.connect() as conn:
            conn.execute("UPDATE integrity_jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,updated_at=? WHERE job_id=?", (time.time(), job_id))
        return self.job(job_id)


class IntegritySession:
    def __init__(self, store, job_id, owner):
        self.store, self.job_id, self.owner = store, job_id, owner
        self.LEASE_SECONDS = store.LEASE_SECONDS
        self.deadline = store.job(job_id)["deadline"]

    def _control(self, row, *, before_send=True):
        now = time.time()
        if row is None or row["status"] != "running" or row["owner"] != self.owner or (row["lease_until"] or 0) <= now:
            raise ExecutionStopped("lost", "executor_lease_lost")
        if before_send and row["cancel_requested"]:
            raise ExecutionStopped("cancelled", "cancel_requested")
        if before_send and row["deadline"] <= now:
            raise ExecutionStopped("expired", "deadline_exceeded")

    def check(self, *, before_send=True):
        with self.store.registry.connect() as conn:
            self._control(conn.execute("SELECT * FROM integrity_jobs WHERE job_id=?", (self.job_id,)).fetchone(), before_send=before_send)
            if before_send and conn.execute("SELECT 1 FROM integrity_attempts WHERE job_id=? AND json_extract(result_json,'$.reservation_exceeded')=1", (self.job_id,)).fetchone():
                raise ExecutionStopped("partially_completed", "reported_reservation_exceeded")

    def existing(self, request):
        with self.store.registry.connect() as conn:
            row = conn.execute("SELECT * FROM integrity_attempts WHERE job_id=? AND request_id=?", (self.job_id, request.request_id)).fetchone()
            if row is not None and row["identity_hash"] != request.manifest()["identity_hash"]:
                raise ExecutionStopped("rejected", "request_identity_changed")
            return dict(row) if row else None

    def reserve(self, request: ProbeRequest, target: ResolvedTarget):
        registry, now = self.store.registry, time.time()
        monitor_store = None
        if target.snapshot.get("inventory_version"):
            from features.model_coverage.monitor import MonitorStore
            # Initialization must occur before our write transaction, never within it.
            monitor_store = MonitorStore(registry)
        with registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM integrity_jobs WHERE job_id=?", (self.job_id,)).fetchone()
            self._control(row)
            if conn.execute("SELECT 1 FROM integrity_attempts WHERE job_id=? AND json_extract(result_json,'$.reservation_exceeded')=1", (self.job_id,)).fetchone():
                raise ExecutionStopped("partially_completed", "reported_reservation_exceeded")
            snapshot = json.loads(row["target_json"])
            if row["execution_mode"] == "offline":
                if target.snapshot != snapshot or request.manifest() not in json.loads(row["manifest_json"]):
                    raise ExecutionStopped("rejected", "evidence_identity_changed")
                if conn.execute("SELECT 1 FROM integrity_attempts WHERE job_id=? AND request_id=?", (self.job_id, request.request_id)).fetchone():
                    raise ExecutionStopped("failed", "attempt_already_permitted")
                attempt_id = "attempt-" + secrets.token_hex(16)
                conn.execute("INSERT INTO integrity_attempts(job_id,request_id,identity_hash,attempt_id,status,input_cap,output_cap,created_at) VALUES(?,?,?,?,'permitted',0,0,?)",
                             (self.job_id, request.request_id, request.manifest()["identity_hash"], attempt_id, now))
                return {"attempt_id": attempt_id, "target_snapshot": snapshot}
            if clean_snapshot(target.snapshot) != snapshot:
                raise ExecutionStopped("rejected", "target_changed")
            current = conn.execute("SELECT * FROM channels WHERE id=?", (snapshot["registry_channel_id"],)).fetchone()
            if current is None or not current["enabled"]:
                raise ExecutionStopped("rejected", "channel_not_found")
            try:
                channel = registry.public(current)
                channel["api_key"] = registry._cipher.decrypt(current["key_enc"].encode()).decode()
            except Exception:
                raise ExecutionStopped("rejected", "connection_unavailable") from None
            if not channel["api_key"] or registry.connection_fingerprint(channel) != snapshot["connection_fingerprint"] or registry.connection_fingerprint(target.channel) != snapshot["connection_fingerprint"]:
                raise ExecutionStopped("rejected", "connection_changed")
            if target.channel.get("model") != snapshot["model"] or target.channel.get("protocol") != snapshot["protocol"]:
                raise ExecutionStopped("rejected", "target_mapping_changed")
            if snapshot.get("production_hash"):
                from features.model_coverage.production import executable_binding
                current_binding, reason = executable_binding(conn, snapshot["registry_channel_id"], snapshot["model"], snapshot["protocol"])
                frozen = {k: snapshot[k] for k in ("channel_identity", "production_source", "production_version", "production_hash")}
                if reason or current_binding != frozen:
                    raise ExecutionStopped("rejected", "production_binding_changed")
            if monitor_store is not None:
                from features.model_coverage.monitor import ContractError
                try:
                    monitor_store._verify_target(conn, {**snapshot, "expected_inventory_version": snapshot["inventory_version"]}, channel=channel)
                except ContractError:
                    raise ExecutionStopped("rejected", "production_binding_changed") from None
            manifest = request.manifest()
            expected = next((m for m in json.loads(row["manifest_json"]) if m["request_id"] == request.request_id), None)
            if manifest != expected:
                raise ExecutionStopped("rejected", "request_identity_changed")
            if request.input_tokens_reserved < estimate_input_tokens(request.probe):
                raise ExecutionStopped("rejected", "input_reservation_too_small")
            if type(request.probe.get("max_tokens")) is not int or request.probe["max_tokens"] > request.output_tokens_reserved:
                raise ExecutionStopped("rejected", "output_reservation_too_small")
            if conn.execute("SELECT 1 FROM integrity_attempts WHERE job_id=? AND request_id=?", (self.job_id, request.request_id)).fetchone():
                raise ExecutionStopped("failed", "attempt_already_permitted")
            consumed = conn.execute("SELECT COUNT(*),COALESCE(SUM(input_cap),0),COALESCE(SUM(output_cap),0) FROM integrity_attempts WHERE job_id=?", (self.job_id,)).fetchone()
            ledger = dict(zip(("requests", "input_tokens_reserved", "output_tokens_reserved"), consumed))
            if not budget_allows(json.loads(row["limits_json"]), ledger, request.input_tokens_reserved, request.output_tokens_reserved):
                raise ExecutionStopped("partially_completed", "budget_exhausted")
            pricing = clean_pricing(json.loads(row["pricing_json"])) if row["pricing_json"] else None
            daily_key = tuple(row[k] for k in ("budget_scope", "budget_key", "budget_date", "timezone"))
            # Compatibility columns are retained; currency never controls execution.
            conn.execute("INSERT OR IGNORE INTO integrity_daily_budgets(budget_scope,budget_key,budget_date,timezone,limit_micro) VALUES(?,?,?,?,0)", daily_key)
            daily = conn.execute("SELECT * FROM integrity_daily_budgets WHERE budget_scope=? AND budget_key=? AND budget_date=? AND timezone=?", daily_key).fetchone()
            daily_limits = json.loads(row["daily_limits_json"])
            for field in ("max_requests", "max_input_tokens", "max_output_tokens"):
                daily_limits[field] = min(daily_limits[field], daily[field]) if daily[field] is not None else daily_limits[field]
            if not budget_allows(daily_limits, {"requests": daily["attempted_requests"], "input_tokens_reserved": daily["input_tokens_reserved"], "output_tokens_reserved": daily["output_tokens_reserved"]}, request.input_tokens_reserved, request.output_tokens_reserved):
                raise ExecutionStopped("partially_completed", "daily_request_budget_exhausted")
            conn.execute("UPDATE integrity_daily_budgets SET max_requests=?,max_input_tokens=?,max_output_tokens=? WHERE budget_scope=? AND budget_key=? AND budget_date=? AND timezone=?", (*[daily_limits[k] for k in ("max_requests", "max_input_tokens", "max_output_tokens")], *daily_key))
            attempt_id = "attempt-" + secrets.token_hex(16)
            conn.execute("INSERT INTO integrity_attempts(job_id,request_id,identity_hash,attempt_id,status,input_cap,output_cap,created_at) VALUES(?,?,?,?, 'permitted',?,?,?)",
                         (self.job_id, request.request_id, manifest["identity_hash"], attempt_id, request.input_tokens_reserved, request.output_tokens_reserved, now))
            conn.execute("UPDATE integrity_attempts SET reserved_cost_micro=?,charged_cost_micro=?,pricing_hash=?,plan_version=? WHERE attempt_id=?", (0, 0, pricing["pricing_hash"] if pricing else None, row["plan_version"], attempt_id))
            conn.execute("UPDATE integrity_daily_budgets SET charged_micro=charged_micro+?,reserved_micro=reserved_micro+?,attempted_requests=attempted_requests+1,input_tokens_reserved=input_tokens_reserved+?,output_tokens_reserved=output_tokens_reserved+? WHERE budget_scope=? AND budget_key=? AND budget_date=? AND timezone=?",
                         (0, 0, request.input_tokens_reserved, request.output_tokens_reserved, *daily_key))
        return {"attempt_id": attempt_id, "target_snapshot": snapshot}

    def complete(self, request, attempt, projection):
        mode = self.store.job(self.job_id)["execution_mode"]
        if mode == "offline":
            from .evidence import validate_analysis_output
            clean = validate_analysis_output(projection)
        else:
            clean = clean_projection(projection)
            clean["request_id"] = request.request_id
        with self.store.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM integrity_jobs WHERE job_id=?", (self.job_id,)).fetchone()
            try:
                self._control(row, before_send=False)
            except ExecutionStopped:
                return False
            stored = conn.execute("SELECT * FROM integrity_attempts WHERE job_id=? AND request_id=? AND attempt_id=?", (self.job_id, request.request_id, attempt["attempt_id"])).fetchone()
            if stored is None or stored["status"] != "permitted":
                return False
            if mode == "offline":
                return conn.execute("UPDATE integrity_attempts SET status='completed',result_json=?,completed_at=?,reported_cost_micro=0 WHERE attempt_id=?", (_json(clean), time.time(), attempt["attempt_id"])).rowcount == 1
            input_usage, output_usage = clean.get("input_tokens_reported"), clean.get("output_tokens_reported")
            clean["reservation_exceeded"] = ((input_usage is not None and input_usage > stored["input_cap"])
                                               or (output_usage is not None and output_usage > stored["output_cap"]))
            pricing = json.loads(row["pricing_json"]) if row["pricing_json"] else None
            reported = estimated_cost_micro(pricing, input_usage, output_usage) if pricing and input_usage is not None and output_usage is not None else None
            charge = stored["charged_cost_micro"] if reported is None else reported
            daily_key = tuple(row[k] for k in ("budget_scope", "budget_key", "budget_date", "timezone"))
            conn.execute("UPDATE integrity_daily_budgets SET charged_micro=charged_micro+?,reported_micro=reported_micro+? WHERE budget_scope=? AND budget_key=? AND budget_date=? AND timezone=?",
                         (charge - stored["charged_cost_micro"], reported or 0, *daily_key))
            return conn.execute("UPDATE integrity_attempts SET status='completed',result_json=?,completed_at=?,reported_cost_micro=?,charged_cost_micro=? WHERE attempt_id=?",
                                (_json(clean), time.time(), reported, charge, attempt["attempt_id"])).rowcount == 1

    def results(self):
        return self.store.job(self.job_id)["results"]

    def heartbeat(self):
        now = time.time()
        with self.store.registry.connect() as conn:
            changed = conn.execute("UPDATE integrity_jobs SET lease_until=?,updated_at=? WHERE job_id=? AND status='running' AND owner=? AND lease_until>?", (now + self.LEASE_SECONDS, now, self.job_id, self.owner, now)).rowcount
        return "continue" if changed else "lost"

    def finish(self, status, reason, skipped):
        if status not in {"completed", "partially_completed", "failed", "cancelled", "expired", "rejected"}:
            raise ValueError("invalid terminal status")
        now = time.time()
        with self.store.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM integrity_jobs WHERE job_id=?", (self.job_id,)).fetchone()
            try:
                self._control(row, before_send=False)
            except ExecutionStopped:
                return False
            conn.execute("UPDATE integrity_attempts SET status='unknown' WHERE job_id=? AND status='permitted'", (self.job_id,))
            unknown = conn.execute("SELECT COUNT(*) FROM integrity_attempts WHERE job_id=? AND status='unknown'", (self.job_id,)).fetchone()[0]
            if status == "completed" and unknown:
                status, reason = "partially_completed", "unknown_attempts"
            conn.execute("UPDATE integrity_jobs SET status=?,reason=?,owner=NULL,lease_until=NULL,skipped_json=?,updated_at=? WHERE job_id=?", (status, reason, _json(skipped), now, self.job_id))
        return True

    def recover(self):
        return self.store.recover_expired_leases()

    def status(self):
        return self.store.job(self.job_id)["status"]
