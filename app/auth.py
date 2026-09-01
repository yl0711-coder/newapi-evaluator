"""管理端账号、会话、CSRF 与审计边界。"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import store
from .config import (BOOTSTRAP_PASSWORD, BOOTSTRAP_USERNAME, COOKIE_SECURE,
                     SESSION_TTL_SECONDS)

SESSION_COOKIE = "api_eval_session"
CSRF_COOKIE = "api_eval_csrf"
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_ATTEMPT_LIMIT = 5
PASSWORD_MIN_LENGTH = 12
_USERNAME_RE = re.compile(r"^[\w.@+-]{3,64}$", re.UNICODE)

router = APIRouter()


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=1024)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"密码至少需要 {PASSWORD_MIN_LENGTH} 个字符")
    salt = secrets.token_bytes(16)
    n, r, p = 2**15, 8, 1
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        dklen=32, maxmem=64 * 1024 * 1024,
    )
    return f"scrypt${n}${r}${p}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        expected = bytes.fromhex(raw_expected)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(raw_salt),
            n=int(raw_n), r=int(raw_r), p=int(raw_p), dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def create_user(username: str, password: str, display_name: str = "") -> int:
    clean_username = username.strip()
    if not _USERNAME_RE.fullmatch(clean_username):
        raise ValueError("用户名需为 3–64 位，可使用文字、字母、数字及 . @ + - _")
    now = time.time()
    return store.insert("users", {
        "username": clean_username,
        "display_name": display_name.strip() or clean_username,
        "password_hash": hash_password(password),
        "active": 1,
        "created_at": now,
        "updated_at": now,
    })


def set_user_active(username: str, active: bool) -> dict[str, Any]:
    """启停独立账号；停用时立即撤销该账号的全部现有会话。"""
    rows = store.query(
        "SELECT id,username,display_name,active FROM users WHERE username=?",
        (username.strip(),),
    )
    if not rows:
        raise ValueError("账号不存在")
    user = rows[0]
    now = time.time()
    store.update("users", user["id"], {"active": int(active), "updated_at": now})
    if not active:
        store.execute(
            "UPDATE sessions SET revoked_at=? "
            "WHERE user_id=? AND revoked_at IS NULL",
            (now, user["id"]),
        )
    return {
        "id": user["id"], "username": user["username"],
        "display_name": user["display_name"], "active": bool(active),
    }


def ensure_bootstrap_account() -> None:
    count = store.query("SELECT COUNT(*) n FROM users")[0]["n"]
    if count:
        return
    if not BOOTSTRAP_USERNAME and not BOOTSTRAP_PASSWORD:
        return
    if not BOOTSTRAP_USERNAME or not BOOTSTRAP_PASSWORD:
        raise RuntimeError(
            "首次账号配置不完整：TEST_BOOTSTRAP_USERNAME 与 "
            "TEST_BOOTSTRAP_PASSWORD 必须同时设置"
        )
    user_id = create_user(BOOTSTRAP_USERNAME, BOOTSTRAP_PASSWORD)
    audit(user_id, BOOTSTRAP_USERNAME, "user.bootstrap", "user", str(user_id), "success")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _attempt_subjects(username: str, ip: str) -> tuple[str, str]:
    return (
        _digest(f"account\n{username.casefold().strip()}"),
        _digest(f"ip\n{ip}"),
    )


def _is_rate_limited(subject: str) -> bool:
    cutoff = time.time() - LOGIN_WINDOW_SECONDS
    store.execute("DELETE FROM login_attempts WHERE attempted_at<?", (cutoff,))
    rows = store.query(
        "SELECT COUNT(*) n FROM login_attempts "
        "WHERE subject_hash=? AND succeeded=0 AND attempted_at>=?",
        (subject, cutoff),
    )
    return rows[0]["n"] >= LOGIN_ATTEMPT_LIMIT


def _record_attempt(subject: str, succeeded: bool) -> None:
    if succeeded:
        store.execute("DELETE FROM login_attempts WHERE subject_hash=?", (subject,))
        return
    store.insert("login_attempts", {
        "subject_hash": subject,
        "succeeded": 0,
        "attempted_at": time.time(),
    })


def _new_session(user_id: int) -> tuple[str, str, dict[str, Any]]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = time.time()
    session_id = store.insert("sessions", {
        "user_id": user_id,
        "token_hash": _digest(token),
        "csrf_hash": _digest(csrf),
        "created_at": now,
        "last_seen_at": now,
        "expires_at": now + SESSION_TTL_SECONDS,
    })
    session = store.get("sessions", session_id)
    assert session is not None
    return token, csrf, session


def _cookie_secure(request: Request) -> bool:
    return request.url.scheme == "https" if COOKIE_SECURE is None else COOKIE_SECURE


def _set_session_cookies(
    request: Request, response: Response, token: str, csrf: str,
) -> None:
    common = {
        "secure": _cookie_secure(request),
        "samesite": "strict",
        "path": "/",
        "max_age": SESSION_TTL_SECONDS,
    }
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **common)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)


def _clear_session_cookies(request: Request, response: Response) -> None:
    secure = _cookie_secure(request)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=secure, samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=secure, samesite="strict")


def _session_for_request(request: Request) -> tuple[dict[str, Any], dict[str, Any]] | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    now = time.time()
    rows = store.query(
        "SELECT sessions.*,users.username,users.display_name,users.active "
        "FROM sessions JOIN users ON users.id=sessions.user_id "
        "WHERE sessions.token_hash=? AND sessions.revoked_at IS NULL "
        "AND sessions.expires_at>?",
        (_digest(token), now),
    )
    if not rows or not rows[0]["active"]:
        return None
    row = rows[0]
    user = {
        "id": row["user_id"],
        "username": row["username"],
        "display_name": row["display_name"],
    }
    return user, row


def _csrf_valid(request: Request, session: dict[str, Any]) -> bool:
    header = request.headers.get("x-csrf-token", "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    return bool(
        header and cookie
        and hmac.compare_digest(header, cookie)
        and hmac.compare_digest(_digest(header), session["csrf_hash"])
    )


def audit(
    user_id: int | None, actor: str, action: str, object_type: str,
    object_id: str, result: str, *, request_id: str = "", ip: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    store.insert("audit_events", {
        "user_id": user_id,
        "actor": actor,
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "result": result,
        "request_id": request_id,
        "ip": ip,
        "detail_json": store.dumps(detail or {}),
        "created_at": time.time(),
    })


_AUDIT_TABLES = {
    "channels": "channels", "targets": "targets", "tasks": "tasks",
    "platform-groups": "platform_groups", "model-families": "model_families",
    "recommendations": "recommendations", "incidents": "incidents",
    "local-runners": "paired_runners", "system-alerts": "system_alerts",
}
_AUDIT_SAFE_FIELDS = {
    "id", "name", "model", "protocol", "env", "status", "lifecycle_status",
    "enabled", "multiplier", "platform_group_id", "group_id", "pool", "recorded",
    "sort_order", "display_name", "replacement_model_id", "updated_at", "archived_at",
    "last_verdict", "cancel_flag", "attempt_count", "dead_lettered_at",
}


def _change_summary(path: str) -> dict[str, Any] | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 3 or parts[0] != "api":
        return None
    table = _AUDIT_TABLES.get(parts[1])
    object_id = next((int(part) for part in parts[2:] if part.isdigit()), None)
    if not table or object_id is None:
        return None
    row = store.get(table, object_id)
    if not row:
        return None
    return {key: row[key] for key in _AUDIT_SAFE_FIELDS if key in row}


def _audit_request(
    request: Request, status_code: int, *,
    before: dict[str, Any] | None = None, after: dict[str, Any] | None = None,
) -> None:
    user = getattr(request.state, "user", None)
    parts = [part for part in request.url.path.split("/") if part]
    object_type = parts[1] if len(parts) > 1 and parts[0] == "api" else (parts[0] if parts else "")
    object_id = next((part for part in parts[2:] if part.isdigit()), "")
    audit(
        user["id"] if user else None,
        user["username"] if user else "anonymous",
        f"http.{request.method.lower()}", object_type, object_id,
        "success" if status_code < 400 else "failure",
        request_id=request.state.request_id, ip=_client_ip(request),
        detail={"path": request.url.path, "status_code": status_code,
                "before": before, "after": after},
    )


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def authentication_boundary(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or secrets.token_hex(12)
        path = request.url.path
        signed_webhook = path.startswith("/api/incidents/webhook/")
        runner_agent = path.startswith("/api/runner-agent/")
        public = signed_webhook or runner_agent or path in {
            "/api/health", "/api/auth/login", "/login.html", "/login.js",
            "/style.css", "/favicon.ico",
        }
        session_data = None if public else _session_for_request(request)
        if not public and session_data is None:
            if path.startswith("/api/"):
                response = JSONResponse({"detail": "请先登录"}, status_code=401)
            else:
                target = quote(path if path.startswith("/") else "/", safe="/")
                response = RedirectResponse(f"/login.html?next={target}", status_code=303)
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                _audit_request(request, response.status_code)
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        if session_data:
            request.state.user, request.state.session = session_data
        if (request.method in {"POST", "PUT", "PATCH", "DELETE"}
                and path != "/api/auth/login"
                and not signed_webhook
                and not runner_agent
                and not _csrf_valid(request, request.state.session)):
            response = JSONResponse({"detail": "CSRF 校验失败，请刷新页面后重试"}, status_code=403)
            _audit_request(request, response.status_code)
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        before = _change_summary(path) if request.method in {
            "POST", "PUT", "PATCH", "DELETE"
        } else None
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} \
                and path != "/api/auth/login":
            _audit_request(
                request, response.status_code, before=before,
                after=_change_summary(path),
            )
        return response


@router.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    username = body.username.strip()
    ip = _client_ip(request)
    subjects = _attempt_subjects(username, ip)
    if any(_is_rate_limited(subject) for subject in subjects):
        audit(None, username, "auth.login", "session", "", "rate_limited", ip=ip)
        raise HTTPException(status_code=429, detail="尝试次数过多，请 15 分钟后重试")
    rows = store.query("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,))
    if not rows:
        user_count = store.query("SELECT COUNT(*) n FROM users")[0]["n"]
        for subject in subjects:
            _record_attempt(subject, False)
        if not user_count:
            raise HTTPException(
                status_code=503,
                detail="尚未创建管理员账号，请先运行 manage_accounts.py add <用户名>",
            )
        audit(None, username, "auth.login", "session", "", "failure", ip=ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    user = rows[0]
    valid = bool(user["active"]) and verify_password(body.password, user["password_hash"])
    for subject in subjects:
        _record_attempt(subject, valid)
    if not valid:
        audit(user["id"], user["username"], "auth.login", "session", "", "failure", ip=ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token, csrf, session = _new_session(user["id"])
    store.update("users", user["id"], {"last_login_at": time.time(), "updated_at": time.time()})
    _set_session_cookies(request, response, token, csrf)
    audit(user["id"], user["username"], "auth.login", "session", str(session["id"]), "success", ip=ip)
    return {
        "user": {"id": user["id"], "username": user["username"],
                 "display_name": user["display_name"]},
        "csrf_token": csrf,
    }


@router.get("/api/auth/me")
def me(request: Request) -> dict[str, Any]:
    store.update("sessions", request.state.session["id"], {"last_seen_at": time.time()})
    return {
        "user": request.state.user,
        "csrf_token": request.cookies.get(CSRF_COOKIE, ""),
    }


@router.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    store.update("sessions", request.state.session["id"], {"revoked_at": time.time()})
    _clear_session_cookies(request, response)
    return {"status": "logged_out"}


@router.post("/api/auth/change-password")
def change_password(body: PasswordChangeIn, request: Request) -> dict[str, str]:
    user = store.get("users", request.state.user["id"])
    if not user or not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="当前密码错误")
    store.update("users", user["id"], {
        "password_hash": hash_password(body.new_password),
        "updated_at": time.time(),
    })
    store.execute(
        "UPDATE sessions SET revoked_at=? WHERE user_id=? AND id<>? AND revoked_at IS NULL",
        (time.time(), user["id"], request.state.session["id"]),
    )
    return {"status": "password_changed"}


@router.get("/api/audit-events")
def list_audit_events(
    limit: int = 100, actor: str = "", action: str = "",
    object_type: str = "", result: str = "", from_ts: float | None = None,
    to_ts: float | None = None,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 500))
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (("actor", actor), ("action", action),
                          ("object_type", object_type), ("result", result)):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    if from_ts is not None:
        clauses.append("created_at>=?")
        params.append(from_ts)
    if to_ts is not None:
        clauses.append("created_at<=?")
        params.append(to_ts)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = store.query(
        "SELECT id,user_id,actor,action,object_type,object_id,result,request_id,ip,"
        "detail_json,created_at FROM audit_events" + where +
        " ORDER BY id DESC LIMIT ?",
        tuple([*params, safe_limit]),
    )
    output = []
    for row in rows:
        detail = store.loads(row.pop("detail_json"), {})
        output.append({**row, "detail": detail})
    return output
