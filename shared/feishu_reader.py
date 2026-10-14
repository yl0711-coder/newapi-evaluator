"""Read only the channel columns through Feishu's public application APIs."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx

from .network import guarded_transport
from .registry import RegistryError


FIELDS = {"渠道名称": {1}, "模型家族": {1}, "倍率": {2}, "baseurl": {1, 15},
          "apikey": {1}, "是否在线": {3}}
API = "https://open.feishu.cn/open-apis"
MAX_ROWS = 10000


class FeishuReadError(RegistryError):
    pass


def table_address(value: str) -> dict:
    try:
        parts = urlsplit(value.strip())
        match = re.fullmatch(r"/(base|wiki)/([A-Za-z0-9]{6,128})/?", parts.path)
        tables = parse_qs(parts.query).get("table", [])
        valid = (parts.scheme == "https" and parts.hostname
                 and parts.hostname.endswith(".feishu.cn") and parts.port in {None, 443}
                 and parts.username is None and parts.password is None and not parts.fragment
                 and match and len(tables) == 1 and re.fullmatch(r"tbl[A-Za-z0-9]{6,128}", tables[0]))
    except ValueError:
        valid = False
    if not valid:
        raise RegistryError("请填写包含 table 参数的飞书多维表格链接（base 或 wiki）")
    kind, token = match.groups()
    return {"kind": kind, "token": token, "table_id": tables[0],
            "url": f"https://{parts.hostname}/{kind}/{token}?table={tables[0]}"}


def text_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and all(isinstance(part, dict) and part.get("type") == "text"
                                       and isinstance(part.get("text"), str) for part in value):
        return "".join(part["text"] for part in value).strip()
    raise RegistryError("文本字段格式无效")


def url_cell(value) -> str:
    if isinstance(value, dict):
        if not isinstance(value.get("link"), str):
            raise RegistryError("baseurl 超链接缺少地址")
        return value["link"].strip()
    return text_cell(value)


class FeishuReader:
    def __init__(self, *, transport=None):
        self.transport = transport

    async def _request(self, client, method, path, **kwargs):
        try:
            async with client.stream(method, API + path, **kwargs) as response:
                if response.status_code != 200:
                    raise FeishuReadError(f"飞书读取失败（HTTP {response.status_code}），请检查应用授权")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > 4 * 1024 * 1024:
                        raise FeishuReadError("飞书单页数据超过读取上限")
                    body.extend(chunk)
            value = json.loads(body)
        except FeishuReadError:
            raise
        except httpx.HTTPError:
            raise FeishuReadError("无法连接飞书，请检查网络后重试") from None
        except (ValueError, UnicodeError, RecursionError):
            raise FeishuReadError("飞书返回的数据格式无效") from None
        if not isinstance(value, dict) or type(value.get("code")) is not int:
            raise FeishuReadError("飞书返回的数据格式无效")
        if value["code"] != 0:
            raise FeishuReadError("飞书拒绝读取，请检查应用凭据、读取权限及表格协作者授权")
        return value

    async def _pages(self, client, path, *, fields=False):
        items, seen = [], set()
        token = ""
        for _ in range(20):
            params = {"page_size": 100 if fields else 500}
            if token:
                params["page_token"] = token
            kwargs = {} if fields else {"json": {"field_names": list(FIELDS), "automatic_fields": False}}
            response = await self._request(client, "GET" if fields else "POST", path, params=params, **kwargs)
            data = response.get("data")
            if (not isinstance(data, dict) or not isinstance(data.get("items"), list)
                    or type(data.get("has_more")) is not bool):
                raise FeishuReadError("飞书分页结果不完整，本次未同步")
            items.extend(data["items"])
            if len(items) > MAX_ROWS:
                raise FeishuReadError("飞书记录超过 10000 条，本次未同步")
            if not data["has_more"]:
                return items
            token = data.get("page_token")
            if not isinstance(token, str) or not token or len(token) > 2048 or token in seen:
                raise FeishuReadError("飞书分页游标异常，本次未同步")
            seen.add(token)
        raise FeishuReadError("飞书分页未读完，本次未同步")

    async def read(self, settings: dict, secret: str) -> dict:
        address = table_address(settings["table_url"])
        async with httpx.AsyncClient(transport=self.transport or guarded_transport(), timeout=15,
                                     trust_env=False, follow_redirects=False) as client:
            auth = await self._request(client, "POST", "/auth/v3/tenant_access_token/internal",
                                       json={"app_id": settings["app_id"], "app_secret": secret})
            token = auth.get("tenant_access_token")
            if (not isinstance(token, str) or not token or len(token) > 4096
                    or any(ord(c) < 33 or ord(c) > 126 for c in token)):
                raise FeishuReadError("飞书未返回有效的应用访问凭证")
            client.headers["Authorization"] = "Bearer " + token
            app_token = address["token"]
            if address["kind"] == "wiki":
                response = await self._request(client, "GET", "/wiki/v2/spaces/get_node",
                                               params={"token": app_token})
                data = response.get("data")
                node = data.get("node") if isinstance(data, dict) else None
                if (not isinstance(node, dict) or node.get("obj_type") != "bitable"
                        or not isinstance(node.get("obj_token"), str)
                        or not re.fullmatch(r"[A-Za-z0-9]{6,128}", node["obj_token"])):
                    raise FeishuReadError("该知识库链接未解析为多维表格")
                app_token = node["obj_token"]
            path = f"/bitable/v1/apps/{app_token}/tables/{address['table_id']}"
            columns = await self._pages(client, path + "/fields", fields=True)
            schema = {field.get("field_name"): field.get("type") for field in columns if isinstance(field, dict)}
            if any(type(schema.get(name)) is not int or schema[name] not in kinds for name, kinds in FIELDS.items()):
                raise FeishuReadError("请核对表格列名及类型：渠道名称、模型家族、倍率、baseurl、apikey、是否在线")
            rows = await self._pages(client, path + "/records/search")
            ids = set()
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("record_id"), str)
                        or not re.fullmatch(r"rec[A-Za-z0-9]{1,128}", row["record_id"])
                        or row["record_id"] in ids or not isinstance(row.get("fields"), dict)):
                    raise FeishuReadError("飞书记录身份重复或数据不完整，本次未同步")
                ids.add(row["record_id"])
            identity = hashlib.sha256(f"{app_token}/{address['table_id']}".encode()).hexdigest()
            return {"source_id": identity, "rows": rows}
