from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import httpx
from fastapi import HTTPException

from . import egress, paired_admission, paired_evidence, paired_protocol, protocol, store
from .security import credential_fingerprint, decrypt, encrypt, redact_url

BUSINESS_STATUSES = {"pending_test", "offline", "online", "disabled"}
ACTIVE_BATCH_STATUSES = {"draft", "awaiting_fidelity_truth", "testing", "pending_retest", "awaiting_conclusion"}
SUCCESSFUL_PAIRED_STATES = {"completed", "completed_with_insufficient_metrics"}
TERMINAL_PAIRED_STATES = {*SUCCESSFUL_PAIRED_STATES, "stopped", "canceled"}
TRUTH_SECONDS = 30 * 24 * 60 * 60
EVIDENCE_WINDOW_SECONDS = 7 * 24 * 60 * 60
CHECK_TIMEOUT_SECONDS = 60
_online_workers: set[asyncio.Task] = set()


def _json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _configuration_fingerprint(
    *, base_url: str, protocol_name: str, model_mappings: list[dict[str, str]],
    upstream_multiplier: float, route: str, group_name: str, credential: str,
) -> str:
    return "cc1:" + _json_hash({
        "base_url": base_url.rstrip("/"),
        "protocol": protocol_name,
        "model_mappings": sorted(model_mappings, key=lambda item: item["canonical_model"]),
        "upstream_multiplier": float(upstream_multiplier),
        "route": route,
        "group_name": group_name,
        "credential_fingerprint": credential,
    })


def _configuration(configuration_id: int) -> dict[str, Any]:
    configuration = store.get("channel_configurations", configuration_id)
    if not configuration:
        raise HTTPException(status_code=404, detail="精确连接配置不存在")
    return configuration


def mappings(configuration_id: int) -> list[dict[str, Any]]:
    return store.query(
        "SELECT mappings.*,targets.name target_name FROM configuration_model_mappings mappings "
        "JOIN targets ON targets.id=mappings.legacy_target_id "
        "WHERE mappings.configuration_id=? ORDER BY mappings.sort_order,mappings.id",
        (configuration_id,),
    )


def _safe_reason(value: str) -> str:
    return value.strip()[:1000]


def _audit(
    configuration_id: int, action: str, *, previous: dict[str, Any] | None = None,
    next_value: dict[str, Any] | None = None, reason_code: str = "", reason: str = "",
    user: dict[str, Any] | None = None,
) -> None:
    store.insert("configuration_audits", {
        "configuration_id": configuration_id,
        "action": action,
        "previous_value_json": store.dumps(previous or {}),
        "next_value_json": store.dumps(next_value or {}),
        "reason_code": reason_code,
        "reason": _safe_reason(reason),
        "user_id": user.get("id") if user else None,
        "actor": user.get("username", "") if user else "",
        "created_at": time.time(),
    })


def _configuration_truth(configuration: dict[str, Any]) -> dict[str, Any] | None:
    rows = store.query(
        "SELECT * FROM configuration_fidelity_truths WHERE configuration_id=? "
        "AND configuration_fingerprint=? AND status='active' AND revoked_at IS NULL "
        "AND expires_at>? ORDER BY id DESC LIMIT 1",
        (configuration["id"], configuration["configuration_fingerprint"], time.time()),
    )
    if not rows:
        return None
    truth = rows[0]
    source = store.get("paired_tasks", truth["source_task_id"], key="task_id")
    if not source or source.get("fidelity_manifest_root") != truth["source_manifest_root"]:
        return None
    checked = paired_evidence.verify_manifest(
        truth["source_task_id"], truth["source_manifest_root"], expected_stage="fidelity",
    )
    return truth if checked["ok"] else None


def _fidelity_status(configuration: dict[str, Any]) -> dict[str, Any]:
    if configuration["business_status"] == "online":
        return {"eligible": True, "code": "online_exempt", "label": "已上线，免保真"}
    truth = _configuration_truth(configuration)
    if truth:
        return {
            "eligible": True, "code": "truth_valid", "label": "有效人工保真“真”定义",
            "truth_id": truth["id"], "expires_at": truth["expires_at"],
        }
    waiting = store.query(
        "SELECT paired.task_id FROM paired_tasks paired JOIN targets ON targets.id=paired.candidate_target_id "
        "WHERE targets.exact_configuration_id=? AND paired.state='awaiting_fidelity_truth' "
        "ORDER BY paired.updated_at DESC LIMIT 1",
        (configuration["id"],),
    )
    if waiting:
        return {
            "eligible": False, "code": "awaiting_human_truth", "label": "等待人工判断",
            "task_id": waiting[0]["task_id"],
        }
    return {"eligible": False, "code": "required", "label": "需要保真"}


def _latest_check(configuration_id: int, *, kind: str | None = None) -> dict[str, Any] | None:
    sql = "SELECT * FROM configuration_connectivity_checks WHERE configuration_id=?"
    params: tuple[Any, ...] = (configuration_id,)
    if kind:
        sql += " AND kind=?"
        params = (configuration_id, kind)
    rows = store.query(sql + " ORDER BY id DESC LIMIT 1", params)
    if not rows:
        return None
    check = rows[0]
    items = store.query(
        "SELECT * FROM configuration_connectivity_check_items WHERE check_id=? ORDER BY id",
        (check["id"],),
    )
    passed = sum(item["status"] == "passed" for item in items)
    failed = sum(item["status"] == "failed" for item in items)
    check["items"] = items
    check["summary"] = f"{passed}/{len(items)} 个模型通过" if items else "尚未检查模型"
    if failed:
        check["summary"] += f"，{failed} 个失败"
    return check


def _active_online_check(configuration_id: int) -> dict[str, Any] | None:
    rows = store.query(
        "SELECT * FROM configuration_connectivity_checks WHERE configuration_id=? "
        "AND kind='online' AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
        (configuration_id,),
    )
    if not rows:
        return None
    check = rows[0]
    items = store.query(
        "SELECT status FROM configuration_connectivity_check_items WHERE check_id=? ORDER BY id",
        (check["id"],),
    )
    done = sum(item["status"] in {"passed", "failed", "canceled"} for item in items)
    return {
        "id": check["id"], "status": check["status"],
        "progress_label": f"{done}/{len(items)} 个模型已完成",
    }


