"""监测告警接入、受控证据收集与保守异常归因。"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import httpx

from . import egress, metric_store, store
from .security import decrypt, encrypt, mask, scrub

SIGNATURE_TOLERANCE_SECONDS = 300
MERGE_WINDOW_SECONDS = 10 * 60
EVIDENCE_WINDOW_SECONDS = 5 * 60
ROUTE_RETENTION_SECONDS = 30 * 86400
ATTRIBUTION_NAMES = {
    "upstream_global": "上游整体异常",
    "single_channel": "单个渠道异常",
    "production_network": "生产网络异常",
    "relay_internal": "中转站自身异常",
    "user_quota": "用户级限额",
    "user_safety": "用户安全限制",
    "permission": "API Key 或模型权限问题",
    "upstream_rate_limit": "上游 429 限流",
    "user_network": "用户侧网络不稳",
    "insufficient": "证据不足",
}


class IncidentError(ValueError):
    pass


def source_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "enabled": bool(row["enabled"]),
        "secret_masked": mask(decrypt(row["secret_enc"])),
        "last_alert_at": row["last_alert_at"], "last_error": row["last_error"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "webhook_path": f"/api/incidents/webhook/{row['id']}",
    }


def create_source(name: str, secret: str, enabled: bool) -> dict[str, Any]:
    now = time.time()
    source_id = store.insert("monitor_sources", {
        "name": name.strip(), "secret_enc": encrypt(secret),
        "enabled": 1 if enabled else 0, "created_at": now, "updated_at": now,
    })
    row = store.get("monitor_sources", source_id)
    assert row is not None
    return source_out(row)


def verify_signature(source: dict[str, Any], timestamp: str, signature: str, body: bytes) -> None:
    if not source["enabled"]:
        raise IncidentError("监测告警源已停用")
    try:
        timestamp_value = int(timestamp)
    except ValueError as exc:
        raise IncidentError("签名时间戳无效") from exc
    if abs(time.time() - timestamp_value) > SIGNATURE_TOLERANCE_SECONDS:
        raise IncidentError("签名已过期")
    secret = decrypt(source["secret_enc"]).encode("utf-8")
    expected = hmac.new(
        secret, timestamp.encode("ascii") + b"." + body, hashlib.sha256
    ).hexdigest()
    supplied = signature.removeprefix("sha256=").strip().lower()
    if not hmac.compare_digest(expected, supplied):
        raise IncidentError("签名校验失败")


def ingest_alert(source_id: int, alert: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    route_chains = _validated_route_chains(alert.get("metadata", {}))
    duplicate = store.query(
        "SELECT incident_id FROM incident_alerts WHERE source_id=? AND event_id=?",
        (source_id, alert["event_id"]),
    )
    if duplicate:
        incident = store.get("incidents", duplicate[0]["incident_id"])
        assert incident is not None
        return incident_out(incident), True
    now = time.time()
    matching = store.query(
        "SELECT * FROM incidents WHERE source_id=? AND channel=? AND model=? "
        "AND last_seen_at>=? AND status IN ('collecting','open','needs_review') "
        "ORDER BY last_seen_at DESC LIMIT 1",
        (source_id, alert["channel"], alert.get("model", ""),
         alert["event_time"] - MERGE_WINDOW_SECONDS),
    )
    if matching:
        incident_id = matching[0]["id"]
        store.update("incidents", incident_id, {
            "last_seen_at": max(matching[0]["last_seen_at"], alert["event_time"]),
            "alert_count": matching[0]["alert_count"] + 1,
            "affected_users": max(matching[0]["affected_users"], alert["affected_users"]),
            "severity": alert["severity"] if alert["severity"] == "critical"
            else matching[0]["severity"],
            "status": "collecting", "updated_at": now,
        })
    else:
        incident_id = store.insert("incidents", {
            "source_id": source_id, "channel": alert["channel"],
            "model": alert.get("model", ""),
            "platform_group": alert.get("platform_group", ""),
            "severity": alert["severity"], "status": "collecting",
            "first_seen_at": alert["event_time"], "last_seen_at": alert["event_time"],
            "alert_count": 1, "affected_users": alert["affected_users"],
            "attribution_json": "{}", "created_at": now, "updated_at": now,
        })
    metadata_without_routes = {
        key: value for key, value in alert.get("metadata", {}).items()
        if key != "route_attempts"
    }
    store.insert("incident_alerts", {
        "incident_id": incident_id, "source_id": source_id,
        "event_id": alert["event_id"], "event_time": alert["event_time"],
        "symptom": alert["symptom"], "severity": alert["severity"],
        "affected_users": alert["affected_users"],
        "metadata_json": store.dumps(_safe_metadata(metadata_without_routes)),
        "received_at": now,
    })
    _save_route_chains(incident_id, route_chains, now)
    store.update("monitor_sources", source_id, {
        "last_alert_at": now, "last_error": "", "updated_at": now,
    })
    incident = store.get("incidents", incident_id)
    assert incident is not None
    return incident_out(incident), False


def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    forbidden = {"prompt", "messages", "content", "response", "api_key", "key",
                 "authorization", "raw_user_id", "user_id"}

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()
                    if key.casefold() not in forbidden}
        if isinstance(value, list):
            return [clean(item) for item in value[:100]]
        return scrub(value) if isinstance(value, str) else value

    return clean(metadata)


def _validated_route_chains(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    attempts = metadata.get("route_attempts") or []
    if not isinstance(attempts, list) or len(attempts) > 500:
        raise IncidentError("匿名路由尝试链必须是最多 500 条的数组")
    forbidden = {"prompt", "messages", "content", "response", "api_key", "key",
                 "authorization", "raw_user_id", "user_id", "token"}

    def contains_forbidden(value: Any) -> bool:
        if isinstance(value, dict):
            return any(str(key).casefold() in forbidden or contains_forbidden(child)
                       for key, child in value.items())
        if isinstance(value, list):
            return any(contains_forbidden(item) for item in value)
        return False

    output = []
    for item in attempts:
        if (not isinstance(item, dict) or not item.get("chain_id")
                or not isinstance(item.get("happened_at"), (int, float))
                or contains_forbidden(item)):
            raise IncidentError("匿名路由尝试链字段无效或包含敏感内容")
        user_hash = str(item.get("anonymous_user_hash") or "")
        if user_hash and not (user_hash.startswith("h1:") and len(user_hash) == 67):
            raise IncidentError("匿名用户指纹格式无效")
        safe = _safe_metadata(item)
        if isinstance(safe.get("attempts"), list):
            safe["attempts"] = safe["attempts"][:50]
        output.append(safe)
    return output


def _save_route_chains(
    incident_id: int, route_chains: list[dict[str, Any]], now: float,
) -> None:
    store.execute("DELETE FROM incident_route_chains WHERE expires_at<?", (now,))
    for item in route_chains:
        store.execute(
            "INSERT INTO incident_route_chains "
            "(incident_id,chain_id,happened_at,anonymous_user_hash,detail_json,created_at,expires_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(incident_id,chain_id) DO UPDATE SET "
            "detail_json=excluded.detail_json,expires_at=excluded.expires_at",
            (incident_id, str(item["chain_id"]), float(item["happened_at"]),
             str(item.get("anonymous_user_hash") or ""), store.dumps(item), now,
             now + ROUTE_RETENTION_SECONDS),
        )


def _add_evidence(
    incident_id: int, evidence_type: str, source: str, summary: str,
    detail: dict[str, Any], *, supports: str = "", contradicts: str = "",
) -> None:
    store.insert("incident_evidence", {
        "incident_id": incident_id, "evidence_type": evidence_type,
        "source": source, "supports": supports, "contradicts": contradicts,
        "summary": scrub(summary), "detail_json": store.dumps(detail),
        "collected_at": time.time(),
    })


def _collect_metrics(incident: dict[str, Any]) -> None:
    start = int(incident["first_seen_at"] - EVIDENCE_WINDOW_SECONDS)
    end = int(incident["last_seen_at"] + EVIDENCE_WINDOW_SECONDS)
    clauses = ["bucket_start>=?", "bucket_start<=?"]
    params: list[Any] = [start, end]
    for field in ("platform_group", "model", "channel"):
        if incident[field]:
            clauses.append(f"{field}=?")
            params.append(incident[field])
    rows = metric_store.query(
        "SELECT * FROM metric_buckets WHERE " + " AND ".join(clauses), tuple(params)
    )
    counts = {
        field: sum(int(row.get(field) or 0) for row in rows)
        for field in (
            "request_count", "attempt_count", "success_count", "auth_error_count",
            "rate_limit_count", "timeout_count", "stream_break_count",
            "upstream_5xx_count", "network_error_count", "protocol_error_count",
        )
    }
    counts["active_users"] = max(
        (int(row.get("active_users") or 0) for row in rows), default=0
    )
    counts["bucket_count"] = len(rows)
    attempts = counts["attempt_count"]
    counts["success_rate"] = round(counts["success_count"] / attempts, 6) if attempts else None
    summary = "告警窗口没有分钟指标" if not rows else (
        f"告警窗口 {attempts} 次尝试，成功率 "
        f"{counts['success_rate']:.1%}" if counts["success_rate"] is not None
        else f"告警窗口 {attempts} 次尝试"
    )
    _add_evidence(incident["id"], "minute_metrics", "只读生产指标", summary, counts)


def _collect_changes(incident: dict[str, Any]) -> None:
    rows = store.query(
        "SELECT action,object_type,object_id,result,created_at FROM audit_events "
        "WHERE created_at>=? AND created_at<=? ORDER BY created_at",
        (incident["first_seen_at"] - 1800, incident["last_seen_at"] + 300),
    )
    _add_evidence(
        incident["id"], "configuration_changes", "测试平台审计",
        f"告警前后发现 {len(rows)} 条平台配置操作",
        {"changes": rows[:100], "truncated": len(rows) > 100},
    )
    routes = store.query(
        "SELECT happened_at,anonymous_user_hash,detail_json FROM incident_route_chains "
        "WHERE incident_id=? AND expires_at>=? ORDER BY happened_at LIMIT 500",
        (incident["id"], time.time()),
    )
    affected_hashes = len({row["anonymous_user_hash"] for row in routes
                           if row["anonymous_user_hash"]})
    _add_evidence(
        incident["id"], "route_chain", "已验签监测告警",
        f"告警携带 {len(routes)} 条匿名路由尝试链，涉及 {affected_hashes} 个匿名用户"
        if routes else "告警未携带匿名路由尝试链，相关归因降低置信度",
        {"available": bool(routes), "chain_count": len(routes),
         "anonymous_user_count": affected_hashes},
        contradicts="" if routes else "high_confidence",
    )


def location_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"],
        "location_type": row["location_type"], "endpoint": row["endpoint"],
        "token_masked": mask(decrypt(row["token_enc"])) if row["token_enc"] else "",
        "enabled": bool(row["enabled"]),
        "max_requests_per_hour": row["max_requests_per_hour"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def create_location(body: dict[str, Any]) -> dict[str, Any]:
    endpoint = egress.validate_url(body["endpoint"])
    now = time.time()
    location_id = store.insert("incident_probe_locations", {
        "name": body["name"].strip(), "location_type": body["location_type"],
        "endpoint": endpoint, "token_enc": encrypt(body.get("token", ""))
        if body.get("token") else "", "enabled": 1 if body.get("enabled", True) else 0,
        "max_requests_per_hour": body.get("max_requests_per_hour", 12),
        "created_at": now, "updated_at": now,
    })
    row = store.get("incident_probe_locations", location_id)
    assert row is not None
    return location_out(row)


async def _run_probe(incident: dict[str, Any], location: dict[str, Any]) -> None:
    recent = store.query(
        "SELECT COUNT(*) n FROM probe_runs WHERE location_id=? AND started_at>=?",
        (location["id"], time.time() - 3600),
    )[0]["n"]
    if recent >= location["max_requests_per_hour"]:
        _add_evidence(
            incident["id"], "probe", location["name"],
            "探针达到每小时频率上限，未执行", {"status": "rate_limited"},
        )
        return
    started = time.time()
    run_id = store.insert("probe_runs", {
        "incident_id": incident["id"], "location_id": location["id"],
        "status": "running", "result_json": "{}", "error": "",
        "started_at": started,
    })
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if location["token_enc"]:
        headers["Authorization"] = f"Bearer {decrypt(location['token_enc'])}"
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True,
            max_redirects=egress.EGRESS_MAX_REDIRECTS,
            event_hooks=egress.event_hooks(),
        ) as client:
            response = await client.post(location["endpoint"], headers=headers, json={
                "incident_id": incident["id"], "channel": incident["channel"],
                "model": incident["model"], "platform_group": incident["platform_group"],
                "probe_profile": "lightweight-dedicated-account-v1",
            })
            response.raise_for_status()
            egress.ensure_response_size(response)
            payload = response.json()
        success = bool(payload.get("success"))
        latency = payload.get("latency_ms")
        safe = _safe_metadata(payload)
        store.update("probe_runs", run_id, {
            "status": "success", "success": 1 if success else 0,
            "latency_ms": latency, "result_json": store.dumps(safe),
            "finished_at": time.time(),
        })
        _add_evidence(
            incident["id"], "probe", location["name"],
            f"{location['location_type']} 位置轻量探针{'成功' if success else '失败'}",
            {"location_type": location["location_type"], **safe},
        )
    except Exception as exc:
        error = scrub(str(exc))
        store.update("probe_runs", run_id, {
            "status": "failed", "success": 0, "error": error,
            "finished_at": time.time(),
        })
        _add_evidence(
            incident["id"], "probe", location["name"],
            f"{location['location_type']} 位置探针离线或调用失败，归因置信度降低",
            {"location_type": location["location_type"], "error": error},
        )


async def collect_evidence(incident_id: int) -> None:
    incident = store.get("incidents", incident_id)
    if not incident:
        return
    _collect_metrics(incident)
    _collect_changes(incident)
    for location in store.query(
        "SELECT * FROM incident_probe_locations WHERE enabled=1 ORDER BY location_type,id"
    ):
        await _run_probe(incident, location)
    classify(incident_id)


def classify(incident_id: int) -> dict[str, Any]:
    incident = store.get("incidents", incident_id)
    if not incident:
        raise IncidentError("异常事件不存在")
    alerts = store.query(
        "SELECT * FROM incident_alerts WHERE incident_id=? ORDER BY event_time", (incident_id,)
    )
    evidence = evidence_rows(incident_id)
    metrics_row = next((row for row in reversed(evidence)
                        if row["evidence_type"] == "minute_metrics"), None)
    metrics = metrics_row["detail"] if metrics_row else {}
    symptoms = " ".join(row["symptom"].casefold() for row in alerts)
    metadata_text = " ".join(row["metadata_json"].casefold() for row in alerts)
    probes = [row for row in evidence if row["evidence_type"] == "probe"]
    production = next((row for row in probes
                       if row["detail"].get("location_type") == "production"), None)
    independent = next((row for row in probes
                        if row["detail"].get("location_type") == "independent"), None)
    production_ok = production and production["detail"].get("success") is True
    independent_ok = independent and independent["detail"].get("success") is True

    cause = "insufficient"
    confidence = .35
    support: list[str] = []
    contradictions: list[str] = []
    action = "保留现状并由人工结合监测平台证据复核"
    if metrics.get("auth_error_count", 0) > 0 or any(
        word in symptoms for word in ("401", "403", "permission", "auth")
    ):
        cause, confidence = "permission", .82
        support.append("告警窗口出现鉴权或权限错误")
        action = "核对专用测试账号、API Key 与模型权限；不要自动停用渠道"
    elif metrics.get("rate_limit_count", 0) > 0 or "429" in symptoms:
        cause, confidence = "upstream_rate_limit", .82
        support.append("告警窗口出现 429 限流")
        action = "核对上游额度与限流窗口，必要时人工降载或联系供应商"
    elif any(word in metadata_text + symptoms for word in ("safety", "安全限制")):
        cause, confidence = "user_safety", .76
        support.append("告警明确指向用户安全限制")
        action = "核对该匿名用户范围的安全策略，不自动封禁用户"
    elif any(word in metadata_text + symptoms for word in ("quota", "余额", "限额")):
        cause, confidence = "user_quota", .76
        support.append("告警明确指向用户级配额")
        action = "核对匿名用户额度与令牌配置"
    elif production and independent and not production_ok and independent_ok:
        cause, confidence = "production_network", .86
        support.append("生产位置失败而独立位置成功")
        action = "检查生产出口、DNS、TLS 和代理链路"
    elif production and independent and not production_ok and not independent_ok:
        cause, confidence = "upstream_global", .78
        support.append("两个独立位置探针均失败")
        action = "联系上游并观察其他渠道；在人工确认前不自动改变路由"
    elif independent_ok and (metrics.get("success_rate") or 0) < .8:
        cause, confidence = "single_channel", .72
        support.append("生产指标异常但独立探针当前成功，可能是间歇性单渠道问题")
        contradictions.append("独立探针当前成功，无法证明上游持续异常")
        action = "人工复测该渠道并对比同模型其他渠道"
    elif metrics.get("network_error_count", 0) and incident["affected_users"] <= 1:
        cause, confidence = "user_network", .58
        support.append("影响范围很小且网络错误占主导")
        action = "让受影响用户提供本地网络时序，并对比专用探针"
    if not production or not independent:
        contradictions.append("双位置探针证据不完整，置信度已受限")
        confidence = min(confidence, .68)
    if not metrics.get("bucket_count"):
        contradictions.append("告警窗口缺少分钟指标")
        confidence = min(confidence, .55)
    attribution = {
        "cause": cause, "cause_name": ATTRIBUTION_NAMES[cause],
        "confidence": round(confidence, 2), "supporting_evidence": support,
        "contradictions": contradictions,
        "affected_scope": {
            "channel": incident["channel"], "model": incident["model"],
            "platform_group": incident["platform_group"],
            "affected_users": incident["affected_users"],
        },
        "recommended_action": action,
        "needs_human_review": confidence < .9,
        "automation_boundary": "不会自动停用渠道、改变路由权重或封禁用户。",
        "generated_at": time.time(),
    }
    store.update("incidents", incident_id, {
        "status": "needs_review", "attribution_json": store.dumps(attribution),
        "updated_at": time.time(),
    })
    return attribution


def evidence_rows(incident_id: int) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT * FROM incident_evidence WHERE incident_id=? ORDER BY collected_at,id",
        (incident_id,),
    )
    return [{**row, "detail": store.loads(row["detail_json"], {})} for row in rows]


def incident_out(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "attribution": store.loads(row["attribution_json"], {})}


def incident_detail(incident_id: int) -> dict[str, Any]:
    row = store.get("incidents", incident_id)
    if not row:
        raise IncidentError("异常事件不存在")
    alerts = store.query(
        "SELECT id,event_id,event_time,symptom,severity,affected_users,received_at "
        "FROM incident_alerts WHERE incident_id=? ORDER BY event_time", (incident_id,)
    )
    probes = store.query(
        "SELECT runs.*,locations.name location_name,locations.location_type "
        "FROM probe_runs runs JOIN incident_probe_locations locations "
        "ON locations.id=runs.location_id WHERE runs.incident_id=? ORDER BY runs.started_at",
        (incident_id,),
    )
    return {**incident_out(row), "alerts": alerts,
            "evidence": evidence_rows(incident_id), "probe_runs": probes}
