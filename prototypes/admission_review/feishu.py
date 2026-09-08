from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .mapping import DEFAULT_CHANNEL_FIELD, DEFAULT_GROUP_FIELD


FEISHU_ORIGIN = "https://open.feishu.cn"


class FeishuError(RuntimeError):
    """A deliberately redacted Feishu delivery failure."""


@dataclass(frozen=True)
class FeishuSettings:
    app_id: str = ""
    app_secret: str = ""
    app_token: str = ""
    table_id: str = ""
    channel_field: str = DEFAULT_CHANNEL_FIELD
    group_field: str = DEFAULT_GROUP_FIELD

    @classmethod
    def from_env(cls) -> "FeishuSettings":
        return cls(
            app_id=os.environ.get("ADMISSION_FEISHU_APP_ID", "").strip(),
            app_secret=os.environ.get("ADMISSION_FEISHU_APP_SECRET", "").strip(),
            app_token=os.environ.get("ADMISSION_FEISHU_APP_TOKEN", "").strip(),
            table_id=os.environ.get("ADMISSION_FEISHU_TABLE_ID", "").strip(),
            channel_field=(
                os.environ.get("ADMISSION_FEISHU_CHANNEL_FIELD", "").strip()
                or DEFAULT_CHANNEL_FIELD
            ),
            group_field=(
                os.environ.get("ADMISSION_FEISHU_GROUP_FIELD", "").strip()
                or DEFAULT_GROUP_FIELD
            ),
        )

    @property
    def configured(self) -> bool:
        return all((self.app_id, self.app_secret, self.app_token, self.table_id))

    @property
    def configuration_status(self) -> str:
        values = (self.app_id, self.app_secret, self.app_token, self.table_id)
        if self.configured:
            return "ready"
        if any(values):
            return "incomplete"
        return "missing"


class BitableWriter:
    def __init__(
        self,
        settings: FeishuSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not settings.configured:
            raise ValueError("飞书多维表格配置不完整")
        self.settings = settings
        self.transport = transport
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def _post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=FEISHU_ORIGIN,
                timeout=httpx.Timeout(15.0),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                response = await client.post(path, **kwargs)
        except httpx.HTTPError as exc:
            raise FeishuError(f"飞书网络请求失败：{type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise FeishuError(f"飞书接口返回非 JSON（HTTP {response.status_code}）") from exc
        code = payload.get("code") if isinstance(payload, dict) else None
        if response.status_code != 200 or code not in (0, None):
            raise FeishuError(
                f"飞书接口返回失败（HTTP {response.status_code}，code {code}）"
            )
        if not isinstance(payload, dict):
            raise FeishuError("飞书接口返回格式不正确")
        return payload

    async def _tenant_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token
        async with self._token_lock:
            now = time.monotonic()
            if self._token and now < self._token_expires_at:
                return self._token
            payload = await self._post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self.settings.app_id,
                    "app_secret": self.settings.app_secret,
                },
            )
            token = payload.get("tenant_access_token")
            if not isinstance(token, str) or not token:
                raise FeishuError("飞书接口未返回 tenant_access_token")
            expires = payload.get("expire", 7200)
            lifetime = float(expires) if isinstance(expires, (int, float)) else 7200.0
            self._token = token
            self._token_expires_at = now + max(60.0, lifetime - 60.0)
            return token

    async def create_record(self, fields: dict[str, str]) -> str:
        if set(fields) != {self.settings.channel_field, self.settings.group_field}:
            raise ValueError("飞书写入必须且只能包含渠道和测试分组")
        for identifier in (self.settings.app_token, self.settings.table_id):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
                raise ValueError("飞书多维表格标识格式不正确")
        token = await self._tenant_token()
        app_token = quote(self.settings.app_token, safe="")
        table_id = quote(self.settings.table_id, safe="")
        payload = await self._post(
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers={"Authorization": f"Bearer {token}"},
            json={"fields": fields},
        )
        record = payload.get("data", {}).get("record", {})
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise FeishuError("飞书接口未返回记录 ID")
        return record_id