def _current_admission_summary(configuration_id: int) -> dict[str, Any] | None:
    rows = store.query(
        "SELECT batches.id,batches.status,conclusions.verdict,conclusions.created_at "
        "FROM admission_batches batches LEFT JOIN admission_batch_conclusions conclusions "
        "ON conclusions.batch_id=batches.id AND conclusions.conclusion_version=batches.current_conclusion_version "
        "WHERE batches.configuration_id=? ORDER BY batches.created_at DESC LIMIT 1",
        (configuration_id,),
    )
    if not rows:
        return None
    row = rows[0]
    labels = {"admit": "已准入", "do_not_admit": "暂不准入"}
    return {
        "batch_id": row["id"],
        "status": row["status"],
        "status_label": labels.get(row.get("verdict"), {
            "awaiting_conclusion": "等待整体结论",
            "pending_retest": "待补测",
            "testing": "测试中",
        }.get(row["status"], "暂无结论")),
    }


def _next_action(configuration: dict[str, Any], fidelity: dict[str, Any]) -> str:
    if _active_online_check(configuration["id"]):
        return "none"
    if configuration["business_status"] == "disabled":
        return "none"
    current = _current_admission_summary(configuration["id"])
    if current and current["status"] in ACTIVE_BATCH_STATUSES:
        return "view_batch"
    if configuration["business_status"] == "online":
        return "none"
    if fidelity["code"] == "awaiting_human_truth":
        return "review_fidelity"
    if fidelity["eligible"]:
        return "create_admission_batch"
    return "create_fidelity"


