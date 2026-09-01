"""模型与渠道的运营生命周期、别名解析和可审计状态迁移。"""
from __future__ import annotations

import difflib
import re
import time
from typing import Any

from . import store

MODEL_STATES = {
    "experimental": "实验",
    "enabled": "启用",
    "observe": "观察",
    "deprecated": "弃用",
    "retired": "退役",
}
CHANNEL_STATES = {
    "candidate": "候选",
    "tested": "已测试",
    "approved": "已批准",
    "online_verified": "已上线核验",
    "synced": "已同步",
    "archived": "已归档",
}
MODEL_TRANSITIONS = {
    "experimental": ("enabled", "observe", "retired"),
    "enabled": ("observe", "deprecated", "retired"),
    "observe": ("enabled", "deprecated", "retired"),
    "deprecated": ("observe", "retired"),
    "retired": ("experimental",),
}
CHANNEL_TRANSITIONS = {
    "candidate": ("tested", "archived"),
    "tested": ("candidate", "approved", "archived"),
    "approved": ("tested", "online_verified", "archived"),
    "online_verified": ("approved", "synced", "archived"),
    "synced": ("online_verified", "archived"),
    "archived": ("candidate",),
}
TESTABLE_MODEL_STATES = frozenset({"experimental", "enabled", "observe"})
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,119}$")


class LifecycleError(ValueError):
    pass


def canonical_model_id(value: str) -> str:
    canonical = value.strip().casefold()
    if not _MODEL_ID.fullmatch(canonical):
        raise LifecycleError(
            "规范模型 ID 只能使用小写字母、数字及 . _ : / -，最长 120 位"
        )
    return canonical


def state_catalog() -> dict[str, Any]:
    return {
        "model": [{"code": code, "name": name} for code, name in MODEL_STATES.items()],
        "channel": [{"code": code, "name": name} for code, name in CHANNEL_STATES.items()],
        "model_transitions": MODEL_TRANSITIONS,
        "channel_transitions": CHANNEL_TRANSITIONS,
    }


def allowed_transitions(object_type: str, status: str) -> tuple[str, ...]:
    transitions = MODEL_TRANSITIONS if object_type == "model" else CHANNEL_TRANSITIONS
    return transitions.get(status, ())


def _transition(
    object_type: str, object_id: int, to_status: str, reason: str,
    user: dict[str, Any], replacement_model_id: int | None = None,
) -> dict[str, Any]:
    table = "model_family_models" if object_type == "model" else "channels"
    states = MODEL_STATES if object_type == "model" else CHANNEL_STATES
    row = store.get(table, object_id)
    if not row:
        raise LifecycleError("对象不存在")
    current = row["lifecycle_status"]
    if to_status not in states:
        raise LifecycleError("未知生命周期状态")
    if to_status == current:
        raise LifecycleError("对象已经处于该状态")
    if to_status not in allowed_transitions(object_type, current):
        raise LifecycleError(
            f"不允许从“{states[current]}”直接变为“{states[to_status]}”"
        )
    clean_reason = reason.strip()
    if len(clean_reason) < 2:
        raise LifecycleError("请填写至少 2 个字的变更原因")
    if object_type == "model" and to_status == "retired":
        if replacement_model_id is not None:
            replacement = store.get("model_family_models", replacement_model_id)
            if not replacement or replacement["family_id"] != row["family_id"]:
                raise LifecycleError("替代模型必须属于同一模型家族")
            if replacement["id"] == object_id:
                raise LifecycleError("替代模型不能是自身")
            if replacement["lifecycle_status"] in {"deprecated", "retired"}:
                raise LifecycleError("替代模型必须仍可用于新任务")
        patch = {
            "lifecycle_status": to_status,
            "replacement_model_id": replacement_model_id,
            "enabled": 0,
            "archived_at": time.time(),
            "updated_at": time.time(),
        }
    else:
        patch = {
            "lifecycle_status": to_status,
            "archived_at": time.time() if to_status == "archived" else None,
            "updated_at": time.time(),
        }
        if object_type == "model" and current == "retired":
            patch["replacement_model_id"] = None
    store.update(table, object_id, patch)
    store.insert("model_lifecycle_history", {
        "object_type": object_type,
        "object_id": object_id,
        "from_status": current,
        "to_status": to_status,
        "replacement_model_id": replacement_model_id,
        "reason": clean_reason,
        "created_by": user["id"],
        "actor": user["username"],
        "created_at": time.time(),
    })
    updated = store.get(table, object_id)
    assert updated is not None
    return updated


