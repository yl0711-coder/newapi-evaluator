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


async def request_models(base_url, api_key, auth_kind="auto", transport=None):
    """Read the same bounded list for saved and temporary connections."""
    kinds = ("openai", "anthropic") if auth_kind == "auto" else (auth_kind,)
    try:
        for kind in kinds:
            models, error = await _request_models(base_url, api_key, kind, transport)
            if error not in {"http_401", "http_403"}:
                return models, error
        return models, error
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return None, "timeout"
    except (httpx.HTTPError, ValueError, RegistryError):
        return None, "fetch_failed"


async def _request_models(base_url, api_key, auth_kind, transport):
    headers = {"Authorization": f"Bearer {api_key}"} if auth_kind == "openai" else {
        "x-api-key": api_key, "anthropic-version": "2023-06-01"}
    async with httpx.AsyncClient(transport=transport or guarded_transport(), timeout=15,
                                 follow_redirects=False, trust_env=False) as client:
        async def collect():
            found, cursors = set(), set()
            params = {}
            for _ in range(20):
                async with client.stream("GET", models_url(base_url), headers=headers, params=params) as response:
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
                    if api_key and api_key in name:
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
        return await asyncio.wait_for(collect(), timeout=30)


async def fetch_models(registry, channel_id, auth_kind, transport=None):
    channel = registry.resolve(channel_id)
    catalog = Catalog(registry)
    fingerprint = registry.connection_fingerprint(channel)
    request_id = catalog.begin_fetch(channel_id, auth_kind)
    try:
        models, error = await request_models(channel["base_url"], channel["api_key"], auth_kind, transport)
        if registry.connection_fingerprint(registry.get(channel_id, secret=True)) != fingerprint:
            models, error = None, "connection_changed"
        catalog.finish_fetch(channel_id, request_id, fingerprint, models, error)
    except asyncio.CancelledError:
        catalog.finish_fetch(channel_id, request_id, fingerprint, error="cancelled")
        raise
    return {"channel_id": channel_id, "ok": not error, "error": error, "models": models or []}