def _public_configuration(configuration: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    model_mappings = mappings(configuration["id"])
    fidelity = _fidelity_status(configuration)
    latest_check = _latest_check(configuration["id"])
    active_check = _active_online_check(configuration["id"])
    status_labels = {
        "pending_test": "待测试", "offline": "未上线", "online": "已上线", "disabled": "已停用",
    }
    current_admission = _current_admission_summary(configuration["id"])
    attention = int(bool(active_check)) + int(
        bool(current_admission and current_admission["status"] == "pending_retest")
    )
    result = {
        "id": configuration["id"], "source_configuration_id": configuration.get("source_configuration_id"),
        "family_id": configuration["family_id"], "channel_name": configuration["channel_name"],
        "display_name": configuration["display_name"], "protocol": configuration["protocol"],
        "base_url_masked": redact_url(configuration["base_url"]),
        "upstream_multiplier": configuration["upstream_multiplier"], "route": configuration["route"],
        "group_name": configuration["group_name"], "business_status": configuration["business_status"],
        "business_status_label": status_labels[configuration["business_status"]],
        "credential_fingerprint": configuration["credential_fingerprint"],
        "fingerprint": configuration["configuration_fingerprint"],
        "fingerprint_short": configuration["configuration_fingerprint"][-12:],
        "note": configuration["note"], "model_mappings": [{
            "id": item["id"], "canonical_model": item["canonical_model"],
            "request_model": item["request_model"],
        } for item in model_mappings],
        "fidelity": fidelity, "fidelity_status_label": fidelity["label"],
        "latest_connectivity_check": _public_check(latest_check) if latest_check else None,
        "active_check": active_check, "current_results": {"admission": current_admission, "comparison": None},
        "attention_count": attention, "next_action": _next_action(configuration, fidelity),
        "permissions": {}, "created_at": configuration["created_at"], "updated_at": configuration["updated_at"],
    }
    if detail:
        result["base_url"] = configuration["base_url"]
        result["history"] = _history(configuration["id"])
    return result


def _public_check(check: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": check["id"], "kind": check["kind"], "status": check["status"],
        "summary": check.get("summary", ""), "error": check.get("reason", ""),
        "started_at": check["started_at"], "finished_at": check.get("finished_at"),
        "items": [{
            "canonical_model": item["canonical_model"], "request_model": item["request_model"],
            "status": item["status"], "error": item["error_text"],
        } for item in check.get("items", [])],
    }


def _history(configuration_id: int) -> list[dict[str, Any]]:
    checks = store.query(
        "SELECT * FROM configuration_connectivity_checks WHERE configuration_id=? ORDER BY id DESC LIMIT 30",
        (configuration_id,),
    )
    batches = store.query(
        "SELECT id,status,created_at,updated_at FROM admission_batches WHERE configuration_id=? "
        "ORDER BY created_at DESC LIMIT 30",
        (configuration_id,),
    )
    output = [{
        "title": "上线检查" if row["kind"] == "online" else "普通连接检查",
        "status": row["status"], "status_label": row["status"],
        "created_at": row["started_at"], "completed_at": row["finished_at"], "current": False,
    } for row in checks]
    output.extend({
        "title": "准入批次", "status": row["status"], "status_label": row["status"],
        "created_at": row["created_at"], "completed_at": row["updated_at"], "current": False,
    } for row in batches)
    return sorted(output, key=lambda row: row.get("created_at") or 0, reverse=True)


def list_configurations(*, eligible_for_admission: bool = False) -> dict[str, Any]:
    configurations = store.query("SELECT * FROM channel_configurations ORDER BY updated_at DESC,id DESC")
    public = [_public_configuration(configuration) for configuration in configurations]
    if eligible_for_admission:
        public = [item for item in public if item["business_status"] == "pending_test" and item["fidelity"]["eligible"]]
    for configuration in public:
        family = store.get("model_families", configuration["family_id"])
        configuration["family_name"] = family["name"] if family else "未分配家族"
    return {
        "configurations": public,
        "summary": {
            "channels": len({item["channel_name"] for item in public}),
            "configurations": len(public),
            "pending_test": sum(item["business_status"] == "pending_test" for item in public),
            "online": sum(item["business_status"] == "online" for item in public),
            "attention": sum(item["attention_count"] for item in public),
        },
    }


def get_configuration(configuration_id: int) -> dict[str, Any]:
    configuration = _configuration(configuration_id)
    result = _public_configuration(configuration, detail=True)
    family = store.get("model_families", configuration["family_id"])
    result["family_name"] = family["name"] if family else "未分配家族"
    return result


def _validate_mappings(family_id: int, value: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    clean: list[dict[str, str]] = []
    # 家族只决定配置在渠道页和定时选择页中的归属。精确连接本身必须
    # 保存完整映射，因此允许它同时声明平台发布的跨家族主测模型。
    available_models = {
        row["model"] for row in store.query(
            "SELECT model FROM model_family_models WHERE enabled=1 AND archived_at IS NULL",
        )
    }
    for raw in value:
        canonical = str(raw.get("canonical_model") or "").strip()
        request_model = str(raw.get("request_model") or "").strip()
        if not canonical or not request_model:
            raise HTTPException(status_code=400, detail="模型映射必须同时填写规范模型和实际请求模型")
        if canonical not in available_models:
            raise HTTPException(status_code=400, detail=f"模型 {canonical} 尚未在模型目录中启用")
        if canonical in seen:
            raise HTTPException(status_code=400, detail=f"模型 {canonical} 重复映射")
        seen.add(canonical)
        clean.append({"canonical_model": canonical, "request_model": request_model})
    if not clean:
        raise HTTPException(status_code=400, detail="至少需要一条模型映射")
    return clean


def create_configuration(body: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    family_id = int(body["family_id"])
    family = store.get("model_families", family_id)
    if not family or family.get("archived_at"):
        raise HTTPException(status_code=400, detail="请选择可用的模型家族")
    source_id = body.get("source_configuration_id")
    source = _configuration(int(source_id)) if source_id else None
    channel_name = str(body.get("channel_name") or "").strip()
    display_name = str(body.get("display_name") or "").strip()
    protocol_name = str(body.get("protocol") or "").strip()
    base_url = str(body.get("base_url") or "").strip().rstrip("/")
    route = str(body.get("route") or "").strip()
    group_name = str(body.get("group_name") or "").strip()
    note = str(body.get("note") or "").strip()
    if not channel_name or not display_name or not base_url:
        raise HTTPException(status_code=400, detail="请完整填写渠道名称、配置名称和地址")
    if protocol_name not in {"openai", "anthropic"}:
        raise HTTPException(status_code=400, detail="第一阶段只支持 OpenAI compatible 或 Anthropic compatible")
    try:
        multiplier = float(body["upstream_multiplier"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="上游采购倍率必须是数字") from exc
    if not 0 < multiplier <= 100:
        raise HTTPException(status_code=400, detail="上游采购倍率必须在 0 到 100 之间")
    api_key = body.get("api_key") or ""
    if not api_key and source:
        api_key = decrypt(source["key_enc"])
    if not api_key:
        raise HTTPException(status_code=400, detail="新精确连接配置必须填写凭据")
    try:
        paired_protocol.validate_protocol(protocol_name)
        egress.validate_url(base_url)
    except (paired_protocol.AdapterError, egress.EgressDenied) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    model_mappings = _validate_mappings(family_id, list(body.get("model_mappings") or []))
    credential = credential_fingerprint(api_key)
    fingerprint = _configuration_fingerprint(
        base_url=base_url, protocol_name=protocol_name, model_mappings=model_mappings,
        upstream_multiplier=multiplier, route=route, group_name=group_name, credential=credential,
    )
    existing = store.query(
        "SELECT id FROM channel_configurations WHERE configuration_fingerprint=?", (fingerprint,)
    )
    if existing:
        raise HTTPException(status_code=409, detail="这份精确连接配置已经存在")
    now = time.time()
    with store.cursor() as cur:
        cur.execute(
            "INSERT INTO channels (name,protocol,base_url,group_name,env,key_enc,source,edited_fields,"
            "created_at,updated_at,lifecycle_status,business_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (display_name, protocol_name, base_url, group_name, "prod", encrypt(api_key), "configuration",
             "[]", now, now, "candidate", ""),
        )
        legacy_channel_id = int(cur.lastrowid)
        cur.execute("UPDATE channels SET business_id=? WHERE id=?", (f"configuration-{legacy_channel_id}", legacy_channel_id))
        cur.execute(
            "INSERT INTO channel_configurations (source_configuration_id,legacy_channel_id,family_id,"
            "channel_name,display_name,protocol,base_url,key_enc,credential_fingerprint,"
            "upstream_multiplier,route,group_name,configuration_fingerprint,business_status,note,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source["id"] if source else None, legacy_channel_id, family_id, channel_name, display_name,
             protocol_name, base_url, encrypt(api_key), credential, multiplier, route, group_name, fingerprint,
             "pending_test", note, now, now),
        )
        configuration_id = int(cur.lastrowid)
        for order, mapping in enumerate(model_mappings):
            target_name = f"{channel_name} · {display_name} · {mapping['canonical_model']}"
            cur.execute(
                "INSERT INTO targets (channel_id,name,protocol,base_url,model,canonical_model,route,group_name,env,"
                "key_enc,upstream_multiplier,source,edited_fields,recorded,status,exact_configuration_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (legacy_channel_id, target_name, protocol_name, base_url, mapping["request_model"],
                 mapping["canonical_model"], route, group_name, "prod", encrypt(api_key), multiplier,
                 "configuration", "[]", 1, "pending", configuration_id, now, now),
            )
            legacy_target_id = int(cur.lastrowid)
            cur.execute(
                "INSERT INTO configuration_model_mappings (configuration_id,canonical_model,request_model,"
                "legacy_target_id,sort_order,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (configuration_id, mapping["canonical_model"], mapping["request_model"], legacy_target_id,
                 order, now, now),
            )
    _audit(configuration_id, "configuration_created", next_value={
        "fingerprint": fingerprint, "business_status": "pending_test",
    }, user=user)
    return get_configuration(configuration_id)


def update_presentation(configuration_id: int, display_name: str, note: str, user: dict[str, Any]) -> dict[str, Any]:
    """Edit only descriptive fields; the exact connection fingerprint is unchanged."""
    configuration = _configuration(configuration_id)
    name = display_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="配置显示名称不能为空")
    clean_note = note.strip()
    if configuration["display_name"] == name and configuration["note"] == clean_note:
        return get_configuration(configuration_id)
    now = time.time()
    store.update("channel_configurations", configuration_id, {
        "display_name": name, "note": clean_note, "updated_at": now,
    })
    store.update("channels", int(configuration["legacy_channel_id"]), {"name": name, "updated_at": now})
    _audit(configuration_id, "configuration_presentation_updated", previous={
        "display_name": configuration["display_name"], "note": configuration["note"],
    }, next_value={"display_name": name, "note": clean_note}, user=user)
    return get_configuration(configuration_id)


