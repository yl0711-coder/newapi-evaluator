from __future__ import annotations

import hashlib
import hmac
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from .config import DATA_DIR, prepare_data_dir


class RegistryError(ValueError):
    pass


class Conflict(RegistryError):
    pass


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    url = str(data.get("base_url", "")).strip().rstrip("/")
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError as exc:
        raise RegistryError("地址格式或端口无效") from exc
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise RegistryError("请填写完整的 HTTP 或 HTTPS 地址")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise RegistryError("地址不能包含账号、密码、查询参数或片段")
    multiplier = float(data.get("multiplier", 1))
    if not math.isfinite(multiplier) or not 0 < multiplier <= 1000:
        raise RegistryError("倍率必须大于 0 且不超过 1000")
    key = str(data.get("api_key", "")).strip()
    if not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise RegistryError("密钥必须是非空的可打印 ASCII 字符，且不超过 4096 字符")
    name = str(data.get("name") or parts.hostname).strip()
    if not name or len(name) > 160:
        raise RegistryError("渠道名称不能超过 160 字符")
    scope = str(data.get("scope", "")).strip().casefold()
    note = str(data.get("note", "")).strip()
    if len(scope) > 80 or len(note) > 2000:
        raise RegistryError("分类或备注过长")
    status = str(data.get("status", "recorded"))
    if status not in {"recorded", "online"}:
        raise RegistryError("渠道状态无效")
    return {"name": name, "base_url": url, "scope": scope, "multiplier": multiplier,
            "api_key": key, "note": note, "enabled": bool(data.get("enabled", True)),
            "source_kind": str(data.get("source_kind", "manual"))[:80], "status": status}


class Registry:
    def __init__(self, directory: Path = DATA_DIR):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory / "channels.db"
        self.key_path = self.directory / "channels.key"
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT 1 FROM sqlite_master WHERE name='registry_meta'").fetchone()
            if not self.key_path.exists():
                if existing:
                    raise RegistryError("公共渠道库缺少 channels.key，请恢复配套密钥文件")
                with self.key_path.open("xb") as file:
                    self.key_path.chmod(0o600)
                    file.write(Fernet.generate_key())
            self.key_path.chmod(0o600)
            self._key = self.key_path.read_bytes()
            try:
                self._cipher = Fernet(self._key)
            except (ValueError, TypeError) as exc:
                raise RegistryError("公共渠道库密钥文件无效") from exc
            conn.execute("CREATE TABLE IF NOT EXISTS registry_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            sentinel = conn.execute("SELECT value FROM registry_meta WHERE key='key_check'").fetchone()
            if sentinel:
                try:
                    self._cipher.decrypt(sentinel[0].encode())
                except InvalidToken as exc:
                    raise RegistryError("channels.key 与公共渠道库不匹配") from exc
            else:
                conn.execute("INSERT INTO registry_meta VALUES('key_check',?)", (self.encrypt("registry-v1"),))
            conn.execute("""CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, base_url TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT '', multiplier REAL NOT NULL, key_enc TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE, note TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL,
                source_kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'recorded', version INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        self.path.chmod(0o600)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def encrypt(self, value: str) -> str:
        return self._cipher.encrypt(value.encode()).decode()

    def _identity(self, data: dict) -> str:
        payload = json.dumps([data[k] for k in ("base_url", "scope", "multiplier", "api_key")])
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def public(row) -> dict:
        result = {k: row[k] for k in ("id", "name", "base_url", "scope", "multiplier", "note",
                                      "enabled", "source_kind", "status", "version", "created_at", "updated_at")}
        result["enabled"] = bool(result["enabled"])
        result.update(has_key=True, key_masked="***")
        return result

    def list(self) -> list[dict]:
        with self.connect() as conn:
            return [self.public(row) for row in conn.execute("SELECT * FROM channels ORDER BY name,scope,multiplier,id")]

    def get(self, channel_id: int, *, secret: bool = False) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
        if row is None:
            raise KeyError("公共渠道不存在")
        result = self.public(row)
        if secret:
            try:
                result["api_key"] = self._cipher.decrypt(row["key_enc"].encode()).decode()
            except InvalidToken as exc:
                raise RegistryError("渠道密钥无法解密，请检查配套密钥文件") from exc
        return result

    def resolve(self, channel_id: int) -> dict:
        result = self.get(channel_id, secret=True)
        if not result["enabled"]:
            raise RegistryError("该公共渠道已停用")
        return result

    def _insert(self, conn, data: dict) -> tuple[int, bool]:
        fingerprint = self._identity(data)
        row = conn.execute("SELECT id FROM channels WHERE fingerprint=?", (fingerprint,)).fetchone()
        if row:
            return row[0], False
        now = time.time()
        cur = conn.execute("""INSERT INTO channels
            (name,base_url,scope,multiplier,key_enc,fingerprint,note,enabled,source_kind,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (data["name"], data["base_url"], data["scope"], data["multiplier"],
            self.encrypt(data["api_key"]), fingerprint, data["note"], int(data["enabled"]), data["source_kind"], data["status"], now, now))
        return cur.lastrowid, True

    def import_records(self, records: list[dict]) -> dict:
        clean = [normalize(record) for record in records]
        if not clean or len(clean) > 1000:
            raise RegistryError("每批应包含 1 到 1000 条完整渠道记录")
        ids = []
        added = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for record in clean:
                channel_id, created = self._insert(conn, record)
                ids.append(channel_id)
                added += int(created)
        return {"ids": ids, "added": added, "skipped": len(clean) - added}

    def save(self, data: dict, channel_id: int | None = None, version: int | None = None) -> dict:
        if channel_id is None:
            clean = normalize(data)
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                new_id, created = self._insert(conn, clean)
                if not created:
                    raise Conflict("这条渠道资料已经存在，请通过编辑更新状态或配置")
            return self.get(new_id)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
            if row is None:
                raise KeyError("公共渠道不存在")
            if version != row["version"]:
                raise Conflict("渠道已被修改，请刷新后重新编辑")
            if not data.get("api_key"):
                data = {**data, "api_key": self._cipher.decrypt(row["key_enc"].encode()).decode()}
            clean = normalize(data)
            try:
                conn.execute("""UPDATE channels SET name=?,base_url=?,scope=?,multiplier=?,key_enc=?,fingerprint=?,
                    note=?,enabled=?,status=?,version=version+1,updated_at=? WHERE id=?""",
                    (clean["name"], clean["base_url"], clean["scope"], clean["multiplier"], self.encrypt(clean["api_key"]),
                     self._identity(clean), clean["note"], int(clean["enabled"]), clean["status"], time.time(), channel_id))
            except sqlite3.IntegrityError as exc:
                raise Conflict("相同地址、密钥、分类和倍率的渠道已存在") from exc
        return self.get(channel_id)


_registry: Registry | None = None


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        prepare_data_dir()
        _registry = Registry()
    return _registry
