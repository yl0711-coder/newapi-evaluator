"""渠道快照：复用现有配置解析、协议适配、出站安全与脱敏规则。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import egress, importer, probes, protocol
from .security import mask, redact_url, scrub

SCHEMA_VERSION = "channel-snapshot/v1"


class SnapshotConfigError(ValueError):
    """快照输入不完整或不受支持。"""


def parse_snapshot_config(text: str) -> dict[str, str]:
    """使用已有导入器解析渠道配置，不引入第二套字段语义。"""
    try:
        parsed = importer.parse_config(text)
    except (TypeError, ValueError) as exc:
        raise SnapshotConfigError("输入配置无效") from exc
    if not parsed.get("base_url"):
        raise SnapshotConfigError("缺少渠道 base_url")
    if not parsed.get("api_key"):
        raise SnapshotConfigError("缺少渠道凭据")
    if parsed.get("protocol") not in {"openai", "anthropic"}:
        raise SnapshotConfigError("渠道协议仅支持 openai 或 anthropic")
    return {
        "name": str(parsed.get("name") or "").strip(),
        "base_url": str(parsed["base_url"]).strip(),
        "model": str(parsed.get("model") or "").strip(),
        "api_key": str(parsed["api_key"]),
        "protocol": str(parsed["protocol"]),
    }


def _safe_alias(value: str, key: str) -> str:
    alias = scrub(value or "未命名渠道", key)
    if key:
        alias = alias.replace(mask(key), "[已脱敏]")
    alias = " ".join((alias or "未命名渠道").split())
    return alias[:160]


def _json_fingerprint(prefix: str, value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _snapshot_identity(config: dict[str, str]) -> tuple[str, str, str]:
    endpoint_masked = redact_url(config["base_url"])
    endpoint_parts = urlsplit(endpoint_masked)
    base_url_masked = urlunsplit((
        endpoint_parts.scheme, endpoint_parts.netloc, "", "", "",
    ))
    stable_id = _json_fingerprint("ch1", {
        "protocol": config["protocol"],
        "base_url_masked": base_url_masked.rstrip("/"),
    })
    fingerprint = _json_fingerprint("cs1", {
        "stable_id": stable_id,
        "protocol": config["protocol"],
        "endpoint_masked": endpoint_masked.rstrip("/"),
        "model": config["model"],
    })
    return base_url_masked, stable_id, fingerprint


def _safe_http_error(status_code: int) -> str:
    reason = probes.classify(status_code, "")
    return f"{reason}（HTTP {status_code}）"


async def build_snapshot(
    config: dict[str, str], *, timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """探测模型清单并返回脱敏快照；从不保存原始响应或请求头。"""
    if not 1 <= timeout_seconds <= 120:
        raise SnapshotConfigError("探测超时必须在 1 到 120 秒之间")

    base_url_masked, stable_id, fingerprint = _snapshot_identity(config)
    model_count = 1 if config.get("model") else 0
    count_source = "configuration" if model_count else "unavailable"
    probe_status = "待确认"
    http_status: int | None = None
    error_summary = ""

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            max_redirects=egress.EGRESS_MAX_REDIRECTS,
            event_hooks=egress.event_hooks(),
            trust_env=False,
        ) as client:
            response = await client.get(
                protocol.models_url(config["protocol"], config["base_url"]),
                headers=protocol.headers(config["protocol"], config["api_key"]),
            )
        egress.ensure_response_size(response)
        http_status = response.status_code
        if not 200 <= response.status_code < 300:
            error_summary = _safe_http_error(response.status_code)
        else:
            try:
                names = protocol.list_models(config["protocol"], response.json())
            except ValueError:
                names = []
                error_summary = "协议（模型清单不是有效 JSON）"
            if names:
                model_count = len(set(names))
                count_source = "upstream"
                probe_status = "通过"
            elif not error_summary:
                error_summary = "协议（模型清单为空或格式不受支持）"
    except httpx.TimeoutException:
        error_summary = "超时（模型清单探测未完成）"
    except egress.EgressDenied:
        error_summary = "出站安全策略拒绝该目标"
    except httpx.TooManyRedirects:
        error_summary = "协议（重定向超过安全上限）"
    except httpx.RequestError as exc:
        error_summary = f"连接失败（{type(exc).__name__}）"

    extracted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": SCHEMA_VERSION,
        "channel": {
            "alias": _safe_alias(config["name"], config["api_key"]),
            "stable_id": stable_id,
            "protocol": config["protocol"],
            "base_url_masked": base_url_masked,
            "model_count": model_count,
            "model_count_source": count_source,
            "configuration_fingerprint": fingerprint,
        },
        "extracted_at": extracted_at,
        "probe": {
            "status": probe_status,
            "http_status": http_status,
            "error_summary": error_summary,
        },
    }
