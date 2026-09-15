from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import struct
import time
import uuid
import warnings
import zlib
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image, PngImagePlugin, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from typing import Literal

from shared.network import guarded_transport

MODEL = "gpt-image-2"
MAX_RESPONSE_BYTES = 48 * 1024 * 1024
MAX_PIXELS = 8_294_400
BOUNDARY = "图片、返回模型名和请求编号只能作为响应证据，不能独立证明上游模型身份。"
PROGRESS = logging.getLogger("image_quality.progress")
PROGRESS.setLevel(logging.INFO)
PROGRESS.propagate = False
if not PROGRESS.handlers:
    PROGRESS.addHandler(logging.StreamHandler())


class JSONBoundary:
    """Locate a UTF-8 JSON envelope; json.loads remains the syntax validator."""
    tokens = re.compile(rb'\\[\s\S]|\\$|["{}\[\]]')

    def __init__(self):
        self.started = False
        self.other_root = False
        self.in_string = False
        self.skip_byte = False
        self.depth = 0
        self.prefix = b""

    def feed(self, chunk):
        if self.other_root or not chunk:
            return False
        if not self.started:
            chunk = (self.prefix + chunk).lstrip(b" \t\r\n")
            self.prefix = b""
            if chunk.startswith(b"\xef") and len(chunk) < 3:
                self.prefix = chunk
                return False
            if chunk.startswith(b"\xef\xbb\xbf"):
                chunk = chunk[3:].lstrip(b" \t\r\n")
            if not chunk:
                return False
            if chunk[0] not in (ord("{"), ord("[")):
                self.other_root = True
                return False
            self.started = True
        offset = 1 if self.skip_byte else 0
        self.skip_byte = False
        for match in self.tokens.finditer(chunk, offset):
            token = match.group()
            if self.in_string:
                if token.startswith(b"\\"):
                    self.skip_byte = len(token) == 1
                elif token == b'"':
                    self.in_string = False
            elif token == b'"':
                self.in_string = True
            elif token in (b"{", b"["):
                self.depth += 1
            elif token in (b"}", b"]"):
                self.depth -= 1
                if self.depth == 0:
                    return True
        return False


def endpoint_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        raise ValueError("Base URL 格式无效") from None
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or any(char.isspace() for char in value)):
        raise ValueError("Base URL 仅接受 HTTP(S) 地址，不含凭据、查询参数或片段")
    if port == 0:
        raise ValueError("端口必须在 1 至 65535 之间")
    path = parts.path.rstrip("/")
    if not path:
        path = "/v1"
    if not path.endswith("/images/generations"):
        path += "/images/generations"
    try:
        return str(httpx.URL(urlunsplit((parts.scheme, parts.netloc, path, "", ""))))
    except httpx.InvalidURL:
        raise ValueError("Base URL 主机或路径格式无效") from None


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=1, max_length=2048)
    model: Literal["gpt-image-2"] = MODEL
    size: Literal["1024x1024", "1024x1536", "1536x1024"] = "1024x1536"
    quality: Literal["low", "medium", "high"] = "high"
    timeout_seconds: float = Field(default=600, ge=1, le=600, allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        endpoint_url(value)
        return value.strip()


class GenerationInput(Settings):
    api_key: SecretStr
    prompt: str = Field(min_length=1, max_length=16000)
    confirm_live: bool = Field(default=False, strict=True)

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        key = value.get_secret_value()
        if not key or len(key) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise ValueError("Key 不能为空，也不能含空格或非 ASCII 字符")
        return value

    @field_validator("prompt")
    @classmethod
    def valid_prompt(cls, value):
        if not value.strip():
            raise ValueError("提示词不能为空")
        return value


def fingerprint(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def inspect_settings(settings: Settings) -> dict:
    endpoint = endpoint_url(settings.base_url)
    return {
        "model": MODEL, "protocol": "openai-images",
        "host_alias": "host-" + fingerprint(urlsplit(endpoint).hostname)[:12],
        "endpoint_fingerprint": fingerprint(endpoint),
        "size": settings.size, "quality": settings.quality,
        "timeout_seconds": settings.timeout_seconds,
        "output_format": "png", "n": 1,
    }


def safe_identifier(value, secrets):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value)
            or re.search(r"(?i)(sk-|bearer|password|api[_-]?key|secret|token)", value)
            or any(secret and secret in value for secret in secrets)):
        return None
    return value


def safe_usage(value):
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        number = value.get(key)
        if type(number) is int and 0 <= number <= 10**12:
            result[key] = number
    details = value.get("input_tokens_details")
    if isinstance(details, dict):
        result["input_tokens_details"] = {
            key: details[key] for key in ("text_tokens", "image_tokens")
            if type(details.get(key)) is int and 0 <= details[key] <= 10**12
        }
    return result or None


class InvalidImage(ValueError):
    pass


