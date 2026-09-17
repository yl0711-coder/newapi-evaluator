"""Bounded, explicitly requested model-list discovery using saved credentials."""
import asyncio
import json
import re
from urllib.parse import urlsplit, urlunsplit

import httpx

from shared.network import guarded_transport
from shared.registry import RegistryError
from .catalog import Catalog, model_name


MAX_BYTES = 2_000_000
MAX_MODELS = 10000


def models_url(base_url):
    parts = urlsplit(base_url)
    path = re.sub(r"/(chat/completions|responses|messages|models)/?$", "", parts.path.rstrip("/"))
    path += "/models" if path.endswith("/v1") else "/v1/models"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


async def fetch_models(registry, channel_id, auth_kind):
    channel = registry.resolve(channel_id)
    catalog = Catalog(registry)
    fingerprint = registry.connection_fingerprint(channel)
    request_id = catalog.begin_fetch(channel_id, auth_kind)
    error = "fetch_failed"
    try:
        headers = {"Authorization": f"Bearer {channel['api_key']}"} if auth_kind == "openai" else {
            "x-api-key": channel["api_key"], "anthropic-version": "2023-06-01"}
        async with httpx.AsyncClient(transport=guarded_transport(), timeout=15,
                                     follow_redirects=False, trust_env=False) as client:
            async def collect():
                found = set()
                params = {}
                cursors = set()
                for _ in range(20):
                    async with client.stream("GET", models_url(channel["base_url"]), headers=headers, params=params) as response:
                        if response.status_code != 200:
                            return None, f"http_{response.status_code}"
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > MAX_BYTES:
                                return None, "list_too_large"
                    data = json.loads(content)
                    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                        return None, "invalid_list"
                    for item in data["data"]:
                        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                            return None, "invalid_list"
                        name = model_name(item["id"])
                        if channel["api_key"] in name:
                            return None, "invalid_list"
                        found.add(name)
                        if len(found) > MAX_MODELS:
                            return None, "list_too_large"
                    if not data.get("has_more"):
                        return sorted(found), ""
                    cursor = data.get("last_id")
                    if auth_kind != "anthropic" or not isinstance(cursor, str) or cursor not in found or cursor in cursors:
                        return None, "incomplete_list"
                    cursors.add(cursor)
                    params = {"after_id": cursor, "limit": 1000}
                return None, "incomplete_list"
            models, error = await asyncio.wait_for(collect(), timeout=30)
        if registry.connection_fingerprint(registry.get(channel_id, secret=True)) != fingerprint:
            models, error = None, "connection_changed"
        catalog.finish_fetch(channel_id, request_id, fingerprint, models, error)
    except asyncio.CancelledError:
        catalog.finish_fetch(channel_id, request_id, fingerprint, error="cancelled")
        raise
    except (asyncio.TimeoutError, httpx.TimeoutException):
        error = "timeout"
        catalog.finish_fetch(channel_id, request_id, fingerprint, error=error)
    except (httpx.HTTPError, ValueError, RegistryError):
        error = "fetch_failed"
        catalog.finish_fetch(channel_id, request_id, fingerprint, error=error)
    return {"channel_id": channel_id, "ok": not error, "error": error}
