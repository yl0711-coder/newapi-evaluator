"""Durable local Eval task queue with idempotency and cancellation."""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from shared.registry import Conflict, RegistryError

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,159}$")
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_DEPTH = 8
MAX_NODES = 1000
_SENSITIVE_FIELD = re.compile(r"(?i)(api[\s_-]?key|token|cookie|password|secret|authorization|^auth$)")
_SENSITIVE_TEXT = re.compile(
    r"(?i)(sk-|(?:bearer|basic)\s|https?://|(?:api[\s_-]?key|token|cookie|password|secret|auth(?:orization)?)"
    r"[\"']?\s*[:=])"
)


class WorkflowQueue:
    def __init__(self, registry):
        self.registry = registry
        with registry.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS eval_workflow_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT NOT NULL UNIQUE,
                task_type TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL,
                budget_seconds INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0, result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                started_at REAL, finished_at REAL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_workflow_tasks_state ON eval_workflow_tasks(state,created_at,id)")
            conn.execute("""CREATE TABLE IF NOT EXISTS eval_workflow_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, kind TEXT NOT NULL,
                payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(task_id,kind))""")

    @staticmethod
    def _out(row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["result"] = json.loads(result.pop("result_json"))
        result["cancel_requested"] = bool(result["cancel_requested"])
        return result

    @staticmethod
    def _safe_payload(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> None:
        nodes = nodes or [0]
        nodes[0] += 1
        if depth > MAX_DEPTH or nodes[0] > MAX_NODES:
            raise RegistryError("任务负载层级或节点数量超过限制")
        if isinstance(value, dict):
            for key, child in value.items():
                if len(str(key)) > 80:
                    raise RegistryError("任务负载字段名过长")
                if _SENSITIVE_FIELD.search(str(key)) or _SENSITIVE_TEXT.search(str(key)):
                    raise RegistryError("任务负载不能包含凭据字段")
                WorkflowQueue._safe_payload(child, depth=depth + 1, nodes=nodes)
        elif isinstance(value, (list, tuple)):
            for child in value:
                WorkflowQueue._safe_payload(child, depth=depth + 1, nodes=nodes)
        elif isinstance(value, str):
            if len(value) > 4096 or _SENSITIVE_TEXT.search(value):
                raise RegistryError("任务负载不能包含凭据、敏感地址或过长文本")

    def enqueue(self, task_type: str, payload: dict[str, Any], *, idempotency_key: str, budget_seconds: int = 900) -> dict[str, Any]:
        if not task_type or not _KEY.fullmatch(task_type) or not isinstance(payload, dict):
            raise RegistryError("任务类型或负载无效")
        self._safe_payload(payload)
        if not idempotency_key or not _KEY.fullmatch(idempotency_key):
            raise RegistryError("幂等键无效")
        if not isinstance(budget_seconds, int) or not 1 <= budget_seconds <= 86400:
            raise RegistryError("任务预算必须在 1 到 86400 秒之间")
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        safe_payload = {"payload": payload, "payload_hash": digest}
        if len(json.dumps(safe_payload, ensure_ascii=False).encode()) > MAX_PAYLOAD_BYTES:
            raise RegistryError("任务负载超过 64 KiB 限制")
        now = time.time()
        with self.registry.connect() as conn:
            existing = conn.execute("SELECT * FROM eval_workflow_tasks WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                old = json.loads(existing["payload_json"])
                if old.get("payload_hash") != digest or existing["task_type"] != task_type:
                    raise Conflict("幂等键已用于不同任务")
                return self._out(existing)
            cur = conn.execute("""INSERT INTO eval_workflow_tasks
                (idempotency_key,task_type,payload_json,state,budget_seconds,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""", (idempotency_key, task_type, json.dumps(safe_payload, ensure_ascii=False),
                "queued", budget_seconds, now, now))
            row = conn.execute("SELECT * FROM eval_workflow_tasks WHERE id=?", (cur.lastrowid,)).fetchone()
        return self._out(row)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.registry.connect() as conn:
            rows = conn.execute("SELECT * FROM eval_workflow_tasks ORDER BY created_at DESC,id DESC LIMIT ?", (min(500, max(1, int(limit))),)).fetchall()
        return [self._out(row) for row in rows]

    def cancel(self, task_id: int) -> dict[str, Any]:
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM eval_workflow_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError("任务不存在")
            if row["state"] in {"succeeded", "failed", "cancelled"}:
                return self._out(row)
            state = "cancelled" if row["state"] == "queued" else row["state"]
            now = time.time()
            conn.execute("UPDATE eval_workflow_tasks SET state=?,cancel_requested=1,updated_at=? WHERE id=?", (state, now, task_id))
            row = conn.execute("SELECT * FROM eval_workflow_tasks WHERE id=?", (task_id,)).fetchone()
        return self._out(row)

    def claim_next(self) -> dict[str, Any] | None:
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM eval_workflow_tasks WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            now = time.time()
            changed = conn.execute("UPDATE eval_workflow_tasks SET state='running',attempts=attempts+1,started_at=?,updated_at=? WHERE id=? AND state='queued'", (now, now, row["id"])).rowcount
            if changed != 1:
                return None
            row = conn.execute("SELECT * FROM eval_workflow_tasks WHERE id=?", (row["id"],)).fetchone()
        return self._out(row)

    def finish(self, task_id: int, state: str, result: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
        if state not in {"succeeded", "failed", "cancelled"}:
            raise RegistryError("任务终态无效")
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM eval_workflow_tasks WHERE id=?", (task_id,)).fetchone()
            if current is None:
                raise KeyError("任务不存在")
            if current["state"] != "running":
                raise Conflict("任务尚未领取或已经进入终态")
            if current["cancel_requested"]:
                state, result, error = "cancelled", {"reason": "cancel_requested"}, ""
            if result is not None and not isinstance(result, dict):
                raise RegistryError("任务结果必须是 JSON 对象")
            # JSON key normalization can collapse distinct keys; validate every original value first.
            self._safe_payload(result)
            try:
                result_json = json.dumps(result if result is not None else {}, ensure_ascii=False, allow_nan=False)
                persisted_result = json.loads(result_json)
            except (TypeError, ValueError, OverflowError, RecursionError):
                raise RegistryError("任务结果必须是有效 JSON") from None
            self._safe_payload(persisted_result)
            if len(result_json.encode()) > MAX_PAYLOAD_BYTES:
                raise RegistryError("任务结果超过 64 KiB 限制")
            if not isinstance(error, str) or _SENSITIVE_TEXT.search(error):
                raise RegistryError("任务错误不能包含凭据或敏感地址")
            now = time.time()
            conn.execute("UPDATE eval_workflow_tasks SET state=?,result_json=?,error=?,finished_at=?,updated_at=? WHERE id=?",
                         (state, result_json, error[:300], now, now, task_id))
            row = conn.execute("SELECT * FROM eval_workflow_tasks WHERE id=?", (task_id,)).fetchone()
        return self._out(row)

    def request_cancelled(self, task_id: int) -> bool:
        with self.registry.connect() as conn:
            row = conn.execute("SELECT cancel_requested,state FROM eval_workflow_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError("任务不存在")
        return bool(row["cancel_requested"] or row["state"] == "cancelled")

    def enqueue_outbox(self, task_id: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._safe_payload(payload)
        if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_PAYLOAD_BYTES:
            raise RegistryError("Outbox 负载超过 64 KiB 限制")
        now = time.time()
        with self.registry.connect() as conn:
            if not conn.execute("SELECT 1 FROM eval_workflow_tasks WHERE id=?", (task_id,)).fetchone():
                raise KeyError("任务不存在")
            conn.execute("INSERT OR IGNORE INTO eval_workflow_outbox(task_id,kind,payload_json,created_at,updated_at) VALUES(?,?,?,?,?)",
                         (task_id, kind, json.dumps(payload, ensure_ascii=False), now, now))
            row = conn.execute("SELECT * FROM eval_workflow_outbox WHERE task_id=? AND kind=?", (task_id, kind)).fetchone()
        result = dict(row); result["payload"] = json.loads(result.pop("payload_json")); return result
