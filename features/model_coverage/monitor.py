"""Monitor—Eval internal contract v1.0: signed access, inventory, probe jobs and result cursors.

Only Monitor calls Eval. Eval never calls Monitor, never changes production routing, and
reports evidence (not disable decisions). Results are append-only rows whose autoincrement
id is the pull cursor; each is written only while the claiming executor still owns the running
job. Reproduction events are written in the same transaction as the job's terminal state.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from shared.registry import Conflict, RegistryError

SCHEMA_VERSION = "1.0"
CLIENT = "monitor"
SIGNATURE_WINDOW_SECONDS = 300
MAX_BODY_BYTES = 4 * 1024 * 1024
JOB_TYPES = {"admission", "patrol", "incident", "recovery", "release_validation"}
PRIORITIES = {"p0", "p1", "p2", "p3"}
PROTOCOLS = {"openai", "anthropic", "responses"}
JOB_STATES = {"queued", "running", "completed", "partially_completed", "cancelled", "expired", "rejected", "failed"}
TERMINAL_STATES = JOB_STATES - {"queued", "running"}
MAX_ROUNDS = 5
MAX_JOB_SECONDS = 3600
# Per-job hard ceilings; a request above them is rejected rather than truncated.
BUDGET_LIMITS = {"max_requests": 60, "max_input_tokens": 200000, "max_output_tokens": 100000, "max_cost_usd": 20.0}
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,159}$")
_SENSITIVE = re.compile(r"(?i)(sk-[A-Za-z0-9]|bearer\s|https?://|cookie=|token=|api[_-]?key)")


class ContractError(Exception):
    """Contract error rendered as the unified ``{"schema_version","error":{...}}`` body."""

    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.retryable = status, code, message, retryable
        self.headers = headers or {}


def rfc3339(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: Any, field: str) -> float:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
        raise ContractError(400, "invalid_timestamp", f"{field} must be RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ContractError(400, "invalid_timestamp", f"{field} must be RFC3339 UTC") from None
    return parsed.timestamp()


def credentials() -> tuple[str, bytes] | None:
    """Monitor-only key; unset means the internal API is closed, never open."""
    key_id = os.getenv("EVAL_MONITOR_KEY_ID", "").strip()
    secret = os.getenv("EVAL_MONITOR_SECRET", "")
    if not key_id and not secret:
        return None
    if not _IDENTITY.fullmatch(key_id) or len(secret) < 32:
        raise RuntimeError("EVAL_MONITOR_KEY_ID 与至少 32 位的 EVAL_MONITOR_SECRET 必须同时配置")
    return key_id, secret.encode()


def canonical(method: str, path_qs: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    return "\n".join([method.upper(), path_qs, timestamp, nonce, hashlib.sha256(body).hexdigest()]).encode()


def sign(secret: bytes, method: str, path_qs: str, timestamp: str, nonce: str, body: bytes) -> str:
    return "v1=" + hmac.new(secret, canonical(method, path_qs, timestamp, nonce, body), hashlib.sha256).hexdigest()


def _clean(value: Any, field: str, *, pattern: re.Pattern[str] = _IDENTITY, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    text = value.strip() if isinstance(value, str) else ""
    if not pattern.fullmatch(text) or _SENSITIVE.search(text):
        raise ContractError(400, "invalid_field", f"{field} is invalid")
    return text


def _plain(value: Any, field: str, limit: int = 160) -> str:
    if not isinstance(value, str) or len(value) > limit or _SENSITIVE.search(value) or any(ord(c) < 32 for c in value):
        raise ContractError(400, "invalid_field", f"{field} is invalid")
    return value.strip()


def _integer(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ContractError(400, "invalid_field", f"{field} is invalid")
    return value


def check_schema(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ContractError(400, "invalid_request", "request body must be a JSON object")
    version = body.get("schema_version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+", version):
        raise ContractError(400, "invalid_request", "schema_version is required")
    if version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise ContractError(422, "unsupported_schema_version", "schema_version major is not supported")
    return body


def body_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# Synthetic probe scenarios; Eval never forwards user prompts. ``input_cap`` is a fixed
# per-request reservation (not a measured count) used only for budget admission.
SCENARIOS = {
    "short_stream": {"stream": True, "prompt": "请连续写出 20 个从 101 开始的整数，用英文逗号分隔，不要解释。", "max_tokens": 128, "input_cap": 64},
    "long_stream": {"stream": True, "prompt": "请连续写出 80 个从 301 开始的整数，用英文逗号分隔，不要解释。", "max_tokens": 320, "input_cap": 64},
    "non_stream": {"stream": False, "prompt": "用一句话回答：中国的首都是哪里？", "max_tokens": 256, "input_cap": 64},
    "instruction": {"stream": False, "prompt": "只输出一个单词：OK。不要标点，不要解释。", "expect": "OK", "judge": "exact", "max_tokens": 200, "input_cap": 64},
}
RESPONSES_OUTPUT_FLOOR = 4096  # Responses probes always request at least this many output tokens.
USER_CONTENT_FIELDS = {"prompt", "prompts", "messages", "input", "content", "system", "user_content", "request_body"}
RUNNER_VERSION = "eval-monitor-executor-1.0"
POLICY_VERSION = "monitor-probe-v1"


def output_reservation(protocol: str, scenario: str) -> int:
    cap = SCENARIOS[scenario]["max_tokens"]
    return max(RESPONSES_OUTPUT_FLOOR, cap) if protocol == "responses" else cap


def executor_enabled() -> bool:
    return os.getenv("EVAL_MONITOR_EXECUTOR", "off").strip().lower() == "live"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000):013x}{secrets.token_hex(6)}"


class MonitorStore:
    """Durable contract state in the shared registry database (no secrets stored)."""

    def __init__(self, registry):
        self.registry = registry
        with registry.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS monitor_channel_identities (
                channel_identity TEXT PRIMARY KEY, registry_channel_id INTEGER NOT NULL UNIQUE,
                updated_at REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS monitor_nonces (
                key_id TEXT NOT NULL, nonce TEXT NOT NULL, seen_at REAL NOT NULL, PRIMARY KEY(key_id, nonce))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS monitor_inventories (
                inventory_version TEXT PRIMARY KEY, content_hash TEXT NOT NULL, generated_at REAL NOT NULL,
                received_at REAL NOT NULL, newapi_version TEXT NOT NULL, channels_json TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS monitor_probe_jobs (
                job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
                source_event_id TEXT, job_type TEXT NOT NULL, priority TEXT NOT NULL, channel_identity TEXT NOT NULL,
                registry_channel_id INTEGER, model TEXT NOT NULL, protocol TEXT NOT NULL, probe_path TEXT NOT NULL,
                scenarios_json TEXT NOT NULL, rounds INTEGER NOT NULL, not_before REAL NOT NULL, expires_at REAL NOT NULL,
                budget_json TEXT NOT NULL, expected_inventory_version TEXT, reason TEXT NOT NULL,
                status TEXT NOT NULL, status_reason TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0,
                progress_json TEXT NOT NULL DEFAULT '{}', consumed_json TEXT NOT NULL DEFAULT '{}',
                skipped_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                started_at REAL, finished_at REAL, lease_until REAL, lease_owner TEXT)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_monitor_probe_jobs_status ON monitor_probe_jobs(status,not_before)")
            # Append-only: rows are never updated; id is the pull cursor.
            conn.execute("""CREATE TABLE IF NOT EXISTS monitor_probe_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT, result_id TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL,
                body_json TEXT NOT NULL, created_at REAL NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_monitor_probe_results_job ON monitor_probe_results(job_id,id)")
            # Cancel requests are audited even when they do not change the job (contract §7).
            conn.execute("""CREATE TABLE IF NOT EXISTS monitor_job_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, action TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, status_before TEXT NOT NULL, status_after TEXT NOT NULL,
                created_at REAL NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_monitor_job_audit_job ON monitor_job_audit(job_id,id)")
            conn.execute("""CREATE TABLE IF NOT EXISTS monitor_probe_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, body_json TEXT NOT NULL,
                created_at REAL NOT NULL)""")

    # ---- replay protection -------------------------------------------------------------
    def remember_nonce(self, key_id: str, nonce: str, now: float) -> bool:
        with self.registry.connect() as conn:
            conn.execute("DELETE FROM monitor_nonces WHERE seen_at<?", (now - 2 * SIGNATURE_WINDOW_SECONDS,))
            return conn.execute("INSERT OR IGNORE INTO monitor_nonces VALUES(?,?,?)", (key_id, nonce, now)).rowcount == 1

    # ---- channel identity binding (manual, from the channels page) ---------------------
    def identities(self) -> dict[int, str]:
        with self.registry.connect() as conn:
            return {row["registry_channel_id"]: row["channel_identity"]
                    for row in conn.execute("SELECT * FROM monitor_channel_identities")}

    def bind_identity(self, registry_channel_id: int, channel_identity: str | None) -> dict[str, Any]:
        self.registry.get(registry_channel_id)
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM monitor_channel_identities WHERE registry_channel_id=?", (registry_channel_id,))
            if channel_identity:
                identity = channel_identity.strip()
                if not _IDENTITY.fullmatch(identity) or _SENSITIVE.search(identity):
                    raise RegistryError("NewAPI 渠道身份格式无效")
                owner = conn.execute("SELECT registry_channel_id FROM monitor_channel_identities WHERE channel_identity=?", (identity,)).fetchone()
                if owner:
                    raise Conflict(f"该 NewAPI 渠道身份已绑定到公共渠道 #{owner[0]}")
                conn.execute("INSERT INTO monitor_channel_identities VALUES(?,?,?)", (identity, registry_channel_id, time.time()))
        return {"channel_id": registry_channel_id, "channel_identity": channel_identity or None}

    def resolve_identity(self, channel_identity: str) -> int | None:
        with self.registry.connect() as conn:
            row = conn.execute("SELECT registry_channel_id FROM monitor_channel_identities WHERE channel_identity=?", (channel_identity,)).fetchone()
        return row[0] if row else None

    # ---- production inventory ----------------------------------------------------------
    def import_inventory(self, inventory_version: str, body: dict[str, Any], eval_models: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
        """Store a redacted snapshot; identical resubmission is idempotent, different content conflicts.

        ``eval_models`` maps registry channel id to its coverage rows (model/protocol/measurement).
        """
        version = _clean(inventory_version, "inventory_version")
        if body.get("inventory_version", version) != version:
            raise ContractError(400, "invalid_inventory_version", "inventory_version in path and body differ")
        generated_at = parse_time(body.get("generated_at"), "generated_at")
        if generated_at > time.time() + SIGNATURE_WINDOW_SECONDS:
            raise ContractError(400, "invalid_timestamp", "generated_at is in the future")
        newapi_version = _plain(body.get("newapi_version", ""), "newapi_version", 80)
        raw = body.get("channels")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 5000:
            raise ContractError(400, "invalid_field", "channels must contain 1 to 5000 entries")
        channels, seen = [], set()
        for item in raw:
            if not isinstance(item, dict):
                raise ContractError(400, "invalid_field", "channel entry must be an object")
            identity = _clean(item.get("channel_identity"), "channel_identity")
            if identity in seen:
                raise ContractError(400, "invalid_field", "duplicate channel_identity")
            seen.add(identity)
            models = item.get("models") or []
            if not isinstance(models, list) or len(models) > 500:
                raise ContractError(400, "invalid_field", "models must be a list of at most 500 entries")
            channels.append({
                "channel_identity": identity,
                "newapi_channel_id": item.get("newapi_channel_id") if isinstance(item.get("newapi_channel_id"), int) else None,
                "display_name": _plain(item.get("display_name", ""), "display_name"),
                "supplier_failure_domain_id": _clean(item.get("supplier_failure_domain_id"), "supplier_failure_domain_id", optional=True),
                "enabled_status": _plain(item.get("enabled_status", "unknown"), "enabled_status", 40),
                "groups": sorted({_clean(g, "groups") for g in (item.get("groups") or [])}),
                "models": sorted({_clean(m, "models") for m in models}),
                "channel_type": _plain(item.get("channel_type", ""), "channel_type", 80),
                "production_role": _plain(item.get("production_role", ""), "production_role", 40),
                "business_criticality": _plain(item.get("business_criticality", ""), "business_criticality", 40),
                "config_fingerprint": _plain(item.get("config_fingerprint", ""), "config_fingerprint", 100),
            })
        channels.sort(key=lambda c: c["channel_identity"])
        digest = hashlib.sha256(_json({"generated_at": generated_at, "newapi_version": newapi_version, "channels": channels}).encode()).hexdigest()
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT content_hash FROM monitor_inventories WHERE inventory_version=?", (version,)).fetchone()
            if row and row[0] != digest:
                raise ContractError(409, "inventory_version_conflict", "same inventory_version was submitted with different content")
            newest = conn.execute("SELECT MAX(generated_at) FROM monitor_inventories").fetchone()[0]
            # Same-second snapshots with different content have no defined order, so they are refused too.
            if not row and newest is not None and generated_at <= newest:
                # A late, older snapshot must never replace the current one.
                raise ContractError(409, "invalid_inventory_version", "inventory version is older than current")
            if not row:
                conn.execute("INSERT INTO monitor_inventories VALUES(?,?,?,?,?,?)",
                             (version, digest, generated_at, time.time(), newapi_version, _json(channels)))
        return {"schema_version": SCHEMA_VERSION, "inventory_version": version, "accepted": True,
                "channel_count": len(channels), "coverage_gaps": self._gaps(channels, eval_models)}

    def _gaps(self, channels: list[dict[str, Any]], eval_models: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        bound = {identity: channel_id for channel_id, identity in self.identities().items()}
        gaps = []
        for channel in channels:
            channel_id = bound.get(channel["channel_identity"])
            rows = {row["upstream_model"]: row for row in eval_models.get(channel_id, [])} if channel_id else {}
            for model in channel["models"]:
                row = rows.get(model)
                if channel_id is None:
                    reason, protocol = "target_channel_not_verified", None
                elif row is None:
                    reason, protocol = "model_not_in_eval_catalog", None
                elif row["measurement"]["status"] in {"untested", "stale", "connection_changed"}:
                    reason, protocol = "no_fresh_probe", row["protocol"]
                else:
                    continue
                gaps.append({"channel_identity": channel["channel_identity"], "model": model, "protocol": protocol, "reason": reason})
        return gaps[:1000]

    def latest_inventory_version(self) -> str | None:
        with self.registry.connect() as conn:
            row = conn.execute("SELECT inventory_version FROM monitor_inventories ORDER BY generated_at DESC, rowid DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def inventory_channel(self, inventory_version: str, channel_identity: str) -> dict[str, Any] | None:
        with self.registry.connect() as conn:
            row = conn.execute("SELECT channels_json FROM monitor_inventories WHERE inventory_version=?", (inventory_version,)).fetchone()
        if row is None:
            raise ContractError(409, "unknown_inventory_version", "expected_inventory_version has not been synchronized")
        return next((c for c in json.loads(row[0]) if c["channel_identity"] == channel_identity), None)

    # ---- probe jobs ------------------------------------------------------------------------
    def create_job(self, body: dict[str, Any], header_key: str, resolve_channel, egress_check) -> tuple[dict[str, Any], bool]:
        """Validate and queue a job. Returns (response, created). Rejections raise ContractError."""
        key = _clean(body.get("idempotency_key"), "idempotency_key")
        if header_key != key:
            raise ContractError(400, "idempotency_key_mismatch", "Idempotency-Key header must equal body idempotency_key")
        if USER_CONTENT_FIELDS.intersection(body):
            raise ContractError(400, "user_content_forbidden", "probe jobs cannot carry user prompts or request bodies")
        digest = body_hash(body)
        with self.registry.connect() as conn:
            existing = conn.execute("SELECT * FROM monitor_probe_jobs WHERE idempotency_key=?", (key,)).fetchone()
        if existing:
            if existing["request_hash"] != digest:
                raise ContractError(409, "idempotency_conflict", "idempotency_key was used for a different job")
            return self._accepted(existing), False
        job = self._validate_job(body, resolve_channel, egress_check)
        now = time.time()
        job_id = _new_id("probe")
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            again = conn.execute("SELECT * FROM monitor_probe_jobs WHERE idempotency_key=?", (key,)).fetchone()
            if again:
                if again["request_hash"] != digest:
                    raise ContractError(409, "idempotency_conflict", "idempotency_key was used for a different job")
                return self._accepted(again), False
            conn.execute("""INSERT INTO monitor_probe_jobs(job_id,idempotency_key,request_hash,source_event_id,job_type,priority,
                channel_identity,registry_channel_id,model,protocol,probe_path,scenarios_json,rounds,not_before,expires_at,
                budget_json,expected_inventory_version,reason,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                (job_id, key, digest, job["source_event_id"], job["job_type"], job["priority"], job["channel_identity"],
                 job["registry_channel_id"], job["model"], job["protocol"], job["probe_path"], _json(job["scenarios"]),
                 job["rounds"], job["not_before"], job["expires_at"], _json(job["budget"]),
                 job["expected_inventory_version"], job["reason"], now, now))
            row = conn.execute("SELECT * FROM monitor_probe_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._accepted(row), True

    @staticmethod
    def _accepted(row) -> dict[str, Any]:
        start = max(row["not_before"], row["created_at"])
        return {"schema_version": SCHEMA_VERSION, "job_id": row["job_id"], "status": row["status"],
                "created_at": rfc3339(row["created_at"]),
                "estimated_start_at": rfc3339(start) if row["status"] == "queued" else None}

    def _validate_job(self, body: dict[str, Any], resolve_channel, egress_check) -> dict[str, Any]:
        job_type = body.get("job_type")
        if job_type not in JOB_TYPES:
            raise ContractError(400, "invalid_field", "job_type is invalid")
        priority = body.get("priority", "p2")
        if priority not in PRIORITIES:
            raise ContractError(400, "invalid_field", "priority is invalid")
        protocol = body.get("protocol")
        if protocol not in PROTOCOLS:
            raise ContractError(422, "protocol_not_supported", "protocol is not supported by Eval")
        probe_path = body.get("probe_path", "direct")
        if probe_path == "end_to_end":
            # No isolated gateway identity exists yet, so business-metric exclusion cannot be guaranteed.
            raise ContractError(422, "end_to_end_isolation_unavailable", "end_to_end probes require an isolated identity Eval does not have")
        if probe_path != "direct":
            raise ContractError(400, "invalid_field", "probe_path is invalid")
        scenarios = body.get("scenarios")
        if (not isinstance(scenarios, list) or not 1 <= len(scenarios) <= len(SCENARIOS)
                or len(set(scenarios)) != len(scenarios) or any(s not in SCENARIOS for s in scenarios)):
            raise ContractError(400, "invalid_field", f"scenarios must be distinct values of {sorted(SCENARIOS)}")
        rounds = _integer(body.get("rounds", 1), "rounds", 1, MAX_ROUNDS)
        now = time.time()
        not_before = parse_time(body["not_before"], "not_before") if body.get("not_before") is not None else now
        expires_at = parse_time(body.get("expires_at"), "expires_at")
        if expires_at <= now or expires_at <= not_before or expires_at - max(now, not_before) > MAX_JOB_SECONDS:
            raise ContractError(400, "invalid_time_window", f"expires_at must be in the future and within {MAX_JOB_SECONDS}s of start")
        budget = self._budget(body.get("budget"), protocol, scenarios, rounds)
        identity = _clean(body.get("channel_identity"), "channel_identity")
        model = _clean(body.get("model"), "model")
        expected = _clean(body.get("expected_inventory_version"), "expected_inventory_version", optional=True)
        if expected:
            self._check_inventory(expected, identity, model)
        registry_channel_id = self.resolve_identity(identity)
        if registry_channel_id is None:
            raise ContractError(422, "target_channel_not_verified", "channel_identity is not bound to an Eval channel")
        channel = resolve_channel(registry_channel_id)
        if channel is None:
            raise ContractError(404, "channel_not_found", "bound Eval channel was deleted or disabled")
        egress_check(channel, protocol)
        return {"job_type": job_type, "priority": priority, "protocol": protocol, "probe_path": probe_path,
                "scenarios": scenarios, "rounds": rounds, "not_before": not_before, "expires_at": expires_at,
                "budget": budget, "channel_identity": identity, "model": model, "registry_channel_id": registry_channel_id,
                "expected_inventory_version": expected,
                "source_event_id": _clean(body.get("source_event_id"), "source_event_id", optional=True),
                "reason": _plain(body.get("reason", ""), "reason", 300)}

    @staticmethod
    def _budget(raw: Any, protocol: str, scenarios: list[str], rounds: int) -> dict[str, Any]:
        if not isinstance(raw, dict) or set(raw) - set(BUDGET_LIMITS):
            raise ContractError(400, "invalid_budget", f"budget fields: {sorted(BUDGET_LIMITS)}")
        budget: dict[str, Any] = {}
        for field, ceiling in BUDGET_LIMITS.items():
            value = raw.get(field)
            if field == "max_cost_usd":
                if value is None:
                    budget[field] = None
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= ceiling:
                    raise ContractError(400, "invalid_budget", f"{field} must be in (0, {ceiling}]")
                budget[field] = float(value)
                continue
            budget[field] = _integer(value, f"budget.{field}", 1, int(ceiling))
        planned = {
            "requests": len(scenarios) * rounds,
            "input_tokens": sum(SCENARIOS[s]["input_cap"] for s in scenarios) * rounds,
            "output_tokens": sum(output_reservation(protocol, s) for s in scenarios) * rounds,
        }
        over = [name for name in planned if planned[name] > budget["max_" + name]]
        if over:
            raise ContractError(422, "budget_exceeded", "planned " + ", ".join(f"{n}={planned[n]}" for n in over) + " exceeds budget")
        # Eval has no price table; cost is never estimated or reported as zero.
        return {**budget, "planned": planned}

    def _check_inventory(self, expected: str, identity: str, model: str) -> None:
        channel = self.inventory_channel(expected, identity)
        if channel is None:
            raise ContractError(404, "channel_not_found", "channel_identity is not in expected_inventory_version")
        if model not in channel["models"]:
            raise ContractError(422, "model_not_in_inventory", "model is not served by this channel in expected_inventory_version")
        latest = self.latest_inventory_version()
        if latest and latest != expected:
            current = self.inventory_channel(latest, identity)
            if current is None or model not in current["models"] or current["config_fingerprint"] != channel["config_fingerprint"]:
                raise ContractError(409, "inventory_version_conflict", "target changed in a newer inventory; resynchronize before probing")

    def _row(self, job_id: str):
        with self.registry.connect() as conn:
            row = conn.execute("SELECT * FROM monitor_probe_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ContractError(404, "job_not_found", "probe job does not exist")
        return row

    def job(self, job_id: str) -> dict[str, Any]:
        self.expire_due()
        row = self._row(job_id)
        budget = json.loads(row["budget_json"])
        with self.registry.connect() as conn:
            result_ids = [r[0] for r in conn.execute("SELECT result_id FROM monitor_probe_results WHERE job_id=? ORDER BY id", (job_id,))]
        return {"schema_version": SCHEMA_VERSION, "job_id": row["job_id"], "status": row["status"],
                "status_reason": row["status_reason"] or None, "job_type": row["job_type"], "priority": row["priority"],
                "source_event_id": row["source_event_id"], "channel_identity": row["channel_identity"],
                "model": row["model"], "protocol": row["protocol"], "probe_path": row["probe_path"],
                "scenarios": json.loads(row["scenarios_json"]), "rounds": row["rounds"],
                "cancel_requested": bool(row["cancel_requested"]),
                "progress": {"planned_requests": budget["planned"]["requests"], **json.loads(row["progress_json"])},
                "budget": {k: v for k, v in budget.items() if k != "planned"},
                "budget_consumed": json.loads(row["consumed_json"]) or None,
                "skipped": json.loads(row["skipped_json"]), "result_ids": result_ids,
                "created_at": rfc3339(row["created_at"]), "not_before": rfc3339(row["not_before"]),
                "expires_at": rfc3339(row["expires_at"]), "started_at": rfc3339(row["started_at"]),
                "finished_at": rfc3339(row["finished_at"]),
                "estimated_finish_at": rfc3339(row["expires_at"]) if row["status"] in {"queued", "running"} else None}

    def recent_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        self.expire_due()
        with self.registry.connect() as conn:
            rows = conn.execute("SELECT job_id FROM monitor_probe_jobs ORDER BY created_at DESC, job_id DESC LIMIT ?",
                                (min(200, max(1, int(limit))),)).fetchall()
        return [self.job(row[0]) for row in rows]

    def cancel(self, job_id: str, idempotency_key: str = "") -> dict[str, Any]:
        """Idempotent. Queued jobs end now; running jobs stop before their next request. Always audited."""
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM monitor_probe_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise ContractError(404, "job_not_found", "probe job does not exist")
            now, before = time.time(), row[0]
            after = before
            if before == "queued":
                conn.execute("""UPDATE monitor_probe_jobs SET status='cancelled',status_reason='cancelled_before_start',
                    cancel_requested=1,finished_at=?,updated_at=? WHERE job_id=?""", (now, now, job_id))
                after = "cancelled"
            elif before == "running":
                conn.execute("UPDATE monitor_probe_jobs SET cancel_requested=1,updated_at=? WHERE job_id=?", (now, job_id))
            conn.execute("INSERT INTO monitor_job_audit(job_id,action,idempotency_key,status_before,status_after,created_at) VALUES(?,?,?,?,?,?)",
                         (job_id, "cancel", idempotency_key, before, after, now))
        return self.job(job_id)

    def audit(self, job_id: str) -> list[dict[str, Any]]:
        with self.registry.connect() as conn:
            rows = conn.execute("SELECT action,idempotency_key,status_before,status_after,created_at FROM monitor_job_audit WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        return [{**dict(r), "created_at": rfc3339(r["created_at"])} for r in rows]

    def expire_due(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self.registry.connect() as conn:
            return conn.execute("""UPDATE monitor_probe_jobs SET status='expired',status_reason='expired_before_start',
                finished_at=?,updated_at=? WHERE status='queued' AND expires_at<=?""", (now, now, now)).rowcount

    LEASE_SECONDS = 120

    def recover_expired_leases(self) -> int:
        """End running jobs whose lease has lapsed (crashed or stopped executor); evidence is kept.

        Only lapsed leases are touched, so a live executor in another process that keeps
        heartbeating is never failed here; its ownership check then stops it if this ever races.
        """
        now = time.time()
        with self.registry.connect() as conn:
            return conn.execute("""UPDATE monitor_probe_jobs SET status=CASE WHEN EXISTS(SELECT 1 FROM monitor_probe_results r
                WHERE r.job_id=monitor_probe_jobs.job_id) THEN 'partially_completed' ELSE 'failed' END,
                status_reason='executor_lease_lost',finished_at=?,updated_at=?,lease_until=NULL,lease_owner=NULL,
                progress_json=json_set(COALESCE(progress_json,'{}'),'$.completed_requests',
                    (SELECT COUNT(*) FROM monitor_probe_results r WHERE r.job_id=monitor_probe_jobs.job_id))
                WHERE status='running' AND (lease_until IS NULL OR lease_until<?)""", (now, now, now)).rowcount

    def claim(self, owner: str) -> dict[str, Any] | None:
        """Claim the highest-priority due job (p0 first, then oldest) for executor ``owner``."""
        now = time.time()
        self.expire_due(now)
        self.recover_expired_leases()
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""SELECT job_id FROM monitor_probe_jobs WHERE status='queued' AND not_before<=?
                ORDER BY priority, created_at, job_id LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            conn.execute("""UPDATE monitor_probe_jobs SET status='running',started_at=?,updated_at=?,lease_until=?,lease_owner=?
                WHERE job_id=? AND status='queued'""", (now, now, now + self.LEASE_SECONDS, owner, row[0]))
            job = conn.execute("SELECT * FROM monitor_probe_jobs WHERE job_id=?", (row[0],)).fetchone()
        return {**dict(job), "scenarios": json.loads(job["scenarios_json"]), "budget": json.loads(job["budget_json"])}

    def heartbeat(self, job_id: str, owner: str, progress: dict[str, Any], consumed: dict[str, Any]) -> str:
        """Persist progress and extend the lease. Returns "continue", "cancel" or "lost".

        "lost" means the job is no longer running under this owner (recovered, ended elsewhere);
        the executor must stop immediately without sending or recording anything more.
        """
        now = time.time()
        with self.registry.connect() as conn:
            changed = conn.execute("""UPDATE monitor_probe_jobs SET progress_json=?,consumed_json=?,lease_until=?,updated_at=?
                WHERE job_id=? AND status='running' AND lease_owner=?""",
                (_json(progress), _json(consumed), now + self.LEASE_SECONDS, now, job_id, owner)).rowcount
            if changed != 1:
                return "lost"
            row = conn.execute("SELECT cancel_requested FROM monitor_probe_jobs WHERE job_id=?", (job_id,)).fetchone()
        return "cancel" if row[0] else "continue"

    def append_result(self, job_id: str, owner: str, body: dict[str, Any]) -> str | None:
        """Append only while ``owner`` still holds the running job; None means ownership was lost."""
        result_id = _new_id("result")
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM monitor_probe_jobs WHERE job_id=? AND status='running' AND lease_owner=?",
                                (job_id, owner)).fetchone():
                return None
            conn.execute("INSERT INTO monitor_probe_results(result_id,job_id,body_json,created_at) VALUES(?,?,?,?)",
                         (result_id, job_id, _json({**body, "result_id": result_id}), time.time()))
        return result_id

    def finish_job(self, job_id: str, status: str, reason: str, progress: dict[str, Any], consumed: dict[str, Any],
                   skipped: list[dict[str, Any]], event: dict[str, Any] | None = None, *, owner: str) -> bool:
        """Set the terminal state if ``owner`` still holds the job; returns False when ownership was lost."""
        if status not in TERMINAL_STATES:
            raise ValueError("invalid terminal status")
        now = time.time()
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("""UPDATE monitor_probe_jobs SET status=?,status_reason=?,progress_json=?,consumed_json=?,
                skipped_json=?,finished_at=?,updated_at=?,lease_until=NULL,lease_owner=NULL
                WHERE job_id=? AND status='running' AND lease_owner=?""",
                (status, reason, _json(progress), _json(consumed), _json(skipped[:200]), now, now, job_id, owner)).rowcount
            # The event commits with the terminal state, so a pulled event always has final results.
            if changed and event:
                event_id = _new_id("eval-event")
                conn.execute("INSERT INTO monitor_probe_events(event_id,body_json,created_at) VALUES(?,?,?)",
                             (event_id, _json({**event, "event_id": event_id}), now))
        return bool(changed)

    # ---- cursors -------------------------------------------------------------------------
    @staticmethod
    def _cursor(value: str) -> int:
        if value in ("", "0"):
            return 0
        if not re.fullmatch(r"c1\.[0-9]{1,18}", value):
            raise ContractError(400, "invalid_cursor", "cursor is not a value returned by Eval")
        return int(value[3:])

    def _page(self, table: str, cursor: str, limit: int) -> dict[str, Any]:
        after = self._cursor(cursor)
        limit = _integer(limit, "limit", 1, 200)
        with self.registry.connect() as conn:
            rows = conn.execute(f"SELECT id,body_json FROM {table} WHERE id>? ORDER BY id LIMIT ?", (after, limit + 1)).fetchall()
        items = [json.loads(row["body_json"]) for row in rows[:limit]]
        last = rows[:limit][-1]["id"] if rows[:limit] else after
        return {"schema_version": SCHEMA_VERSION, "items": items, "next_cursor": f"c1.{last}", "has_more": len(rows) > limit}

    def results(self, cursor: str, limit: int) -> dict[str, Any]:
        return self._page("monitor_probe_results", cursor, limit)

    def events(self, cursor: str, limit: int) -> dict[str, Any]:
        return self._page("monitor_probe_events", cursor, limit)


# Transport status -> (outcome, channel_result, error_category). error_category uses the
# fault_class enum of the request classification truth table; eval-side causes never blame the channel.
_CLASSIFY = {
    "completed": ("success", "healthy", None),
    "content_mismatch": ("content_mismatch", "healthy", "semantic_quality"),
    "timeout": ("timeout", "upstream_error", "transport_timeout"),
    "network_error": ("error", "upstream_error", "transport_connect"),
    "auth_error": ("error", "upstream_error", "auth_quota_account"),
    "rate_limited": ("rate_limited", "rate_limited", "rate_limit_capacity"),
    "upstream_5xx": ("error", "upstream_error", "upstream_5xx"),
    "upstream_error": ("error", "upstream_error", "upstream_5xx"),
    "http_error": ("error", "unknown", "unknown"),
    "empty_response": ("empty", "protocol_error", "empty_or_truncated_output"),
    "truncated": ("incomplete", "protocol_error", "responses_incomplete"),
    "invalid_response": ("protocol_error", "protocol_error", "protocol_invalid"),
    "egress_denied": ("eval_rejected", "unknown", "eval_egress_denied"),
    "platform_error": ("eval_error", "unknown", "eval_internal_error"),
}
_SUMMARY = {
    "completed": "response completed with a terminal signal", "content_mismatch": "response completed but did not match the probe expectation",
    "stream_break": "stream ended before terminal event", "timeout": "request timed out", "network_error": "connection to upstream failed",
    "auth_error": "upstream rejected the channel credential", "rate_limited": "upstream returned 429",
    "upstream_5xx": "upstream returned 5xx", "upstream_error": "upstream reported a failed response",
    "http_error": "upstream returned a non-success HTTP status", "empty_response": "response had no visible content",
    "truncated": "response ended without a completed status", "invalid_response": "response violated the protocol format",
    "egress_denied": "Eval egress policy blocked the target", "platform_error": "Eval executor error",
}
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,159}$")


def to_result(job: dict[str, Any], scenario: str, round_number: int, raw: dict[str, Any], started: float, finished: float) -> dict[str, Any]:
    status = raw.get("status", "platform_error")
    if status == "stream_break":
        category = "stream_interrupted_midstream" if raw.get("ttft_ms") is not None else "stream_interrupted_before_first_event"
        outcome, channel_result = "interrupted", "interrupted"
    else:
        outcome, channel_result, category = _CLASSIFY.get(status, ("eval_error", "unknown", "eval_internal_error"))
    stream = SCENARIOS[scenario]["stream"]
    responded = status in {"completed", "content_mismatch", "stream_break", "empty_response", "truncated"}
    match = re.search(r"HTTP (\d{3})", str(raw.get("error") or ""))
    reported = str(raw.get("actual_model") or "")
    return {
        "job_id": job["job_id"], "source_event_id": job["source_event_id"], "channel_identity": job["channel_identity"],
        "inventory_version": job["expected_inventory_version"], "model_requested": job["model"],
        "model_reported": reported if _MODEL.fullmatch(reported) else ("unrecognized" if reported else None),
        "model_mismatch": bool(raw.get("model_mismatch")), "protocol": job["protocol"], "probe_path": job["probe_path"],
        "scenario": scenario, "round": round_number, "started_at": rfc3339(started), "finished_at": rfc3339(finished),
        "outcome": outcome, "channel_result": channel_result, "error_category": category,
        # Only HTTP error statuses are observed by the transport; success statuses are not recorded.
        "http_status": int(match.group(1)) if match else None,
        "ttft_ms": raw.get("ttft_ms"), "duration_ms": raw.get("latency_ms"),
        "stream_complete": (status in {"completed", "content_mismatch"}) if stream else None,
        "usage_status": ("complete" if raw.get("usage_complete") else "missing") if responded else "unknown",
        "attempt_count": 1, "evidence_summary": _SUMMARY.get(status, "Eval executor error"),
        "runner_version": RUNNER_VERSION, "policy_version": POLICY_VERSION,
        "exclude_from_business_metrics": True, "supersedes_result_id": None,
    }


def reproduced_event(job: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Emit an event only when every round of one scenario failed with the same category."""
    by_scenario: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        by_scenario.setdefault(item["scenario"], []).append(item)
    for scenario, items in by_scenario.items():
        categories = {i["error_category"] for i in items}
        if (len(items) == job["rounds"] and len(categories) == 1 and None not in categories
                and not next(iter(categories)).startswith("eval_") and next(iter(categories)) != "semantic_quality"):
            return {"channel_identity": job["channel_identity"], "model": job["model"], "protocol": job["protocol"],
                    "event_type": "probe_failure_reproduced", "state": "observed", "scenario": scenario,
                    "error_category": next(iter(categories)), "source_event_id": job["source_event_id"], "job_id": job["job_id"],
                    "first_seen_at": items[0]["started_at"], "last_seen_at": items[-1]["finished_at"],
                    "confidence": "medium" if len(items) >= 2 else "low", "result_ids": [i["result_id"] for i in items],
                    # Evidence only: Monitor decides; Eval never recommends disabling a channel.
                    "recommended_monitor_action": "compare_with_production_traffic"}
    return None


async def execute_job(store: MonitorStore, job: dict[str, Any], resolve_channel, send) -> str:
    """Run one claimed job; any unexpected error ends it as failed instead of leaving it running."""
    owner = job["lease_owner"]
    try:
        return await _execute_job(store, job, owner, resolve_channel, send)
    except Exception:
        # Keep what is known: request count and reservations from the local ledger, results from storage.
        ledger = job.get("_ledger") or {}
        consumed = ledger.get("consumed") or {}
        with store.registry.connect() as conn:
            done = conn.execute("SELECT COUNT(*) FROM monitor_probe_results WHERE job_id=?", (job["job_id"],)).fetchone()[0]
        progress = {"completed_requests": done, "planned_requests": job["budget"]["planned"]["requests"]}
        steps = ledger.get("steps") or []
        skipped = [{"scenario": s, "round": r, "reason": "executor_error"} for r, s in steps[ledger.get("next_index", 0):]]
        store.finish_job(job["job_id"], "partially_completed" if done else "failed", "executor_error",
                         progress, consumed, skipped, owner=owner)
        raise


async def _execute_job(store: MonitorStore, job: dict[str, Any], owner: str, resolve_channel, send) -> str:
    """Run one claimed job sequentially. ``send(channel, probe)`` performs a single upstream request.

    Before every request the executor re-checks ownership, cancellation, expiry and remaining
    budget, so a stopped job never issues a new request. A request already in flight finishes,
    but its result is discarded if ownership was lost meanwhile.
    """
    planned = job["budget"]["planned"]
    progress = {"completed_requests": 0, "planned_requests": planned["requests"]}
    consumed = {"requests": 0, "input_tokens_reserved": 0, "output_tokens_reserved": 0, "output_tokens_reported": 0}
    skipped: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    steps = [(round_number, scenario) for round_number in range(1, job["rounds"] + 1) for scenario in job["scenarios"]]
    # Shared with execute_job so an unexpected error can still report consumption and skipped steps.
    job["_ledger"] = {"consumed": consumed, "steps": steps, "next_index": 0}
    status, reason = "completed", ""
    channel = resolve_channel(job["registry_channel_id"])
    if channel is None:
        store.finish_job(job["job_id"], "rejected", "channel_not_found", progress, consumed,
                         [{"scenario": s, "round": r, "reason": "channel_not_found"} for r, s in steps], owner=owner)
        return "rejected"
    for index, (round_number, scenario) in enumerate(steps):
        stop = None
        beat = store.heartbeat(job["job_id"], owner, progress, consumed)
        if beat == "lost":
            return "lost"
        if beat == "cancel":
            stop = ("cancelled", "cancel_requested")
        elif time.time() >= job["expires_at"]:
            stop = ("expired", "expired_during_run")
        elif (consumed["requests"] + 1 > job["budget"]["max_requests"]
              or consumed["output_tokens_reserved"] + output_reservation(job["protocol"], scenario) > job["budget"]["max_output_tokens"]):
            stop = ("partially_completed", "budget_exhausted")
        if stop:
            status, reason = stop
            skipped.extend({"scenario": s, "round": r, "reason": reason} for r, s in steps[index:])
            break
        probe = {"id": f"monitor-{scenario}", "name": scenario, **{k: v for k, v in SCENARIOS[scenario].items() if k != "input_cap"}}
        started = time.time()
        try:
            raw = await send({**channel, "model": job["model"], "protocol": job["protocol"]}, probe)
        except Exception:  # never let one request crash the job; the error type is not user content
            raw = {"status": "platform_error"}
        consumed["requests"] += 1
        job["_ledger"]["next_index"] = index + 1
        consumed["input_tokens_reserved"] += SCENARIOS[scenario]["input_cap"]
        consumed["output_tokens_reserved"] += output_reservation(job["protocol"], scenario)
        if isinstance(raw.get("output_tokens"), int):
            consumed["output_tokens_reported"] += raw["output_tokens"]
        body = to_result(job, scenario, round_number, raw, started, time.time())
        result_id = store.append_result(job["job_id"], owner, body)
        if result_id is None:
            return "lost"
        body["result_id"] = result_id
        results.append(body)
        progress["completed_requests"] += 1
    # Cancel keeps "cancelled" even with earlier results; expiry after partial work is partial.
    if status == "expired" and results:
        status, reason = "partially_completed", "expired_during_run"
    if not store.finish_job(job["job_id"], status, reason, progress, consumed, skipped, reproduced_event(job, results), owner=owner):
        return "lost"
    return status
