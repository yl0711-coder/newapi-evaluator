"""Bounded Ark calls without retries, redirects, proxy inheritance or body logging."""
import asyncio
import base64
from dataclasses import dataclass, field
from contextvars import ContextVar
import io
import ipaddress
import json
import logging
import re
import socket
import time
from urllib.parse import urlsplit
import warnings

import httpx
import httpcore
from PIL import Image, UnidentifiedImageError

from shared.network import SocketEgressDenied, guarded_transport, socket_egress_denied
from .catalog import OFFICIAL_BASE_URL, request_body
from .media import MediaError, sanitize_mp4, valid_mp4
from .privacy import contains_caller_text

MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_VIDEO_BYTES = 128 * 1024 * 1024
MAX_JSON_BYTES = 36 * 1024 * 1024
MAX_PIXELS = 20_000_000
TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
USAGE_FIELDS = {"completion_tokens", "total_tokens", "output_tokens", "generated_images"}
SENSITIVE_HTTP_CONTEXT = ContextVar("volcengine_sensitive_http_context", default=False)


class _SensitiveHTTPFilter(logging.Filter):
    def filter(self, _record):
        return not SENSITIVE_HTTP_CONTEXT.get()


# HTTPcore debug records can contain response headers (including signed locations).
# Filters are scoped by async context and leave other modules' HTTP diagnostics intact.
for _logger_name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
    _logger = logging.getLogger(_logger_name)
    if not any(isinstance(value, _SensitiveHTTPFilter) for value in _logger.filters):
        _logger.addFilter(_SensitiveHTTPFilter())


@dataclass
class Result:
    status: str
    http_status: int | None = None
    error_category: str | None = None
    elapsed_ms: float | None = None
    task_id: str | None = None
    upstream_status: str | None = None
    usage: dict = field(default_factory=dict)
    media: bytes | None = field(default=None, repr=False)
    media_status: str = "not_available"
    media_bytes: int | None = None
    media_http_status: int | None = None


class BoundaryError(ValueError):
    def __init__(self, code, http_status=None):
        super().__init__(code)
        self.http_status = http_status


class MediaNetwork:
    """Bind HTTPS media sockets to public IPs, even if deployment allows private APIs."""
    def __init__(self, guarded_backend):
        self.backend = guarded_backend

    def __getattr__(self, name):
        return getattr(self.backend, name)

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        started = time.monotonic()
        try:
            rows = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM), timeout)
        except (socket.gaierror, TimeoutError):
            raise SocketEgressDenied("媒体域名无法安全解析") from None
        addresses = list(dict.fromkeys(row[4][0] for row in rows))
        if not addresses or any(not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast
                                or ipaddress.ip_address(address).is_reserved for address in addresses):
            raise SocketEgressDenied("媒体仅允许公网地址")
        last_error = None
        for address in addresses:
            remaining = None if timeout is None else max(0, timeout - (time.monotonic() - started))
            try:
                # The checked numeric address prevents a second hostname lookup/rebinding.
                # HTTPcore retains the original origin for Host and TLS SNI.
                return await self.backend.connect_tcp(address, port, remaining, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last_error = exc
        raise last_error


def http_category(status):
    if status == 401:
        return "authentication_rejected"
    if status == 402:
        return "balance_or_billing_required"
    if status == 403:
        return "permission_or_account_restriction"
    if status == 404:
        return "model_or_task_not_found"
    if status == 429:
        return "rate_limited"
    if 300 <= status < 400:
        return "redirect_rejected"
    if status >= 500:
        return "upstream_unavailable"
    return "invalid_parameters" if status == 400 else "http_error"


def safe_usage(value):
    if not isinstance(value, dict):
        return {}
    return {key: number for key, number in value.items() if key in USAGE_FIELDS
            and type(number) is int and 0 <= number <= 10**15}


def image_bytes(encoded):
    if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 4:
        raise BoundaryError("media_too_large_or_invalid")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise BoundaryError("media_too_large_or_invalid")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > MAX_PIXELS or image.width < 1 or image.height < 1:
                    raise BoundaryError("media_too_large_or_invalid")
                image.load()
                # Re-encoding excludes metadata, URLs, prompts and upstream textual chunks.
                clean = Image.new("RGB", image.size)
                clean.paste(image.convert("RGB"))
                output = io.BytesIO()
                clean.save(output, format="PNG")
        if output.tell() > MAX_IMAGE_BYTES:
            raise BoundaryError("media_too_large_or_invalid")
        return output.getvalue()
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise BoundaryError("media_too_large_or_invalid") from None


def media_url(value, *, key="", prompt_digest=None, prompt_length=None):
    """An upstream URL is a short-lived credential, never a public report field."""
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 for c in value):
        raise BoundaryError("media_url_rejected")
    if contains_caller_text(value, secrets=(key,), prompt_digest=prompt_digest, prompt_length=prompt_length):
        raise BoundaryError("sensitive_media_url_rejected")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise BoundaryError("media_url_rejected") from None
    host = (parts.hostname or "").lower().rstrip(".")
    if (parts.scheme != "https" or not host or parts.username is not None or parts.password is not None
            or parts.fragment or port not in {None, 443} or host in {"ark.cn-beijing.volces.com", "volces.com", "volcengine.com"}
            or host.startswith(("console.", "docs.", "www."))):
        raise BoundaryError("media_url_rejected")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (not ip.is_global or ip.is_multicast or ip.is_reserved):
        raise BoundaryError("media_url_rejected")
    return value


