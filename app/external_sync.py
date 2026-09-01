"""人工上线确认、生产只读核验与飞书多维表格 Outbox。"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import egress, lifecycle, store, workbench
from .security import decrypt, encrypt, scrub

VERIFY_PROTOCOL_VERSION = "1"
OUTBOX_TICK_SECONDS = 15
MAX_RETRY_SECONDS = 3600
_worker: asyncio.Task | None = None


class SyncError(ValueError):
    pass


def source_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "endpoint": row["endpoint"],
        "protocol_version": row["protocol_version"], "enabled": bool(row["enabled"]),
        "token_configured": bool(row["token_enc"]),
        "last_checked_at": row["last_checked_at"], "last_error": row["last_error"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def create_source(body: dict[str, Any]) -> dict[str, Any]:
    endpoint = egress.validate_url(body["endpoint"])
    if urlsplit(endpoint).query:
        raise SyncError("上线核验凭据不能放在 URL 查询参数中")
    now = time.time()
    source_id = store.insert("online_verification_sources", {
        "name": body["name"].strip(), "endpoint": endpoint,
        "token_enc": encrypt(body.get("token", "")) if body.get("token") else "",
        "protocol_version": body.get("protocol_version", "1"),
        "enabled": 1 if body.get("enabled", True) else 0,
        "last_error": "", "created_at": now, "updated_at": now,
    })
    row = store.get("online_verification_sources", source_id)
    assert row is not None
    return source_out(row)


def _safe_base_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _channel_targets(channel_id: int) -> list[dict[str, Any]]:
    return store.query(
        "SELECT targets.*,platform_groups.multiplier platform_multiplier," 
        "model_families.name family_name FROM targets "
        "LEFT JOIN platform_groups ON platform_groups.id=targets.platform_group_id "
        "LEFT JOIN model_families ON model_families.id=platform_groups.family_id "
        "WHERE targets.channel_id=? AND targets.archived_at IS NULL ORDER BY targets.model,id",
        (channel_id,),
    )


def config_draft(channel: dict[str, Any]) -> dict[str, Any]:
    targets = _channel_targets(channel["id"])
    return {
        "business_id": channel["business_id"], "name": channel["name"],
        "protocol": channel["protocol"], "base_url": _safe_base_url(channel["base_url"]),
        "credential": "使用人工上线流程中的安全密钥，不在草案中保存或显示",
        "models": [{
            "canonical_model": target["model"],
            "platform_group_id": target["platform_group_id"],
            "platform_multiplier": target["platform_multiplier"],
            "upstream_multiplier": target["upstream_multiplier"],
        } for target in targets],
        "generated_at": time.time(),
        "automation_boundary": "该草案只供人工在中转站上线，测试平台不会写生产配置。",
    }


def launch_out(row: dict[str, Any]) -> dict[str, Any]:
    channel = store.get("channels", row["channel_id"])
    return {
        **row, "channel_name": (channel or {}).get("name", ""),
        "business_id": (channel or {}).get("business_id", ""),
        "config_draft": store.loads(row["config_draft_json"], {}),
        "verification": store.loads(row["verification_json"], {}),
    }


def ensure_launch(channel_id: int) -> dict[str, Any]:
    channel = store.get("channels", channel_id)
    if not channel:
        raise SyncError("渠道不存在")
    rows = store.query("SELECT * FROM channel_launches WHERE channel_id=?", (channel_id,))
    if rows:
        return launch_out(rows[0])
    completed = store.query(
        "SELECT COUNT(*) n FROM tasks JOIN targets ON targets.id=tasks.target_id "
        "WHERE targets.channel_id=? AND tasks.kind='admission' "
        "AND tasks.status IN ('success','partial') AND tasks.report!=''", (channel_id,)
    )[0]["n"]
    if not completed:
        raise SyncError("渠道尚未产生可复核的准入报告")
    now = time.time()
    launch_id = store.insert("channel_launches", {
        "channel_id": channel_id, "status": "tested",
        "config_draft_json": store.dumps(config_draft(channel)),
        "verification_json": "{}", "owner_note": "",
        "created_at": now, "updated_at": now,
    })
    row = store.get("channel_launches", launch_id)
    assert row is not None
    return launch_out(row)


def confirm_launch(
    channel_id: int, user: dict[str, Any], owner_note: str,
) -> dict[str, Any]:
    launch = ensure_launch(channel_id)
    if launch["status"] not in {"tested", "confirmed"}:
        raise SyncError("当前上线状态不能重复确认")
    channel = store.get("channels", channel_id)
    assert channel is not None
    current = channel["lifecycle_status"]
    system = {"id": user["id"], "username": user["username"]}
    if current == "candidate":
        lifecycle.transition_channel(channel_id, "tested", "已有准入报告并进入上线确认", system)
        current = "tested"
    if current == "tested":
        lifecycle.transition_channel(channel_id, "approved", "用户确认技术接入建议", system)
    elif current != "approved":
        raise SyncError("只有已测试渠道可以确认技术接入建议")
    now = time.time()
    store.update("channel_launches", launch["id"], {
        "status": "confirmed", "confirmed_by": user["id"], "confirmed_at": now,
        "owner_note": owner_note.strip(), "updated_at": now,
    })
    return launch_out(store.get("channel_launches", launch["id"]))


async def verify_online(channel_id: int, source_id: int) -> dict[str, Any]:
    launch = ensure_launch(channel_id)
    if launch["status"] not in {"confirmed", "verified", "synced"}:
        raise SyncError("必须先确认技术接入建议，再核验实际上线")
    source = store.get("online_verification_sources", source_id)
    channel = store.get("channels", channel_id)
    if not source or not source["enabled"]:
        raise SyncError("上线核验数据源不存在或已停用")
    assert channel is not None
    headers = {"Accept": "application/json", "X-Protocol-Version": VERIFY_PROTOCOL_VERSION}
    if source["token_enc"]:
        headers["Authorization"] = f"Bearer {decrypt(source['token_enc'])}"
    try:
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True,
            max_redirects=egress.EGRESS_MAX_REDIRECTS,
            event_hooks=egress.event_hooks(),
        ) as client:
            response = await client.get(
                source["endpoint"], headers=headers,
                params={"business_id": channel["business_id"]},
            )
        response.raise_for_status()
        egress.ensure_response_size(response)
        payload = response.json()
    except Exception as exc:
        error = scrub(str(exc))
        store.update("online_verification_sources", source_id, {
            "last_checked_at": time.time(), "last_error": error, "updated_at": time.time(),
        })
        raise SyncError("无法读取中转站实际上线状态") from exc
    if str(payload.get("version")) != source["protocol_version"]:
        raise SyncError("上线核验协议版本不一致")
    actual = payload.get("channel") or {}
    if actual.get("business_id") != channel["business_id"] or actual.get("status") != "online":
        raise SyncError("中转站只读接口尚未确认该业务渠道在线")
    expected_models = {row["model"] for row in _channel_targets(channel_id)}
    actual_models = {str(item.get("canonical_model") or item.get("model") or "")
                     for item in (actual.get("models") or [])}
    missing = sorted(expected_models - actual_models)
    if missing:
        raise SyncError(f"中转站实际在线模型缺失：{'、'.join(missing)}")
    now = time.time()
    verification = {
        "source_id": source_id, "business_id": channel["business_id"],
        "actual_status": "online", "models": sorted(actual_models),
        "verified_at": now, "read_only": True,
    }
    store.update("channel_launches", launch["id"], {
        "status": "verified", "verification_source_id": source_id,
        "verification_json": store.dumps(verification), "verified_at": now,
        "updated_at": now,
    })
    updated_channel = store.get("channels", channel_id)
    assert updated_channel is not None
    if updated_channel["lifecycle_status"] == "approved":
        lifecycle.transition_channel(
            channel_id, "online_verified", "中转站只读接口确认真实上线",
            {"id": None, "username": "system"},
        )
    store.update("online_verification_sources", source_id, {
        "last_checked_at": now, "last_error": "", "updated_at": now,
    })
    return launch_out(store.get("channel_launches", launch["id"]))


def feishu_settings_out(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "app_id": row["app_id"], "app_secret_configured": bool(row["app_secret_enc"]),
        "base_token": row["base_token"], "channel_table_id": row["channel_table_id"],
        "model_table_id": row["model_table_id"], "enabled": bool(row["enabled"]),
        "updated_at": row["updated_at"],
    }


def save_feishu_settings(body: dict[str, Any]) -> dict[str, Any]:
    current = store.get("feishu_bitable_settings", 1)
    secret = body.get("app_secret", "")
    if not secret and not current:
        raise SyncError("首次配置必须填写飞书应用密钥")
    data = {
        "app_id": body["app_id"].strip(),
        "app_secret_enc": encrypt(secret) if secret else current["app_secret_enc"],
        "base_token": body["base_token"].strip(),
        "channel_table_id": body["channel_table_id"].strip(),
        "model_table_id": body["model_table_id"].strip(),
        "enabled": 1 if body.get("enabled", True) else 0,
        "updated_at": time.time(),
    }
    if current:
        store.update("feishu_bitable_settings", 1, data)
    else:
        store.insert("feishu_bitable_settings", {"id": 1, **data})
    result = feishu_settings_out(store.get("feishu_bitable_settings", 1))
    assert result is not None
    return result


def _domain(value: str) -> str:
    return urlsplit(value).hostname or ""


def desired_records(channel_id: int) -> dict[str, Any]:
    channel = store.get("channels", channel_id)
    launch_rows = store.query("SELECT * FROM channel_launches WHERE channel_id=?", (channel_id,))
    if not channel or not launch_rows or launch_rows[0]["status"] not in {"verified", "synced"}:
        raise SyncError("只有真实上线核验成功的渠道才能同步飞书")
    launch = launch_rows[0]
    targets = _channel_targets(channel_id)
    first_test = store.query(
        "SELECT MIN(tasks.created_at) at FROM tasks JOIN targets ON targets.id=tasks.target_id "
        "WHERE targets.channel_id=? AND tasks.kind='admission'", (channel_id,)
    )[0]["at"]
    latest_inspect = store.query(
        "SELECT MAX(tasks.finished_at) at FROM tasks JOIN targets ON targets.id=tasks.target_id "
        "WHERE targets.channel_id=? AND tasks.kind='inspect'", (channel_id,)
    )[0]["at"]
    channel_fields = {
        "业务渠道 ID": channel["business_id"], "渠道名称": channel["name"],
        "协议": channel["protocol"], "脱敏域名": _domain(channel["base_url"]),
        "上游采购倍率": max((row["upstream_multiplier"] or 0 for row in targets), default=0),
        "当前状态": lifecycle.CHANNEL_STATES[channel["lifecycle_status"]],
        "首次测试时间": first_test or 0, "实际上线确认时间": launch["verified_at"] or 0,
        "最近巡检时间": latest_inspect or 0, "负责人或备注": launch["owner_note"],
        "测试平台详情链接": f"launches.html?channel={channel_id}",
    }
    model_records = []
    for target in targets:
        family_model = store.query(
            "SELECT models.*,families.name family_name FROM model_family_models models "
            "JOIN model_families families ON families.id=models.family_id "
            "WHERE models.model=? ORDER BY models.id LIMIT 1", (target["model"],)
        )
        model_meta = family_model[0] if family_model else {}
        labels = store.query(
            "SELECT usage_profile,status,pack_version FROM target_usage_labels "
            "WHERE target_id=? ORDER BY created_at DESC", (target["id"],)
        )
        latest_task = store.get("tasks", target["last_task_id"]) if target["last_task_id"] else None
        snapshot = store.loads((latest_task or {}).get("snapshot"), {})
        model_records.append({
            "business_id": f"{channel['business_id']}::{target['model']}",
            "fields": {
                "模型映射 ID": f"{channel['business_id']}::{target['model']}",
                "渠道业务 ID": channel["business_id"], "规范模型 ID": target["model"],
                "模型家族": model_meta.get("family_name", target.get("family_name") or ""),
                "平台倍率组": f"{target.get('family_name') or ''} {target.get('platform_multiplier') or 0:g}x",
                "模型生命周期": lifecycle.MODEL_STATES.get(
                    model_meta.get("lifecycle_status", "enabled"), "启用"),
                "基础准入结论": target["last_verdict"],
                "Agent 标签": next((row["status"] for row in labels if row["usage_profile"] == "agent"), "证据不足"),
                "编程标签": next((row["status"] for row in labels if row["usage_profile"] == "coding"), "证据不足"),
                "客服标签": next((row["status"] for row in labels if row["usage_profile"] == "customer_service"), "证据不足"),
                "题库与专项版本": str(snapshot.get("pack_version") or (latest_task or {}).get("pack_version") or ""),
                "标杆来源": str(target.get("benchmark_id") or ""),
                "最近报告链接": f"task.html?id={target['last_task_id']}" if target["last_task_id"] else "",
                "最近巡检状态": target["status"],
                "归档或退役状态": lifecycle.MODEL_STATES.get(
                    model_meta.get("lifecycle_status", "enabled"), "启用"),
            },
        })
    _assert_no_secrets({"channel": channel_fields, "models": model_records})
    return {"channel": {"business_id": channel["business_id"], "fields": channel_fields},
            "models": model_records}


def _assert_no_secrets(value: Any) -> None:
    forbidden = {"api_key", "key", "authorization", "key_enc", "app_secret",
                 "raw_response", "response_body", "request_body"}
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in forbidden:
                raise SyncError("同步数据包含禁止的敏感字段")
            _assert_no_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_secrets(item)


async def _feishu_token(settings: dict[str, Any]) -> str:
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True,
        max_redirects=egress.EGRESS_MAX_REDIRECTS,
        event_hooks=egress.event_hooks(),
    ) as client:
        response = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": settings["app_id"],
                  "app_secret": decrypt(settings["app_secret_enc"])},
        )
    response.raise_for_status()
    egress.ensure_response_size(response)
    payload = response.json()
    if payload.get("code") not in (0, None) or not payload.get("tenant_access_token"):
        raise SyncError("飞书应用鉴权失败")
    return payload["tenant_access_token"]


async def _list_records(
    settings: dict[str, Any], table_id: str, token: str,
) -> list[dict[str, Any]]:
    records = []
    page_token = ""
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True,
        max_redirects=egress.EGRESS_MAX_REDIRECTS,
        event_hooks=egress.event_hooks(),
    ) as client:
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
                   f"{settings['base_token']}/tables/{table_id}/records")
            response = await client.get(
                url, headers={"Authorization": f"Bearer {token}"}, params=params,
            )
            response.raise_for_status()
            egress.ensure_response_size(response)
            payload = response.json()
            if payload.get("code") not in (0, None):
                raise SyncError(f"飞书读取表格失败：{payload.get('msg', 'unknown')}")
            data = payload.get("data") or {}
            records.extend(data.get("items") or [])
            if not data.get("has_more"):
                return records
            page_token = data.get("page_token") or ""


def _managed_fields(fields: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    return {key: fields.get(key) for key in desired}


def _diff(desired: dict[str, Any], remote: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"field": key, "before": remote.get(key), "after": value}
            for key, value in desired.items() if remote.get(key) != value]


async def preview_sync(channel_id: int) -> dict[str, Any]:
    settings = store.get("feishu_bitable_settings", 1)
    if not settings or not settings["enabled"]:
        raise SyncError("尚未启用飞书多维表格配置")
    desired = desired_records(channel_id)
    token = await _feishu_token(settings)
    channel_records, model_records = await asyncio.gather(
        _list_records(settings, settings["channel_table_id"], token),
        _list_records(settings, settings["model_table_id"], token),
    )
    channel_remote = next((item for item in channel_records
                           if item.get("fields", {}).get("业务渠道 ID")
                           == desired["channel"]["business_id"]), None)
    model_remote = {item.get("fields", {}).get("模型映射 ID"): item
                    for item in model_records}
    preview = {
        "channel": {
            "business_id": desired["channel"]["business_id"],
            "remote_record_id": (channel_remote or {}).get("record_id"),
            "changes": _diff(desired["channel"]["fields"],
                             (channel_remote or {}).get("fields", {})),
        },
        "models": [{
            "business_id": item["business_id"],
            "remote_record_id": model_remote.get(item["business_id"], {}).get("record_id"),
            "changes": _diff(item["fields"],
                             model_remote.get(item["business_id"], {}).get("fields", {})),
        } for item in desired["models"]],
    }
    remote = {"channel": channel_remote or {},
              "models": [model_remote.get(item["business_id"], {})
                         for item in desired["models"]]}
    conflicts = _detect_conflicts(desired, remote)
    launch = store.query("SELECT * FROM channel_launches WHERE channel_id=?", (channel_id,))[0]
    now = time.time()
    job_id = store.insert("external_sync_jobs", {
        "provider": "feishu", "channel_id": channel_id, "launch_id": launch["id"],
        "status": "conflict" if conflicts else "preview",
        "desired_json": store.dumps(desired), "remote_json": store.dumps(remote),
        "diff_json": store.dumps({**preview, "conflicts": conflicts}),
        "attempts": 0, "last_error": "", "created_at": now, "updated_at": now,
    })
    return sync_job_out(store.get("external_sync_jobs", job_id))


def _detect_conflicts(desired: dict[str, Any], remote: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts = []
    entries = [("channel", desired["channel"])] + [("model", item) for item in desired["models"]]
    remote_entries = [remote.get("channel") or {}] + list(remote.get("models") or [])
    for (kind, item), remote_item in zip(entries, remote_entries):
        records = store.query(
            "SELECT * FROM external_sync_records WHERE provider='feishu' "
            "AND table_kind=? AND business_id=?", (kind, item["business_id"]),
        )
        if not records or not remote_item:
            continue
        last = store.loads(records[0]["last_synced_json"], {})
        current = _managed_fields(remote_item.get("fields", {}), item["fields"])
        if current != last and current != item["fields"]:
            conflicts.append({
                "table_kind": kind, "business_id": item["business_id"],
                "message": "飞书记录在上次同步后被手工修改，请人工确认差异",
                "last_synced": last, "remote": current, "desired": item["fields"],
            })
    return conflicts


def sync_job_out(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        raise SyncError("同步任务不存在")
    return {**row, "desired": store.loads(row["desired_json"], {}),
            "remote": store.loads(row["remote_json"], {}),
            "diff": store.loads(row["diff_json"], {})}


def confirm_sync(job_id: int, user_id: int) -> dict[str, Any]:
    row = store.get("external_sync_jobs", job_id)
    if not row:
        raise SyncError("同步任务不存在")
    if row["status"] == "conflict":
        raise SyncError("飞书存在手工修改冲突，不能自动覆盖")
    if row["status"] != "preview":
        raise SyncError("同步任务不处于待确认状态")
    now = time.time()
    store.update("external_sync_jobs", job_id, {
        "status": "confirmed", "confirmed_by": user_id,
        "confirmed_at": now, "next_attempt_at": now, "updated_at": now,
    })
    return sync_job_out(store.get("external_sync_jobs", job_id))


async def _upsert_record(
    settings: dict[str, Any], table_id: str, token: str, business_id: str,
    id_field: str, fields: dict[str, Any], existing: dict[str, Any] | None,
) -> str:
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
           f"{settings['base_token']}/tables/{table_id}/records")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True,
        max_redirects=egress.EGRESS_MAX_REDIRECTS,
        event_hooks=egress.event_hooks(),
    ) as client:
        if existing and existing.get("record_id"):
            response = await client.put(
                f"{url}/{existing['record_id']}", headers=headers, json={"fields": fields},
            )
        else:
            response = await client.post(url, headers=headers, json={"fields": fields})
    response.raise_for_status()
    egress.ensure_response_size(response)
    payload = response.json()
    if payload.get("code") not in (0, None):
        raise SyncError(f"飞书写入失败：{payload.get('msg', 'unknown')}")
    record = (payload.get("data") or {}).get("record") or payload.get("data") or {}
    record_id = record.get("record_id") or (existing or {}).get("record_id")
    if not record_id:
        raise SyncError(f"飞书未返回 {id_field}={business_id} 的记录 ID")
    return record_id


def _save_sync_record(kind: str, business_id: str, record_id: str, fields: dict[str, Any]) -> None:
    now = time.time()
    rows = store.query(
        "SELECT id FROM external_sync_records WHERE provider='feishu' "
        "AND table_kind=? AND business_id=?", (kind, business_id),
    )
    data = {"remote_record_id": record_id, "last_synced_json": store.dumps(fields),
            "synced_at": now}
    if rows:
        store.update("external_sync_records", rows[0]["id"], data)
    else:
        store.insert("external_sync_records", {
            "provider": "feishu", "table_kind": kind, "business_id": business_id,
            **data,
        })


async def execute_sync(job_id: int) -> dict[str, Any]:
    row = store.get("external_sync_jobs", job_id)
    if not row or row["status"] not in {"confirmed", "retry"}:
        raise SyncError("同步任务不处于可执行状态")
    settings = store.get("feishu_bitable_settings", 1)
    if not settings or not settings["enabled"]:
        raise SyncError("飞书配置未启用")
    desired = store.loads(row["desired_json"], {})
    attempts = row["attempts"] + 1
    store.update("external_sync_jobs", job_id, {
        "status": "sending", "attempts": attempts, "updated_at": time.time(),
    })
    try:
        token = await _feishu_token(settings)
        channel_records, model_records = await asyncio.gather(
            _list_records(settings, settings["channel_table_id"], token),
            _list_records(settings, settings["model_table_id"], token),
        )
        channel_existing = next((item for item in channel_records
                                 if item.get("fields", {}).get("业务渠道 ID")
                                 == desired["channel"]["business_id"]), None)
        models_existing = {item.get("fields", {}).get("模型映射 ID"): item
                           for item in model_records}
        current_remote = {"channel": channel_existing or {},
                          "models": [models_existing.get(item["business_id"], {})
                                     for item in desired["models"]]}
        conflicts = _detect_conflicts(desired, current_remote)
        if conflicts:
            store.update("external_sync_jobs", job_id, {
                "status": "conflict", "last_error": "飞书记录存在手工修改冲突",
                "diff_json": store.dumps({"conflicts": conflicts}),
                "updated_at": time.time(),
            })
            return sync_job_out(store.get("external_sync_jobs", job_id))
        record_id = await _upsert_record(
            settings, settings["channel_table_id"], token,
            desired["channel"]["business_id"], "业务渠道 ID",
            desired["channel"]["fields"], channel_existing,
        )
        _save_sync_record("channel", desired["channel"]["business_id"],
                          record_id, desired["channel"]["fields"])
        for item in desired["models"]:
            model_record_id = await _upsert_record(
                settings, settings["model_table_id"], token, item["business_id"],
                "模型映射 ID", item["fields"], models_existing.get(item["business_id"]),
            )
            _save_sync_record("model", item["business_id"], model_record_id, item["fields"])
        now = time.time()
        store.update("external_sync_jobs", job_id, {
            "status": "success", "last_error": "", "finished_at": now,
            "updated_at": now,
        })
        launch = store.get("channel_launches", row["launch_id"])
        if launch:
            store.update("channel_launches", launch["id"], {
                "status": "synced", "updated_at": now,
            })
        channel = store.get("channels", row["channel_id"])
        if channel and channel["lifecycle_status"] == "online_verified":
            lifecycle.transition_channel(
                channel["id"], "synced", "飞书渠道表与模型映射表同步成功",
                {"id": None, "username": "system"},
            )
    except Exception as exc:
        delay = min(MAX_RETRY_SECONDS, 60 * 2 ** min(attempts - 1, 6))
        store.update("external_sync_jobs", job_id, {
            "status": "retry", "last_error": scrub(str(exc)),
            "next_attempt_at": time.time() + delay, "updated_at": time.time(),
        })
    return sync_job_out(store.get("external_sync_jobs", job_id))


async def start() -> None:
    global _worker
    if _worker and not _worker.done():
        return
    _worker = asyncio.create_task(_outbox_loop(), name="external-sync-outbox")


async def stop() -> None:
    global _worker
    if not _worker:
        return
    _worker.cancel()
    await asyncio.gather(_worker, return_exceptions=True)
    _worker = None


async def _outbox_loop() -> None:
    while True:
        try:
            rows = store.query(
                "SELECT id FROM external_sync_jobs WHERE status IN ('confirmed','retry') "
                "AND next_attempt_at<=? ORDER BY next_attempt_at,id LIMIT 10", (time.time(),)
            )
            for row in rows:
                await execute_sync(row["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(OUTBOX_TICK_SECONDS)
