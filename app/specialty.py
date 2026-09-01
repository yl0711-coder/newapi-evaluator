"""代码维护的专项测试包；专项结论与基础准入互不覆盖。"""
from __future__ import annotations

import time
from typing import Any

from . import store


def _formal_item(item_id: str, profile: str, prompt: str, expected: dict[str, Any],
                 max_tokens: int) -> dict[str, Any]:
    return {
        "id": item_id, "version": 1, "template_id": item_id,
        "template_version": 1, "profile": profile, "max_tokens": max_tokens,
        "prompt": prompt, "score_domain": "specialty",
        "completion_policy": "whole_response_required",
        "grader": {"id": "json_schema_exact", "version": "1.0.0",
                   "config": {"expected_value": expected}},
        "mock_answer": store.dumps(expected),
        "comparison_key": f"{item_id}/v1", "method_id": item_id,
    }


PACKS: dict[str, dict[str, Any]] = {
    "agent": {
        "name": "Agent 专项", "version": "agent-2026.08.1", "threshold": .75,
        "items": [
            _formal_item("agent.plan", "agent",
                         "只输出 JSON：action=lookup，ticket=NX-42，fallback=human。",
                         {"action": "lookup", "ticket": "NX-42", "fallback": "human"}, 300),
            _formal_item("agent.multiturn", "agent",
                         "只输出 JSON：ticket=NX-42，permission=read_only，action=lookup。",
                         {"ticket": "NX-42", "permission": "read_only", "action": "lookup"}, 300),
            _formal_item("agent.recovery", "agent",
                         "工具连续两次超时。只输出 JSON：backoff=true，max_retries=2，handoff=human。",
                         {"backoff": True, "max_retries": 2, "handoff": "human"}, 300),
        ],
    },
    "coding": {
        "name": "编程专项", "version": "coding-2026.08.1", "threshold": .75,
        "items": [
            _formal_item("coding.debug", "coding",
                         "竞态条件导致重复构建。只输出 JSON：cause=race_condition，fix=lock。",
                         {"cause": "race_condition", "fix": "lock"}, 300),
            _formal_item("coding.patch", "coding",
                         "空字符串被当成模型名。只输出 JSON：cause=blank_allowed，fix=strip_and_reject，tests=2。",
                         {"cause": "blank_allowed", "fix": "strip_and_reject", "tests": 2}, 300),
            _formal_item("coding.review", "coding",
                         "SQL 直接拼接用户输入。只输出 JSON：risk=sql_injection，fix=parameterized_query。",
                         {"risk": "sql_injection", "fix": "parameterized_query"}, 300),
        ],
    },
    "customer_service": {
        "name": "客服专项", "version": "service-2026.08.1", "threshold": .75,
        "items": [
            _formal_item("service.classify", "customer_service",
                         "退款进度查询。只输出 JSON：intent=refund，priority=normal，next=ask_order_id。",
                         {"intent": "refund", "priority": "normal", "next": "ask_order_id"}, 250),
            _formal_item("service.boundary", "customer_service",
                         "不能确认今天到账。只输出 JSON：promise=false，next=query_status。",
                         {"promise": False, "next": "query_status"}, 250),
            _formal_item("service.concise", "customer_service",
                         "登录失败且不得索要密码。只输出 JSON：steps=3，asks_password=false。",
                         {"steps": 3, "asks_password": False}, 250),
        ],
    },
}


def ensure_versions() -> None:
    now = time.time()
    for code, pack in PACKS.items():
        store.execute(
            "INSERT OR IGNORE INTO specialty_pack_versions "
            "(code,name,version,item_count,metadata_json,published_at) VALUES (?,?,?,?,?,?)",
            (code, pack["name"], pack["version"], len(pack["items"]),
             store.dumps({"threshold": pack["threshold"]}), now),
        )


def catalog() -> list[dict[str, Any]]:
    return [{"code": code, "name": pack["name"], "version": pack["version"],
             "item_count": len(pack["items"]), "threshold": pack["threshold"]}
            for code, pack in PACKS.items()]


def expand(codes: list[str]) -> list[dict[str, Any]]:
    steps = []
    for code in codes:
        pack = PACKS.get(code)
        if not pack:
            raise ValueError(f"未知专项测试：{code}")
        for item in pack["items"]:
            steps.append({"kind": "specialty", "item": item, "profile": code,
                          "pack_version": pack["version"]})
    return steps


def estimate(codes: list[str]) -> dict[str, Any]:
    selected = [PACKS[code] for code in codes if code in PACKS]
    requests = sum(len(pack["items"]) for pack in selected)
    tokens = sum(sum(int(item["max_tokens"]) for item in pack["items"]) for pack in selected)
    return {"requests": requests, "tokens": tokens,
            "versions": {code: PACKS[code]["version"] for code in codes if code in PACKS}}


def aggregate(steps: list[dict[str, Any]], selected: list[str], recommended: list[str]) -> dict[str, Any]:
    output = {}
    for code in set(selected) | set(recommended):
        pack = PACKS.get(code)
        if not pack:
            continue
        profile_steps = [step for step in steps if step["extra"].get("specialty_profile") == code]
        graded = [step for step in profile_steps
                  if (step["extra"].get("grade") or {}).get("status")
                  in {"passed", "failed", "partial"}
                  or step["extra"].get("graded")]
        scores = [float((step["extra"].get("grade") or {}).get(
            "score", step["extra"].get("scored", 0.0)) or 0.0) for step in graded]
        score = sum(scores) / len(scores) if scores else None
        if code not in selected:
            status = "evidence_insufficient"
        elif len(graded) < len(pack["items"]):
            status = "evidence_insufficient"
        else:
            status = "suitable" if score is not None and score >= pack["threshold"] else "not_suitable"
        output[code] = {
            "name": pack["name"], "version": pack["version"], "status": status,
            "score": round(score, 4) if score is not None else None,
            "graded": len(graded), "total": len(pack["items"]),
            "recommended": code in recommended, "selected": code in selected,
            "items": [{"id": step["extra"].get("item"),
                       "score": (step["extra"].get("grade") or {}).get(
                           "score", step["extra"].get("scored")),
                       "graded": (step["extra"].get("grade") or {}).get("status")
                       in {"passed", "failed", "partial"}
                       or bool(step["extra"].get("graded")),
                       "detail": step["detail"]} for step in profile_steps],
        }
    return output


def recommended_profiles(platform_group_label: str, models: list[str]) -> list[str]:
    if not models:
        return []
    marks = ",".join("?" for _ in models)
    rows = store.query(
        f"SELECT DISTINCT usage_profile FROM supply_gap_recommendations "
        f"WHERE platform_group=? AND model IN ({marks}) AND status='candidate'",
        (platform_group_label, *models),
    )
    return sorted({row["usage_profile"] for row in rows
                   if row["usage_profile"] in PACKS})
