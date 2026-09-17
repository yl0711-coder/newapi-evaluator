"""Common model definitions, exact channel mappings and manual discovery snapshots."""
from __future__ import annotations

import json
import re
import time
import uuid

from shared.registry import Conflict, RegistryError


DEFAULT_MODELS = (
    ("gpt-5.5", "GPT-5.5", "GPT", "openai"),
    ("gpt-5.6-luna", "GPT-5.6 Luna", "GPT", "openai"),
    ("gpt-5.6-terra", "GPT-5.6 Terra", "GPT", "openai"),
    ("gpt-5.6-sol", "GPT-5.6 Sol", "GPT", "openai"),
    ("gpt-6-astra", "GPT-6 Astra", "GPT", "responses"),
    ("claude-fable-5.1", "Claude Fable 5.1", "Claude", "anthropic"),
    ("claude-fable-5", "Claude Fable 5", "Claude", "anthropic"),
    ("claude-opus-5", "Claude Opus 5", "Claude", "anthropic"),
    ("claude-opus-4.8", "Claude Opus 4.8", "Claude", "anthropic"),
    ("claude-opus-4.7", "Claude Opus 4.7", "Claude", "anthropic"),
    ("claude-sonnet-5", "Claude Sonnet 5", "Claude", "anthropic"),
    ("claude-sonnet-4.6", "Claude Sonnet 4.6", "Claude", "anthropic"),
)
PROTOCOLS = {"openai", "anthropic", "responses"}


def model_name(value: str) -> str:
    value = value.strip()
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,159}", value)
            or value.lower().startswith(("sk-", "bearer", "http:", "https:"))):
        raise RegistryError("模型 ID 格式无效，请填写模型标识，不要填写密钥或地址")
    return value


class Catalog:
    def __init__(self, registry):
        self.registry = registry
        with registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS model_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL, family TEXT NOT NULL, protocol TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS channel_model_bindings (
                channel_id INTEGER NOT NULL, model_id INTEGER NOT NULL, upstream_model TEXT NOT NULL,
                protocol TEXT NOT NULL, PRIMARY KEY(channel_id,model_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS channel_model_discovery (
                channel_id INTEGER PRIMARY KEY, request_id TEXT NOT NULL,
                attempted_at REAL NOT NULL, error TEXT NOT NULL, succeeded_at REAL,
                connection_fingerprint TEXT NOT NULL DEFAULT '',
                auth_kind TEXT NOT NULL, models_json TEXT NOT NULL DEFAULT '[]')""")
            if not conn.execute("SELECT 1 FROM registry_meta WHERE key='model_catalog_seeded'").fetchone():
                conn.executemany("INSERT OR IGNORE INTO model_catalog(model,label,family,protocol) VALUES(?,?,?,?)", DEFAULT_MODELS)
                conn.execute("INSERT INTO registry_meta VALUES('model_catalog_seeded','1')")

    def models(self):
        with self.registry.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM model_catalog ORDER BY id")]

    def mappings(self):
        with self.registry.connect() as conn:
            return {(row["channel_id"], row["model_id"]): dict(row)
                    for row in conn.execute("SELECT * FROM channel_model_bindings")}

    def add(self, model: str, label: str, family: str, protocol: str):
        model = model_name(model)
        if protocol not in PROTOCOLS or not label.strip() or not family.strip():
            raise RegistryError("请填写名称、分组和有效协议")
        if model == "gpt-6-astra" and protocol != "responses":
            raise RegistryError("GPT-6 Astra 使用 Responses 协议")
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM model_catalog WHERE model=?", (model,)).fetchone():
                raise Conflict("常用模型已存在")
            if conn.execute("SELECT count(*) FROM model_catalog").fetchone()[0] >= 200:
                raise RegistryError("常用模型清单最多支持 200 个模型")
            row = conn.execute("INSERT INTO model_catalog(model,label,family,protocol) VALUES(?,?,?,?)",
                               (model, label.strip(), family.strip(), protocol))
            return row.lastrowid

    def binding(self, channel_id: int, model_id: int):
        with self.registry.connect() as conn:
            row = conn.execute("""SELECT c.*, COALESCE(b.upstream_model,c.model) AS upstream_model,
                COALESCE(b.protocol,c.protocol) AS request_protocol FROM model_catalog c
                LEFT JOIN channel_model_bindings b ON b.model_id=c.id AND b.channel_id=? WHERE c.id=?""",
                               (channel_id, model_id)).fetchone()
        if row is None:
            raise KeyError("常用模型不存在")
        return dict(row)

    def bind(self, channel_id: int, model_id: int, upstream_model: str, protocol: str):
        channel = self.registry.get(channel_id, secret=True)
        model = self.binding(channel_id, model_id)
        upstream_model = model_name(upstream_model)
        if channel["api_key"] in upstream_model:
            raise RegistryError("模型 ID 不能包含凭据")
        if protocol not in PROTOCOLS or (model["model"] == "gpt-6-astra" and protocol != "responses"):
            raise RegistryError("模型协议无效；GPT-6 Astra 使用 Responses")
        with self.registry.connect() as conn:
            conn.execute("""INSERT INTO channel_model_bindings VALUES(?,?,?,?)
                ON CONFLICT(channel_id,model_id) DO UPDATE SET upstream_model=excluded.upstream_model,
                protocol=excluded.protocol""", (channel_id, model_id, upstream_model, protocol))

    def discoveries(self):
        with self.registry.connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM channel_model_discovery")]
        for row in rows:
            row["models"] = json.loads(row.pop("models_json"))
            row.pop("request_id")
        return {row["channel_id"]: row for row in rows}

    def begin_fetch(self, channel_id: int, auth_kind: str):
        request_id = uuid.uuid4().hex
        with self.registry.connect() as conn:
            conn.execute("""INSERT INTO channel_model_discovery(channel_id,request_id,attempted_at,error,auth_kind)
                VALUES(?,?,?,'fetching',?) ON CONFLICT(channel_id) DO UPDATE SET
                request_id=excluded.request_id, attempted_at=excluded.attempted_at,
                error='fetching', auth_kind=excluded.auth_kind""", (channel_id, request_id, time.time(), auth_kind))
        return request_id

    def finish_fetch(self, channel_id, request_id, fingerprint, models=None, error=""):
        with self.registry.connect() as conn:
            if models is None:
                conn.execute("UPDATE channel_model_discovery SET error=? WHERE channel_id=? AND request_id=?",
                             (error, channel_id, request_id))
            else:
                conn.execute("""UPDATE channel_model_discovery SET error='', succeeded_at=?,
                    connection_fingerprint=?, models_json=? WHERE channel_id=? AND request_id=?""",
                             (time.time(), fingerprint, json.dumps(models), channel_id, request_id))