def _new_check(configuration_id: int, kind: str, user: dict[str, Any]) -> dict[str, Any]:
    configuration = _configuration(configuration_id)
    if kind == "online":
        active = _active_online_check(configuration_id)
        if active:
            return store.get("configuration_connectivity_checks", active["id"]) or {}
    if kind == "ordinary" and _active_online_check(configuration_id):
        raise HTTPException(status_code=409, detail="上线检查进行中，不能同时执行普通连接检查")
    if kind == "ordinary" and store.query(
        "SELECT id FROM configuration_connectivity_checks WHERE configuration_id=? "
        "AND kind='ordinary' AND status IN ('queued','running') LIMIT 1",
        (configuration_id,),
    ):
        raise HTTPException(status_code=409, detail="该配置已有普通连接检查正在执行")
    now = time.time()
    check_id = store.insert("configuration_connectivity_checks", {
        "configuration_id": configuration_id, "kind": kind, "status": "queued",
        "initiated_by": user["id"], "initiated_actor": user["username"], "started_at": now,
    })
    for mapping in mappings(configuration_id):
        store.insert("configuration_connectivity_check_items", {
            "check_id": check_id, "mapping_id": mapping["id"],
            "canonical_model": mapping["canonical_model"], "request_model": mapping["request_model"],
        })
    _audit(configuration_id, f"{kind}_connectivity_check_started", user=user)
    return store.get("configuration_connectivity_checks", check_id) or {}


async def _check_mapping(configuration: dict[str, Any], item: dict[str, Any]) -> tuple[str, int | None, str]:
    try:
        key = decrypt(configuration["key_enc"])
        canonical = {
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8, "stream": True, "temperature": 0.0,
        }
        payload = paired_protocol.adapt_request(configuration["protocol"], item["request_model"], canonical)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(CHECK_TIMEOUT_SECONDS), follow_redirects=True,
            max_redirects=egress.EGRESS_MAX_REDIRECTS, event_hooks=egress.event_hooks(),
        ) as client:
            async with client.stream(
                "POST", protocol.chat_url(configuration["protocol"], configuration["base_url"]),
                headers=protocol.headers(configuration["protocol"], key), json=payload,
            ) as response:
                if not 200 <= response.status_code < 300:
                    return "failed", response.status_code, f"HTTP {response.status_code}"
                state = paired_protocol.new_stream_state(configuration["protocol"])
                async for line in response.aiter_lines():
                    paired_protocol.consume_sse_line(state, line)
                final = paired_protocol.finalize_stream(state)
        if not final["normal_terminal"]:
            return "failed", 200, "上游未形成完整流式协议终态"
        if not str(final["text"] or "").strip():
            return "failed", 200, "上游返回空正文"
        return "passed", 200, ""
    except paired_protocol.AdapterError as exc:
        return "failed", None, f"协议适配失败：{str(exc)[:180]}"
    except (httpx.HTTPError, ValueError) as exc:
        return "failed", None, f"请求失败：{type(exc).__name__}"


async def run_check(check_id: int) -> dict[str, Any]:
    check = store.get("configuration_connectivity_checks", check_id)
    if not check:
        raise RuntimeError("configuration_check_missing")
    configuration = _configuration(check["configuration_id"])
    if check["status"] == "canceled":
        return check
    store.update("configuration_connectivity_checks", check_id, {"status": "running"})
    for item in store.query(
        "SELECT * FROM configuration_connectivity_check_items WHERE check_id=? AND status IN ('pending','running') ORDER BY id",
        (check_id,),
    ):
        latest = store.get("configuration_connectivity_checks", check_id)
        if not latest or latest["status"] == "canceled":
            break
        store.update("configuration_connectivity_check_items", item["id"], {
            "status": "running", "started_at": time.time(),
        })
        status, http_status, error = await _check_mapping(configuration, item)
        store.update("configuration_connectivity_check_items", item["id"], {
            "status": status, "http_status": http_status, "error_text": error,
            "finished_at": time.time(),
        })
    latest = store.get("configuration_connectivity_checks", check_id)
    if not latest or latest["status"] == "canceled":
        return latest or check
    items = store.query(
        "SELECT * FROM configuration_connectivity_check_items WHERE check_id=? ORDER BY id", (check_id,)
    )
    passed = bool(items) and all(item["status"] == "passed" for item in items)
    failed_items = [item for item in items if item["status"] == "failed"]
    reason = failed_items[0]["error_text"] if failed_items else ""
    status = "passed" if passed else "failed"
    store.update("configuration_connectivity_checks", check_id, {
        "status": status, "reason": reason, "finished_at": time.time(),
    })
    if latest["kind"] == "online" and passed and latest["status"] != "canceled":
        before = configuration["business_status"]
        store.update("channel_configurations", configuration["id"], {
            "business_status": "online", "updated_at": time.time(),
        })
        _audit(configuration["id"], "business_status_changed", previous={"status": before},
               next_value={"status": "online"}, reason_code="connectivity_check_passed",
               reason="全部模型映射通过基础连接检查", user={
                   "id": latest.get("initiated_by"), "username": latest.get("initiated_actor", ""),
               })
    _audit(configuration["id"], f"{latest['kind']}_connectivity_check_finished",
           next_value={"status": status}, reason=reason, user={
               "id": latest.get("initiated_by"), "username": latest.get("initiated_actor", ""),
           })
    return store.get("configuration_connectivity_checks", check_id) or latest


async def ordinary_connectivity_check(configuration_id: int, user: dict[str, Any]) -> dict[str, Any]:
    check = _new_check(configuration_id, "ordinary", user)
    completed = await run_check(check["id"])
    completed = _latest_check(configuration_id, kind="ordinary") or completed
    return _public_check(completed)


