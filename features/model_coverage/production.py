"""Local, redacted production coverage snapshots and Eval comparison."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from typing import Any

from shared.registry import Conflict, RegistryError


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,159}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,159}$")
_PROTOCOLS = {"openai", "anthropic", "responses"}
_STATES = {"online", "offline", "unknown"}


def _clean_text(value: Any, field: str, pattern: re.Pattern[str]) -> str:
    text = str(value or "").strip()
    if not text or not pattern.fullmatch(text) or re.search(r"(?i)(sk-|bearer|https?://|cookie|token=)", text):
        raise RegistryError(f"生产覆盖 {field} 无效或包含敏感信息")
    return text


def normalize_snapshot(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if not isinstance(payload, dict):
        raise RegistryError("生产覆盖快照必须是 JSON 对象")
    source = _clean_text(payload.get("source"), "来源", _IDENTITY)
    version = _clean_text(payload.get("version"), "版本", _IDENTITY)
    cursor = str(payload.get("cursor", "")).strip()
    if len(cursor) > 256 or re.search(r"(?i)(sk-|bearer|https?://|cookie|token=)", cursor):
        raise RegistryError("生产覆盖游标无效或包含敏感信息")
    try:
        generated_at = float(payload.get("generated_at", 0))
    except (TypeError, ValueError) as exc:
        raise RegistryError("生产覆盖生成时间无效") from exc
    if not math.isfinite(generated_at) or generated_at <= 0 or generated_at > time.time() + 300:
        raise RegistryError("生产覆盖生成时间无效")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 10000:
        raise RegistryError("生产覆盖快照必须包含 1 到 10000 条记录")
    items = []
    seen = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise RegistryError("生产覆盖记录格式无效")
        identity = _clean_text(raw.get("channel_identity"), "渠道身份", _IDENTITY)
        model = _clean_text(raw.get("model"), "模型", _MODEL)
        protocol = str(raw.get("protocol", "")).strip()
        state = str(raw.get("production_status", "unknown")).strip()
        if protocol not in _PROTOCOLS or state not in _STATES:
            raise RegistryError("生产覆盖协议或状态无效")
        eval_channel_id = raw.get("eval_channel_id")
        if eval_channel_id is not None and (not isinstance(eval_channel_id, int) or eval_channel_id < 1):
            raise RegistryError("Eval 渠道映射无效")
        key = (identity, model, protocol)
        if key in seen:
            raise RegistryError("生产覆盖快照包含重复记录")
        seen.add(key)
        items.append({"channel_identity": identity, "model": model, "protocol": protocol,
                      "production_status": state, "eval_channel_id": eval_channel_id})
    cursor_hash = hashlib.sha256(cursor.encode()).hexdigest() if cursor else ""
    normalized = {"source": source, "version": version, "generated_at": generated_at,
                  "cursor_hash": cursor_hash, "items": sorted(items, key=lambda x: (x["channel_identity"], x["model"], x["protocol"]))}
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return normalized, hashlib.sha256(encoded.encode()).hexdigest()


class ProductionCoverage:
    def __init__(self, registry):
        self.registry = registry
        with registry.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS production_coverage_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, version TEXT NOT NULL,
                generated_at REAL NOT NULL, cursor TEXT NOT NULL, content_hash TEXT NOT NULL,
                received_at REAL NOT NULL, items_json TEXT NOT NULL,
                UNIQUE(source, version, content_hash))""")

    def import_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot, digest = normalize_snapshot(payload)
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conflict = conn.execute("SELECT content_hash FROM production_coverage_snapshots WHERE source=? AND version=? ORDER BY id DESC LIMIT 1",
                                    (snapshot["source"], snapshot["version"])).fetchone()
            if conflict and conflict[0] != digest:
                raise Conflict("相同生产覆盖来源和版本的内容不一致，已拒绝覆盖")
            existing = conn.execute("SELECT id FROM production_coverage_snapshots WHERE source=? AND version=? AND content_hash=?",
                                    (snapshot["source"], snapshot["version"], digest)).fetchone()
            if existing:
                return {"id": existing[0], "added": False, "content_hash": digest}
            try:
                cur = conn.execute("""INSERT INTO production_coverage_snapshots
                    (source,version,generated_at,cursor,content_hash,received_at,items_json)
                    VALUES(?,?,?,?,?,?,?)""", (snapshot["source"], snapshot["version"], snapshot["generated_at"],
                    snapshot["cursor_hash"], digest, time.time(), json.dumps(snapshot["items"], ensure_ascii=False, allow_nan=False)))
            except sqlite3.IntegrityError as exc:
                raise Conflict("相同生产覆盖来源和版本的内容发生并发冲突") from exc
            return {"id": cur.lastrowid, "added": True, "content_hash": digest}

    def overview(self, eval_rows: list[dict[str, Any]]) -> dict[str, Any]:
        with self.registry.connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM production_coverage_snapshots ORDER BY source, generated_at DESC, id DESC")]
        latest = {}
        for row in rows:
            latest.setdefault(row["source"], row)
        eval_keys = {(r["channel_id"], r["upstream_model"], r["protocol"]): r for r in eval_rows}
        sources = []
        now = time.time()
        for source, row in latest.items():
            items = json.loads(row["items_json"])
            diff = []
            for item in items:
                key = (item["eval_channel_id"], item["model"], item["protocol"])
                state = "conflict" if item["eval_channel_id"] is None else "covered" if key in eval_keys else "missing"
                if now - row["generated_at"] > 48 * 3600:
                    state = "stale"
                diff.append({**item, "coverage": state})
            sources.append({"source": source, "version": row["version"], "generated_at": row["generated_at"],
                            "received_at": row["received_at"], "content_hash": row["content_hash"], "items": diff})
        return {"sources": sources, "rules": {"stale_after_seconds": 48 * 3600}}