def transition_model(
    model_id: int, to_status: str, reason: str, user: dict[str, Any],
    replacement_model_id: int | None = None,
) -> dict[str, Any]:
    return _transition(
        "model", model_id, to_status, reason, user, replacement_model_id
    )


def transition_channel(
    channel_id: int, to_status: str, reason: str, user: dict[str, Any],
) -> dict[str, Any]:
    return _transition("channel", channel_id, to_status, reason, user)


def history(object_type: str, object_id: int, limit: int = 100) -> list[dict[str, Any]]:
    return store.query(
        "SELECT * FROM model_lifecycle_history WHERE object_type=? AND object_id=? "
        "ORDER BY created_at DESC,id DESC LIMIT ?",
        (object_type, object_id, limit),
    )


def aliases(family_id: int) -> list[dict[str, Any]]:
    return store.query(
        "SELECT aliases.*,models.model canonical_model,models.display_name "
        "FROM model_aliases aliases JOIN model_family_models models "
        "ON models.id=aliases.model_id WHERE aliases.family_id=? "
        "ORDER BY aliases.alias COLLATE NOCASE,aliases.id",
        (family_id,),
    )


def match_models(family_id: int, discovered: list[str]) -> dict[str, Any]:
    models = store.query(
        "SELECT * FROM model_family_models WHERE family_id=? "
        "AND enabled=1 AND lifecycle_status IN ('experimental','enabled','observe') "
        "ORDER BY sort_order,id",
        (family_id,),
    )
    alias_rows = aliases(family_id)
    exact: dict[str, dict[str, Any]] = {}
    for model in models:
        exact[model["model"].casefold()] = model
    for alias in alias_rows:
        model = next((item for item in models if item["id"] == alias["model_id"]), None)
        if model:
            exact[alias["alias"].casefold()] = model

    matched = []
    fuzzy = []
    consumed_model_ids: set[int] = set()
    unmapped = []
    candidates = list(exact)
    for upstream in discovered:
        key = upstream.casefold()
        model = exact.get(key)
        if model:
            matched.append({
                "canonical": model["model"], "upstream": upstream,
                "match_type": "canonical" if key == model["model"].casefold() else "alias",
                "confirmed": True,
            })
            consumed_model_ids.add(model["id"])
            continue
        close = difflib.get_close_matches(key, candidates, n=3, cutoff=0.72)
        suggestions = []
        seen: set[int] = set()
        for candidate in close:
            suggested = exact[candidate]
            if suggested["id"] in seen:
                continue
            seen.add(suggested["id"])
            suggestions.append({
                "model_id": suggested["id"],
                "canonical": suggested["model"],
                "display_name": suggested["display_name"],
                "score": round(difflib.SequenceMatcher(None, key, candidate).ratio(), 3),
            })
        if suggestions:
            fuzzy.append({"upstream": upstream, "candidates": suggestions, "confirmed": False})
        else:
            unmapped.append(upstream)
    return {
        "expected": [model["model"] for model in models],
        "matched": matched,
        "missing": [model["model"] for model in models if model["id"] not in consumed_model_ids],
        "fuzzy_candidates": fuzzy,
        "unmapped": unmapped,
        "identity_notice": "名称与别名只用于映射，不证明真实上游身份；模糊候选必须人工确认。",
    }