class ArkTransport:
    def __init__(self, *, transport=None):
        self.injected = transport

    async def _request(self, method, url, *, key=None, body=None, limit=MAX_JSON_BYTES,
                       timeout=60, discard=False, media_kind=None):
        backend = self.injected or guarded_transport()
        if media_kind is not None and self.injected is None:
            backend._pool._network_backend = MediaNetwork(backend._pool._network_backend)
        response = None
        log_context = SENSITIVE_HTTP_CONTEXT.set(True)
        try:
            headers = {"Accept": "application/json"} if media_kind is None else {"Accept": "video/mp4"}
            headers["Accept-Encoding"] = "identity"
            if key:
                headers["Authorization"] = "Bearer " + key
            if body is not None:
                headers["Content-Type"] = "application/json"
            request = httpx.Request(method, url, headers=headers,
                                    content=json.dumps(body).encode() if body is not None else None,
                                    extensions={"timeout": {"connect": 15, "read": timeout, "write": 30, "pool": 15}})
            # The low-level transport avoids httpx's request logger exposing signed media URLs.
            # It provides no redirect/cookie/proxy/retry layer; guarded_transport checks sockets.
            async with asyncio.timeout(timeout):
                response = await backend.handle_async_request(request)
                status = response.status_code
                if discard or not 200 <= status < 300:
                    return status, None
                if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                    raise BoundaryError("content_encoding_rejected")
                length = response.headers.get("content-length")
                if length and (not length.isdigit() or int(length) > limit):
                    raise BoundaryError("response_too_large")
                if media_kind == "video" and response.headers.get("content-type", "").split(";", 1)[0].lower() != "video/mp4":
                    raise BoundaryError("media_type_rejected")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > limit:
                        raise BoundaryError("response_too_large")
                    content.extend(chunk)
                return status, bytes(content)
        except (httpx.HTTPError, TimeoutError, BoundaryError) as exc:
            if response is not None:
                if isinstance(exc, BoundaryError):
                    exc.http_status = response.status_code
                else:
                    exc.volcengine_http_status = response.status_code
            raise
        finally:
            try:
                if response is not None:
                    await response.aclose()
                if self.injected is None:
                    await backend.aclose()
            finally:
                SENSITIVE_HTTP_CONTEXT.reset(log_context)

    async def auth(self, key):
        started = time.monotonic()
        try:
            status, _ = await self._request("GET", OFFICIAL_BASE_URL + "/contents/generations/tasks?page_size=1", key=key, discard=True)
            result = Result("passed" if 200 <= status < 300 else "failed", http_status=status,
                            error_category=None if 200 <= status < 300 else http_category(status))
            if status != 401 and result.status == "failed":
                result.status = "inconclusive"
        except (httpx.HTTPError, TimeoutError, BoundaryError) as exc:
            result = Result("inconclusive", error_category=self._exception_category(exc))
        result.elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        return result

    @staticmethod
    def _exception_category(exc):
        if isinstance(exc, (BoundaryError, MediaError)):
            return str(exc)
        if socket_egress_denied(exc):
            return "egress_denied"
        return "timeout" if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else "connection_failed"

    async def submit(self, item, prompt, key):
        started = time.monotonic()
        result = Result("unknown")
        try:
            path = "/images/generations" if item["kind"] == "image" else "/contents/generations/tasks"
            status, raw = await self._request("POST", OFFICIAL_BASE_URL + path, key=key,
                                              body=request_body(item, prompt), timeout=180 if item["kind"] == "image" else 60)
            result.http_status = status
            if not 200 <= status < 300:
                result.status = "unknown" if status >= 500 else "failed"
                result.error_category = http_category(status)
                return result
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                result.error_category = "invalid_response"
                return result
            if not isinstance(data, dict):
                result.error_category = "invalid_response"
                return result
            result.usage = safe_usage(data.get("usage"))
            if item["kind"] == "video":
                task_id = data.get("id")
                if (isinstance(task_id, str) and TASK_ID.fullmatch(task_id)
                        and not contains_caller_text(task_id, secrets=(key, prompt))):
                    result.status, result.task_id = "submitted", task_id
                else:
                    result.error_category = "missing_task_id"
                return result
            images = data.get("data")
            if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
                result.status, result.error_category = "inconclusive", "single_image_contract_failed"
                return result
            if result.usage.get("generated_images") not in {None, 1}:
                result.status, result.error_category = "inconclusive", "single_image_contract_failed"
                return result
            result.media = image_bytes(images[0].get("b64_json"))
            result.status, result.media_status = "passed", "saved"
            result.media_bytes = len(result.media)
        except (httpx.HTTPError, TimeoutError, BoundaryError) as exc:
            result.http_status = result.http_status or getattr(exc, "http_status", None) or getattr(exc, "volcengine_http_status", None)
            result.error_category = self._exception_category(exc)
            # A response/media error cannot authorize a second POST.
            if isinstance(exc, BoundaryError) and result.http_status is not None:
                result.status = "inconclusive"
        finally:
            result.elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        return result

    async def poll(self, task_id, key, *, prompt_digest=None, prompt_length=None):
        if not TASK_ID.fullmatch(task_id) or contains_caller_text(task_id, secrets=(key,), prompt_digest=prompt_digest, prompt_length=prompt_length):
            return Result("inconclusive", error_category="invalid_task_id")
        started = time.monotonic()
        result = Result("inconclusive", task_id=task_id)
        try:
            status, raw = await self._request("GET", OFFICIAL_BASE_URL + "/contents/generations/tasks/" + task_id, key=key, timeout=30)
            result.http_status = status
            if not 200 <= status < 300:
                result.error_category = http_category(status)
                return result
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                result.error_category = "invalid_response"
                return result
            if not isinstance(data, dict) or data.get("status") not in {"queued", "running", "succeeded", "failed", "cancelled", "expired"}:
                result.error_category = "invalid_task_status"
                return result
            result.upstream_status = data["status"]
            result.usage = safe_usage(data.get("usage"))
            if data["status"] in {"queued", "running"}:
                result.status = "polling"
            elif data["status"] != "succeeded":
                result.status, result.error_category = "failed", "generation_" + data["status"]
            else:
                result.media_status = "downloading"
                content = data.get("content")
                url = media_url(content.get("video_url") if isinstance(content, dict) else None, key=key,
                                prompt_digest=prompt_digest, prompt_length=prompt_length)
                # No Authorization or Referer is attached to media-origin requests.
                media_status, video = await self._request("GET", url, limit=MAX_VIDEO_BYTES, timeout=90, media_kind="video")
                result.media_http_status = media_status
                if media_status != 200:
                    result.error_category, result.media_status = "media_download_failed", "failed"
                else:
                    video = sanitize_mp4(video, secrets=(key, url), prompt_digest=prompt_digest, prompt_length=prompt_length)
                    result.status, result.media_status, result.media = "passed", "saved", video
                    result.media_bytes = len(video)
        except (httpx.HTTPError, TimeoutError, BoundaryError, MediaError) as exc:
            result.error_category = self._exception_category(exc)
            known_status = getattr(exc, "http_status", None) or getattr(exc, "volcengine_http_status", None)
            if result.media_status == "downloading":
                result.media_http_status = known_status or result.media_http_status
            else:
                result.http_status = result.http_status or known_status
            if result.upstream_status == "succeeded":
                result.media_status = "failed"
        finally:
            result.elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        return result