def _track_online_worker(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _online_workers.add(task)
    task.add_done_callback(_online_workers.discard)


async def _run_online_check_after_ordinary(check_id: int, configuration_id: int) -> None:
    while store.query(
        "SELECT id FROM configuration_connectivity_checks WHERE configuration_id=? "
        "AND kind='ordinary' AND status IN ('queued','running') LIMIT 1",
        (configuration_id,),
    ):
        latest = store.get("configuration_connectivity_checks", check_id)
        if not latest or latest["status"] == "canceled":
            return
        await asyncio.sleep(0.2)
    latest = store.get("configuration_connectivity_checks", check_id)
    if latest and latest["status"] != "canceled":
        await run_check(check_id)


def start_online_check(
    configuration_id: int, user: dict[str, Any], *, reason_code: str, note: str,
) -> dict[str, Any]:
    configuration = _configuration(configuration_id)
    if configuration["business_status"] == "disabled":
        raise HTTPException(status_code=400, detail="已停用配置不能标记为已上线")
    active = _active_online_check(configuration_id)
    if active:
        existing = _latest_check(configuration_id, kind="online")
        return _public_check(existing or {"id": active["id"], "kind": "online", "status": active["status"]})
    check = _new_check(configuration_id, "online", user)
    store.update("configuration_connectivity_checks", check["id"], {"reason_code": reason_code})
    _audit(configuration_id, "online_mark_requested", previous={
        "status": configuration["business_status"],
    }, reason_code=reason_code, reason=note, user=user)
    if check["status"] == "queued":
        _track_online_worker(_run_online_check_after_ordinary(check["id"], configuration_id))
    return _public_check(_latest_check(configuration_id, kind="online") or check)


def cancel_online_check(configuration_id: int, user: dict[str, Any]) -> dict[str, Any]:
    active = _active_online_check(configuration_id)
    if not active:
        raise HTTPException(status_code=409, detail="当前没有可取消的上线检查")
    now = time.time()
    store.update("configuration_connectivity_checks", active["id"], {
        "status": "canceled", "canceled_at": now, "finished_at": now,
        "reason": "管理员取消上线检查",
    })
    store.execute(
        "UPDATE configuration_connectivity_check_items SET status='canceled',finished_at=? "
        "WHERE check_id=? AND status='pending'", (now, active["id"]),
    )
    _audit(configuration_id, "online_connectivity_check_canceled", reason="管理员取消", user=user)
    return _public_check(_latest_check(configuration_id, kind="online") or {})


def change_business_status(
    configuration_id: int, to_status: str, reason_code: str, note: str, user: dict[str, Any],
) -> dict[str, Any]:
    configuration = _configuration(configuration_id)
    if to_status not in BUSINESS_STATUSES:
        raise HTTPException(status_code=400, detail="未知的配置业务状态")
    if to_status == "online":
        raise HTTPException(status_code=400, detail="标记已上线必须通过基础连接检查")
    if _active_online_check(configuration_id):
        raise HTTPException(status_code=409, detail="请先取消正在执行的上线检查，再调整配置业务状态")
    if not reason_code:
        raise HTTPException(status_code=400, detail="调整业务状态必须选择理由")
    if to_status == "disabled" and not note.strip():
        raise HTTPException(status_code=400, detail="标记已停用必须填写具体原因")
    if configuration["business_status"] == to_status:
        return get_configuration(configuration_id)
    store.update("channel_configurations", configuration_id, {
        "business_status": to_status, "updated_at": time.time(),
    })
    _audit(configuration_id, "business_status_changed", previous={"status": configuration["business_status"]},
           next_value={"status": to_status}, reason_code=reason_code, reason=note, user=user)
    if to_status != "online":
        from . import scheduled_configurations
        labels = {"pending_test": "待测试", "offline": "未上线", "disabled": "已停用"}
        for row in store.query(
            "SELECT scheduled_test_id FROM scheduled_configuration_targets WHERE configuration_id=?",
            (configuration_id,),
        ):
            scheduled_configurations.pause(
                int(row["scheduled_test_id"]),
                f"目标精确连接配置已改为{labels[to_status]}，请重新确认计划目标",
            )
    return get_configuration(configuration_id)


def create_fidelity_task(configuration_id: int, user: dict[str, Any]) -> int:
    configuration = _configuration(configuration_id)
    if configuration["business_status"] in {"online", "disabled"}:
        raise HTTPException(status_code=400, detail="当前配置不需要或不能执行保真")
    fidelity = _fidelity_status(configuration)
    if fidelity["eligible"]:
        raise HTTPException(status_code=409, detail="当前配置已有可复用的有效保真定义")
    if fidelity.get("task_id"):
        return int(fidelity["task_id"])
    first = mappings(configuration_id)[0]
    created = paired_admission.create(
        int(first["legacy_target_id"]), user,
        idempotency_key=f"configuration-fidelity-{configuration_id}-{int(time.time() * 1000)}",
    )
    task_id = int(created["task"]["id"])
    _audit(configuration_id, "fidelity_task_created", next_value={"task_id": task_id}, user=user)
    return task_id


def revoke_fidelity_truth(truth_id: int, reason: str, user: dict[str, Any]) -> dict[str, Any]:
    truth = store.get("configuration_fidelity_truths", truth_id)
    if not truth or truth.get("status") != "active" or truth.get("revoked_at"):
        raise HTTPException(status_code=404, detail="有效精确配置保真定义不存在")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="废止保真定义必须填写原因")
    now = time.time()
    store.update("configuration_fidelity_truths", truth_id, {
        "status": "revoked", "revoked_at": now,
    })
    _audit(truth["configuration_id"], "fidelity_truth_revoked", next_value={"truth_id": truth_id},
           reason=reason, user=user)
    return {"id": truth_id, "status": "revoked", "revoked_at": now}


def _benchmark_options(configuration: dict[str, Any], mapping: dict[str, Any]) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT configs.*,mappings.id mapping_id,mappings.request_model,mappings.legacy_target_id "
        "FROM channel_configurations configs JOIN configuration_model_mappings mappings "
        "ON mappings.configuration_id=configs.id WHERE configs.business_status='online' "
        "AND mappings.canonical_model=? AND configs.id!=? ORDER BY configs.channel_name,configs.display_name",
        (mapping["canonical_model"], configuration["id"]),
    )
    return [{
        "configuration_id": row["id"], "mapping_id": row["mapping_id"],
        "target_id": row["legacy_target_id"], "channel_name": row["channel_name"],
        "display_name": row["display_name"], "upstream_multiplier": row["upstream_multiplier"],
        "canonical_model": mapping["canonical_model"], "request_model": row["request_model"],
    } for row in rows]