def prepare_png(raw: bytes):
    """Remove free-form metadata before decoding; the final image is re-encoded."""
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise InvalidImage("unexpected_image_format")
    kept = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB"}
    unsupported = {b"iCCP", b"cICP", b"mDCV", b"cLLI", b"acTL", b"fcTL", b"fdAT", b"sBIT", b"bKGD"}
    colour_lengths = {b"gAMA": 4, b"cHRM": 32, b"sRGB": 1}
    colours = {}
    result = bytearray(raw[:8])
    offset = 8
    while offset + 12 <= len(raw):
        length = struct.unpack_from(">I", raw, offset)[0]
        end = offset + 12 + length
        if end > len(raw):
            raise InvalidImage("invalid_image")
        kind = raw[offset + 4:offset + 8]
        data = raw[offset + 8:end - 4]
        checksum = struct.unpack_from(">I", raw, end - 4)[0]
        if zlib.crc32(kind + data) != checksum:
            raise InvalidImage("invalid_image")
        if kind in unsupported or (kind not in kept and not kind[0] & 32):
            raise InvalidImage("unsupported_png_features")
        if kind == b"IHDR" and (length != 13 or (data[8] == 16 and data[9] != 0)):
            # Pillow preserves 16-bit greyscale, but decodes 16-bit RGB(A) to 8 bits.
            raise InvalidImage("unsupported_png_features")
        if kind in colour_lengths:
            if length != colour_lengths[kind] or kind in colours:
                raise InvalidImage("invalid_image")
            if kind == b"sRGB" and data[0] > 3:
                raise InvalidImage("invalid_image")
            if kind == b"gAMA" and not 0 < struct.unpack(">I", data)[0] <= 1_000_000:
                raise InvalidImage("unsupported_png_features")
            if kind == b"cHRM" and any(value > 100_000 for value in struct.unpack(">8I", data)):
                raise InvalidImage("unsupported_png_features")
            colours[kind] = data
        if kind in kept:
            # These chunks encode numeric rendering parameters, never free-form text.
            limits = {b"IHDR": 13, b"PLTE": 768, b"tRNS": 256, b"gAMA": 4,
                      b"cHRM": 32, b"sRGB": 1, b"IEND": 0}
            if kind in limits and length > limits[kind]:
                raise InvalidImage("invalid_image")
            result.extend(raw[offset:end])
        if kind == b"IEND":
            info = PngImagePlugin.PngInfo()
            for name, data in colours.items():
                info.add(name, data)
            return bytes(result), info
        offset = end
    raise InvalidImage("invalid_image")


