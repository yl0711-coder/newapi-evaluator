from __future__ import annotations

import base64
import hmac
import os
from urllib.parse import urlsplit

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def install_access(app):
    username = os.getenv("PLATFORM_USERNAME", "")
    password = os.getenv("PLATFORM_PASSWORD", "")
    if bool(username) != bool(password) or (password and len(password) < 12):
        raise RuntimeError("请同时配置 PLATFORM_USERNAME 和至少 12 位的 PLATFORM_PASSWORD")

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        return JSONResponse({"detail": [{"loc": error["loc"], "msg": error["msg"]}
                                        for error in exc.errors()]}, status_code=422)

    @app.middleware("http")
    async def access(request, call_next):
        if username:
            try:
                scheme, encoded = request.headers.get("authorization", "").split(" ", 1)
                provided = base64.b64decode(encoded, validate=True).decode().split(":", 1)
                authorized = (scheme.lower() == "basic" and len(provided) == 2
                              and hmac.compare_digest(provided[0].encode(), username.encode())
                              and hmac.compare_digest(provided[1].encode(), password.encode()))
            except (ValueError, UnicodeError):
                authorized = False
            if not authorized:
                return JSONResponse({"detail": "请登录测试工作台"}, status_code=401,
                                    headers={"WWW-Authenticate": 'Basic realm="model-test-workbench"', "Cache-Control": "no-store"})
        elif request.url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
            return JSONResponse({"detail": "远程访问需要配置登录账号"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if (request.headers.get("sec-fetch-site") == "cross-site"
                    or (origin and urlsplit(origin).netloc != request.headers.get("host"))):
                return JSONResponse({"detail": "不接受跨站操作"}, status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})
        return response