def batch_preview(configuration_id: int) -> dict[str, Any]:
    configuration = _configuration(configuration_id)
    fidelity = _fidelity_status(configuration)
    batch_rows = store.query(
        "SELECT id FROM admission_batches WHERE configuration_id=? AND status IN ('draft','awaiting_fidelity_truth','testing','pending_retest','awaiting_conclusion') "
        "ORDER BY created_at DESC LIMIT 1", (configuration_id,),
    )
    batch = batch_out(batch_rows[0]["id"], refresh=True) if batch_rows else None
    models = [{
        "mapping_id": mapping["id"], "canonical_model": mapping["canonical_model"],
        "request_model": mapping["request_model"],
        "benchmark_options": _benchmark_options(configuration, mapping),
    } for mapping in mappings(configuration_id)]
    all_benchmarks = all(model["benchmark_options"] for model in models)
    eligible = configuration["business_status"] == "pending_test" and fidelity["eligible"]
    blocking = ""
    if configuration["business_status"] != "pending_test":
        blocking = "只有“待测试”的新配置可以启动准入评测；已上线配置请创建普通对比。"
    elif not fidelity["eligible"]:
        blocking = "请先完成自动保真并由保真审核员记录“真”。"
    elif not all_benchmarks:
        blocking = "至少一个模型缺少支持相同具体模型的已上线标杆。"
    return {
        "configuration": get_configuration(configuration_id), "fidelity": fidelity,
        "eligible": eligible and all_benchmarks and not batch, "blocking_reason": blocking,
        "models": models, "batch": batch, "estimate": paired_admission.estimate(
            int(mappings(configuration_id)[0]["legacy_target_id"]),
        ),
    }