def normalize_png(encoded):
    if not isinstance(encoded, str) or len(encoded) > MAX_RESPONSE_BYTES:
        raise InvalidImage("missing_image")
    try:
        raw = base64.b64decode(encoded, validate=True)
        prepared, colour_info = prepare_png(raw)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(prepared)) as original:
                if original.format != "PNG":
                    raise InvalidImage("unexpected_image_format")
                width, height = original.size
                if width <= 0 or height <= 0 or width * height > MAX_PIXELS or max(width, height) > 3840:
                    raise InvalidImage("image_size_limit")
                original.verify()
            with Image.open(io.BytesIO(prepared)) as original:
                original.load()
                if original.mode not in {"1", "L", "LA", "RGB", "RGBA", "P", "I;16"}:
                    raise InvalidImage("unsupported_png_features")
                # Rebuild pixels so IDAT tails and unused palette entries cannot carry text.
                pixels = original if original.mode == "I;16" else original.convert("RGBA")
                clean = Image.frombytes(pixels.mode, pixels.size, pixels.tobytes())
                options = {}
                if original.mode == "I;16" and "transparency" in original.info:
                    options["transparency"] = original.info["transparency"]
                output = io.BytesIO()
                clean.save(output, format="PNG", pnginfo=colour_info, **options)
        return output.getvalue(), width, height, hashlib.sha256(raw).hexdigest()
    except InvalidImage:
        raise
    except (ValueError, OSError, SyntaxError, UnidentifiedImageError,
            Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise InvalidImage("invalid_image") from None


async def generate(body: GenerationInput, transport=None) -> dict:
    if not body.confirm_live:
        raise ValueError("live_confirmation_required")
    payload = {"model": MODEL, "prompt": body.prompt, "n": 1, "size": body.size,
               "quality": body.quality, "output_format": "png"}
    report = {
        "schema_version": 1, "sample_id": uuid.uuid4().hex,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "settings": inspect_settings(body), "request_fingerprint": fingerprint(payload),
        "status": "failed", "http_status": None, "returned_model": None,
        "request_id": None, "usage": None, "upstream_seconds": None,
        "total_seconds": None, "error_code": None, "boundary": BOUNDARY,
        "diagnostics": {"phase": "requesting", "received_bytes": 0,
                        "headers_seconds": None, "first_byte_seconds": None,
                        "content_type": None, "response_completion": None},
    }
    started = time.monotonic()
    secret = body.api_key.get_secret_value()
    secrets = (secret, body.prompt, body.base_url)
    diagnostics = report["diagnostics"]

    def progress(event, *, terminal=False):
        # Only locally generated identifiers, fixed categories and numeric metrics.
        PROGRESS.info(json.dumps({
            "event": event, "sample_id": report["sample_id"],
            "started_at_utc": report["started_at_utc"],
            "phase": diagnostics["phase"], "received_bytes": diagnostics["received_bytes"],
            "content_type": diagnostics["content_type"], "http_status": report["http_status"],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "status": report["status"] if terminal else "running",
            "error_code": report["error_code"] if terminal else None,
        }))

    progress("started")

    async def send():
        timeout = httpx.Timeout(body.timeout_seconds, connect=min(20, body.timeout_seconds))
        async with httpx.AsyncClient(transport=transport or guarded_transport(),
                                     trust_env=False, follow_redirects=False, timeout=timeout) as client:
            async with client.stream("POST", endpoint_url(body.base_url), json=payload,
                                     headers={"Authorization": "Bearer " + secret}) as response:
                report["http_status"] = response.status_code
                report["request_id"] = safe_identifier(response.headers.get("x-request-id"), secrets)
                diagnostics["phase"] = "receiving_body"
                diagnostics["headers_seconds"] = round(time.monotonic() - started, 3)
                media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                diagnostics["content_type"] = ("event_stream" if media_type == "text/event-stream" else
                                               "json" if media_type == "application/json" or media_type.endswith("+json") else
                                               "other" if media_type else None)
                progress("headers_received")
                if not 200 <= response.status_code < 300:
                    diagnostics["response_completion"] = "http_status"
                    report["error_code"] = ("redirect_rejected" if response.is_redirect else
                                            "rate_limited" if response.status_code == 429 else
                                            "authentication_failed" if response.status_code in (401, 403) else
                                            "upstream_http_error")
                    return
                if diagnostics["content_type"] == "event_stream":
                    raise InvalidImage("unexpected_stream_response")
                content = bytearray()
                boundary = JSONBoundary()
                logged_bytes = 0
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise InvalidImage("response_too_large")
                    content.extend(chunk)
                    diagnostics["received_bytes"] = len(content)
                    if chunk and diagnostics["first_byte_seconds"] is None:
                        diagnostics["first_byte_seconds"] = round(time.monotonic() - started, 3)
                        progress("body_started")
                    if len(content) - logged_bytes >= 1024 * 1024:
                        logged_bytes = len(content)
                        progress("body_progress")
                    if boundary.feed(chunk):
                        diagnostics["response_completion"] = "json_complete"
                        break
                else:
                    diagnostics["response_completion"] = "transport_eof"
                report["upstream_seconds"] = round(time.monotonic() - started, 3)
                diagnostics["phase"] = "validating_json"
                progress("body_received")
                try:
                    data = json.loads(content)
                except (ValueError, UnicodeError):
                    raise InvalidImage("invalid_json") from None
                if not isinstance(data, dict):
                    raise InvalidImage("invalid_response")
                report["returned_model"] = safe_identifier(data.get("model"), secrets)
                report["usage"] = safe_usage(data.get("usage"))
                if data.get("error"):
                    raise InvalidImage("upstream_error")
                images = data.get("data")
                if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
                    raise InvalidImage("missing_image")
                if not images[0].get("b64_json") and images[0].get("url"):
                    raise InvalidImage("image_url_only")
                diagnostics["phase"] = "processing_image"
                progress("image_processing")
                image, width, height, raw_hash = await asyncio.to_thread(normalize_png, images[0].get("b64_json"))
                report["image"] = {
                    "mime_type": "image/png", "width": width, "height": height,
                    "bytes": len(image), "sha256": hashlib.sha256(image).hexdigest(),
                    "upstream_image_sha256": raw_hash, "metadata_removed": True,
                    "b64_json": base64.b64encode(image).decode(),
                }
                report["size_matches"] = f"{width}x{height}" == body.size
                diagnostics["phase"] = "closing_response"
        report["status"] = "success"
        diagnostics["phase"] = "completed"
    try:
        await asyncio.wait_for(send(), timeout=body.timeout_seconds)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        report["error_code"] = "timeout_result_unknown"
    except httpx.RequestError:
        report["error_code"] = "connection_failed"
    except InvalidImage as exc:
        report["error_code"] = str(exc)
    except asyncio.CancelledError:
        report["status"] = "cancelled"
        report["error_code"] = "client_cancelled"
        raise
    except Exception:
        report["error_code"] = "internal_error"
        raise
    finally:
        report["total_seconds"] = round(time.monotonic() - started, 3)
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        progress("finished", terminal=True)
    return report
