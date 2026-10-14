"""Manual Feishu inventory sync into the existing encrypted channel registry."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import Counter

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .feishu_reader import FeishuReadError, FeishuReader, table_address, text_cell, url_cell
from .registry import Conflict, RegistryError, get_registry, normalize


router = APIRouter(prefix="/api/registry/feishu")
META_KEY = "feishu_channel_sync"


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    app_id: str = Field(min_length=1, max_length=128)
    app_secret: SecretStr = Field(default=SecretStr(""), max_length=4096)
    table_url: str = Field(min_length=1, max_length=1000)
    version: int = Field(default=0, ge=0)


class SyncInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_read: bool = False


class ChannelSync:
    def __init__(self, registry):
        self.registry = registry
        with registry.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS feishu_channel_bindings (
                source_id TEXT NOT NULL, record_id TEXT NOT NULL, channel_id INTEGER NOT NULL UNIQUE,
                PRIMARY KEY(source_id,record_id))""")

    @staticmethod
    def _load(conn):
        row = conn.execute("SELECT value FROM registry_meta WHERE key=?", (META_KEY,)).fetchone()
        return json.loads(row[0]) if row else {"version": 0}

    @staticmethod
    def _store(conn, data):
        conn.execute("INSERT INTO registry_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (META_KEY, json.dumps(data, ensure_ascii=False)))

    def public(self):
        with self.registry.connect() as conn:
            value = self._load(conn)
        return {"version": value["version"], "app_id": value.get("app_id", ""),
                "table_url": value.get("table_url", ""), "has_secret": bool(value.get("secret_enc")),
                "configured": bool(value.get("secret_enc")), "running": value.get("lease_until", 0) > time.time(),
                "last_result": value.get("last_result"), "last_error": value.get("last_error", ""),
                "last_attempt": value.get("last_attempt"), "last_success": value.get("last_success")}

    def configure(self, body: SettingsInput):
        if not re.fullmatch(r"cli_[A-Za-z0-9]+", body.app_id):
            raise RegistryError("App ID 格式无效")
        url = table_address(body.table_url)["url"]
        secret = body.app_secret.get_secret_value().strip()
        if secret and (not secret.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in secret)):
            raise RegistryError("App Secret 格式无效")
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = self._load(conn)
            if previous["version"] != body.version:
                raise Conflict("飞书设置已被修改，请刷新后重试")
            if not secret and (not previous.get("secret_enc") or previous.get("app_id") != body.app_id):
                raise RegistryError("首次配置或更换应用时需要填写 App Secret")
            same_source = previous.get("table_url") == url and previous.get("app_id") == body.app_id
            value = dict(previous) if same_source else {}
            value.pop("source_id", None)
            value.update(version=body.version + 1, app_id=body.app_id, table_url=url,
                         secret_enc=self.registry.encrypt(secret) if secret else previous["secret_enc"],
                         lease_id="", lease_until=0, last_error="")
            self._store(conn, value)
        return self.public()

    def begin(self):
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            value = self._load(conn)
            if not value.get("secret_enc"):
                raise RegistryError("请先配置飞书应用和表格链接")
            if value.get("lease_until", 0) > time.time():
                raise Conflict("已有同步正在进行，请稍后刷新结果")
            value.update(lease_id=uuid.uuid4().hex, lease_until=time.time() + 120, last_attempt=time.time())
            self._store(conn, value)
        return value

    def fail(self, settings, message):
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._load(conn)
            if current.get("lease_id") == settings["lease_id"]:
                current.update(lease_id="", lease_until=0, last_error=message)
                self._store(conn, current)

    @staticmethod
    def prepare(rows, secret):
        prepared = []
        secrets = [secret]
        for row in rows:
            try:
                key = text_cell(row["fields"].get("apikey"))
                if key:
                    secrets.append(key)
            except RegistryError:
                pass
        for row in rows:
            item = {"record_id": row["record_id"], "action": "skip", "reason": ""}
            fields = row["fields"]
            try:
                status = text_cell(fields.get("是否在线"))
                key = text_cell(fields.get("apikey"))
                if status in {"已下线", "待确认"} or (status == "已上线" and not key):
                    item["action"] = "disable"
                elif status != "已上线":
                    item["reason"] = "上线状态未填写或无法识别"
                else:
                    multiplier = fields.get("倍率")
                    if type(multiplier) not in {int, float}:
                        raise RegistryError("请补充有效的采购倍率")
                    data = normalize({"name": text_cell(fields.get("渠道名称")),
                                      "scope": text_cell(fields.get("模型家族")),
                                      "base_url": url_cell(fields.get("baseurl")), "api_key": key,
                                      "multiplier": multiplier, "source_kind": "feishu",
                                      "enabled": True, "status": "online"})
                    if any(value in data[field] for value in secrets for field in ("name", "scope", "base_url")):
                        raise RegistryError("渠道资料中含有凭据，请只在 apikey 列填写密钥")
                    item.update(action="upsert", data=data)
            except (RegistryError, ValueError, TypeError, OverflowError):
                item.update(action="skip", reason="请检查 baseurl、apikey、倍率及文本字段格式")
            prepared.append(item)
        return prepared

    def apply(self, settings, snapshot, prepared):
        result = {"total": len(prepared), "eligible": sum(x["action"] == "upsert" for x in prepared),
                  "added": 0, "updated": 0, "unchanged": 0, "disabled": 0, "skipped": 0,
                  "pending": 0, "missing": 0, "issues": [], "synced_at": time.time()}
        source_id = snapshot["source_id"]
        active = Counter(self.registry._identity(x["data"]) for x in prepared if x["action"] == "upsert")
        with self.registry.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._load(conn)
            if (current["version"] != settings["version"] or current.get("lease_id") != settings["lease_id"]
                    or current.get("lease_until", 0) <= time.time()):
                raise Conflict("飞书设置或同步任务已变化，本次结果未写入")
            if current.get("source_id") not in {None, source_id}:
                raise Conflict("链接指向的多维表格已变化，请重新保存连接设置")
            bindings = {row["record_id"]: row["channel_id"] for row in conn.execute(
                "SELECT record_id,channel_id FROM feishu_channel_bindings WHERE source_id=?", (source_id,))}
            result["missing"] = len(set(bindings) - {x["record_id"] for x in prepared})
            for item in prepared:
                channel_id = bindings.get(item["record_id"])
                row = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone() if channel_id else None
                reason = item["reason"]
                if item["action"] == "disable":
                    if row and active[row["fingerprint"]]:
                        reason = "同一连接还有上线记录，请整理重复记录后同步"
                    elif row and (row["enabled"] or row["status"] == "online"):
                        conn.execute("UPDATE channels SET enabled=0,status='recorded',version=version+1,updated_at=? WHERE id=?",
                                     (time.time(), channel_id))
                        result["disabled"] += 1
                    else:
                        result["skipped"] += 1
                elif item["action"] == "upsert":
                    data = item["data"]
                    fingerprint = self.registry._identity(data)
                    duplicate = conn.execute("SELECT id FROM channels WHERE fingerprint=?", (fingerprint,)).fetchone()
                    if active[fingerprint] > 1:
                        reason = "表内有重复连接，请保留一条后同步"
                    elif duplicate and duplicate["id"] != channel_id:
                        reason = "公共库已存在相同连接，保留原记录，未覆盖"
                    elif row:
                        changed = (row["fingerprint"] != fingerprint or row["name"] != data["name"]
                                   or not row["enabled"] or row["status"] != "online"
                                   or self.registry.credential_status(row) != "available")
                        if changed:
                            conn.execute("""UPDATE channels SET name=?,base_url=?,scope=?,multiplier=?,key_enc=?,fingerprint=?,
                                enabled=1,status='online',version=version+1,updated_at=? WHERE id=?""",
                                (data["name"], data["base_url"], data["scope"], data["multiplier"], self.registry.encrypt(data["api_key"]),
                                 fingerprint, time.time(), channel_id))
                            result["updated"] += 1
                        else:
                            result["unchanged"] += 1
                    else:
                        channel_id, _ = self.registry._insert(conn, data)
                        conn.execute("INSERT OR REPLACE INTO feishu_channel_bindings VALUES(?,?,?)",
                                     (source_id, item["record_id"], channel_id))
                        result["added"] += 1
                if reason:
                    result["pending"] += 1
                    if len(result["issues"]) < 100:
                        result["issues"].append({"record_id": item["record_id"], "reason": reason})
            current.update(source_id=source_id, last_result=result, last_success=result["synced_at"],
                           last_error="", lease_id="", lease_until=0)
            self._store(conn, current)
        return result

    async def sync(self, reader=None):
        settings = self.begin()
        try:
            secret = self.registry._cipher.decrypt(settings["secret_enc"].encode()).decode()
            snapshot = await asyncio.wait_for((reader or FeishuReader()).read(settings, secret), timeout=90)
            return self.apply(settings, snapshot, self.prepare(snapshot["rows"], secret))
        except asyncio.CancelledError:
            self.fail(settings, "同步已中断，保留上次同步结果")
            raise
        except (InvalidToken, UnicodeError):
            message = "飞书凭据无法解密，请重新填写 App Secret"
            self.fail(settings, message)
            raise RegistryError(message) from None
        except (TimeoutError, asyncio.TimeoutError):
            message = "飞书同步超时，现有渠道未改变"
            self.fail(settings, message)
            raise FeishuReadError(message) from None
        except RegistryError as exc:
            self.fail(settings, str(exc))
            raise
        except Exception:
            message = "飞书同步未完成，现有渠道未改变，请重试"
            self.fail(settings, message)
            raise RegistryError(message) from None


@router.get("/settings")
async def settings():
    return ChannelSync(get_registry()).public()


@router.put("/settings")
async def configure(body: SettingsInput):
    try:
        return ChannelSync(get_registry()).configure(body)
    except RegistryError as exc:
        raise HTTPException(409 if isinstance(exc, Conflict) else 400, str(exc)) from None


@router.post("/sync")
async def sync(body: SyncInput):
    if not body.confirm_read:
        raise HTTPException(400, "请通过手动同步按钮确认读取飞书表格")
    try:
        return await ChannelSync(get_registry()).sync()
    except RegistryError as exc:
        status = 409 if isinstance(exc, Conflict) else 502 if isinstance(exc, FeishuReadError) else 400
        raise HTTPException(status, str(exc)) from None