def _validate_batch_selection(configuration: dict[str, Any], selections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidate_mappings = {row["canonical_model"]: row for row in mappings(configuration["id"])}
    selected = {str(row.get("canonical_model") or ""): int(row.get("benchmark_configuration_id") or 0) for row in selections}
    if set(selected) != set(candidate_mappings):
        raise HTTPException(status_code=400, detail="必须为全部待准入具体模型分别选择一个标杆")
    rows: list[dict[str, Any]] = []
    for canonical_model, candidate_mapping in candidate_mappings.items():
        benchmark_configuration = _configuration(selected[canonical_model])
        if benchmark_configuration["id"] == configuration["id"]:
            raise HTTPException(status_code=400, detail="候选端和标杆端不能使用同一份精确连接配置")
        if benchmark_configuration["business_status"] != "online":
            raise HTTPException(status_code=400, detail=f"{canonical_model} 的标杆尚未上线")
        matching = store.query(
            "SELECT * FROM configuration_model_mappings WHERE configuration_id=? AND canonical_model=?",
            (benchmark_configuration["id"], canonical_model),
        )
        if not matching:
            raise HTTPException(status_code=400, detail=f"标杆不支持 {canonical_model}")
        benchmark_mapping = matching[0]
        rows.append({
            "candidate_mapping": candidate_mapping, "benchmark_configuration": benchmark_configuration,
            "benchmark_mapping": benchmark_mapping,
        })
    return rows


async def _create_model_task(
    batch_model_id: int, candidate_mapping: dict[str, Any], benchmark_mapping: dict[str, Any],
    user: dict[str, Any], *, parent_task_id: int | None = None,
) -> int:
    created = paired_admission.rerun(
        parent_task_id, user,
        idempotency_key=f"admission-batch-model-rerun-{batch_model_id}-{int(time.time() * 1000)}",
    ) if parent_task_id else paired_admission.create(
        int(candidate_mapping["legacy_target_id"]), user,
        idempotency_key=f"admission-batch-model-{batch_model_id}-{int(time.time() * 1000)}",
    )
    task_id = int(created["task"]["id"])
    batch_model = store.get("admission_batch_models", batch_model_id) or {}
    batch_id = batch_model.get("batch_id")
    store.update("paired_tasks", task_id, {"admission_batch_model_id": batch_model_id}, key="task_id")
    task = store.get("tasks", task_id)
    if task:
        public_snapshot = store.loads(task["snapshot"], {})
        public_snapshot["admission_batch"] = {
            "batch_id": batch_id, "batch_model_id": batch_model_id,
            "canonical_model": candidate_mapping["canonical_model"], "parent_task_id": parent_task_id,
        }
        store.update("tasks", task_id, {"snapshot": store.dumps(public_snapshot)})
    paired_evidence.append(task_id, "admission_batch_link", {
        "batch_id": batch_id, "batch_model_id": batch_model_id,
        "canonical_model": candidate_mapping["canonical_model"], "parent_task_id": parent_task_id,
    })
    paired = created["paired"]
    started = paired_admission.start_pair(
        task_id, int(benchmark_mapping["legacy_target_id"]), request_limit=None,
        token_limit=None, money_limit=None, expected_state_version=int(paired["state_version"]),
        user=user, idempotency_key=f"admission-batch-start-{batch_model_id}-{task_id}",
    )
    from . import runner
    await runner.submit(task_id)
    return int(started["task"]["id"])


async def create_batch(
    configuration_id: int, selections: list[dict[str, Any]], user: dict[str, Any],
) -> dict[str, Any]:
    preview = batch_preview(configuration_id)
    if preview.get("batch"):
        return preview["batch"]
    if not preview["eligible"]:
        raise HTTPException(status_code=400, detail=preview["blocking_reason"] or "当前配置不能启动准入批次")
    configuration = _configuration(configuration_id)
    validated = _validate_batch_selection(configuration, selections)
    now = time.time()
    batch_id = store.insert("admission_batches", {
        "configuration_id": configuration_id, "status": "draft", "created_by": user["id"],
        "created_at": now, "updated_at": now,
    })
    try:
        for selection in validated:
            candidate = selection["candidate_mapping"]
            benchmark = selection["benchmark_mapping"]
            model_id = store.insert("admission_batch_models", {
                "batch_id": batch_id, "mapping_id": candidate["id"],
                "canonical_model": candidate["canonical_model"], "request_model": candidate["request_model"],
                "benchmark_configuration_id": selection["benchmark_configuration"]["id"],
                "benchmark_mapping_id": benchmark["id"], "benchmark_target_id": benchmark["legacy_target_id"],
                "status": "testing", "created_at": now, "updated_at": now,
            })
            task_id = await _create_model_task(model_id, candidate, benchmark, user)
            store.update("admission_batch_models", model_id, {"current_task_id": task_id, "updated_at": time.time()})
        store.update("admission_batches", batch_id, {"status": "testing", "updated_at": time.time()})
    except Exception:
        store.update("admission_batches", batch_id, {"status": "pending_retest", "updated_at": time.time()})
        raise
    _audit(configuration_id, "admission_batch_created", next_value={"batch_id": batch_id}, user=user)
    return batch_out(batch_id, refresh=True)


def _refresh_batch(batch: dict[str, Any]) -> dict[str, Any]:
    if batch["status"] == "concluded":
        return batch
    model_rows = store.query("SELECT * FROM admission_batch_models WHERE batch_id=? ORDER BY id", (batch["id"],))
    now = time.time()
    successful = 0
    running = False
    updates: list[tuple[int, dict[str, Any]]] = []
    for model in model_rows:
        task_id = model.get("current_task_id")
        paired = store.get("paired_tasks", task_id, key="task_id") if task_id else None
        if paired and paired["state"] in SUCCESSFUL_PAIRED_STATES:
            integrity = paired_admission.verify_integrity(int(task_id), paired)
            if integrity["ok"] and paired.get("report_version"):
                successful += 1
                updates.append((model["id"], {
                    "status": "completed", "current_report_task_id": task_id,
                    "current_report_version": paired["report_version"], "current_report_at": paired["updated_at"],
                    "report_invalidated_at": None, "updated_at": now,
                }))
                continue
        if paired and paired["state"] not in TERMINAL_PAIRED_STATES:
            running = True
            updates.append((model["id"], {"status": "testing", "updated_at": now}))
        else:
            updates.append((model["id"], {"status": "pending_retest", "updated_at": now}))
    for model_id, patch in updates:
        store.update("admission_batch_models", model_id, patch)
    refreshed = store.get("admission_batches", batch["id"]) or batch
    models = store.query("SELECT * FROM admission_batch_models WHERE batch_id=? ORDER BY id", (batch["id"],))
    report_times = [float(model["current_report_at"]) for model in models
                    if model.get("current_report_task_id") and not model.get("report_invalidated_at")]
    if len(report_times) == len(models) and report_times:
        earliest = min(report_times)
        if max(report_times) - earliest <= EVIDENCE_WINDOW_SECONDS and now <= earliest + EVIDENCE_WINDOW_SECONDS:
            status = "awaiting_conclusion"
        else:
            status = "pending_retest"
            for model in models:
                if model.get("current_report_at") and float(model["current_report_at"]) < now - EVIDENCE_WINDOW_SECONDS:
                    store.update("admission_batch_models", model["id"], {
                        "current_report_task_id": None, "current_report_version": None,
                        "current_report_at": None, "status": "pending_retest", "updated_at": now,
                    })
    else:
        status = "testing" if running else "pending_retest"
    if refreshed["status"] != status:
        store.update("admission_batches", refreshed["id"], {"status": status, "updated_at": now})
    return store.get("admission_batches", batch["id"]) or refreshed


def _batch_model_out(model: dict[str, Any]) -> dict[str, Any]:
    benchmark = _configuration(model["benchmark_configuration_id"])
    report = None
    if model.get("current_report_task_id"):
        report = {
            "task_id": model["current_report_task_id"], "version": model["current_report_version"],
            "summary": "当前成功封存正式报告",
            "href": f"task.html?id={model['current_report_task_id']}",
        }
    return {
        "id": model["id"], "canonical_model": model["canonical_model"],
        "request_model": model["request_model"], "state": model["status"],
        "benchmark_configuration_id": model["benchmark_configuration_id"],
        "benchmark_label": f"{benchmark['channel_name']} · {benchmark['display_name']} · {benchmark['upstream_multiplier']}×",
        "current_report": report,
    }


def batch_out(batch_id: int, *, refresh: bool = False) -> dict[str, Any]:
    batch = store.get("admission_batches", batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="准入批次不存在")
    if refresh:
        batch = _refresh_batch(batch)
    model_rows = store.query("SELECT * FROM admission_batch_models WHERE batch_id=? ORDER BY id", (batch_id,))
    model_tasks = [_batch_model_out(row) for row in model_rows]
    current_reports = [row for row in model_rows if row.get("current_report_task_id") and not row.get("report_invalidated_at")]
    report_times = [float(row["current_report_at"]) for row in current_reports if row.get("current_report_at")]
    earliest = min(report_times) if report_times else None
    window_valid = bool(earliest and len(current_reports) == len(model_rows)
                        and max(report_times) - earliest <= EVIDENCE_WINDOW_SECONDS
                        and time.time() <= earliest + EVIDENCE_WINDOW_SECONDS)
    conclusion_rows = store.query(
        "SELECT * FROM admission_batch_conclusions WHERE batch_id=? ORDER BY conclusion_version DESC",
        (batch_id,),
    )
    labels = {"admit": "准入", "do_not_admit": "暂不准入"}
    history = [{
        **row, "verdict_label": labels.get(row["verdict"], row["verdict"]),
    } for row in conclusion_rows]
    current = history[0] if history else None
    return {
        "id": batch["id"], "configuration_id": batch["configuration_id"], "status": batch["status"],
        "model_tasks": model_tasks, "current_report_count": len(current_reports),
        "evidence_window": {
            "valid": window_valid, "expires_at": earliest + EVIDENCE_WINDOW_SECONDS if earliest else None,
            "reason": "报告时间跨度超过 7 天或已过期" if report_times and not window_valid else "",
        },
        "current_conclusion_version": batch["current_conclusion_version"],
        "current_conclusion": current, "conclusion_history": history,
        "expected_report_versions": [{"canonical_model": row["canonical_model"], "task_id": row["current_report_task_id"], "report_version": row["current_report_version"]} for row in current_reports],
        "permissions": {}, "created_at": batch["created_at"], "updated_at": batch["updated_at"],
    }


def _batch_ready_for_conclusion(batch: dict[str, Any], report_versions: list[dict[str, Any]]) -> None:
    refreshed = _refresh_batch(batch)
    if refreshed["status"] not in {"awaiting_conclusion", "concluded"}:
        raise HTTPException(status_code=409, detail="当前批次尚未具备全部有效的正式报告")
    output = batch_out(refreshed["id"])
    if not output["evidence_window"]["valid"]:
        raise HTTPException(status_code=409, detail="当前正式报告已超出 7 天证据窗口，不能提交或更正结论")
    expected = output["expected_report_versions"]
    if sorted(report_versions, key=lambda item: item["canonical_model"]) != sorted(expected, key=lambda item: item["canonical_model"]):
        raise HTTPException(status_code=409, detail="模型活动报告版本已变化，请刷新后重新确认")


def submit_batch_conclusion(
    batch_id: int, verdict: str, reason: str, evidence_refs: list[str], report_versions: list[dict[str, Any]],
    expected_conclusion_version: int, user: dict[str, Any],
) -> dict[str, Any]:
    if verdict not in {"admit", "do_not_admit"}:
        raise HTTPException(status_code=400, detail="准入批次只能提交“准入”或“暂不准入”结论")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="人工结论必须填写理由")
    if verdict == "admit" and not evidence_refs:
        raise HTTPException(status_code=400, detail="选择准入时必须引用具体模型报告或证据")
    batch = store.get("admission_batches", batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="准入批次不存在")
    if int(batch["current_conclusion_version"]) != expected_conclusion_version:
        raise HTTPException(status_code=409, detail="人工结论版本已变化，请刷新后重试")
    _batch_ready_for_conclusion(batch, report_versions)
    previous_rows = store.query(
        "SELECT * FROM admission_batch_conclusions WHERE batch_id=? ORDER BY conclusion_version DESC LIMIT 1",
        (batch_id,),
    )
    correction = batch["status"] == "concluded"
    if correction:
        if not previous_rows:
            raise HTTPException(status_code=409, detail="已结束批次缺少原结论，无法更正")
        if previous_rows[0]["verdict"] == verdict:
            raise HTTPException(status_code=400, detail="更正后的结论与当前有效结论相同")
        previous_versions = store.loads(previous_rows[0]["report_versions_json"], [])
        if sorted(previous_versions, key=lambda item: item["canonical_model"]) != sorted(report_versions, key=lambda item: item["canonical_model"]):
            raise HTTPException(status_code=409, detail="结论更正必须绑定原结论使用的同一组活动报告")
    configuration = _configuration(batch["configuration_id"])
    if correction and previous_rows and previous_rows[0]["verdict"] == "admit" \
            and verdict == "do_not_admit" and configuration["business_status"] == "online":
        raise HTTPException(status_code=409, detail="配置仍为已上线；请由管理员先确认退出业务并改为未上线，再更正为暂不准入")
    now = time.time()
    next_version = int(batch["current_conclusion_version"]) + 1
    self_review = int(batch["created_by"] == user["id"])
    store.insert("admission_batch_conclusions", {
        "batch_id": batch_id, "conclusion_version": next_version, "verdict": verdict,
        "reason": reason.strip(), "evidence_refs_json": store.dumps(evidence_refs),
        "report_versions_json": store.dumps(report_versions), "self_review": self_review,
        "user_id": user["id"], "actor": user["username"], "created_at": now,
    })
    store.update("admission_batches", batch_id, {
        "status": "concluded", "current_conclusion_version": next_version,
        "closed_at": now, "updated_at": now,
    })
    if verdict == "admit":
        if configuration["business_status"] != "offline":
            store.update("channel_configurations", configuration["id"], {
                "business_status": "offline", "updated_at": now,
            })
            _audit(configuration["id"], "business_status_changed", previous={"status": configuration["business_status"]},
                   next_value={"status": "offline"}, reason_code="admission_approved",
                   reason="配置级准入结论为准入，等待管理员明确上线", user=user)
            from . import scheduled_configurations
            for row in store.query(
                "SELECT scheduled_test_id FROM scheduled_configuration_targets WHERE configuration_id=?",
                (configuration["id"],),
            ):
                scheduled_configurations.pause(
                    int(row["scheduled_test_id"]), "目标精确连接配置已由准入结论转为未上线，请重新确认计划目标",
                )
    elif correction and previous_rows and previous_rows[0]["verdict"] == "admit":
        if configuration["business_status"] == "offline":
            store.update("channel_configurations", configuration["id"], {
                "business_status": "pending_test", "updated_at": now,
            })
            _audit(configuration["id"], "business_status_changed", previous={"status": "offline"},
                   next_value={"status": "pending_test"}, reason_code="admission_corrected",
                   reason="当前准入结论已更正为暂不准入", user=user)
    _audit(configuration["id"], "admission_conclusion_corrected" if correction else "admission_conclusion_submitted", next_value={
        "batch_id": batch_id, "verdict": verdict, "conclusion_version": next_version,
    }, reason=reason, user=user)
    return batch_out(batch_id, refresh=False)