def mock_transport():
    """Synthetic single-output provider; no network fallback is reachable in mock mode."""
    def handler(request):
        if request.url.host == "media.example.org":
            return httpx.Response(200, content=MOCK_VIDEO, headers={"content-type": "video/mp4"})
        if request.method == "GET" and request.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"items": []})
        if request.method == "GET":
            return httpx.Response(200, json={"status": "succeeded", "usage": {"completion_tokens": 38400},
                                           "content": {"video_url": "https://media.example.org/synthetic.mp4?signature=synthetic"}})
        body = json.loads(request.content)
        if request.url.path.endswith("/images/generations"):
            output = io.BytesIO()
            Image.new("RGB", (128, 128), "navy").save(output, "PNG")
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(output.getvalue()).decode()}],
                                           "usage": {"generated_images": 1}})
        return httpx.Response(200, json={"id": "synthetic-" + body["model"]})
    return httpx.MockTransport(handler)


# A one-second 64px navy fixture encoded by ffmpeg; contains no upstream media.
MOCK_VIDEO = base64.b64decode("AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAORbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAA+gAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAArx0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAA+gAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAEAAAABAAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAPoAAAQAAABAAAAAAI0bWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAABAAAAAQABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAAB321pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAZ9zdGJsAAAAv3N0c2QAAAAAAAAAAQAAAK9hdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAEAAQABIAAAASAAAAAAAAAABFExhdmM2My4xLjEwMSBsaWJ4MjY0AAAAAAAAAAAAAAAAGP//AAAANWF2Y0MBZAAK/+EAGGdkAAqs2UQmwEQAAAMABAAAAwBAPEiWWAEABmjr48siwP34+AAAAAAQcGFzcAAAAAEAAAABAAAAFGJ0cnQAAAAAAAAZ4AAAAAAAAAAYc3R0cwAAAAAAAAABAAAACAAACAAAAAAUc3RzcwAAAAAAAAABAAAAAQAAAEhjdHRzAAAAAAAAAAcAAAABAAAQAAAAAAEAACgAAAAAAQAAEAAAAAABAAAAAAAAAAEAAAgAAAAAAQAAIAAAAAACAAAIAAAAABxzdHNjAAAAAAAAAAEAAAABAAAACAAAAAEAAAA0c3RzegAAAAAAAAAAAAAACAAAAtwAAAAOAAAADAAAAAwAAAAMAAAAFAAAAA4AAAAMAAAAFHN0Y28AAAAAAAAAAQAAA8EAAABhdWR0YQAAAFltZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAACxpbHN0AAAAJKl0b28AAAAcZGF0YQAAAAEAAAAATGF2ZjYzLjEuMTAxAAAACGZyZWUAAANEbWRhdAAAAq0GBf//qdxF6b3m2Ui3lizYINkj7u94MjY0IC0gY29yZSAxNjUgcjMyMjIgYjM1NjA1YSAtIEguMjY0L01QRUctNCBBVkMgY29kZWMgLSBDb3B5bGVmdCAyMDAzLTIwMjUgLSBodHRwOi8vd3d3LnZpZGVvbGFuLm9yZy94MjY0Lmh0bWwgLSBvcHRpb25zOiBjYWJhYz0xIHJlZj0zIGRlYmxvY2s9MTowOjAgYW5hbHlzZT0weDM6MHgxMTMgbWU9aGV4IHN1Ym1lPTcgcHN5PTEgcHN5X3JkPTEuMDA6MC4wMCBtaXhlZF9yZWY9MSBtZV9yYW5nZT0xNiBjaHJvbWFfbWU9MSB0cmVsbGlzPTEgOHg4ZGN0PTEgY3FtPTAgZGVhZHpvbmU9MjEsMTEgZmFzdF9wc2tpcD0xIGNocm9tYV9xcF9vZmZzZXQ9LTIgdGhyZWFkcz0yIGxvb2thaGVhZF90aHJlYWRzPTEgc2xpY2VkX3RocmVhZHM9MCBucj0wIGRlY2ltYXRlPTEgaW50ZXJsYWNlZD0wIGJsdXJheV9jb21wYXQ9MCBjb25zdHJhaW5lZF9pbnRyYT0wIGJmcmFtZXM9MyBiX3B5cmFtaWQ9MiBiX2FkYXB0PTEgYl9iaWFzPTAgZGlyZWN0PTEgd2VpZ2h0Yj0xIG9wZW5fZ29wPTAgd2VpZ2h0cD0yIGtleWludD0yNTAga2V5aW50X21pbj04IHNjZW5lY3V0PTQwIGludHJhX3JlZnJlc2g9MCByY19sb29rYWhlYWQ9NDAgcmM9Y3JmIG1idHJlZT0xIGNyZj0yMy4wIHFjb21wPTAuNjAgcXBtaW49MCBxcG1heD02OSBxcHN0ZXA9NCBpcF9yYXRpbz0xLjQwIGFxPTE6MS4wMACAAAAAJ2WIhAAR//7n4/wKba7/cTSgY7WtbXcqLo1x7YozakvH3bhD2xGfwQAAAApBmiRsQQ/+qlfeAAAACEGeQniH/wTFAAAACAGeYXRDvwW8AAAACAGeY2pDvwW9AAAAEEGaZ0moQWiZTAh3//6pnTUAAAAKQZ6FRREsO/8FvQAAAAgBnqZqQ78FvQ==")
