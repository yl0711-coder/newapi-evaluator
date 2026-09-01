"""双端准入的可叠加角色与资源范围授权。"""
from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException, Request

from . import auth, store

ROLES = {
    "viewer", "operator", "fidelity_reviewer", "admission_reviewer", "admin",
    "raw_export",
}
BOOTSTRAP_ROLES = (
    "viewer", "operator", "fidelity_reviewer", "admission_reviewer", "admin",
)


def ensure_bootstrap_roles() -> None:
    if store.query("SELECT id FROM user_roles WHERE revoked_at IS NULL LIMIT 1"):
        return
    users = store.query("SELECT id,username FROM users WHERE active=1 ORDER BY id LIMIT 1")
    if not users:
        return
    user = users[0]
    now = time.time()
    for role in BOOTSTRAP_ROLES:
        store.insert("user_roles", {
            "user_id": user["id"], "role": role,
            "scope_type": "global", "scope_id": "*",
            "granted_by": user["id"],
            "reason": "首个管理账号初始化",
            "created_at": now,
        })
    auth.audit(
        user["id"], user["username"], "paired.roles.bootstrap", "user",
        str(user["id"]), "success", detail={"roles": list(BOOTSTRAP_ROLES)},
    )


def active_roles(user_id: int) -> list[dict[str, Any]]:
    return store.query(
        "SELECT id,user_id,role,scope_type,scope_id,granted_by,reason,created_at "
        "FROM user_roles WHERE user_id=? AND revoked_at IS NULL ORDER BY id",
        (user_id,),
    )


def has_role(
    user_id: int, role: str, *, channel_id: int | None = None,
    task_id: int | None = None,
) -> bool:
    if role not in ROLES:
        return False
    rows = active_roles(user_id)
    for row in rows:
        if row["role"] != role:
            continue
        if row["scope_type"] == "global" and row["scope_id"] == "*":
            return True
        if channel_id is not None and row["scope_type"] == "channel" \
                and row["scope_id"] == str(channel_id):
            return True
        if task_id is not None and row["scope_type"] == "task" \
                and row["scope_id"] == str(task_id):
            return True
    return False


def require(
    request: Request, role: str, *, channel_id: int | None = None,
    task_id: int | None = None,
) -> dict[str, Any]:
    user = request.state.user
    allowed = has_role(user["id"], role, channel_id=channel_id, task_id=task_id)
    if role != "raw_export":
        allowed = allowed or has_role(user["id"], "admin", channel_id=channel_id, task_id=task_id)
    if not allowed:
        raise HTTPException(status_code=403, detail=f"缺少 {role} 授权")
    return user


def grant(
    actor: dict[str, Any], *, user_id: int, role: str, scope_type: str,
    scope_id: str, reason: str,
) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError("未知角色")
    if not store.get("users", user_id):
        raise ValueError("用户不存在")
    existing = store.query(
        "SELECT * FROM user_roles WHERE user_id=? AND role=? AND scope_type=? "
        "AND scope_id=? AND revoked_at IS NULL",
        (user_id, role, scope_type, scope_id),
    )
    if existing:
        return existing[0]
    row_id = store.insert("user_roles", {
        "user_id": user_id, "role": role, "scope_type": scope_type,
        "scope_id": scope_id, "granted_by": actor["id"],
        "reason": reason.strip(), "created_at": time.time(),
    })
    auth.audit(
        actor["id"], actor["username"], "paired.role.grant", "user_role",
        str(row_id), "success", detail={
            "user_id": user_id, "role": role, "scope_type": scope_type,
            "scope_id": scope_id, "reason": reason.strip(),
        },
    )
    row = store.get("user_roles", row_id)
    assert row is not None
    return row


def revoke(actor: dict[str, Any], role_id: int, reason: str) -> dict[str, Any]:
    row = store.get("user_roles", role_id)
    if not row or row.get("revoked_at"):
        raise ValueError("有效角色授权不存在")
    revoked_at = time.time()
    store.update("user_roles", role_id, {"revoked_at": revoked_at})
    auth.audit(
        actor["id"], actor["username"], "paired.role.revoke", "user_role",
        str(role_id), "success", detail={"reason": reason.strip()},
    )
    return {**row, "revoked_at": revoked_at}