async def continue_batch(batch_id: int, models: list[str], reason: str, user: dict[str, Any]) -> dict[str, Any]:
    if not models or not reason.strip():
        raise HTTPException(status_code=400, detail="继续补测必须选择模型并填写原因")
    batch = store.get("admission_batches", batch_id)
    if not batch or batch["status"] == "concluded":
        raise HTTPException(status_code=400, detail="该准入批次已经结束，不能继续补测")
    configuration = _configuration(batch["configuration_id"])
    fidelity = _fidelity_status(configuration)
    if not fidelity["eligible"]:
        raise HTTPException(status_code=409, detail="当前保真定义不可复用，请先创建新的保真任务")
    rows = store.query(
        "SELECT * FROM admission_batch_models WHERE batch_id=? AND canonical_model IN (%s)" % ",".join("?" * len(models)),
        (batch_id, *models),
    )
    if len(rows) != len(set(models)):
        raise HTTPException(status_code=400, detail="选择了不属于当前批次的模型")
    now = time.time()
    for row in rows:
        candidate = store.get("configuration_model_mappings", row["mapping_id"])
        benchmark = store.get("configuration_model_mappings", row["benchmark_mapping_id"])
        if not candidate or not benchmark:
            raise HTTPException(status_code=409, detail="补测所需的配置映射已不可用")
        store.update("admission_batch_models", row["id"], {
            "current_report_task_id": None, "current_report_version": None, "current_report_at": None,
            "report_invalidated_at": now, "status": "testing", "updated_at": now,
        })
        task_id = await _create_model_task(
            row["id"], candidate, benchmark, user, parent_task_id=row.get("current_task_id"),
        )
        store.update("admission_batch_models", row["id"], {
            "current_task_id": task_id, "updated_at": time.time(),
        })
    store.update("admission_batches", batch_id, {"status": "testing", "updated_at": time.time()})
    _audit(configuration["id"], "admission_batch_continue", next_value={"batch_id": batch_id, "models": models}, reason=reason, user=user)
    return batch_out(batch_id, refresh=True)


def resume_online_checks() -> None:
    for check in store.query(
        "SELECT id FROM configuration_connectivity_checks WHERE kind='online' AND status IN ('queued','running')"
    ):
        _track_online_worker(run_check(check["id"]))
