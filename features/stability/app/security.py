from __future__ import annotations

import base64
import hmac
from typing import Callable

from cryptography.fernet import Fernet
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .config import PASSWORD, SECRET_PATH, USERNAME


def _fernet() -> Fernet:
    if not SECRET_PATH.exists():
        SECRET_PATH.write_bytes(Fernet.generate_key())
        try:
            SECRET_PATH.chmod(0o600)
        except OSError:
            pass
    return Fernet(SECRET_PATH.read_bytes())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return value[:2] + "***"
    return f"{value[:6]}***{value[-4:]}"


def scrub(value: str, secret: str = "") -> str:
    text = value.replace(secret, "[已隐藏]") if secret else value
    return text[:300]


def _authorized(request: Request) -> bool:
    if not USERNAME and not PASSWORD:
        return True
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:], validate=True).decode("utf-8")
        username, password = raw.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(username, USERNAME) and hmac.compare_digest(password, PASSWORD)


async def basic_auth_middleware(request: Request, call_next: Callable) -> Response:
    if request.url.path == "/api/health" or _authorized(request):
        return await call_next(request)
    return JSONResponse(
        {"detail": "需要登录"},
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="stability-test"'},
    )

