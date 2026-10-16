from __future__ import annotations

import asyncio
import json

import httpx

from features.integrity.strategies import canonical_hash
from shared.network import guarded_transport
from .domain import endpoint


class NewAPIError(ValueError):
    pass


def json_object(value):
    if value is None or value == "":
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError, RecursionError):
        raise NewAPIError("newapi_invalid_channel") from None
    if not isinstance(parsed, dict):
        raise NewAPIError("newapi_invalid_channel")
    return parsed


def default_settings(value, defaults):
    settings = json_object(value)
    return all(key in defaults and any(type(item) is type(allowed) and item == allowed
                                      for allowed in defaults[key])
               for key, item in settings.items())


# SOURCE: rc.26 relaykit/dto/channel_settings.go. Unknown settings fail closed.
SETTINGS = {"force_format": (False,), "thinking_to_content": (False,), "proxy": ("",),
            "pass_through_body_enabled": (False,), "system_prompt": ("",),
            "system_prompt_override": (False,), "http_protocol": ("", "auto"),
            "http2_connection_shards": (0, 1)}
OTHER_SETTINGS = {"azure_responses_version": ("",), "vertex_key_type": ("",), "aws_key_type": ("",),
                  "openrouter_enterprise": (None, False), "claude_beta_query": (False,),
                  "allow_service_tier": (False,), "allow_inference_geo": (False,), "allow_speed": (False,),
                  "allow_safety_identifier": (False,), "disable_store": (False,),
                  "allow_include_obfuscation": (False,), "disable_task_polling_sleep": (False,),
                  "upstream_model_update_check_enabled": (False,),
                  "upstream_model_update_auto_sync_enabled": (False,),
                  "upstream_model_update_last_check_time": (0,),
                  "upstream_model_update_last_detected_models": ([],),
                  "upstream_model_update_last_removed_models": ([],),
                  "upstream_model_update_ignored_models": ([],), "advanced_custom": (None,)}


def channel_projection(raw):
    if (not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] < 1
            or type(raw.get("status")) is not int or raw["status"] not in {1, 2, 3}):
        raise NewAPIError("newapi_invalid_channel")
    def values(field):
        value = raw.get(field)
        if not isinstance(value, str) or len(value) > 100000:
            raise NewAPIError("newapi_invalid_channel")
        items = sorted(set(v.strip() for v in value.split(",") if v.strip()))
        if not items or len(items) > 2000 or any(len(v) > 255 for v in items):
            raise NewAPIError("newapi_invalid_channel")
        return items
    info = raw.get("channel_info") or {}
    if not isinstance(info, dict):
        raise NewAPIError("newapi_invalid_channel")
    volatile = {"key", "status", "response_time", "test_time", "used_quota", "balance",
                "balance_updated_time", "created_time", "channel_info"}
    stable = {k: v for k, v in raw.items() if k not in volatile}
    # UpdateChannelStatus writes these two fields on every status transition.
    stable["other_info"] = {k: v for k, v in json_object(raw.get("other_info")).items()
                            if k not in {"status_reason", "status_time"}}
    try:
        config_hash = canonical_hash(stable)
    except (TypeError, ValueError):
        raise NewAPIError("newapi_invalid_channel") from None
    overrides = (any(bool(json_object(raw.get(k))) for k in
                     ("param_override", "header_override", "status_code_mapping"))
                 or any(raw.get(k) not in (None, "") for k in ("other", "openai_organization"))
                 or not default_settings(raw.get("setting"), SETTINGS)
                 or not default_settings(raw.get("settings"), OTHER_SETTINGS))
    return {"id": raw["id"], "status": raw["status"], "models": values("models"), "groups": values("group"),
            "base_url": raw.get("base_url") or "", "model_mapping": raw.get("model_mapping") or "{}",
            "config_hash": config_hash, "multi_key": bool(info.get("is_multi_key")),
            "type": raw.get("type"), "overrides": overrides}


class NewAPI:
    """Only rc.26 management reads and single-channel status writes are exposed."""
    def __init__(self, config, *, transport=None):
        self.base = endpoint(config["base_url"])
        self.headers = {"Authorization": "Bearer " + config["token"]}
        if config.get("user_id"):
            self.headers["New-Api-User"] = str(config["user_id"])
        self.transport = transport

    async def request(self, method, path, *, body=None, params=None):
        try:
            return await asyncio.wait_for(self._request(method, path, body=body, params=params), 20)
        except NewAPIError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, asyncio.TimeoutError, TimeoutError, RecursionError):
            raise NewAPIError("newapi_request_failed") from None

    async def _request(self, method, path, *, body, params):
        async with httpx.AsyncClient(transport=self.transport or guarded_transport(), trust_env=False,
                                     follow_redirects=False, timeout=20) as client:
            async with client.stream(method, self.base + path, headers=self.headers,
                                     json=body, params=params) as response:
                if response.status_code != 200:
                    raise NewAPIError("newapi_http_error")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 4 * 1024 * 1024:
                        raise NewAPIError("newapi_response_limit")
                result = json.loads(content)
                if not isinstance(result, dict) or result.get("success") is not True:
                    raise NewAPIError("newapi_request_rejected")
                return result.get("data")

    async def verify_version(self):
        data = await self.request("GET", "/api/status")
        if not isinstance(data, dict) or data.get("version") not in {"v1.0.0-rc.26", "1.0.0-rc.26"}:
            raise NewAPIError("newapi_version_not_rc26")

    async def channel(self, channel_id):
        row = channel_projection(await self.request("GET", f"/api/channel/{channel_id}"))
        if row["id"] != channel_id:
            raise NewAPIError("newapi_channel_id_mismatch")
        return row

    async def channels(self):
        rows, total = {}, None
        for page in range(1, 101):
            data = await self.request("GET", "/api/channel/", params={"p": page, "page_size": 100, "id_sort": "true"})
            if (not isinstance(data, dict) or not isinstance(data.get("items"), list)
                    or type(data.get("total")) is not int or not 0 <= data["total"] <= 10000):
                raise NewAPIError("newapi_invalid_inventory")
            if total is not None and total != data["total"]:
                raise NewAPIError("newapi_inventory_changed")
            total = data["total"]
            for raw in data["items"]:
                row = channel_projection(raw)
                if row["id"] in rows:
                    raise NewAPIError("newapi_inventory_changed")
                rows[row["id"]] = row
            if len(rows) == total:
                return list(rows.values())
            if not data["items"] or len(rows) > total:
                break
        raise NewAPIError("newapi_inventory_incomplete")

    async def set_status(self, channel_id, status):
        if status not in {1, 2}:
            raise ValueError("invalid channel status")
        result = await self.request("POST", f"/api/channel/{channel_id}/status", body={"status": status})
        if result is not True:
            raise NewAPIError("newapi_status_not_changed")
