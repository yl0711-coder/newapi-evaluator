"""FastAPI 入口。接口只保留三个入口真正需要的那些。"""
import asyncio
import math
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import (advice, auth, backups, channel_configurations, egress, export, external_sync, hardbank, incidents,
               insights, lifecycle, local_runners, metric_store, notifications,
               paired_access, paired_admission, paired_engine, paired_evidence,
               packs, runner, scheduled_configurations, scheduled_measurement, scheduler, specialty, store, workbench)
from .config import GROUP_RECYCLE_DAYS, HARD_IN_CAPABILITY, STABILITY_SCORE_LIMIT, WEB_DIR
from .models import (AdmissionBatchConclusionIn, AdmissionBatchContinueIn, AdmissionBatchIn,
                     BatchAdmissionIn, BenchmarkIn, BenchmarkPatch, BindBenchmarkIn,
                     ChannelConfigurationIn, ChannelImportIn, ChannelIn,
                     ChannelConfigurationPresentationIn,
                     BenchmarkCompareIn, ChannelModelIn, DecideIn, FamilyModelIn,
                     FamilyModelPatch, LifecycleTransitionIn, ModelAliasIn,
                     ModelAliasPatch, ModelFamilyIn, ModelFamilyPatch, PlatformBenchmarkIn,
                     PlatformGroupIn, PlatformGroupPatch, PlatformGroupTargetIn,
                     EmailRecipientIn, FeishuWebhookIn, GroupIn, GroupPatch, ImportPayload,
                     ChannelLaunchConfirmIn, FeishuBitableSettingsIn,
                     IncidentProbeLocationIn, MetricPayloadIn, MetricSourceIn,
                     MetricSourcePatch, MonitorAlertIn, MonitorSourceIn, RateGroupIn,
                     UsageProfileBindingIn,
                     OnlineVerificationSourceIn, RecommendationReviewIn,
                     RunnerHeartbeatIn, RunnerPairIn,
                     RunnerPairingCodeIn, RunnerResultIn,
                     ScheduledReportGroupIn, ScheduledReportGroupTargetsIn,
                     ScheduledConfigurationTestIn, ScheduledPrimaryModelsIn,
                     ScheduledAttributionIn, ScheduledBaselineRebuildIn,
                     ScheduledTargetIn, ScheduledTestIn, SetGoldenIn, SmtpSettingsIn,
                     TargetGroupIn, TargetIn, TaskIn, TaskMetadataIn, UpstreamMultiplierIn,
                     ConfigurationBusinessStatusIn)
from .models import (FidelityDecisionIn, FidelityTruthRevokeIn, PairedCancelIn,
                     PairedConclusionIn, PairedReportRevisionIn,
                     PairedRetentionExtensionIn, PairedRerunIn,
                     PairedStartIn, PairedTaskIn, RoleGrantIn, RoleRevokeIn)
from . import itembank, placement, protocol, test_catalog
from .security import anonymize_subject, decrypt, encrypt, mask
from .importer import parse_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    metric_store.init()
    migrated_metrics = metric_store.migrate_from_business_store()
    if migrated_metrics:
        store.record_system_alert(
            "metric-db-split", "migration", "生产指标已迁移到独立数据库",
            f"共迁移 {migrated_metrics} 条旧指标记录；旧表仅保留用于回滚。",
            "warning",
        )
    auth.ensure_bootstrap_account()
    paired_access.ensure_bootstrap_roles()
    specialty.ensure_versions()
    _purge_expired_deleted_groups()
    await runner.start()
    channel_configurations.resume_online_checks()
    await scheduler.start()
    await insights.start()
    await external_sync.start()
    await backups.start()
    yield
    await backups.stop()
    await external_sync.stop()
    await insights.stop()
    await scheduler.stop()
    await runner.stop()


app = FastAPI(title="模型渠道测试平台", version="1.0", lifespan=lifespan)
auth.install(app)
app.include_router(auth.router)


@app.get("/api/health")
def health() -> dict[str, Any]:
    alerts = store.query(
        "SELECT id,kind,severity,title,detail,occurrence_count,last_seen_at "
        "FROM system_alerts WHERE status='open' ORDER BY last_seen_at DESC LIMIT 20"
    )
    dead_letters = store.query(
        "SELECT COUNT(*) n FROM tasks WHERE dead_lettered_at IS NOT NULL"
    )[0]["n"]
    healthy = scheduler.is_running() and insights.is_running() and not any(
        row["severity"] == "critical" for row in alerts
    )
    return {"status": "ok" if healthy else "degraded",
            "scheduler_running": scheduler.is_running(),
            "metric_collector_running": insights.is_running(),
            "dead_letter_tasks": dead_letters, "alerts": alerts,
            "capacity": backups.capacity_status()}


@app.get("/api/system-alerts")
def system_alerts(status: str = "open") -> list[dict[str, Any]]:
    if status not in {"open", "resolved", "all"}:
        raise HTTPException(status_code=400, detail="status 只能是 open、resolved 或 all")
    where = "" if status == "all" else " WHERE status=?"
    params = () if status == "all" else (status,)
    return store.query(
        "SELECT * FROM system_alerts" + where + " ORDER BY last_seen_at DESC LIMIT 200",
        params,
    )


@app.post("/api/system-alerts/{alert_id}/resolve")
def resolve_system_alert(alert_id: int) -> dict[str, Any]:
    row = store.get("system_alerts", alert_id)
    if not row:
        raise HTTPException(status_code=404, detail="后台告警不存在")
    store.update("system_alerts", alert_id, {
        "status": "resolved", "resolved_at": time.time(),
    })
    return store.get("system_alerts", alert_id)  # type: ignore[return-value]


@app.get("/api/backups/status")
def backup_status() -> dict[str, Any]:
    return backups.status()


@app.post("/api/backups/run")
async def run_encrypted_backup() -> dict[str, Any]:
    return await asyncio.to_thread(backups.run_backup)


@app.post("/api/backups/restore-drill")
async def run_restore_drill() -> dict[str, Any]:
    try:
        return await asyncio.to_thread(backups.restore_drill)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------- 首页概览：三个入口各自的状态提示 ----------

@app.get("/api/summary")
def summary() -> dict[str, Any]:
    configured = store.query("SELECT COUNT(*) n FROM targets")[0]["n"]
    inspecting = store.query(
        "SELECT COUNT(*) n FROM scheduled_tests WHERE enabled=1")[0]["n"]
    running = store.query(
        "SELECT COUNT(*) n FROM tasks WHERE status IN ('queued','running')")[0]["n"]
    risky = store.query(
        "SELECT COUNT(*) n FROM targets WHERE "
        "status IN ('downgrade','reject','manual')")[0]["n"]
    return {
        "targets": configured,
        "scheduled_tests": inspecting,
        "running_tasks": running,
        "risky_targets": risky,
        "queue": runner.queue_size(),
        # 首页卡片副标题直接用这几句，前端不再自己拼状态
        "hints": {
            "admission": f"已配置 {configured} 个模型，可随时提交检测"
                         if configured else "还没有渠道，先去配一个",
            "inspect": f"已启用 {inspecting} 条定时测试计划"
                       if inspecting else "未启用",
            "degrade": f"可对 {configured} 个模型做基线对比"
                       if configured else "需要先配置模型",
            "load": f"可对 {configured} 个模型执行阶梯压测"
                    if configured else "需要先配置模型",
        },
    }


# ---------- 导入配置 ----------

@app.post("/api/import")
def import_config(payload: ImportPayload) -> dict[str, Any]:
    """尽量解析粘贴内容，缺失字段留空供用户补充。Key 不回显明文。"""
    try:
        parsed = parse_config(payload.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    parsed.pop("api_key", None)  # 明文不回传，前端保留在安全输入框里
    return parsed


# ---------- 测试目标 ----------

def _target_out(row: dict[str, Any]) -> dict[str, Any]:
    """对外输出，永不带 key_enc。"""
    return {
        "id": row["id"], "channel_id": row.get("channel_id"),
        "name": row["name"], "protocol": row["protocol"],
        "base_url": row["base_url"], "model": row["model"],
        "group_name": row["group_name"], "env": row["env"],
        "price_in": row["price_in"], "price_out": row["price_out"],
        "upstream_multiplier": row.get("upstream_multiplier"),
        "source": row["source"],
        "edited_fields": store.loads(row["edited_fields"], []),
        "recorded": bool(row["recorded"]),
        "status": row["status"], "last_verdict": row["last_verdict"],
        "last_task_id": row["last_task_id"],
        "has_baseline": bool(row["baseline"]),
        "benchmark_id": row["benchmark_id"],
        "group_id": row["group_id"],
        "platform_group_id": row.get("platform_group_id"),
        "pool": row["pool"] or "ungrouped",
        "key_masked": mask(_safe_key(row)),
        "updated_at": row["updated_at"],
    }


def _safe_key(row: dict[str, Any]) -> str:
    try:
        return decrypt(row["key_enc"]) if row["key_enc"] else ""
    except Exception:
        return ""


def _channel_out(row: dict[str, Any]) -> dict[str, Any]:
    models = store.query(
        "SELECT id,name,model,group_id,recorded,status,last_verdict,last_task_id,updated_at "
        "FROM targets WHERE channel_id=? AND archived_at IS NULL "
        "ORDER BY updated_at DESC", (row["id"],))
    return {
        "id": row["id"], "name": row["name"], "protocol": row["protocol"],
        "business_id": row.get("business_id") or f"channel-{row['id']}",
        "base_url": row["base_url"], "group_name": row["group_name"],
        "family_id": row.get("family_id"),
        "lifecycle_status": row["lifecycle_status"],
        "lifecycle_name": lifecycle.CHANNEL_STATES[row["lifecycle_status"]],
        "allowed_transitions": lifecycle.allowed_transitions(
            "channel", row["lifecycle_status"]),
        "env": row["env"], "source": row["source"],
        "edited_fields": store.loads(row["edited_fields"], []),
        "key_masked": mask(_safe_key(row)), "models": models,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


@app.get("/api/channels")
def list_channels() -> list[dict[str, Any]]:
    return [_channel_out(row) for row in
            store.query("SELECT * FROM channels WHERE lifecycle_status!='archived' "
                        "ORDER BY updated_at DESC")]


@app.get("/api/model-families")
def list_model_families() -> list[dict[str, Any]]:
    return workbench.list_families()


@app.post("/api/model-families")
def create_model_family(body: ModelFamilyIn) -> dict[str, Any]:
    name = body.name.strip()
    if store.query("SELECT id FROM model_families WHERE name=?", (name,)):
        raise HTTPException(status_code=409, detail="模型家族已经存在")
    now = time.time()
    family_id = store.insert("model_families", {
        "name": name, "created_at": now, "updated_at": now})
    family = workbench.family(family_id)
    assert family is not None
    return family


@app.patch("/api/model-families/{family_id}")
def patch_model_family(family_id: int, body: ModelFamilyPatch) -> dict[str, Any]:
    if not store.get("model_families", family_id):
        raise HTTPException(status_code=404, detail="模型家族不存在")
    name = body.name.strip()
    duplicate = store.query(
        "SELECT id FROM model_families WHERE name=? AND id!=?", (name, family_id))
    if duplicate:
        raise HTTPException(status_code=409, detail="模型家族名称已经存在")
    store.update("model_families", family_id, {"name": name, "updated_at": time.time()})
    family = workbench.family(family_id)
    assert family is not None
    return family


@app.delete("/api/model-families/{family_id}")
def archive_model_family(family_id: int) -> dict[str, Any]:
    family = store.get("model_families", family_id)
    if not family:
        raise HTTPException(status_code=404, detail="模型家族不存在")
    models = store.query(
        "SELECT COUNT(*) n FROM model_family_models WHERE family_id=?", (family_id,)
    )[0]["n"]
    groups = store.query(
        "SELECT COUNT(*) n FROM platform_groups WHERE family_id=?", (family_id,)
    )[0]["n"]
    if models or groups:
        store.update("model_families", family_id, {
            "archived_at": time.time(), "updated_at": time.time(),
        })
        return {"status": "archived"}
    store.delete("model_families", family_id)
    return {"status": "deleted"}


@app.post("/api/model-families/{family_id}/models")
def create_family_model(family_id: int, body: FamilyModelIn) -> dict[str, Any]:
    if not store.get("model_families", family_id):
        raise HTTPException(status_code=404, detail="模型家族不存在")
    try:
        model = lifecycle.canonical_model_id(body.model)
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if store.query(
        "SELECT id FROM model_family_models WHERE family_id=? AND model=?",
        (family_id, model),
    ):
        raise HTTPException(status_code=409, detail="该模型已在家族清单中")
    now = time.time()
    model_id = store.insert("model_family_models", {
        "family_id": family_id, "model": model,
        "display_name": body.display_name.strip(),
        "enabled": 1 if body.enabled else 0, "sort_order": body.sort_order,
        "lifecycle_status": "experimental",
        "created_at": now, "updated_at": now,
    })
    for group in store.query("SELECT id FROM platform_groups WHERE family_id=?", (family_id,)):
        workbench.ensure_platform_slots(group["id"], family_id)
    family = workbench.family(family_id)
    assert family is not None
    return next(item for item in family["models"] if item["id"] == model_id)


@app.patch("/api/model-families/{family_id}/models/{model_id}")
def patch_family_model(
    family_id: int, model_id: int, body: FamilyModelPatch,
) -> dict[str, Any]:
    row = store.get("model_family_models", model_id)
    if not row or row["family_id"] != family_id:
        raise HTTPException(status_code=404, detail="家族模型不存在")
    patch: dict[str, Any] = {"updated_at": time.time()}
    if body.display_name is not None:
        patch["display_name"] = body.display_name.strip()
    if body.enabled is not None:
        patch["enabled"] = 1 if body.enabled else 0
    if body.sort_order is not None:
        patch["sort_order"] = body.sort_order
    store.update("model_family_models", model_id, patch)
    family = workbench.family(family_id)
    assert family is not None
    return next(item for item in family["models"] if item["id"] == model_id)


@app.get("/api/lifecycle/catalog")
def lifecycle_catalog() -> dict[str, Any]:
    return lifecycle.state_catalog()


@app.get("/api/lifecycle/history")
def lifecycle_history(object_type: str, object_id: int) -> list[dict[str, Any]]:
    if object_type not in {"model", "channel"}:
        raise HTTPException(status_code=400, detail="对象类型必须是 model 或 channel")
    return lifecycle.history(object_type, object_id)


@app.post("/api/model-families/{family_id}/models/{model_id}/lifecycle")
def transition_family_model(
    family_id: int, model_id: int, body: LifecycleTransitionIn, request: Request,
) -> dict[str, Any]:
    model = store.get("model_family_models", model_id)
    if not model or model["family_id"] != family_id:
        raise HTTPException(status_code=404, detail="家族模型不存在")
    try:
        lifecycle.transition_model(
            model_id, body.to_status, body.reason, request.state.user,
            body.replacement_model_id,
        )
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    family = workbench.family(family_id)
    assert family is not None
    return next(item for item in family["models"] if item["id"] == model_id)


@app.get("/api/model-families/{family_id}/aliases")
def list_model_aliases(family_id: int) -> list[dict[str, Any]]:
    if not store.get("model_families", family_id):
        raise HTTPException(status_code=404, detail="模型家族不存在")
    return lifecycle.aliases(family_id)


@app.post("/api/model-families/{family_id}/aliases")
def create_model_alias(
    family_id: int, body: ModelAliasIn, request: Request,
) -> dict[str, Any]:
    model = store.get("model_family_models", body.model_id)
    if not model or model["family_id"] != family_id:
        raise HTTPException(status_code=404, detail="家族模型不存在")
    alias = body.alias.strip()
    if alias.casefold() == model["model"].casefold():
        raise HTTPException(status_code=400, detail="别名不能与规范模型 ID 相同")
    if store.query(
        "SELECT id FROM model_family_models WHERE family_id=? AND model=? COLLATE NOCASE",
        (family_id, alias),
    ):
        raise HTTPException(status_code=409, detail="别名与家族内规范模型 ID 冲突")
    now = time.time()
    try:
        alias_id = store.insert("model_aliases", {
            "family_id": family_id, "model_id": body.model_id, "alias": alias,
            "created_by": request.state.user["id"], "created_at": now,
            "updated_at": now,
        })
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=409, detail="该别名已经存在") from exc
        raise
    return next(item for item in lifecycle.aliases(family_id) if item["id"] == alias_id)


@app.patch("/api/model-families/{family_id}/aliases/{alias_id}")
def patch_model_alias(
    family_id: int, alias_id: int, body: ModelAliasPatch,
) -> dict[str, Any]:
    row = store.get("model_aliases", alias_id)
    if not row or row["family_id"] != family_id:
        raise HTTPException(status_code=404, detail="模型别名不存在")
    model_id = body.model_id if body.model_id is not None else row["model_id"]
    model = store.get("model_family_models", model_id)
    if not model or model["family_id"] != family_id:
        raise HTTPException(status_code=404, detail="家族模型不存在")
    alias = body.alias.strip() if body.alias is not None else row["alias"]
    if alias.casefold() == model["model"].casefold():
        raise HTTPException(status_code=400, detail="别名不能与规范模型 ID 相同")
    try:
        store.update("model_aliases", alias_id, {
            "model_id": model_id, "alias": alias, "updated_at": time.time(),
        })
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=409, detail="该别名已经存在") from exc
        raise
    return next(item for item in lifecycle.aliases(family_id) if item["id"] == alias_id)


@app.delete("/api/model-families/{family_id}/aliases/{alias_id}")
def delete_model_alias(family_id: int, alias_id: int) -> dict[str, str]:
    row = store.get("model_aliases", alias_id)
    if not row or row["family_id"] != family_id:
        raise HTTPException(status_code=404, detail="模型别名不存在")
    store.delete("model_aliases", alias_id)
    return {"status": "deleted"}


@app.get("/api/platform-groups")
def list_platform_groups() -> list[dict[str, Any]]:
    return workbench.list_platform_groups()


@app.post("/api/platform-groups")
def create_platform_group(body: PlatformGroupIn) -> dict[str, Any]:
    if not store.get("model_families", body.family_id):
        raise HTTPException(status_code=404, detail="模型家族不存在")
    existing = store.query(
        "SELECT id FROM platform_groups WHERE family_id=? "
        "AND ABS(multiplier-?)<0.000000001",
        (body.family_id, body.online_multiplier),
    )
    if existing:
        group = workbench.platform_group(existing[0]["id"])
        assert group is not None
        return group
    now = time.time()
    group_id = store.insert("platform_groups", {
        "family_id": body.family_id, "multiplier": body.online_multiplier,
        "created_at": now, "updated_at": now,
    })
    workbench.ensure_platform_slots(group_id, body.family_id)
    group = workbench.platform_group(group_id)
    assert group is not None
    return group


@app.patch("/api/platform-groups/{group_id}")
def patch_platform_group(group_id: int, body: PlatformGroupPatch) -> dict[str, Any]:
    row = store.get("platform_groups", group_id)
    if not row:
        raise HTTPException(status_code=404, detail="平台倍率组不存在")
    duplicate = store.query(
        "SELECT id FROM platform_groups WHERE family_id=? AND id!=? "
        "AND ABS(multiplier-?)<0.000000001",
        (row["family_id"], group_id, body.online_multiplier),
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="该模型家族的倍率组已经存在")
    store.update("platform_groups", group_id, {
        "multiplier": body.online_multiplier, "updated_at": time.time()})
    group = workbench.platform_group(group_id)
    assert group is not None
    return group


@app.delete("/api/platform-groups/{group_id}")
def archive_platform_group(group_id: int) -> dict[str, Any]:
    group = store.get("platform_groups", group_id)
    if not group:
        raise HTTPException(status_code=404, detail="平台倍率组不存在")
    targets = store.query(
        "SELECT COUNT(*) n FROM targets WHERE platform_group_id=?", (group_id,)
    )[0]["n"]
    benchmarks = store.query(
        "SELECT COUNT(*) n FROM platform_group_benchmarks "
        "WHERE platform_group_id=? AND benchmark_id IS NOT NULL", (group_id,)
    )[0]["n"]
    if targets or benchmarks:
        store.update("platform_groups", group_id, {
            "archived_at": time.time(), "updated_at": time.time(),
        })
        return {"status": "archived"}
    store.delete("platform_groups", group_id)
    return {"status": "deleted"}


@app.put("/api/targets/{target_id}/platform-group")
def set_target_platform_group(
    target_id: int, body: PlatformGroupTargetIn,
) -> dict[str, Any]:
    target = store.get("targets", target_id)
    if not target:
        raise HTTPException(status_code=404, detail="模型不存在")
    if body.platform_group_id is not None:
        group = store.get("platform_groups", body.platform_group_id)
        if not group:
            raise HTTPException(status_code=404, detail="平台倍率组不存在")
        allowed = store.query(
            "SELECT id FROM model_family_models WHERE family_id=? AND model=?",
            (group["family_id"], target["model"]),
        )
        if not allowed:
            raise HTTPException(status_code=400, detail="目标组的模型家族不包含该模型")
    store.update("targets", target_id, {
        "platform_group_id": body.platform_group_id, "updated_at": time.time()})
    updated = store.get("targets", target_id)
    assert updated is not None
    return _target_out(updated)


def _workspace_task(row: dict[str, Any]) -> dict[str, Any]:
    report = store.task_report(row, None)
    capability = ((report or {}).get("metrics") or {}).get("capability") or {}
    overall = capability.get("overall")
    conclusion = (report or {}).get("conclusion") or {}
    return {
        "id": row["id"], "kind": row["kind"], "status": row["status"],
        "verdict": conclusion.get("verdict"), "verdict_code": conclusion.get("code"),
        "score": round(overall * 100) if overall is not None else None,
        "created_at": row["created_at"], "finished_at": row["finished_at"],
    }


def _channel_name_key(name: str) -> str:
    return " ".join(name.strip().split()).casefold()


@app.get("/api/workspace")
def workspace() -> dict[str, Any]:
    """按平台倍率、渠道上游倍率和实际模型返回工作台。"""
    channels = {row["id"]: row for row in
                store.query("SELECT * FROM channels WHERE lifecycle_status!='archived' "
                            "ORDER BY updated_at DESC")}
    targets = store.query(
        "SELECT * FROM targets WHERE archived_at IS NULL ORDER BY updated_at DESC")
    tasks = store.query(
        "SELECT * FROM (SELECT tasks.*,ROW_NUMBER() OVER ("
        "PARTITION BY target_id ORDER BY id DESC) recent_rank FROM tasks "
        "WHERE target_id IS NOT NULL) WHERE recent_rank<=20 ORDER BY id DESC")
    report_counts = {
        int(row["target_id"]): int(row["n"])
        for row in store.query(
            "SELECT target_id,COUNT(*) n FROM tasks "
            "WHERE target_id IS NOT NULL AND report<>'' GROUP BY target_id")
    }
    tasks_by_target: dict[int, list[dict[str, Any]]] = {}
    for task in tasks:
        if task["target_id"] is not None:
            tasks_by_target.setdefault(int(task["target_id"]), []).append(task)
    scheduled_target_ids = {int(row["target_id"]) for row in
                            store.query("SELECT target_id FROM scheduled_test_targets")}
    platform_groups = workbench.list_platform_groups()
    group_by_id = {group["id"]: group for group in platform_groups}
    buckets: dict[int | None, dict[tuple[str, str], dict[str, Any]]] = {
        group["id"]: {} for group in platform_groups}
    buckets[None] = {}
    for target in targets:
        channel = channels.get(int(target["channel_id"]))
        if not channel:
            continue
        recent = [_workspace_task(task) for task in
                  tasks_by_target.get(int(target["id"]), [])[:20]]
        scored = [task["score"] for task in recent if task["score"] is not None]
        trend = scored[0] - scored[1] if len(scored) > 1 else None
        benchmark_row = workbench.benchmark_for_target(target)
        benchmark = workbench.benchmark_out(benchmark_row)
        if benchmark:
            benchmark["channel_name"] = workbench.source_channel(
                benchmark.get("source_task_id"))
        model = {
            "id": target["id"], "name": target["name"], "model": target["model"],
            "platform_group_id": target["platform_group_id"],
            "upstream_multiplier": target["upstream_multiplier"],
            "status": target["status"], "last_verdict": target["last_verdict"],
            "last_task_id": target["last_task_id"],
            "scheduled_test_enabled": int(target["id"]) in scheduled_target_ids,
            "score": scored[0] if scored else None, "trend": trend,
            "benchmark": benchmark,
            "report_count": report_counts.get(int(target["id"]), 0),
            "reports": recent, "updated_at": target["updated_at"],
            "connection": {
                "id": channel["id"], "protocol": channel["protocol"],
                "base_url": channel["base_url"],
                "key_masked": mask(_safe_key(channel)),
            },
        }
        group_id = target["platform_group_id"]
        rate_key = "unset" if target["upstream_multiplier"] is None \
            else format(float(target["upstream_multiplier"]), "g")
        key = (_channel_name_key(channel["name"]), rate_key)
        bucket = buckets.setdefault(group_id, {})
        if key not in bucket:
            bucket[key] = {
                "channel_name": channel["name"],
                "lifecycle_status": channel["lifecycle_status"],
                "lifecycle_name": lifecycle.CHANNEL_STATES[channel["lifecycle_status"]],
                "allowed_transitions": lifecycle.allowed_transitions(
                    "channel", channel["lifecycle_status"]),
                "upstream_multiplier": target["upstream_multiplier"],
                "channel_ids": [], "connections": [], "models": [],
            }
        channel_group = bucket[key]
        if channel["id"] not in channel_group["channel_ids"]:
            channel_group["channel_ids"].append(channel["id"])
            channel_group["connections"].append(model["connection"])
        channel_group["models"].append(model)

    output_groups = []
    for group in platform_groups:
        channel_groups = list(buckets[group["id"]].values())
        roster = {model["model"] for model in group["models"] if model["enabled"]}
        for channel_group in channel_groups:
            present = {model["model"] for model in channel_group["models"]}
            channel_group["pending_models"] = sorted(roster - present)
        output_groups.append({**group, "channel_groups": channel_groups})
    unassigned = list(buckets[None].values())
    return {
        "platform_groups": output_groups,
        "unassigned": unassigned,
        "families": workbench.list_families(),
        "stats": {
            "channels": len({_channel_name_key(row["name"]) for row in channels.values()}),
            "models": len(targets),
            "benchmarks": sum(1 for group in platform_groups
                              for model in group["models"] if model["benchmark"]),
            "attention": sum(1 for target in targets if target["status"] in
                             ("observe", "manual", "downgrade", "reject")),
        },
    }


@app.post("/api/channels")
def create_channel(body: ChannelIn) -> dict[str, Any]:
    base_url = body.base_url.rstrip("/")
    try:
        egress.validate_url(base_url)
    except egress.EgressDenied as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    duplicate = [row for row in store.query(
        "SELECT * FROM channels WHERE protocol=? AND base_url=? "
        "AND lifecycle_status!='archived'",
        (body.protocol, base_url),
    ) if _safe_key(row) == body.api_key]
    if duplicate:
        return _channel_out(duplicate[0])
    now = time.time()
    channel_id = store.insert("channels", {
        "name": body.name, "protocol": body.protocol, "base_url": base_url,
        "group_name": body.group_name, "env": body.env,
        "key_enc": encrypt(body.api_key), "source": body.source,
        "edited_fields": store.dumps(body.edited_fields),
        "created_at": now, "updated_at": now,
    })
    store.update("channels", channel_id, {"business_id": f"channel-{channel_id}"})
    row = store.get("channels", channel_id)
    assert row is not None
    return _channel_out(row)


@app.post("/api/channels/import")
def create_imported_channel(body: ChannelImportIn) -> dict[str, Any]:
    try:
        parsed = parse_config(body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    values = {
        "name": body.name or parsed["name"],
        "base_url": body.base_url or parsed["base_url"],
        "api_key": body.api_key or parsed["api_key"],
        "protocol": body.protocol or parsed["protocol"],
    }
    missing = [label for field, label in (
        ("name", "渠道名称"), ("base_url", "上游地址"), ("api_key", "API Key"),
    ) if not values[field]]
    if missing:
        raise HTTPException(status_code=400, detail=f"粘贴内容还缺：{'、'.join(missing)}")
    return create_channel(ChannelIn(
        **values, source="import",
        edited_fields=[field for field in ("name", "base_url", "api_key", "protocol")
                       if getattr(body, field) is not None],
    ))


@app.post("/api/channels/{channel_id}/lifecycle")
def transition_channel_lifecycle(
    channel_id: int, body: LifecycleTransitionIn, request: Request,
) -> dict[str, Any]:
    try:
        row = lifecycle.transition_channel(
            channel_id, body.to_status, body.reason, request.state.user
        )
    except lifecycle.LifecycleError as exc:
        status_code = 404 if str(exc) == "对象不存在" else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return _channel_out(row)


def _create_channel_target(channel: dict[str, Any], name: str, model: str,
                           price_in: float | None, price_out: float | None,
                           group_id: int | None = None,
                           upstream_multiplier: float | None = None,
                           platform_group_id: int | None = None) -> dict[str, Any]:
    if group_id is not None and not store.get("groups", group_id):
        raise HTTPException(status_code=404, detail="模型分组不存在")
    duplicate = store.query(
        "SELECT * FROM targets WHERE channel_id=? AND model=?", (channel["id"], model))
    if duplicate:
        target = duplicate[0]
        patch = {
            "group_id": group_id,
            "pool": "grouped" if group_id is not None else "ungrouped",
            "price_in": price_in, "price_out": price_out,
            "platform_group_id": platform_group_id,
            "updated_at": time.time(),
        }
        if upstream_multiplier is not None:
            patch["upstream_multiplier"] = upstream_multiplier
        store.update("targets", target["id"], patch)
        updated = store.get("targets", target["id"])
        assert updated is not None
        return updated
    now = time.time()
    target_id = store.insert("targets", {
        "channel_id": channel["id"], "name": name,
        "protocol": channel["protocol"], "base_url": channel["base_url"],
        "model": model, "group_name": channel["group_name"],
        "env": channel["env"], "key_enc": channel["key_enc"],
        "price_in": price_in, "price_out": price_out,
        "upstream_multiplier": upstream_multiplier,
        "platform_group_id": platform_group_id,
        "group_id": group_id,
        "pool": "grouped" if group_id is not None else "ungrouped",
        "source": channel["source"], "edited_fields": channel["edited_fields"],
        "recorded": 1, "status": "pending",
        "created_at": now, "updated_at": now,
    })
    row = store.get("targets", target_id)
    assert row is not None
    return row


@app.get("/api/specialty-packs")
def list_specialty_packs() -> list[dict[str, Any]]:
    return specialty.catalog()


@app.post("/api/channels/{channel_id}/discover-models")
async def discover_channel_models(
    channel_id: int, platform_group_id: int | None = None,
) -> dict[str, Any]:
    channel = store.get("channels", channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="渠道不存在")
    key = _safe_key(channel)
    url = protocol.models_url(channel["protocol"], channel["base_url"])
    try:
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True,
            max_redirects=egress.EGRESS_MAX_REDIRECTS,
            event_hooks=egress.event_hooks(),
        ) as client:
            response = await client.get(
                url, headers=protocol.headers(channel["protocol"], key))
        if response.status_code in (401, 403):
            raise HTTPException(status_code=400, detail="Key 被上游拒绝，请检查权限")
        response.raise_for_status()
        egress.ensure_response_size(response)
        discovered = sorted(set(protocol.list_models(
            channel["protocol"], response.json())))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="无法读取上游模型清单") from exc
    family_id = channel.get("family_id")
    if platform_group_id:
        platform_group = store.get("platform_groups", platform_group_id)
        if not platform_group:
            raise HTTPException(status_code=404, detail="平台倍率组不存在")
        family_id = platform_group["family_id"]
    matching = lifecycle.match_models(family_id, discovered) if family_id else {
        "expected": [], "matched": [], "missing": [], "fuzzy_candidates": [],
        "unmapped": discovered,
        "identity_notice": "名称与别名只用于映射，不证明真实上游身份。",
    }
    return {"discovered": discovered, **matching}


@app.get("/api/admission-plan-preview")
def admission_plan_preview(
    platform_group_id: int, price_in: float = 2.0, price_out: float = 8.0,
) -> dict[str, Any]:
    platform = workbench.platform_group(platform_group_id)
    if not platform:
        raise HTTPException(status_code=404, detail="平台倍率组不存在")
    models = [model for model in platform["models"] if model["enabled"]]
    base = packs.estimate("admission", price_in, price_out, include_hard=False)
    per_model_cost = base["cost"]
    requests_per_model = base["requests"]
    requests = requests_per_model * len(models)
    return {
        "platform_group": {"id": platform["id"], "label": platform["label"]},
        "models": models, "recommended_specialties": [],
        "selected_specialties": [], "specialty_catalog": [],
        "estimate": {
            "model_count": len(models), "requests": requests,
            "tokens": base["tokens"] * len(models),
            "cost": round(per_model_cost * len(models), 6),
            "concurrency": base["concurrency"],
            "estimated_minutes": max(1, math.ceil(requests * 20 / 60)),
            "base_requests_per_model": base["requests"],
            "automatic_requests_per_model": 0,
            "max_requests_per_model": base["requests"],
            "specialty_requests_per_model": 0,
            "base_version": base["version"], "specialty_versions": {},
            "automatic_versions": {},
        },
    }


@app.get("/api/test-plan-snapshots/{plan_id}")
def get_test_plan_snapshot(plan_id: int) -> dict[str, Any]:
    row = store.get("test_plan_snapshots", plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="测试计划快照不存在")
    models = store.loads(row.pop("models_json"), [])
    selected = store.loads(row.pop("selected_specialties_json"), [])
    recommended = store.loads(row.pop("recommended_specialties_json"), [])
    estimate = store.loads(row.pop("estimate_json"), {})
    return {**row, "models": models, "selected_specialties": selected,
            "recommended_specialties": recommended, "estimate": estimate}


@app.post("/api/channels/{channel_id}/models")
def create_channel_model(channel_id: int, body: ChannelModelIn) -> dict[str, Any]:
    channel = store.get("channels", channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="渠道不存在")
    group = store.get("groups", body.group_id) if body.group_id else None
    return _target_out(_create_channel_target(
        channel, body.name, body.model, body.price_in, body.price_out, body.group_id,
        (group or {}).get("multiplier")))


@app.post("/api/channels/{channel_id}/admission-tasks")
async def create_batch_admission(
    channel_id: int, body: BatchAdmissionIn, request: Request,
) -> dict[str, Any]:
    channel = store.get("channels", channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="渠道不存在")
    platform_group = store.get("platform_groups", body.platform_group_id)
    if not platform_group:
        raise HTTPException(status_code=404, detail="平台倍率组不存在")
    if channel.get("family_id") not in (None, platform_group["family_id"]):
        current_family = store.get("model_families", channel["family_id"])
        raise HTTPException(
            status_code=409,
            detail=f"这个 Key 已用于{(current_family or {}).get('name', '其他')}家族，请新建连接",
        )
    models = workbench.enabled_family_models(platform_group["family_id"])
    if not models:
        raise HTTPException(status_code=400, detail="目标模型家族没有启用的模型")
    store.update("channels", channel_id, {
        "family_id": platform_group["family_id"], "updated_at": time.time()})
    model_ids = [item["model"] for item in models]
    base_estimate = packs.estimate(
        "admission", body.price_in or 2.0, body.price_out or 8.0, include_hard=False)
    per_model_cost = base_estimate["cost"]
    plan_id = store.insert("test_plan_snapshots", {
        "channel_id": channel_id, "platform_group_id": body.platform_group_id,
        "upstream_multiplier": body.upstream_multiplier,
        "models_json": store.dumps(model_ids),
        "selected_specialties_json": store.dumps([]),
        "recommended_specialties_json": store.dumps([]),
        "estimate_json": store.dumps({
            "models": len(models),
            "requests": base_estimate["requests"] * len(models),
            "max_requests": base_estimate["requests"] * len(models),
            "tokens": base_estimate["tokens"] * len(models),
            "cost": round(per_model_cost * len(models), 6),
        }),
        "created_by": request.state.user["id"], "created_at": time.time(),
    })
    task_ids = []
    target_ids = []
    for family_model in models:
        model = family_model["model"]
        target = _create_channel_target(
            channel, f"{channel['name']} · {model}", model,
            body.price_in, body.price_out, None, body.upstream_multiplier,
            body.platform_group_id)
        task_id = scheduler.create_task_row(
            "admission", target, include_hard=False, task_options={
                "specialty_profiles": [],
                "recommended_specialty_profiles": [],
                "test_plan_snapshot_id": plan_id,
            })
        await runner.submit(task_id)
        task_ids.append(task_id)
        target_ids.append(target["id"])
    return {
        "task_ids": task_ids, "target_ids": target_ids,
        "platform_group_id": body.platform_group_id,
        "test_plan_snapshot_id": plan_id, "status": "queued",
    }


@app.post("/api/channels/{channel_id}/platform-groups/{group_id}/supplement")
async def supplement_family_models(channel_id: int, group_id: int) -> dict[str, Any]:
    channel = store.get("channels", channel_id)
    platform_group = store.get("platform_groups", group_id)
    if not channel or not platform_group:
        raise HTTPException(status_code=404, detail="渠道或平台倍率组不存在")
    if channel.get("family_id") not in (None, platform_group["family_id"]):
        raise HTTPException(status_code=409, detail="渠道 Key 的模型家族与目标组不一致")
    existing = {row["model"]: row for row in store.query(
        "SELECT * FROM targets WHERE channel_id=? AND platform_group_id=?",
        (channel_id, group_id))}
    enabled = workbench.enabled_family_models(platform_group["family_id"])
    missing = [item for item in enabled if item["model"] not in existing]
    if not missing:
        return {"task_ids": [], "target_ids": [], "status": "complete"}
    rate_rows = store.query(
        "SELECT upstream_multiplier FROM targets WHERE channel_id=? "
        "AND platform_group_id=? AND upstream_multiplier IS NOT NULL LIMIT 1",
        (channel_id, group_id))
    upstream_multiplier = rate_rows[0]["upstream_multiplier"] if rate_rows else None
    if upstream_multiplier is None:
        raise HTTPException(status_code=400, detail="请先为渠道记录上游倍率")
    task_ids = []
    target_ids = []
    for family_model in missing:
        model = family_model["model"]
        target = _create_channel_target(
            channel, f"{channel['name']} · {model}", model,
            None, None, None, upstream_multiplier, group_id)
        task_id = scheduler.create_task_row("admission", target, include_hard=False)
        await runner.submit(task_id)
        task_ids.append(task_id)
        target_ids.append(target["id"])
    return {"task_ids": task_ids, "target_ids": target_ids, "status": "queued"}


def _ensure_model_rate_group(model: str, multiplier: float) -> int:
    match = _find_model_rate_group(model, multiplier)
    if match:
        return int(match["id"])
    _ensure_rate_group_value(multiplier)
    now = time.time()
    return store.insert("groups", {
        "name": placement.model_rate_group_name(model, multiplier),
        "multiplier": multiplier, "benchmark_id": None, "note": "",
        "created_at": now, "updated_at": now,
    })


def _find_model_rate_group(model: str, multiplier: float) -> dict[str, Any] | None:
    model_key = placement.model_group_key(model, multiplier)
    candidates = store.query(
        "SELECT * FROM groups WHERE ABS(multiplier-?) < 0.000000001 ORDER BY id",
        (multiplier,),
    )
    return next((group for group in candidates
                 if placement.model_group_key(group["name"], multiplier) == model_key), None)


def _ensure_rate_group_value(multiplier: float) -> dict[str, Any]:
    existing = store.query(
        "SELECT * FROM rate_groups WHERE ABS(multiplier-?) < 0.000000001",
        (multiplier,),
    )
    if existing:
        return existing[0]
    rate_id = store.insert("rate_groups", {
        "multiplier": multiplier, "created_at": time.time(),
    })
    row = store.get("rate_groups", rate_id)
    assert row is not None
    return row


def _assert_targets_idle(target_ids: list[int]) -> None:
    if not target_ids:
        return
    marks = ",".join("?" * len(target_ids))
    running = store.query(
        f"SELECT id FROM tasks WHERE target_id IN ({marks}) "
        "AND status IN ('queued','running')",
        tuple(target_ids),
    )
    if running:
        raise HTTPException(status_code=400, detail="仍有任务执行中，结束后再删除")


def _delete_channels(
    channel_rows: list[dict[str, Any]], user: dict[str, Any],
) -> dict[str, Any]:
    channel_ids = [int(channel["id"]) for channel in channel_rows]
    marks = ",".join("?" * len(channel_ids))
    targets = store.query(
        f"SELECT id FROM targets WHERE channel_id IN ({marks})", tuple(channel_ids))
    target_ids = [int(target["id"]) for target in targets]
    _assert_targets_idle(target_ids)
    lifecycle_rows = store.query(
        f"SELECT COUNT(*) n FROM model_lifecycle_history WHERE object_type='channel' "
        f"AND object_id IN ({marks})", tuple(channel_ids),
    )[0]["n"]
    if target_ids or lifecycle_rows:
        for channel in channel_rows:
            if channel["lifecycle_status"] != "archived":
                lifecycle.transition_channel(
                    int(channel["id"]), "archived", "用户从工作台归档", user
                )
        return {
            "status": "archived", "archived_connections": len(channel_ids),
            "preserved_models": len(target_ids),
        }
    with store.cursor() as cur:
        cur.execute(f"DELETE FROM channels WHERE id IN ({marks})", tuple(channel_ids))
    return {"status": "deleted", "deleted_connections": len(channel_ids),
            "deleted_models": len(target_ids)}


def _logical_channel_rows(channel_name: str) -> list[dict[str, Any]]:
    key = _channel_name_key(channel_name)
    return [channel for channel in store.query(
        "SELECT * FROM channels WHERE lifecycle_status!='archived'")
            if _channel_name_key(channel["name"]) == key]


@app.delete("/api/channels/{channel_id}")
def delete_channel(channel_id: int, request: Request) -> dict[str, Any]:
    channel = store.get("channels", channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="渠道不存在")
    return _delete_channels([channel], request.state.user)


@app.delete("/api/workspace/channels")
def delete_workspace_channel(channel_name: str, request: Request) -> dict[str, Any]:
    channels = _logical_channel_rows(channel_name)
    if not channels:
        raise HTTPException(status_code=404, detail="渠道不存在")
    return _delete_channels(channels, request.state.user)


def _delete_channel_rate(channel_rows: list[dict[str, Any]],
                         multiplier: float) -> dict[str, Any]:
    channel_ids = [int(channel["id"]) for channel in channel_rows]
    channel_marks = ",".join("?" * len(channel_ids))
    rates = store.query(
        "SELECT DISTINCT groups.multiplier FROM targets "
        "JOIN groups ON groups.id=targets.group_id "
        f"WHERE targets.channel_id IN ({channel_marks})",
        tuple(channel_ids),
    )
    if len(rates) <= 1:
        raise HTTPException(status_code=400, detail="唯一评测档位不可单独删除，请删除整个渠道")
    targets = store.query(
        "SELECT targets.id FROM targets JOIN groups ON groups.id=targets.group_id "
        f"WHERE targets.channel_id IN ({channel_marks}) "
        "AND ABS(groups.multiplier-?) < 0.000000001",
        (*channel_ids, multiplier),
    )
    if not targets:
        raise HTTPException(status_code=404, detail="该渠道没有这个评测档位")
    target_ids = [int(target["id"]) for target in targets]
    marks = ",".join("?" * len(target_ids))
    _assert_targets_idle(target_ids)
    report_count = store.query(
        f"SELECT COUNT(*) n FROM tasks WHERE target_id IN ({marks}) AND report<>''",
        tuple(target_ids),
    )[0]["n"]
    for target_id in target_ids:
        store.delete("targets", target_id)
    return {
        "status": "deleted", "multiplier": multiplier,
        "deleted_models": len(target_ids), "archived_reports": report_count,
    }


@app.delete("/api/channels/{channel_id}/rates/{multiplier}")
def delete_channel_rate(channel_id: int, multiplier: float) -> dict[str, Any]:
    channel = store.get("channels", channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="渠道不存在")
    return _delete_channel_rate([channel], multiplier)


@app.delete("/api/workspace/channels/rates/{multiplier}")
def delete_workspace_channel_rate(channel_name: str, multiplier: float) -> dict[str, Any]:
    channels = _logical_channel_rows(channel_name)
    if not channels:
        raise HTTPException(status_code=404, detail="渠道不存在")
    return _delete_channel_rate(channels, multiplier)


@app.get("/api/targets")
def list_targets() -> list[dict[str, Any]]:
    return [_target_out(r) for r in
            store.query("SELECT * FROM targets WHERE archived_at IS NULL "
                        "ORDER BY updated_at DESC")]


@app.post("/api/targets")
def create_target(body: TargetIn) -> dict[str, Any]:
    """新建并保存目标；检测结论只影响建议，不影响配置可用性。"""
    now = time.time()
    channel = create_channel(ChannelIn(
        name=body.group_name or body.name, base_url=body.base_url,
        api_key=body.api_key, protocol=body.protocol, group_name=body.group_name,
        env=body.env, source=body.source, edited_fields=body.edited_fields))
    tid = store.insert("targets", {
        "channel_id": channel["id"],
        "name": body.name, "protocol": body.protocol,
        "base_url": body.base_url.rstrip("/"), "model": body.model,
        "group_name": body.group_name, "env": body.env,
        "key_enc": encrypt(body.api_key),
        "price_in": body.price_in, "price_out": body.price_out,
        "source": body.source,
        "edited_fields": store.dumps(body.edited_fields),
        "recorded": 1, "status": "pending",
        "created_at": now, "updated_at": now,
    })
    row = store.get("targets", tid)
    assert row is not None
    return _target_out(row)


@app.delete("/api/targets/{target_id}")
def delete_target(target_id: int) -> dict[str, str]:
    target = store.get("targets", target_id)
    if not target:
        raise HTTPException(status_code=404, detail="渠道不存在")
    history = store.query("SELECT COUNT(*) n FROM tasks WHERE target_id=?", (target_id,))[0]["n"]
    if history:
        _assert_targets_idle([target_id])
        store.update("targets", target_id, {
            "archived_at": time.time(), "updated_at": time.time(),
        })
        store.execute("DELETE FROM scheduled_test_targets WHERE target_id=?", (target_id,))
        return {"status": "archived"}
    store.delete("targets", target_id)
    return {"status": "deleted"}


@app.put("/api/targets/{target_id}/group")
def assign_target_group(target_id: int, body: TargetGroupIn) -> dict[str, Any]:
    row = store.get("targets", target_id)
    if not row:
        raise HTTPException(status_code=404, detail="渠道模型不存在")
    group_id = _ensure_model_rate_group(body.model.strip(), body.multiplier)
    store.update("targets", target_id, {
        "group_id": group_id, "pool": "grouped", "updated_at": time.time(),
    })
    updated = store.get("targets", target_id)
    assert updated is not None
    return _target_out(updated)


@app.put("/api/targets/{target_id}/upstream-multiplier")
def update_upstream_multiplier(
    target_id: int, body: UpstreamMultiplierIn,
) -> dict[str, Any]:
    if not store.get("targets", target_id):
        raise HTTPException(status_code=404, detail="渠道模型不存在")
    store.update("targets", target_id, {
        "upstream_multiplier": body.upstream_multiplier, "updated_at": time.time()})
    return {"target_id": target_id, "upstream_multiplier": body.upstream_multiplier}


@app.delete("/api/targets/{target_id}/group")
def unassign_target_group(target_id: int) -> dict[str, Any]:
    row = store.get("targets", target_id)
    if not row:
        raise HTTPException(status_code=404, detail="渠道模型不存在")
    store.update("targets", target_id, {
        "group_id": None, "pool": "ungrouped", "updated_at": time.time(),
    })
    updated = store.get("targets", target_id)
    assert updated is not None
    return _target_out(updated)


@app.get("/api/estimate")
def estimate(kind: str, target_id: int | None = None,
             include_hard: bool | None = None, load_levels: str = "",
             load_requests_per_level: int = 20) -> dict[str, Any]:
    """提交前的预估：请求数、tokens、并发、预计费用。

    带硬题的包会同时返回「关掉硬题」的口径（cost_no_hard 等），
    前端把差价摆出来，用户自己决定这一趟要不要跑硬题。
    """
    price_in, price_out = 2.0, 8.0
    if target_id:
        row = store.get("targets", target_id)
        if row:
            price_in = row["price_in"] or price_in
            price_out = row["price_out"] or price_out
    want_hard = HARD_IN_CAPABILITY if include_hard is None else include_hard
    try:
        est = packs.estimate(kind, price_in, price_out, include_hard=want_hard)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail="未知的测试类型") from exc
    est["desc"] = packs.get_pack(kind)["desc"]
    if kind == "load" and load_levels:
        try:
            levels = [int(value.strip()) for value in load_levels.split(",") if value.strip()]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="压力档位必须是逗号分隔整数") from exc
        if (not levels or len(levels) > 20 or any(level < 1 or level > 2000 for level in levels)
                or not 10 <= load_requests_per_level <= 200):
            raise HTTPException(status_code=400, detail="压力档位或每档请求数超出允许范围")
        actual_requests = len(levels) * load_requests_per_level
        ratio = actual_requests / max(1, est["requests"])
        est.update({
            "requests": actual_requests, "tokens": round(est["tokens"] * ratio),
            "cost": round(est["cost"] * ratio, 6), "concurrency": max(levels),
            "load_levels": levels, "requests_per_level": load_requests_per_level,
        })
    est["hard_default"] = HARD_IN_CAPABILITY
    if est.get("has_hard"):
        est["hard_banks"] = [
            {"bank": b, "name": hardbank.bank_name(b),
             "count": hardbank.count([b]),
             "desc": hardbank.BANK_META.get(b, {}).get("desc", "")}
            for b in hardbank.banks()
        ]
        est["hard_version"] = hardbank.HARD_VERSION
    return est


# ---------- 任务中心 ----------

def _generic_task(task_id: int) -> dict[str, Any]:
    row = store.get("tasks", task_id)
    if not row or store.get("paired_tasks", task_id, key="task_id"):
        raise HTTPException(status_code=404, detail="任务不存在")
    return row

@app.post("/api/tasks")
async def create_task(body: TaskIn) -> dict[str, Any]:
    """提交任务，立刻返回任务号，不等执行结束。"""
    target = store.get("targets", body.target_id)
    if not target:
        raise HTTPException(status_code=404, detail="渠道不存在")
    load_options = None
    if body.kind == "load":
        if body.local_runner_id is None:
            raise HTTPException(status_code=400, detail="压力测试必须选择在线的本地执行器")
        levels = sorted(set(body.load_levels))
        maximum = 2000 if body.load_mode == "open" else 200
        if not levels or any(level < 1 or level > maximum for level in levels):
            raise HTTPException(status_code=400, detail=f"负载档位必须在 1 到 {maximum} 之间")
        load_options = {
            "load": {
                "levels": levels,
                "requests_per_level": body.load_requests_per_level,
                "cooldown_seconds": body.load_cooldown_seconds,
                "stream": body.load_stream,
                "mode": body.load_mode,
                "prompt_profile": body.load_prompt_profile,
                "max_tokens": body.load_max_tokens,
                "interval_seconds": body.load_interval_seconds,
                "burst_period_seconds": body.load_burst_period_seconds,
                "max_in_flight": body.load_max_in_flight,
            }
        }

    bench_id = body.benchmark_id
    if bench_id is not None:
        bench = store.get("benchmarks", bench_id)
        if not bench:
            raise HTTPException(status_code=404, detail="标杆不存在")
        group = store.get("groups", target["group_id"]) if target["group_id"] else None
        if not group or group["benchmark_id"] != bench_id:
            raise HTTPException(status_code=400, detail="只能使用该模型-倍率组自己的标杆")
        if bench["pack_version"] != itembank.PACK_VERSION:
            raise HTTPException(
                status_code=400,
                detail=f"该标杆用的题库是 {bench['pack_version']}，"
                       f"当前是 {itembank.PACK_VERSION}，版本不同不能比分，请重设标杆")

    # 硬题开关落在任务行上：重试同一个任务时口径不变，历史标杆才可比
    want_hard = body.kind == "capability" and (
        HARD_IN_CAPABILITY if body.include_hard is None else body.include_hard)
    task_id = scheduler.create_task_row(
        body.kind, target, include_hard=want_hard,
        task_options=load_options)
    patch: dict[str, Any] = {}
    if body.cost_limit is not None:
        patch["cost_limit"] = body.cost_limit
    if bench_id is not None:
        patch["benchmark_id"] = bench_id
    if patch:
        store.update("tasks", task_id, patch)
    if body.kind == "load":
        try:
            local_job = local_runners.create_job(
                task_id, int(body.local_runner_id), target, _safe_key(target),
                {**(load_options or {}), "cost_limit": body.cost_limit},
            )
        except local_runners.RunnerError as exc:
            store.update("tasks", task_id, {
                "status": "failed", "finished_at": time.time(),
                "progress": store.dumps({"stage": str(exc)}),
            })
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"task_id": task_id, "status": "queued", "runner_job": local_job}
    await runner.submit(task_id)
    return {"task_id": task_id, "status": "queued"}


def _task_out(row: dict[str, Any], with_report: bool = False) -> dict[str, Any]:
    out = {
        "id": row["id"], "kind": row["kind"],
        "target_id": row["target_id"], "target_name": row["target_name"],
        "pack_name": row["pack_name"], "pack_version": row["pack_version"],
        "status": row["status"],
        "progress": store.loads(row["progress"], {}),
        "snapshot": store.loads(row["snapshot"], {}),
        "parent_task_id": row["parent_task_id"],
        "created_at": row["created_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "tags": store.loads(row.get("tags_json"), []),
        "owner": row.get("owner") or "",
        "review_status": row.get("review_status") or "unreviewed",
        "operator_note": row.get("operator_note") or "",
    }
    rep = store.task_report(row, None)
    out["has_report"] = rep is not None
    if rep:
        out["verdict"] = rep["conclusion"]["verdict"]
        out["verdict_code"] = rep["conclusion"]["code"]
    out["selected_benchmark_id"] = row.get("selected_benchmark_id")
    out["selected_benchmark_comparison"] = store.loads(
        row.get("selected_benchmark_comparison"), None
    )
    if with_report:
        out["report"] = rep
    if row["kind"] == "load":
        local_jobs = store.query(
            "SELECT jobs.*,runners.name runner_name,runners.version runner_version," 
            "runners.last_seen_at runner_last_seen_at,runners.revoked_at runner_revoked_at "
            "FROM runner_jobs jobs JOIN paired_runners runners ON runners.id=jobs.runner_id "
            "WHERE jobs.task_id=?", (row["id"],)
        )
        if local_jobs:
            job = local_jobs[0]
            runner_online = bool(
                not job["runner_revoked_at"] and job["runner_last_seen_at"]
                and time.time() - job["runner_last_seen_at"] <= local_runners.RUNNER_ONLINE_SECONDS
            )
            out["local_runner"] = {
                "id": job["runner_id"], "name": job["runner_name"],
                "version": job["runner_version"],
                "status": "online" if runner_online else "offline",
                "job_id": job["id"], "job_status": job["status"],
                "last_seen_at": job["runner_last_seen_at"],
            }
    return out


@app.get("/api/tasks")
def list_tasks(kind: str | None = None, target_id: int | None = None,
               verdict: str | None = None, q: str = "", owner: str = "",
               review_status: str = "", tag: str = "", limit: int = 50) -> list[dict[str, Any]]:
    sql = (
        "SELECT * FROM tasks WHERE NOT EXISTS "
        "(SELECT 1 FROM paired_tasks WHERE paired_tasks.task_id=tasks.id)"
    )
    params: list[Any] = []
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    if target_id:
        sql += " AND target_id=?"
        params.append(target_id)
    if q.strip():
        sql += " AND (target_name LIKE ? OR snapshot LIKE ? OR CAST(id AS TEXT)=?)"
        params.extend((f"%{q.strip()}%", f"%{q.strip()}%", q.strip()))
    if owner:
        sql += " AND owner=?"
        params.append(owner)
    if review_status:
        if review_status not in {"unreviewed", "in_review", "approved", "rejected"}:
            raise HTTPException(status_code=400, detail="审查状态无效")
        sql += " AND review_status=?"
        params.append(review_status)
    if tag:
        sql += " AND tags_json LIKE ?"
        params.append(f'%"{tag.strip()}"%')
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    rows = [_task_out(row) for row in store.query(sql, tuple(params))]
    if verdict:
        rows = [r for r in rows if r.get("verdict_code") == verdict]
    return rows


@app.patch("/api/tasks/{task_id}/metadata")
def update_task_metadata(task_id: int, body: TaskMetadataIn) -> dict[str, Any]:
    _generic_task(task_id)
    tags = list(dict.fromkeys(tag.strip() for tag in body.tags if tag.strip()))
    if any(len(tag) > 40 for tag in tags):
        raise HTTPException(status_code=400, detail="单个标签不能超过 40 字符")
    store.update("tasks", task_id, {
        "tags_json": store.dumps(tags), "owner": body.owner.strip(),
        "review_status": body.review_status, "operator_note": body.note.strip(),
    })
    row = store.get("tasks", task_id)
    assert row is not None
    return _task_out(row)


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int) -> dict[str, Any]:
    row = _generic_task(task_id)
    out = _task_out(row, with_report=True)
    out["events"] = store.list_events(task_id)
    return out


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: int) -> dict[str, str]:
    row = _generic_task(task_id)
    if row["status"] not in ("queued", "running"):
        raise HTTPException(status_code=400, detail="任务已结束，无法取消")
    store.update("tasks", task_id, {"cancel_flag": 1})
    if row["kind"] == "load":
        local_runners.request_cancel(task_id)
    store.add_event(task_id, "用户请求取消", stage="队列", level="warn")
    return {"status": "cancelling"}


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: int) -> dict[str, Any]:
    """重试建新任务，原失败记录保留。"""
    row = _generic_task(task_id)
    target = store.get("targets", row["target_id"]) if row["target_id"] else None
    if not target:
        raise HTTPException(status_code=400, detail="原渠道已删除，无法重试")
    # 沿用原任务的硬题开关：重试是为了拿一份可比的结果，
    # 口径变了就不叫重试了（原来跑了硬题、重试没跑，两份报告没法对比）
    prev_hard = row["include_hard"]
    previous_snapshot = store.loads(row["snapshot"], {})
    task_options = None
    if row["kind"] == "load":
        task_options = {"load": previous_snapshot.get("load")}
    new_id = scheduler.create_task_row(
        row["kind"], target,
        include_hard=None if prev_hard is None else bool(prev_hard),
        task_options=task_options)
    store.update("tasks", new_id, {
        "parent_task_id": task_id, "cost_limit": row["cost_limit"],
        "target_name": row["target_name"]})
    store.add_event(new_id, f"由任务 #{task_id} 重试而来", stage="队列")
    if row["kind"] == "load":
        previous_jobs = store.query(
            "SELECT * FROM runner_jobs WHERE task_id=?", (task_id,)
        )
        if not previous_jobs:
            raise HTTPException(status_code=400, detail="原压力任务没有本地执行器记录")
        try:
            local_runners.create_job(
                new_id, previous_jobs[0]["runner_id"], target, _safe_key(target),
                {"load": store.loads(row["snapshot"], {}).get("load"),
                 "cost_limit": row["cost_limit"]},
            )
        except local_runners.RunnerError as exc:
            store.update("tasks", new_id, {"status": "failed", "finished_at": time.time()})
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        await runner.submit(new_id)
    return {"task_id": new_id, "status": "queued"}


# ---------- 报告导出 ----------

@app.get("/api/tasks/{task_id}/export")
def export_report(task_id: int, fmt: str = "md"):
    """导出 md / html / json。内容基于已脱敏报告，不含完整 Key。"""
    row = _generic_task(task_id)
    rep = store.task_report(row, None)
    if not rep:
        raise HTTPException(status_code=400, detail="该任务还没有报告")
    name = f"report-{task_id}"
    if fmt == "html":
        return HTMLResponse(export.to_html(rep), headers={
            "Content-Disposition": f'attachment; filename="{name}.html"'})
    if fmt == "json":
        return PlainTextResponse(export.to_json(rep), media_type="application/json",
                                 headers={"Content-Disposition":
                                          f'attachment; filename="{name}.json"'})
    return PlainTextResponse(export.to_markdown(rep), media_type="text/markdown",
                             headers={"Content-Disposition":
                                      f'attachment; filename="{name}.md"'})


# ---------- 定时测试与通知 ----------

def _require_global_schedule_role(request: Request, role: str) -> dict[str, Any]:
    user = request.state.user
    if paired_access.has_role(user["id"], "admin") or paired_access.has_role(user["id"], role):
        return user
    raise HTTPException(status_code=403, detail=f"定时测试需要全局 {role} 或 admin 授权")


@app.get("/api/scheduled-configuration-options")
def scheduled_configuration_options(request: Request) -> dict[str, Any]:
    _require_global_schedule_role(request, "viewer")
    return scheduled_configurations.configuration_options()


@app.get("/api/scheduled-primary-models")
def get_scheduled_primary_models(request: Request) -> list[dict[str, Any]]:
    _require_global_schedule_role(request, "viewer")
    return scheduled_configurations.primary_models()


@app.put("/api/scheduled-primary-models")
def update_scheduled_primary_models(
    body: ScheduledPrimaryModelsIn, request: Request,
) -> list[dict[str, Any]]:
    user = _require_global_schedule_role(request, "operator")
    return scheduled_configurations.set_primary_models(body.models, user, body.reason)


@app.get("/api/configuration-scheduled-tests")
def list_configuration_scheduled_tests(request: Request) -> list[dict[str, Any]]:
    _require_global_schedule_role(request, "viewer")
    return scheduled_configurations.list_schedules()


@app.post("/api/configuration-scheduled-tests")
def create_configuration_scheduled_test(
    body: ScheduledConfigurationTestIn, request: Request,
) -> dict[str, Any]:
    user = _require_global_schedule_role(request, "operator")
    values = body.model_dump()
    values["report_minute"] = _report_minute(values.pop("report_time"))
    return scheduled_configurations.create_schedule(values, user)


@app.put("/api/configuration-scheduled-tests/{schedule_id}")
def update_configuration_scheduled_test(
    schedule_id: int, body: ScheduledConfigurationTestIn, request: Request,
) -> dict[str, Any]:
    user = _require_global_schedule_role(request, "operator")
    values = body.model_dump()
    values["report_minute"] = _report_minute(values.pop("report_time"))
    return scheduled_configurations.update_schedule(schedule_id, values, user)


@app.post("/api/configuration-scheduled-tests/{schedule_id}/rebuild-baseline")
def rebuild_configuration_schedule_baseline(
    schedule_id: int, body: ScheduledBaselineRebuildIn, request: Request,
) -> dict[str, Any]:
    user = _require_global_schedule_role(request, "operator")
    return scheduled_configurations.rebuild_baseline(schedule_id, user, body.reason)


@app.get("/api/scheduled-measurement-tasks/{task_id}")
def get_scheduled_measurement_task(task_id: int, request: Request) -> dict[str, Any]:
    task = store.get("tasks", task_id)
    if not task or task.get("kind") != scheduled_measurement.TASK_KIND:
        raise HTTPException(status_code=404, detail="定时监测任务不存在")
    snapshot = store.loads(task["snapshot"], {})
    _require_configuration_role(request, "viewer", int(snapshot["configuration_id"]))
    report_rows = store.query(
        "SELECT version,reason,created_at FROM scheduled_measurement_report_revisions WHERE task_id=? ORDER BY version DESC",
        (task_id,),
    )
    return {
        "task": _task_out(task, with_report=False), "report": store.task_report(task, None),
        "report_revisions": report_rows,
        "requests": [{
            **{key: row[key] for key in ("id", "canonical_model", "template_id", "template_kind", "request_index",
                                          "attempt_kind", "status", "attribution", "error_code", "error_detail", "sent_at", "finished_at")},
            "effective_status": scheduled_measurement._effective_status(row),
        } for row in store.query(
            "SELECT * FROM scheduled_measurement_requests WHERE task_id=? ORDER BY request_index,id", (task_id,)
        )],
    }


@app.get("/api/scheduled-measurement-tasks/{task_id}/evidence")
def get_scheduled_measurement_evidence(
    task_id: int, request: Request, include_raw: bool = False, purpose: str = "",
) -> dict[str, Any]:
    task = store.get("tasks", task_id)
    if not task or task.get("kind") != scheduled_measurement.TASK_KIND:
        raise HTTPException(status_code=404, detail="定时监测任务不存在")
    snapshot = store.loads(task["snapshot"], {})
    configuration_id = int(snapshot["configuration_id"])
    user = _require_configuration_role(request, "viewer", configuration_id)
    configuration = _configuration_row(configuration_id)
    can_view_raw = _configuration_has_role(user, "operator", configuration) \
        or _configuration_has_role(user, "admin", configuration)
    use = purpose.strip()
    if include_raw and not can_view_raw:
        raise HTTPException(status_code=403, detail="查看定时原始证据需要测试操作员或管理员授权")
    if include_raw and not use:
        raise HTTPException(status_code=400, detail="查看原始证据必须填写用途")
    if len(use) > 500:
        raise HTTPException(status_code=400, detail="证据访问用途不能超过 500 字符")
    evidence = []
    for row in store.query(
        "SELECT * FROM scheduled_measurement_evidence WHERE task_id=? ORDER BY record_seq", (task_id,)
    ):
        item = {
            "record_seq": row["record_seq"], "record_type": row["record_type"],
            "request_id": row["request_id"], "payload": store.loads(row["payload_json"], {}),
            "record_hash": row["record_hash"], "previous_hash": row["previous_hash"], "created_at": row["created_at"],
        }
        if include_raw and row.get("raw_block_id"):
            block = store.get("scheduled_measurement_raw_blocks", row["raw_block_id"])
            if not block:
                raise HTTPException(status_code=409, detail="原始证据块缺失，无法读取")
            try:
                from cryptography.fernet import Fernet
                item["raw"] = Fernet(decrypt(block["key_ciphertext"]).encode("ascii")).decrypt(
                    block["ciphertext"].encode("ascii")
                ).decode("utf-8")
            except Exception as exc:
                raise HTTPException(status_code=409, detail="原始证据块无法解密") from exc
        evidence.append(item)
    integrity = scheduled_measurement.verify_evidence(task_id)
    auth.audit(
        user["id"], user["username"],
        "scheduled_measurement.evidence.raw_view" if include_raw else "scheduled_measurement.evidence.view",
        "scheduled_measurement_task", str(task_id), "success",
        detail={"include_raw": include_raw, "purpose": use or "查看脱敏定时证据"},
    )
    return {"task_id": task_id, "integrity": integrity, "records": evidence}


@app.post("/api/scheduled-measurement-requests/{request_id}/attribution")
def correct_scheduled_measurement_attribution(
    request_id: int, body: ScheduledAttributionIn, request: Request,
) -> dict[str, Any]:
    row = store.get("scheduled_measurement_requests", request_id)
    if not row:
        raise HTTPException(status_code=404, detail="定时测量请求不存在")
    user = _require_configuration_role(request, "admin", int(row["configuration_id"]))
    return scheduled_measurement.correct_attribution(request_id, body.attribution, body.reason, user)

def _report_minute(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def _scheduled_test_out(row: dict[str, Any]) -> dict[str, Any]:
    minute = int(row["report_minute"])
    recent_runs = store.query(
        "SELECT id,run_date,report_at,status,initial_sent,supplement_sent "
        "FROM scheduled_runs WHERE scheduled_test_id=? ORDER BY report_at DESC LIMIT 5",
        (row["id"],),
    )
    return {
        "id": row["id"], "name": row["name"],
        "report_time": f"{minute // 60:02d}:{minute % 60:02d}",
        "test_lead_minutes": scheduler.TEST_LEAD_MINUTES,
        "timezone": "Asia/Shanghai",
        "feishu_webhook_ids": store.loads(row["feishu_webhook_ids"], []),
        "email_recipient_ids": store.loads(row["email_recipient_ids"], []),
        "enabled": bool(row["enabled"]), "recent_runs": recent_runs,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _validated_schedule(body: ScheduledTestIn) -> dict[str, Any]:
    feishu_ids = list(dict.fromkeys(body.feishu_webhook_ids))
    email_ids = list(dict.fromkeys(body.email_recipient_ids))
    if any(not store.get("feishu_webhooks", item_id) for item_id in feishu_ids):
        raise HTTPException(status_code=400, detail="包含不存在的飞书接收端")
    if any(not store.get("email_recipients", item_id) for item_id in email_ids):
        raise HTTPException(status_code=400, detail="包含不存在的邮箱接收端")
    return {
        "name": body.name, "report_minute": _report_minute(body.report_time),
        "feishu_webhook_ids": store.dumps(feishu_ids),
        "email_recipient_ids": store.dumps(email_ids),
        "enabled": 1 if body.enabled else 0, "updated_at": time.time(),
    }


@app.get("/api/scheduled-tests")
def list_scheduled_tests() -> list[dict[str, Any]]:
    return [_scheduled_test_out(row) for row in
            store.query("SELECT * FROM scheduled_tests ORDER BY report_minute,name")]


@app.post("/api/scheduled-tests")
def create_scheduled_test(body: ScheduledTestIn) -> dict[str, Any]:
    data = _validated_schedule(body)
    data["created_at"] = data["updated_at"]
    row_id = store.insert("scheduled_tests", data)
    return _scheduled_test_out(store.get("scheduled_tests", row_id) or {})


@app.put("/api/scheduled-tests/{schedule_id}")
def update_scheduled_test(schedule_id: int, body: ScheduledTestIn) -> dict[str, Any]:
    if not store.get("scheduled_tests", schedule_id):
        raise HTTPException(status_code=404, detail="定时计划不存在")
    store.update("scheduled_tests", schedule_id, _validated_schedule(body))
    return _scheduled_test_out(store.get("scheduled_tests", schedule_id) or {})


@app.delete("/api/scheduled-tests/{schedule_id}")
def delete_scheduled_test(schedule_id: int) -> dict[str, bool]:
    if not store.get("scheduled_tests", schedule_id):
        raise HTTPException(status_code=404, detail="定时计划不存在")
    if store.query(
        "SELECT id FROM scheduled_runs WHERE scheduled_test_id=? AND status!='complete'",
        (schedule_id,),
    ):
        raise HTTPException(status_code=409, detail="当前计划有执行中的批次，请先停用并等待完成")
    store.delete("scheduled_tests", schedule_id)
    return {"deleted": True}


@app.put("/api/targets/{target_id}/scheduled-test")
def set_scheduled_test_target(target_id: int, body: ScheduledTargetIn) -> dict[str, Any]:
    target = store.get("targets", target_id)
    if not target:
        raise HTTPException(status_code=404, detail="模型不存在")
    selected = store.query(
        "SELECT id,target_id FROM scheduled_test_targets WHERE target_id=?", (target_id,))
    if body.enabled and not selected:
        store.insert("scheduled_test_targets", {
            "target_id": target_id,
            "created_at": time.time(),
        })
    elif not body.enabled and selected:
        store.execute("DELETE FROM scheduled_report_group_targets WHERE target_id=?", (target_id,))
        store.delete("scheduled_test_targets", selected[0]["id"])
    return {"target_id": target_id, "scheduled_test_enabled": body.enabled}


@app.get("/api/scheduled-test-targets")
def list_scheduled_test_targets() -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT scheduled_test_targets.target_id,"
        "targets.name target_name,targets.model,targets.upstream_multiplier,"
        "channels.name channel_name "
        "FROM scheduled_test_targets JOIN targets ON targets.id=scheduled_test_targets.target_id "
        "LEFT JOIN channels ON channels.id=targets.channel_id "
        "ORDER BY targets.model,channels.name")
    return [{**row, "model_family": scheduler.model_family(row["model"])} for row in rows]


def _scheduled_report_group_out(row: dict[str, Any]) -> dict[str, Any]:
    target_ids = [item["target_id"] for item in store.query(
        "SELECT target_id FROM scheduled_report_group_targets WHERE group_id=? ORDER BY target_id",
        (row["id"],))]
    return {
        "id": row["id"], "model_family": row["model_family"],
        "online_multiplier": row["multiplier"], "target_ids": target_ids,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


@app.get("/api/scheduled-report-groups")
def list_scheduled_report_groups() -> list[dict[str, Any]]:
    return [_scheduled_report_group_out(row) for row in store.query(
        "SELECT * FROM scheduled_report_groups ORDER BY multiplier,model_family")]


@app.post("/api/scheduled-report-groups")
def create_scheduled_report_group(body: ScheduledReportGroupIn) -> dict[str, Any]:
    family = body.model_family.strip()
    if store.query(
        "SELECT id FROM scheduled_report_groups WHERE model_family=? "
        "AND ABS(multiplier-?)<0.000000001", (family, body.online_multiplier)):
        raise HTTPException(status_code=409, detail="该模型上线倍率分组已存在")
    now = time.time()
    row_id = store.insert("scheduled_report_groups", {
        "model_family": family, "multiplier": body.online_multiplier,
        "created_at": now, "updated_at": now})
    return _scheduled_report_group_out(store.get("scheduled_report_groups", row_id) or {})


@app.put("/api/scheduled-report-groups/{group_id}")
def update_scheduled_report_group(
    group_id: int, body: ScheduledReportGroupIn,
) -> dict[str, Any]:
    if not store.get("scheduled_report_groups", group_id):
        raise HTTPException(status_code=404, detail="报告分组不存在")
    family = body.model_family.strip()
    conflict = store.query(
        "SELECT id FROM scheduled_report_groups WHERE id!=? AND model_family=? "
        "AND ABS(multiplier-?)<0.000000001", (group_id, family, body.online_multiplier))
    if conflict:
        raise HTTPException(status_code=409, detail="该模型上线倍率分组已存在")
    members = store.query(
        "SELECT targets.model FROM targets JOIN scheduled_report_group_targets "
        "ON scheduled_report_group_targets.target_id=targets.id "
        "WHERE scheduled_report_group_targets.group_id=?", (group_id,))
    if any(scheduler.model_family(member["model"]) != family for member in members):
        raise HTTPException(status_code=400, detail="请先移除与新模型分组不匹配的渠道")
    store.update("scheduled_report_groups", group_id, {
        "model_family": family, "multiplier": body.online_multiplier,
        "updated_at": time.time()})
    return _scheduled_report_group_out(store.get("scheduled_report_groups", group_id) or {})


@app.put("/api/scheduled-report-groups/{group_id}/targets")
def set_scheduled_report_group_targets(
    group_id: int, body: ScheduledReportGroupTargetsIn,
) -> dict[str, Any]:
    group = store.get("scheduled_report_groups", group_id)
    if not group:
        raise HTTPException(status_code=404, detail="报告分组不存在")
    target_ids = list(dict.fromkeys(body.target_ids))
    if target_ids:
        marks = ",".join("?" for _ in target_ids)
        selected = store.query(
            f"SELECT targets.id,targets.model FROM targets JOIN scheduled_test_targets "
            f"ON scheduled_test_targets.target_id=targets.id WHERE targets.id IN ({marks})",
            tuple(target_ids))
        if len(selected) != len(target_ids):
            raise HTTPException(status_code=400, detail="只能选择工作台已勾选的定时测试模型")
        if any(scheduler.model_family(item["model"]) != group["model_family"]
               for item in selected):
            raise HTTPException(status_code=400, detail="模型与报告分组不匹配")
    store.execute("DELETE FROM scheduled_report_group_targets WHERE group_id=?", (group_id,))
    now = time.time()
    for target_id in target_ids:
        store.insert("scheduled_report_group_targets", {
            "group_id": group_id, "target_id": target_id, "created_at": now})
    return _scheduled_report_group_out(group)


@app.delete("/api/scheduled-report-groups/{group_id}")
def delete_scheduled_report_group(group_id: int) -> dict[str, bool]:
    if not store.get("scheduled_report_groups", group_id):
        raise HTTPException(status_code=404, detail="报告分组不存在")
    store.delete("scheduled_report_groups", group_id)
    return {"deleted": True}


def _feishu_out(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "webhook_masked": "https://open.feishu.cn/***",
            "has_secret": bool(row["secret_enc"]), "updated_at": row["updated_at"]}


@app.get("/api/notifications/feishu")
def list_feishu_webhooks() -> list[dict[str, Any]]:
    return [_feishu_out(row) for row in store.query("SELECT * FROM feishu_webhooks ORDER BY name")]


@app.post("/api/notifications/feishu")
def create_feishu_webhook(body: FeishuWebhookIn) -> dict[str, Any]:
    parsed = urlparse(body.webhook)
    if parsed.scheme != "https" or parsed.hostname != "open.feishu.cn" \
            or not parsed.path.startswith("/open-apis/bot/"):
        raise HTTPException(status_code=400, detail="请输入飞书群机器人的 HTTPS Webhook")
    try:
        egress.validate_url(body.webhook)
    except egress.EgressDenied as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    now = time.time()
    row_id = store.insert("feishu_webhooks", {
        "name": body.name, "webhook_enc": encrypt(body.webhook),
        "secret_enc": encrypt(body.secret) if body.secret else "",
        "created_at": now, "updated_at": now,
    })
    return _feishu_out(store.get("feishu_webhooks", row_id) or {})


@app.delete("/api/notifications/feishu/{destination_id}")
def delete_feishu_webhook(destination_id: int) -> dict[str, bool]:
    if any(destination_id in store.loads(row["feishu_webhook_ids"], []) for row in
           store.query("SELECT feishu_webhook_ids FROM scheduled_tests")):
        raise HTTPException(status_code=409, detail="请先从定时计划中取消选择该飞书接收端")
    store.delete("feishu_webhooks", destination_id)
    return {"deleted": True}


@app.post("/api/notifications/feishu/{destination_id}/test")
async def test_feishu_webhook(destination_id: int) -> dict[str, bool]:
    destination = store.get("feishu_webhooks", destination_id)
    if not destination:
        raise HTTPException(status_code=404, detail="飞书接收端不存在")
    try:
        await notifications.send_feishu_test(destination)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="飞书发送失败，请检查 Webhook 和签名密钥") from exc
    return {"sent": True}


@app.get("/api/notifications/email-recipients")
def list_email_recipients() -> list[dict[str, Any]]:
    return store.query("SELECT id,name,address,created_at,updated_at FROM email_recipients ORDER BY name")


@app.post("/api/notifications/email-recipients")
def create_email_recipient(body: EmailRecipientIn) -> dict[str, Any]:
    now = time.time()
    row_id = store.insert("email_recipients", {
        "name": body.name, "address": body.address,
        "created_at": now, "updated_at": now,
    })
    return store.get("email_recipients", row_id) or {}


@app.delete("/api/notifications/email-recipients/{destination_id}")
def delete_email_recipient(destination_id: int) -> dict[str, bool]:
    if any(destination_id in store.loads(row["email_recipient_ids"], []) for row in
           store.query("SELECT email_recipient_ids FROM scheduled_tests")):
        raise HTTPException(status_code=409, detail="请先从定时计划中取消选择该邮件收件人")
    store.delete("email_recipients", destination_id)
    return {"deleted": True}


@app.post("/api/notifications/email-recipients/{destination_id}/test")
async def test_email_recipient(destination_id: int) -> dict[str, bool]:
    destination = store.get("email_recipients", destination_id)
    if not destination:
        raise HTTPException(status_code=404, detail="邮件收件人不存在")
    try:
        await notifications.send_email_test(destination)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="邮件发送失败，请检查 SMTP 配置和授权码") from exc
    return {"sent": True}


def _smtp_out(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {key: row[key] for key in
            ("host", "port", "tls_mode", "username", "from_name", "from_address", "updated_at")} | {
                "password_configured": bool(row["password_enc"])}


@app.get("/api/notifications/smtp")
def get_smtp_settings() -> dict[str, Any] | None:
    return _smtp_out(store.get("smtp_settings", 1))


@app.put("/api/notifications/smtp")
def set_smtp_settings(body: SmtpSettingsIn) -> dict[str, Any]:
    try:
        egress.validate_host(body.host, body.port)
    except egress.EgressDenied as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    current = store.get("smtp_settings", 1)
    if not body.password and not current:
        raise HTTPException(status_code=400, detail="首次配置必须填写 SMTP 密码或授权码")
    data = {
        "host": body.host, "port": body.port, "tls_mode": body.tls_mode,
        "username": body.username,
        "password_enc": encrypt(body.password) if body.password else current["password_enc"],
        "from_name": body.from_name, "from_address": body.from_address,
        "updated_at": time.time(),
    }
    if current:
        store.update("smtp_settings", 1, data)
    else:
        store.insert("smtp_settings", {"id": 1, **data})
    return _smtp_out(store.get("smtp_settings", 1)) or {}


@app.get("/api/scheduled-runs/{run_id}")
def get_scheduled_run(run_id: int) -> dict[str, Any]:
    row = store.get("scheduled_runs", run_id)
    if not row:
        raise HTTPException(status_code=404, detail="定时批次不存在")
    task_ids = store.loads(row["task_ids"], [])
    tasks = [_task_out(task, with_report=False) for task_id in task_ids
             if (task := store.get("tasks", task_id))]
    return {**row, "task_ids": task_ids, "tasks": tasks,
            "deliveries": store.query(
                "SELECT * FROM notification_deliveries WHERE scheduled_run_id=? ORDER BY id", (run_id,))}


# ---------- 手动巡检 ----------

@app.post("/api/targets/{target_id}/inspect-now")
async def inspect_now(target_id: int) -> dict[str, Any]:
    """手动跑一次抽检，不用等到调度时间。"""
    row = store.get("targets", target_id)
    if not row:
        raise HTTPException(status_code=404, detail="渠道不存在")
    task_id = await scheduler.create_inspect_task(row)
    return {"task_id": task_id, "status": "queued"}


# ---------- 标杆档案 ----------

def _bench_out(row: dict[str, Any]) -> dict[str, Any]:
    groups = store.query(
        "SELECT id,name,multiplier FROM groups WHERE benchmark_id=? "
        "ORDER BY multiplier DESC,name", (row["id"],))
    return {
        "id": row["id"], "name": row["name"],
        "pack_version": row["pack_version"],
        "model_hint": row["model_hint"],
        "dims": store.loads(row["dims"], {}),
        "items": store.loads(row["items"], {}),
        "overall": row["overall"], "tolerance": row["tolerance"],
        "source": row["source"], "source_task_id": row["source_task_id"],
        "groups": groups,
        "note": row["note"],
        "stale": row["pack_version"] != itembank.PACK_VERSION,
        # 硬题分快照。可能为空（标杆是加硬题之前定的，或那一趟关了硬题）。
        # hard_stale 单独判：硬题版本变了只让硬题对比失效，不影响四维能力对比。
        "hard": store.loads(row["hard"], None),
        "hard_stale": bool(
            (store.loads(row["hard"], None) or {}).get("hard_version")
            and (store.loads(row["hard"], None) or {}).get("hard_version")
            != hardbank.HARD_VERSION),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


@app.get("/api/dimensions")
def list_dimensions() -> dict[str, Any]:
    """题库结构：手工填标杆分数时前端要按这个渲染表单。"""
    return {
        "pack_version": itembank.PACK_VERSION,
        "dims": [{
            "name": name,
            "weight": cfg["weight"],
            "item_count": len(cfg["items"]),
            "items": [i["id"] for i in cfg["items"]],
        } for name, cfg in itembank.DIMENSIONS.items()],
    }


@app.get("/api/methods")
def list_methods() -> dict[str, Any]:
    """准入与压测的方法目录，前端用于展示准入口径与标签，不做业务计算。"""
    return test_catalog.method_catalog()


@app.get("/api/benchmarks")
def list_benchmarks() -> list[dict[str, Any]]:
    return [_bench_out(r) for r in
            store.query("SELECT * FROM benchmarks ORDER BY updated_at DESC")]


def _benchmark_snapshot(source_task_id: int) -> dict[str, Any]:
    task = _generic_task(source_task_id)
    report_data = store.task_report(task, None)
    capability = (report_data or {}).get("metrics", {}).get("capability")
    if not capability:
        raise HTTPException(status_code=400, detail="该任务没有完整的四维能力评测结果")
    if capability.get("truncated_unscored"):
        raise HTTPException(status_code=400, detail="该任务有截断题目，不能设为标杆")
    weak_coverage = [
        name for name, value in (capability.get("dims") or {}).items()
        if float(value.get("coverage") or 0) < 0.85
    ] if capability.get("coverage_enforced") else []
    if weak_coverage:
        raise HTTPException(
            status_code=400,
            detail=f"有效覆盖率不足：{'、'.join(weak_coverage)}",
        )
    dims = {name: value["score"] for name, value in capability["dims"].items()
            if value["score"] is not None}
    if not dims:
        raise HTTPException(status_code=400, detail="该任务所有维度都没测到分")
    raw_hard = (report_data or {}).get("metrics", {}).get("hard")
    hard = None
    if raw_hard:
        hard = {
            "hard_version": raw_hard.get("hard_version"),
            "rate": raw_hard.get("rate"), "correct": raw_hard.get("correct"),
            "graded": raw_hard.get("graded"),
            "banks": {key: {
                "rate": value.get("rate"), "correct": value.get("correct"),
                "graded": value.get("graded"), "name": value.get("name"),
            } for key, value in (raw_hard.get("banks") or {}).items()},
            "groups": {key: {
                "rate": value.get("rate"), "correct": value.get("correct"),
                "graded": value.get("graded"),
            } for key, value in (raw_hard.get("groups") or {}).items()},
        }
    snapshot = store.loads(task["snapshot"], {})
    return {
        "task": task, "model": snapshot.get("model", ""),
        "pack_version": capability.get("pack_version") or itembank.PACK_VERSION,
        "dims": dims, "items": capability.get("items") or {},
        "overall": capability.get("overall"), "hard": hard,
    }


@app.get("/api/targets/{target_id}/benchmark-candidates")
def benchmark_candidates(target_id: int) -> list[dict[str, Any]]:
    target = store.get("targets", target_id)
    if not target:
        raise HTTPException(status_code=404, detail="模型不存在")
    candidates = []
    for task in store.query(
        "SELECT * FROM tasks WHERE target_id=? AND kind IN ('admission','capability') "
        "AND report<>'' ORDER BY id DESC LIMIT 30", (target_id,),
    ):
        try:
            snapshot = _benchmark_snapshot(task["id"])
        except HTTPException:
            continue
        candidates.append({
            "task_id": task["id"], "model": snapshot["model"] or target["model"],
            "overall": snapshot["overall"],
            "pack_version": snapshot["pack_version"],
            "created_at": task["created_at"], "finished_at": task["finished_at"],
        })
    return candidates


@app.get("/api/platform-groups/{group_id}/benchmark-slots/{slot_id}/history")
def platform_benchmark_history(group_id: int, slot_id: int) -> list[dict[str, Any]]:
    slot = store.get("platform_group_benchmarks", slot_id)
    if not slot or slot["platform_group_id"] != group_id:
        raise HTTPException(status_code=404, detail="标杆槽位不存在")
    return [workbench.benchmark_out(row) for row in store.query(
        "SELECT * FROM benchmarks WHERE platform_group_id=? AND benchmark_model=? "
        "ORDER BY updated_at DESC", (group_id, slot["model"]))]


@app.put("/api/platform-groups/{group_id}/benchmark-slots/{slot_id}")
def set_platform_benchmark(
    group_id: int, slot_id: int, body: PlatformBenchmarkIn,
) -> dict[str, Any]:
    group = store.get("platform_groups", group_id)
    slot = store.get("platform_group_benchmarks", slot_id)
    if not group or not slot or slot["platform_group_id"] != group_id:
        raise HTTPException(status_code=404, detail="标杆槽位不存在")
    snapshot = _benchmark_snapshot(body.source_task_id)
    if snapshot["pack_version"] != itembank.PACK_VERSION:
        raise HTTPException(status_code=400, detail="任务题库版本已过期，不能设为当前标杆")
    source_task = snapshot["task"]
    source_channel = workbench.source_channel(source_task["id"]) or "渠道"
    platform = workbench.platform_group(group_id)
    assert platform is not None
    now = time.time()
    current_benchmark_id = slot["benchmark_id"]
    if current_benchmark_id:
        store.update("benchmarks", current_benchmark_id, {
            "superseded_at": now, "updated_at": now})
    benchmark_id = store.insert("benchmarks", {
        "name": f"{platform['label']} · {slot['model']} · {source_channel}",
        "pack_version": snapshot["pack_version"],
        "model_hint": snapshot["model"],
        "dims": store.dumps(snapshot["dims"]),
        "items": store.dumps(snapshot["items"]),
        "overall": snapshot["overall"], "tolerance": body.tolerance,
        "source": "task", "source_task_id": body.source_task_id,
        "hard": store.dumps(snapshot["hard"]) if snapshot["hard"] else "",
        "platform_group_id": group_id, "benchmark_model": slot["model"],
        "superseded_at": None,
        "note": f"由任务 #{body.source_task_id} 生成",
        "created_at": now, "updated_at": now,
    })
    store.update("platform_group_benchmarks", slot_id, {
        "benchmark_id": benchmark_id, "updated_at": now})
    updated = workbench.platform_group(group_id)
    assert updated is not None
    return {
        "platform_group": updated,
        "benchmark": next(model["benchmark"] for model in updated["models"]
                          if model["id"] == slot_id),
        "replaced_benchmark_id": current_benchmark_id,
    }


@app.delete("/api/platform-groups/{group_id}/benchmark-slots/{slot_id}")
def unset_platform_benchmark(group_id: int, slot_id: int) -> dict[str, Any]:
    slot = store.get("platform_group_benchmarks", slot_id)
    if not slot or slot["platform_group_id"] != group_id:
        raise HTTPException(status_code=404, detail="标杆槽位不存在")
    now = time.time()
    if slot["benchmark_id"]:
        store.update("benchmarks", slot["benchmark_id"], {
            "superseded_at": now, "updated_at": now})
    store.update("platform_group_benchmarks", slot_id, {
        "benchmark_id": None, "updated_at": now})
    return {"status": "unbound", "benchmark_id": slot["benchmark_id"]}


@app.post("/api/benchmarks")
def create_benchmark(body: BenchmarkIn) -> dict[str, Any]:
    """为模型-倍率组创建标杆，可从已完成任务生成或直接手填维度分。"""
    now = time.time()
    dims: dict[str, float] = {}
    items: dict[str, float] = {}
    overall: float | None = None
    pack_version = itembank.PACK_VERSION
    source = "manual"
    src_task = None
    hard: dict[str, Any] | None = None
    model_hint = body.model_hint
    group = store.get("groups", body.group_id) if body.group_id else None
    if body.group_id and not group:
        raise HTTPException(status_code=404, detail="模型-倍率组不存在")

    if body.source_task_id:
        row = _generic_task(body.source_task_id)
        target = store.get("targets", row["target_id"]) if row["target_id"] else None
        task_group_id = (target or {}).get("group_id")
        if group and task_group_id != group["id"]:
            raise HTTPException(status_code=400, detail="该任务不属于所选模型-倍率组")
        if not group and task_group_id:
            group = store.get("groups", int(task_group_id))
        rep = store.task_report(row, None)
        cap = (rep or {}).get("metrics", {}).get("capability")
        if not cap:
            raise HTTPException(status_code=400,
                                detail="该任务不是能力评测，或还没有评测结果")
        dims = {k: v["score"] for k, v in cap["dims"].items() if v["score"] is not None}
        if not dims:
            raise HTTPException(status_code=400, detail="该任务所有维度都没测到分，不能设为标杆")
        items = cap.get("items") or {}
        overall = cap.get("overall")
        pack_version = cap.get("pack_version") or pack_version
        source = "task"
        src_task = body.source_task_id
        model_hint = model_hint or (store.loads(row["snapshot"], {}) or {}).get("model", "")
        # 硬题分一起存进标杆。那一趟没跑硬题就留空，不编数据。
        # 只留汇总（总正确率 + 分库 + 分学科），逐题明细留在任务报告里。
        raw_hard = (rep or {}).get("metrics", {}).get("hard")
        if raw_hard:
            hard = {
                "hard_version": raw_hard.get("hard_version"),
                "rate": raw_hard.get("rate"),
                "correct": raw_hard.get("correct"),
                "graded": raw_hard.get("graded"),
                "banks": {k: {"rate": v.get("rate"), "correct": v.get("correct"),
                              "graded": v.get("graded"), "name": v.get("name")}
                          for k, v in (raw_hard.get("banks") or {}).items()},
                "groups": {k: {"rate": v.get("rate"), "correct": v.get("correct"),
                               "graded": v.get("graded")}
                           for k, v in (raw_hard.get("groups") or {}).items()},
            }
    elif body.dims:
        known = set(itembank.DIMENSIONS)
        unknown = [d for d in body.dims if d not in known]
        if unknown:
            raise HTTPException(status_code=400,
                                detail=f"未知维度：{'、'.join(unknown)}")
        dims = {k: max(0.0, min(1.0, float(v))) for k, v in body.dims.items()}
        overall = _weighted(dims)
    else:
        raise HTTPException(status_code=400,
                            detail="要么给 source_task_id，要么直接给 dims")

    if not group:
        raise HTTPException(status_code=400, detail="请先选择这份标杆所属的模型-倍率组")

    bid = store.insert("benchmarks", {
        "name": body.name, "pack_version": pack_version,
        "model_hint": model_hint,
        "dims": store.dumps(dims), "items": store.dumps(items),
        "overall": overall, "tolerance": body.tolerance,
        "source": source, "source_task_id": src_task,
        "hard": store.dumps(hard) if hard else "",
        "note": body.note, "created_at": now, "updated_at": now,
    })
    row = store.get("benchmarks", bid)
    assert row is not None
    store.update("groups", group["id"], {"benchmark_id": bid, "updated_at": now})
    return _bench_out(row)


def _weighted(dims: dict[str, float]) -> float | None:
    """按题库权重算总分，只在给了分的维度间归一化。"""
    num = den = 0.0
    for name, score in dims.items():
        w = itembank.DIMENSIONS.get(name, {}).get("weight", 0.0)
        num += score * w
        den += w
    return round(num / den, 4) if den else None


@app.patch("/api/benchmarks/{bench_id}")
def patch_benchmark(bench_id: int, body: BenchmarkPatch) -> dict[str, Any]:
    row = store.get("benchmarks", bench_id)
    if not row:
        raise HTTPException(status_code=404, detail="标杆不存在")
    patch: dict[str, Any] = {"updated_at": time.time()}
    if body.name is not None:
        patch["name"] = body.name
    if body.note is not None:
        patch["note"] = body.note
    if body.tolerance is not None:
        patch["tolerance"] = body.tolerance
    if body.dims is not None:
        known = set(itembank.DIMENSIONS)
        unknown = [d for d in body.dims if d not in known]
        if unknown:
            raise HTTPException(status_code=400,
                                detail=f"未知维度：{'、'.join(unknown)}")
        dims = {k: max(0.0, min(1.0, float(v))) for k, v in body.dims.items()}
        patch["dims"] = store.dumps(dims)
        patch["overall"] = _weighted(dims)
        patch["source"] = "manual"   # 手工改过就不再声称来自任务
    store.update("benchmarks", bench_id, patch)
    updated = store.get("benchmarks", bench_id)
    assert updated is not None
    return _bench_out(updated)


@app.delete("/api/benchmarks/{bench_id}")
def delete_benchmark(bench_id: int) -> dict[str, str]:
    if not store.get("benchmarks", bench_id):
        raise HTTPException(status_code=404, detail="标杆不存在")
    store.delete("benchmarks", bench_id)
    # 解绑引用它的渠道，避免留下悬空外键
    for t in store.query("SELECT id FROM targets WHERE benchmark_id=?", (bench_id,)):
        store.update("targets", t["id"], {"benchmark_id": None})
    for group in store.query("SELECT id FROM groups WHERE benchmark_id=?", (bench_id,)):
        store.update("groups", group["id"], {
            "benchmark_id": None, "updated_at": time.time()})
    return {"status": "deleted"}


@app.post("/api/targets/{target_id}/benchmark")
def bind_benchmark(target_id: int, body: BindBenchmarkIn) -> dict[str, Any]:
    """给渠道绑定默认标杆，之后跑能力评测不用每次选。"""
    row = store.get("targets", target_id)
    if not row:
        raise HTTPException(status_code=404, detail="渠道不存在")
    if body.benchmark_id and not store.get("benchmarks", body.benchmark_id):
        raise HTTPException(status_code=404, detail="标杆不存在")
    store.update("targets", target_id, {
        "benchmark_id": body.benchmark_id, "updated_at": time.time()})
    updated = store.get("targets", target_id)
    assert updated is not None
    return _target_out(updated)


# ---------- 模型分组（步骤 2、3） ----------

@app.get("/api/rate-groups")
def list_rate_groups() -> list[dict[str, Any]]:
    return store.query("SELECT * FROM rate_groups ORDER BY multiplier")


@app.post("/api/rate-groups")
def create_rate_group(body: RateGroupIn) -> dict[str, Any]:
    return _ensure_rate_group_value(body.multiplier)

def _group_out(row: dict[str, Any]) -> dict[str, Any]:
    bench = None
    if row["benchmark_id"]:
        b = store.get("benchmarks", int(row["benchmark_id"]))
        if b:
            bench = {
                "id": b["id"], "name": b["name"],
                "pack_version": b["pack_version"],
                "dims": store.loads(b["dims"], {}),
                "overall": b["overall"],
                "source_task_id": b["source_task_id"],
                "stale": b["pack_version"] != itembank.PACK_VERSION,
            }
    members = store.query(
        "SELECT id, name, model FROM targets WHERE group_id=? ORDER BY name",
        (row["id"],))
    return {
        "id": row["id"], "name": row["name"],
        "model": placement.model_group_model(row["name"], row["multiplier"]),
        "multiplier": row["multiplier"],
        "note": row["note"], "benchmark_id": row["benchmark_id"],
        "benchmark": bench,
        "ready": bool(bench and not bench["stale"]),
        "members": members, "member_count": len(members),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


@app.get("/api/groups")
def list_groups() -> list[dict[str, Any]]:
    return [_group_out(r) for r in
            store.query("SELECT * FROM groups ORDER BY multiplier DESC")]


@app.post("/api/groups")
def create_group(body: GroupIn) -> dict[str, Any]:
    model = placement.model_group_model(body.name, body.multiplier)
    existing = _find_model_rate_group(model, body.multiplier) if model else None
    if existing:
        return _group_out(existing)
    now = time.time()
    if body.benchmark_id and not store.get("benchmarks", body.benchmark_id):
        raise HTTPException(status_code=404, detail="标杆不存在")
    gid = store.insert("groups", {
        "name": placement.model_rate_group_name(model, body.multiplier) if model else body.name,
        "multiplier": body.multiplier,
        "benchmark_id": body.benchmark_id, "note": body.note,
        "created_at": now, "updated_at": now,
    })
    row = store.get("groups", gid)
    assert row is not None
    _ensure_rate_group_value(body.multiplier)
    return _group_out(row)


@app.patch("/api/groups/{group_id}")
def patch_group(group_id: int, body: GroupPatch) -> dict[str, Any]:
    row = store.get("groups", group_id)
    if not row:
        raise HTTPException(status_code=404, detail="分组不存在")
    name = body.name if body.name is not None else row["name"]
    multiplier = body.multiplier if body.multiplier is not None else row["multiplier"]
    model = placement.model_group_model(name, multiplier)
    conflict = _find_model_rate_group(model, multiplier) if model else None
    if conflict and conflict["id"] != group_id:
        raise HTTPException(status_code=409, detail="同模型同倍率分组已经存在")
    patch: dict[str, Any] = {
        "name": placement.model_rate_group_name(model, multiplier) if model else name,
        "multiplier": multiplier, "updated_at": time.time(),
    }
    if body.note is not None:
        patch["note"] = body.note
    if body.benchmark_id is not None:
        if body.benchmark_id and not store.get("benchmarks", body.benchmark_id):
            raise HTTPException(status_code=404, detail="标杆不存在")
        patch["benchmark_id"] = body.benchmark_id or None
    store.update("groups", group_id, patch)
    _ensure_rate_group_value(multiplier)
    updated = store.get("groups", group_id)
    assert updated is not None
    return _group_out(updated)


@app.put("/api/groups/{group_id}/benchmark")
def bind_group_benchmark(group_id: int, body: BindBenchmarkIn) -> dict[str, Any]:
    group = store.get("groups", group_id)
    if not group:
        raise HTTPException(status_code=404, detail="分组不存在")
    benchmark_id = body.benchmark_id
    if benchmark_id is not None and not store.get("benchmarks", benchmark_id):
        raise HTTPException(status_code=404, detail="标杆不存在")
    released_benchmark_id = group["benchmark_id"] if group["benchmark_id"] != benchmark_id else None
    now = time.time()
    with store.cursor() as cur:
        if benchmark_id is not None:
            cur.execute(
                "UPDATE groups SET benchmark_id=NULL,updated_at=? "
                "WHERE benchmark_id=? AND id<>?",
                (now, benchmark_id, group_id),
            )
        cur.execute(
            "UPDATE groups SET benchmark_id=?,updated_at=? WHERE id=?",
            (benchmark_id, now, group_id),
        )
    updated = store.get("groups", group_id)
    assert updated is not None
    return {
        "group": _group_out(updated),
        "released_benchmark_id": released_benchmark_id,
    }


def _purge_expired_deleted_groups() -> None:
    with store.cursor() as cur:
        cur.execute("DELETE FROM deleted_groups WHERE expires_at<=?", (time.time(),))


def _deleted_group_out(row: dict[str, Any]) -> dict[str, Any]:
    benchmark = store.get("benchmarks", row["benchmark_id"]) if row["benchmark_id"] else None
    member_ids = store.loads(row["member_ids"], [])
    members = []
    if member_ids:
        marks = ",".join("?" * len(member_ids))
        members = store.query(
            f"SELECT id,name,model,group_id FROM targets WHERE id IN ({marks}) ORDER BY name",
            tuple(member_ids),
        )
    return {
        "id": row["id"], "name": row["name"], "model": row["model"],
        "multiplier": row["multiplier"], "note": row["note"],
        "benchmark_id": row["benchmark_id"],
        "benchmark_name": (benchmark or {}).get("name"),
        "member_ids": member_ids, "members": members, "member_count": len(member_ids),
        "deleted_at": row["deleted_at"], "expires_at": row["expires_at"],
    }


@app.delete("/api/groups/{group_id}")
def delete_group(group_id: int, confirm_name: str) -> dict[str, Any]:
    group = store.get("groups", group_id)
    if not group:
        raise HTTPException(status_code=404, detail="分组不存在")
    if confirm_name != group["name"]:
        raise HTTPException(status_code=400, detail="确认名称与当前分组名称不一致")
    running = store.query(
        "SELECT tasks.id FROM tasks JOIN targets ON tasks.target_id=targets.id "
        "WHERE targets.group_id=? AND tasks.status IN ('queued','running')",
        (group_id,),
    )
    if running:
        raise HTTPException(status_code=409, detail="该分组仍有任务执行中，结束后再删除")
    members = store.query("SELECT id FROM targets WHERE group_id=? ORDER BY id", (group_id,))
    member_ids = [member["id"] for member in members]
    now = time.time()
    expires_at = now + GROUP_RECYCLE_DAYS * 86400
    with store.cursor() as cur:
        cur.execute(
            "INSERT INTO deleted_groups "
            "(id,name,model,multiplier,benchmark_id,note,member_ids,deleted_at,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (group_id, group["name"],
             placement.model_group_model(group["name"], group["multiplier"]),
             group["multiplier"], group["benchmark_id"], group["note"],
             store.dumps(member_ids), now, expires_at),
        )
        cur.execute(
            "UPDATE targets SET group_id=NULL,pool='ungrouped',updated_at=? WHERE group_id=?",
            (now, group_id),
        )
        cur.execute("DELETE FROM groups WHERE id=?", (group_id,))
    deleted = store.get("deleted_groups", group_id)
    assert deleted is not None
    return {"status": "deleted", "deleted_group": _deleted_group_out(deleted)}


@app.get("/api/deleted-groups")
def list_deleted_groups() -> list[dict[str, Any]]:
    _purge_expired_deleted_groups()
    return [_deleted_group_out(row) for row in store.query(
        "SELECT * FROM deleted_groups ORDER BY deleted_at DESC")]


@app.post("/api/deleted-groups/{group_id}/restore")
def restore_deleted_group(group_id: int) -> dict[str, Any]:
    _purge_expired_deleted_groups()
    snapshot = store.get("deleted_groups", group_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="可恢复分组不存在或已超过 30 天")
    member_ids = [int(value) for value in store.loads(snapshot["member_ids"], [])]
    now = time.time()
    with store.cursor() as cur:
        cur.execute(
            "SELECT * FROM groups WHERE ABS(multiplier-?) < 0.000000001 ORDER BY id",
            (snapshot["multiplier"],),
        )
        candidates = [dict(row) for row in cur.fetchall()]
        destination = next((row for row in candidates
                            if placement.model_group_key(row["name"], row["multiplier"])
                            == placement.model_group_key(snapshot["model"], snapshot["multiplier"])),
                           None)
        merged = destination is not None
        if destination is None:
            cur.execute(
                "INSERT INTO groups (id,name,multiplier,benchmark_id,note,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (snapshot["id"], snapshot["name"], snapshot["multiplier"],
                 None, snapshot["note"], now, now),
            )
            destination_id = snapshot["id"]
            destination_benchmark_id = None
        else:
            destination_id = destination["id"]
            destination_benchmark_id = destination["benchmark_id"]

        restored_member_ids: list[int] = []
        if member_ids:
            marks = ",".join("?" * len(member_ids))
            cur.execute(
                f"SELECT id FROM targets WHERE id IN ({marks}) AND group_id IS NULL",
                tuple(member_ids),
            )
            restored_member_ids = [int(row["id"]) for row in cur.fetchall()]
            if restored_member_ids:
                restore_marks = ",".join("?" * len(restored_member_ids))
                cur.execute(
                    f"UPDATE targets SET group_id=?,pool='grouped',updated_at=? "
                    f"WHERE id IN ({restore_marks}) AND group_id IS NULL",
                    (destination_id, now, *restored_member_ids),
                )

        benchmark_id = snapshot["benchmark_id"]
        benchmark_status = "none"
        if benchmark_id:
            cur.execute("SELECT id FROM benchmarks WHERE id=?", (benchmark_id,))
            benchmark_exists = cur.fetchone() is not None
            cur.execute("SELECT id FROM groups WHERE benchmark_id=?", (benchmark_id,))
            occupying = cur.fetchone()
            if not benchmark_exists:
                benchmark_status = "missing"
            elif destination_benchmark_id == benchmark_id:
                benchmark_status = "already_bound"
            elif occupying:
                benchmark_status = "in_use"
            elif destination_benchmark_id:
                benchmark_status = "destination_occupied"
            else:
                cur.execute(
                    "UPDATE groups SET benchmark_id=?,updated_at=? WHERE id=?",
                    (benchmark_id, now, destination_id),
                )
                benchmark_status = "restored"
        cur.execute("DELETE FROM deleted_groups WHERE id=?", (group_id,))

    restored = store.get("groups", destination_id)
    assert restored is not None
    skipped_member_ids = [value for value in member_ids if value not in restored_member_ids]
    return {
        "status": "merged" if merged else "restored",
        "group": _group_out(restored),
        "restored_member_ids": restored_member_ids,
        "skipped_member_ids": skipped_member_ids,
        "benchmark_status": benchmark_status,
    }


@app.delete("/api/deleted-groups/{group_id}")
def purge_deleted_group(group_id: int, confirm_name: str) -> dict[str, str]:
    snapshot = store.get("deleted_groups", group_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="回收站分组不存在")
    if confirm_name != snapshot["name"]:
        raise HTTPException(status_code=400, detail="确认名称与回收站分组名称不一致")
    store.delete("deleted_groups", group_id)
    return {"status": "purged"}


@app.get("/api/groups/{group_id}/candidates")
def golden_candidates(group_id: int) -> list[dict[str, Any]]:
    """步骤 3 的候选：该组已有模型的历史任务里，带完整四维能力分的记录。"""
    row = store.get("groups", group_id)
    if not row:
        raise HTTPException(status_code=404, detail="分组不存在")
    members = store.query("SELECT id FROM targets WHERE group_id=?", (group_id,))
    if not members:
        return []
    ids = [m["id"] for m in members]
    marks = ",".join("?" * len(ids))
    rows = store.query(
        f"SELECT * FROM tasks WHERE target_id IN ({marks}) "
        f"AND report != '' ORDER BY id DESC LIMIT 100", tuple(ids))
    out = []
    for t in rows:
        rep = store.task_report(t, None)
        cap = (rep or {}).get("metrics", {}).get("capability")
        if not cap or not cap.get("dims"):
            continue
        dims = {k: v["score"] for k, v in cap["dims"].items()
                if v["score"] is not None}
        if not dims:
            continue
        out.append({
            "task_id": t["id"], "kind": t["kind"],
            "target_id": t["target_id"], "target_name": t["target_name"],
            "pack_version": cap.get("pack_version"),
            "usable": cap.get("pack_version") == itembank.PACK_VERSION,
            "dims": dims, "overall": cap.get("overall"),
            "created_at": t["created_at"],
        })
    return out


@app.post("/api/groups/{group_id}/golden")
def set_golden(group_id: int, body: SetGoldenIn) -> dict[str, Any]:
    """步骤 3：把某次历史测试记录设为该组的黄金标杆。"""
    row = store.get("groups", group_id)
    if not row:
        raise HTTPException(status_code=404, detail="分组不存在")
    create_benchmark(BenchmarkIn(
        name=body.name or f"{row['name']} 黄金标杆",
        group_id=group_id,
        source_task_id=body.task_id,
        note=f"由任务 #{body.task_id} 设为 {row['name']} 的黄金标杆",
    ))
    updated = store.get("groups", group_id)
    assert updated is not None
    return _group_out(updated)


# ---------- 分组推荐（步骤 4、5、6） ----------

def _reco_out(row: dict[str, Any]) -> dict[str, Any]:
    tgt = store.get("targets", row["target_id"])
    return {
        "id": row["id"], "task_id": row["task_id"], "target_id": row["target_id"],
        "target_name": (tgt or {}).get("name", ""),
        "target_model": (tgt or {}).get("model", ""),
        "current_group_id": (tgt or {}).get("group_id"),
        "current_pool": (tgt or {}).get("pool", "ungrouped"),
        "status": row["status"],
        "result": store.loads(row["result"], {}),
        "suggested_group_id": row["suggested_group_id"],
        "decided_group_id": row["decided_group_id"],
        "decided_note": row["decided_note"],
        "created_at": row["created_at"], "decided_at": row["decided_at"],
    }


@app.get("/api/recommendations")
def list_recommendations(status: str | None = None,
                         target_id: int | None = None,
                         limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT * FROM recommendations WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if target_id:
        sql += " AND target_id=?"
        params.append(target_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    return [_reco_out(r) for r in store.query(sql, tuple(params))]


@app.get("/api/tasks/{task_id}/recommendation")
def task_recommendation(task_id: int) -> dict[str, Any]:
    """任务页要展示的推荐。没有就返回 null 结构，不报错。"""
    _generic_task(task_id)
    rows = store.query(
        "SELECT * FROM recommendations WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,))
    return _reco_out(rows[0]) if rows else {"id": None, "result": None}


@app.get("/api/tasks/{task_id}/benchmark-comparison")
def get_task_benchmark_comparison(task_id: int) -> dict[str, Any]:
    row = _generic_task(task_id)
    return {
        "task_id": task_id,
        "benchmark_id": row.get("selected_benchmark_id"),
        "result": store.loads(row.get("selected_benchmark_comparison"), None),
    }


@app.post("/api/tasks/{task_id}/benchmark-comparison")
def set_task_benchmark_comparison(
    task_id: int, body: BenchmarkCompareIn,
) -> dict[str, Any]:
    row = _generic_task(task_id)
    rep = store.task_report(row, None)
    if not rep:
        raise HTTPException(status_code=400, detail="该任务还没有报告")
    cap = (rep.get("metrics") or {}).get("capability")
    if not cap or not cap.get("dims"):
        raise HTTPException(status_code=400, detail="该任务没有四维能力评分，无法对比")
    bench_row = store.get("benchmarks", body.benchmark_id)
    if not bench_row:
        raise HTTPException(status_code=404, detail="标杆不存在")
    benchmark = {
        "id": bench_row["id"],
        "name": bench_row["name"],
        "pack_version": bench_row["pack_version"],
        "dims": store.loads(bench_row["dims"], {}),
        "overall": bench_row["overall"],
        "tolerance": bench_row["tolerance"],
    }
    cmp = placement.compare_capability_to_benchmark(cap, benchmark, strict=True)
    if not cmp:
        raise HTTPException(status_code=400, detail="该标杆缺少可比维度分")
    if not cmp.get("comparable"):
        raise HTTPException(status_code=400, detail=cmp.get("skip_reason", "标杆不可比"))

    store.update("tasks", task_id, {
        "selected_benchmark_id": body.benchmark_id,
        "selected_benchmark_comparison": store.dumps(cmp),
    })
    store.add_event(
        task_id, f"对比标杆：{bench_row['name']}", stage="对比", level="info")
    return {"task_id": task_id, "benchmark_id": body.benchmark_id, "result": cmp}


@app.post("/api/recommendations/{reco_id}/decide")
def decide(reco_id: int, body: DecideIn) -> dict[str, Any]:
    """步骤 6：人工定夺。系统只在这一步才真正改渠道的分组字段。"""
    row = store.get("recommendations", reco_id)
    if not row:
        raise HTTPException(status_code=404, detail="推荐记录不存在")
    if row["status"] != "pending":
        raise HTTPException(status_code=400, detail="该推荐已处理过")
    target = store.get("targets", row["target_id"])
    if not target:
        raise HTTPException(status_code=404, detail="渠道已删除")

    now = time.time()
    if not body.accept:
        store.update("recommendations", reco_id, {
            "status": "rejected", "decided_note": body.note, "decided_at": now})
        return _reco_out(store.get("recommendations", reco_id))  # type: ignore[arg-type]

    gid = body.group_id
    if row.get("target_platform_group_id") is not None:
        if gid is not None:
            group = store.get("platform_groups", gid)
            if not group or group["archived_at"] is not None:
                raise HTTPException(status_code=404, detail="平台倍率组不存在")
            allowed = store.query(
                "SELECT id FROM model_family_models WHERE family_id=? AND model=?",
                (group["family_id"], target["model"]),
            )
            if not allowed:
                raise HTTPException(status_code=400, detail="目标平台组不包含该模型")
            store.update("targets", row["target_id"], {
                "platform_group_id": gid, "pool": "grouped", "updated_at": now,
            })
        else:
            store.update("targets", row["target_id"], {
                "platform_group_id": None, "pool": "trial", "updated_at": now,
            })
        store.update("recommendations", reco_id, {
            "status": "accepted", "decided_platform_group_id": gid,
            "decided_note": body.note, "decided_at": now,
        })
        return _reco_out(store.get("recommendations", reco_id))  # type: ignore[arg-type]
    if gid is not None:
        if not store.get("groups", gid):
            raise HTTPException(status_code=404, detail="分组不存在")
        store.update("targets", row["target_id"], {
            "group_id": gid, "pool": "grouped", "updated_at": now})
    else:
        # 不指定分组 = 放入试玩池
        store.update("targets", row["target_id"], {
            "group_id": None, "pool": "trial", "updated_at": now})

    store.update("recommendations", reco_id, {
        "status": "accepted", "decided_group_id": gid,
        "decided_note": body.note, "decided_at": now})
    return _reco_out(store.get("recommendations", reco_id))  # type: ignore[arg-type]


@app.get("/api/placement-config")
def placement_config() -> dict[str, Any]:
    """前端展示判定口径用，避免阈值只写在代码里、界面上说不清。"""
    return {
        "qualify_ratio": placement.QUALIFY_RATIO,
        "weak_dim_ratio": placement.WEAK_DIM_RATIO,
        "strict_ability_ratio": placement.STRICT_ABILITY_RATIO,
        "timeout_rate_limit": advice.TIMEOUT_RATE_LIMIT,
        "stability_score_limit": STABILITY_SCORE_LIMIT,
        "pack_version": itembank.PACK_VERSION,
        "dim_order": itembank.DIM_ORDER,
        "weights": itembank.WEIGHTS,
    }


# ---------- 生产洞察：只读采集、健康、排行与供给缺口 ----------

@app.get("/api/insights/usage-profiles")
def list_usage_profiles() -> list[dict[str, Any]]:
    return store.query(
        "SELECT id,code,name,critical,description FROM usage_profiles ORDER BY id"
    )


def _usage_binding_out(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "secondary_profiles": store.loads(row["secondary_profiles"], [])}


@app.get("/api/insights/usage-bindings")
def list_usage_bindings() -> list[dict[str, Any]]:
    return [_usage_binding_out(row) for row in store.query(
        "SELECT * FROM usage_profile_bindings ORDER BY subject_type,subject_label,id"
    )]


@app.post("/api/insights/usage-bindings")
def upsert_usage_binding(
    body: UsageProfileBindingIn, request: Request,
) -> dict[str, Any]:
    subject_hash = anonymize_subject(body.subject_type, body.subject_id)
    secondary = list(dict.fromkeys(
        profile for profile in body.secondary_profiles
        if profile != body.primary_profile
    ))
    now = time.time()
    store.execute(
        "INSERT INTO usage_profile_bindings "
        "(subject_type,subject_hash,subject_label,primary_profile,secondary_profiles,"
        "note,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(subject_type,subject_hash) DO UPDATE SET "
        "subject_label=excluded.subject_label,primary_profile=excluded.primary_profile,"
        "secondary_profiles=excluded.secondary_profiles,note=excluded.note,"
        "updated_at=excluded.updated_at",
        (body.subject_type, subject_hash, body.subject_label.strip(),
         body.primary_profile, store.dumps(secondary), body.note.strip(),
         request.state.user["id"], now, now),
    )
    row = store.query(
        "SELECT * FROM usage_profile_bindings WHERE subject_type=? AND subject_hash=?",
        (body.subject_type, subject_hash),
    )[0]
    return _usage_binding_out(row)


@app.delete("/api/insights/usage-bindings/{binding_id}")
def delete_usage_binding(binding_id: int) -> dict[str, str]:
    if not store.get("usage_profile_bindings", binding_id):
        raise HTTPException(status_code=404, detail="用途绑定不存在")
    store.delete("usage_profile_bindings", binding_id)
    return {"status": "deleted"}


@app.get("/api/insights/usage-bindings/export")
def export_usage_bindings() -> dict[str, Any]:
    """中转站只读拉取匿名映射；不返回录入值、标签、备注或请求正文。"""
    rows = list_usage_bindings()
    return {"version": "1", "generated_at": time.time(), "bindings": [{
        "subject_type": row["subject_type"], "subject_hash": row["subject_hash"],
        "primary_profile": row["primary_profile"],
        "secondary_profiles": row["secondary_profiles"],
    } for row in rows]}


@app.get("/api/security/egress-policy")
def egress_policy() -> dict[str, Any]:
    return {
        "default": "仅公网 HTTP/HTTPS；内网、回环、链路本地、保留地址默认拒绝",
        "deployment_allowlist": list(egress.EGRESS_ALLOWLIST),
        "max_redirects": egress.EGRESS_MAX_REDIRECTS,
        "max_response_bytes": egress.EGRESS_MAX_RESPONSE_BYTES,
        "management": "由部署者通过 TEST_EGRESS_ALLOWLIST 修改，页面不开放动态放行",
    }


@app.get("/api/insights/sources")
def list_metric_sources() -> list[dict[str, Any]]:
    return [insights.source_out(row) for row in
            metric_store.query("SELECT * FROM metric_sources ORDER BY id")]


@app.post("/api/insights/sources")
def create_metric_source(body: MetricSourceIn) -> dict[str, Any]:
    try:
        endpoint = insights.validate_source_endpoint(body.endpoint)
    except (egress.EgressDenied, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    now = time.time()
    try:
        source_id = metric_store.insert("metric_sources", {
            "name": body.name.strip(), "endpoint": endpoint,
            "token_enc": encrypt(body.token) if body.token else "",
            "protocol_version": body.protocol_version, "cursor": "",
            "enabled": int(body.enabled),
            "poll_interval_seconds": body.poll_interval_seconds,
            "created_at": now, "updated_at": now,
        })
    except Exception as exc:
        raise HTTPException(status_code=409, detail="数据源名称已存在") from exc
    row = metric_store.get("metric_sources", source_id)
    assert row is not None
    return insights.source_out(row)


@app.patch("/api/insights/sources/{source_id}")
def update_metric_source(source_id: int, body: MetricSourcePatch) -> dict[str, Any]:
    current = metric_store.get("metric_sources", source_id)
    if not current:
        raise HTTPException(status_code=404, detail="指标数据源不存在")
    values = body.model_dump(exclude_unset=True)
    patch: dict[str, Any] = {"updated_at": time.time()}
    if "name" in values:
        patch["name"] = values["name"].strip()
    if "endpoint" in values:
        try:
            patch["endpoint"] = insights.validate_source_endpoint(values["endpoint"])
        except (egress.EgressDenied, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "token" in values:
        patch["token_enc"] = encrypt(values["token"]) if values["token"] else ""
    for field in ("protocol_version", "poll_interval_seconds"):
        if field in values:
            patch[field] = values[field]
    if "enabled" in values:
        patch["enabled"] = int(values["enabled"])
    try:
        metric_store.update("metric_sources", source_id, patch)
    except Exception as exc:
        raise HTTPException(status_code=409, detail="数据源名称已存在") from exc
    row = metric_store.get("metric_sources", source_id)
    assert row is not None
    return insights.source_out(row)


@app.delete("/api/insights/sources/{source_id}")
def delete_metric_source(source_id: int) -> dict[str, bool]:
    if not metric_store.get("metric_sources", source_id):
        raise HTTPException(status_code=404, detail="指标数据源不存在")
    count = metric_store.query(
        "SELECT COUNT(*) n FROM metric_buckets WHERE source_id=?", (source_id,)
    )[0]["n"]
    if count:
        raise HTTPException(status_code=409, detail="该数据源已有历史指标，请停用而不是删除")
    metric_store.delete("metric_sources", source_id)
    return {"deleted": True}


@app.post("/api/insights/sources/{source_id}/collect")
async def collect_metric_source(source_id: int) -> dict[str, Any]:
    try:
        return await insights.collect_source(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"指标采集失败：{str(exc)[:200]}") from exc


@app.post("/api/insights/sources/{source_id}/ingest")
def ingest_metric_payload(source_id: int, body: MetricPayloadIn) -> dict[str, Any]:
    source = metric_store.get("metric_sources", source_id)
    if not source:
        raise HTTPException(status_code=404, detail="指标数据源不存在")
    try:
        result = insights.ingest_payload(source, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    now = time.time()
    metric_store.update("metric_sources", source_id, {
        "cursor": result["next_cursor"], "last_attempt_at": now,
        "last_success_at": now, "last_error": "", "updated_at": now,
    })
    return result


def _insight_filters(
    platform_group: str = "", model_family: str = "", model: str = "",
    usage_profile: str = "", channel: str = "",
) -> dict[str, str]:
    return {"platform_group": platform_group, "model_family": model_family,
            "model": model, "usage_profile": usage_profile, "channel": channel}


@app.get("/api/insights/overview")
def production_overview(
    minutes: int = 5, platform_group: str = "", model_family: str = "",
    model: str = "", usage_profile: str = "", channel: str = "",
) -> dict[str, Any]:
    return insights.health_overview(
        _insight_filters(platform_group, model_family, model, usage_profile, channel),
        minutes,
    )


@app.get("/api/insights/trend")
def production_trend(
    hours: int = 24, platform_group: str = "", model_family: str = "",
    model: str = "", usage_profile: str = "", channel: str = "",
) -> list[dict[str, Any]]:
    return insights.trend(
        _insight_filters(platform_group, model_family, model, usage_profile, channel), hours,
    )


@app.get("/api/insights/rankings")
def production_rankings(days: int = 7) -> dict[str, Any]:
    return insights.rankings(days)


@app.get("/api/insights/supply-gaps")
def supply_gap_recommendations() -> list[dict[str, Any]]:
    return insights.list_supply_gaps()


@app.post("/api/insights/supply-gaps/refresh")
def refresh_supply_gap_recommendations() -> list[dict[str, Any]]:
    return insights.refresh_supply_gaps()


@app.get("/api/insights/recommendation-reviews")
def recommendation_reviews() -> list[dict[str, Any]]:
    return insights.list_recommendation_reviews()


@app.post("/api/insights/supply-gaps/{recommendation_id}/reviews")
def create_recommendation_review(
    recommendation_id: int, body: RecommendationReviewIn, request: Request,
) -> dict[str, Any]:
    try:
        return insights.create_recommendation_review(
            recommendation_id, body.channel_id, body.production_channel,
            body.activated_at, request.state.user["id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=409, detail="该建议与渠道已经建立复盘") from exc
        raise


@app.post("/api/insights/recommendation-reviews/{review_id}/run")
def run_recommendation_review(review_id: int, horizon_days: int) -> dict[str, Any]:
    try:
        return insights.run_recommendation_review(review_id, horizon_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/insights/retention/run")
def run_metric_retention() -> dict[str, Any]:
    return insights.run_retention()


# ---------- 监测告警：签名接入、证据与保守归因 ----------

@app.get("/api/incidents/sources")
def list_monitor_sources() -> list[dict[str, Any]]:
    return [incidents.source_out(row) for row in
            store.query("SELECT * FROM monitor_sources ORDER BY name,id")]


@app.post("/api/incidents/sources")
def create_monitor_source(body: MonitorSourceIn) -> dict[str, Any]:
    try:
        return incidents.create_source(body.name, body.secret, body.enabled)
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=409, detail="监测告警源名称已经存在") from exc
        raise


@app.delete("/api/incidents/sources/{source_id}")
def delete_monitor_source(source_id: int) -> dict[str, str]:
    source = store.get("monitor_sources", source_id)
    if not source:
        raise HTTPException(status_code=404, detail="监测告警源不存在")
    linked = store.query(
        "SELECT COUNT(*) n FROM incidents WHERE source_id=?", (source_id,)
    )[0]["n"]
    if linked:
        store.update("monitor_sources", source_id, {
            "enabled": 0, "updated_at": time.time(),
        })
        return {"status": "archived"}
    store.delete("monitor_sources", source_id)
    return {"status": "deleted"}


@app.post("/api/incidents/webhook/{source_id}")
async def monitor_alert_webhook(
    source_id: int, request: Request, background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    source = store.get("monitor_sources", source_id)
    if not source:
        raise HTTPException(status_code=404, detail="监测告警源不存在")
    body = await request.body()
    try:
        incidents.verify_signature(
            source, request.headers.get("x-monitor-timestamp", ""),
            request.headers.get("x-monitor-signature", ""), body,
        )
    except incidents.IncidentError as exc:
        store.update("monitor_sources", source_id, {
            "last_error": str(exc), "updated_at": time.time(),
        })
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    try:
        alert = MonitorAlertIn.model_validate_json(body).model_dump()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="告警结构不符合协议") from exc
    try:
        incident, duplicate = incidents.ingest_alert(source_id, alert)
    except incidents.IncidentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not duplicate:
        background_tasks.add_task(incidents.collect_evidence, incident["id"])
    return {
        "status": "duplicate" if duplicate else "accepted",
        "incident_id": incident["id"], "merged_alert_count": incident["alert_count"],
    }


@app.get("/api/incidents")
def list_incidents(status: str = "") -> list[dict[str, Any]]:
    sql = "SELECT * FROM incidents"
    params: tuple[Any, ...] = ()
    if status:
        sql += " WHERE status=?"
        params = (status,)
    return [incidents.incident_out(row) for row in
            store.query(sql + " ORDER BY last_seen_at DESC,id DESC", params)]


@app.get("/api/incidents/{incident_id:int}")
def get_incident(incident_id: int) -> dict[str, Any]:
    try:
        return incidents.incident_detail(incident_id)
    except incidents.IncidentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/incidents/{incident_id:int}/collect")
async def recollect_incident(incident_id: int) -> dict[str, Any]:
    if not store.get("incidents", incident_id):
        raise HTTPException(status_code=404, detail="异常事件不存在")
    await incidents.collect_evidence(incident_id)
    return incidents.incident_detail(incident_id)


@app.get("/api/incidents/probe-locations")
def list_probe_locations() -> list[dict[str, Any]]:
    return [incidents.location_out(row) for row in store.query(
        "SELECT * FROM incident_probe_locations ORDER BY location_type,name,id")]


@app.post("/api/incidents/probe-locations")
def create_probe_location(body: IncidentProbeLocationIn) -> dict[str, Any]:
    try:
        return incidents.create_location(body.model_dump())
    except (incidents.IncidentError, egress.EgressDenied, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=409, detail="探针位置名称已经存在") from exc
        raise


@app.delete("/api/incidents/probe-locations/{location_id}")
def delete_probe_location(location_id: int) -> dict[str, str]:
    row = store.get("incident_probe_locations", location_id)
    if not row:
        raise HTTPException(status_code=404, detail="探针位置不存在")
    history = store.query(
        "SELECT COUNT(*) n FROM probe_runs WHERE location_id=?", (location_id,)
    )[0]["n"]
    if history:
        store.update("incident_probe_locations", location_id, {
            "enabled": 0, "updated_at": time.time(),
        })
        return {"status": "archived"}
    store.delete("incident_probe_locations", location_id)
    return {"status": "deleted"}


# ---------- 本地压力执行器：一次性配对、主动领取和签名回传 ----------

@app.get("/api/local-runners")
def list_local_runners() -> list[dict[str, Any]]:
    return [local_runners.runner_out(row) for row in
            store.query("SELECT * FROM paired_runners ORDER BY created_at DESC,id DESC")]


@app.post("/api/local-runners/pairing-codes")
def create_runner_pairing_code(
    body: RunnerPairingCodeIn, request: Request,
) -> dict[str, Any]:
    return local_runners.create_pairing_code(body.name, request.state.user["id"])


@app.delete("/api/local-runners/{runner_id}")
def revoke_local_runner(runner_id: int) -> dict[str, str]:
    row = store.get("paired_runners", runner_id)
    if not row:
        raise HTTPException(status_code=404, detail="本地执行器不存在")
    active = store.query(
        "SELECT COUNT(*) n FROM runner_jobs WHERE runner_id=? "
        "AND status IN ('queued','claimed','cancel_requested')", (runner_id,)
    )[0]["n"]
    if active:
        raise HTTPException(status_code=409, detail="执行器仍有未结束任务，不能撤销")
    store.update("paired_runners", runner_id, {
        "status": "revoked", "revoked_at": time.time(), "updated_at": time.time(),
    })
    return {"status": "revoked"}


@app.get("/api/local-runners/jobs")
def list_local_runner_jobs() -> list[dict[str, Any]]:
    return [local_runners.job_out(row) for row in
            store.query("SELECT * FROM runner_jobs ORDER BY created_at DESC,id DESC LIMIT 200")]


def _runner_agent(request: Request, runner_id: int) -> dict[str, Any]:
    try:
        return local_runners.authenticate(
            runner_id, request.headers.get("authorization", "")
        )
    except local_runners.RunnerError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/runner-agent/pair")
def pair_local_runner(body: RunnerPairIn) -> dict[str, Any]:
    try:
        return local_runners.pair(
            body.pairing_code, body.encryption_public_key,
            body.signing_public_key, body.version, body.capabilities,
        )
    except local_runners.RunnerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runner-agent/{runner_id}/heartbeat")
def local_runner_heartbeat(
    runner_id: int, body: RunnerHeartbeatIn, request: Request,
) -> dict[str, Any]:
    runner_row = _runner_agent(request, runner_id)
    return local_runners.heartbeat(runner_row, body.version, body.capabilities)


@app.post("/api/runner-agent/{runner_id}/poll")
def local_runner_poll(runner_id: int, request: Request) -> dict[str, Any]:
    runner_row = _runner_agent(request, runner_id)
    return {"job": local_runners.poll(runner_row),
            "poll_after_seconds": 5,
            "minimum_version": local_runners.MIN_RUNNER_VERSION}


@app.get("/api/runner-agent/{runner_id}/jobs/{job_id}")
def local_runner_job_status(
    runner_id: int, job_id: int, request: Request,
) -> dict[str, Any]:
    runner_row = _runner_agent(request, runner_id)
    try:
        return local_runners.runner_job_status(runner_row, job_id)
    except local_runners.RunnerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/runner-agent/{runner_id}/results")
def local_runner_result(
    runner_id: int, body: RunnerResultIn, request: Request,
) -> dict[str, Any]:
    runner_row = _runner_agent(request, runner_id)
    try:
        return local_runners.submit_result(
            runner_row, body.job_id, body.status, body.report,
            body.telemetry, body.signature,
        )
    except local_runners.RunnerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------- 人工上线、只读核验与飞书结构化同步 ----------

@app.get("/api/launches")
def list_channel_launches() -> list[dict[str, Any]]:
    return [external_sync.launch_out(row) for row in
            store.query("SELECT * FROM channel_launches ORDER BY updated_at DESC,id DESC")]


@app.get("/api/channels/{channel_id}/launch")
def get_channel_launch(channel_id: int) -> dict[str, Any]:
    try:
        return external_sync.ensure_launch(channel_id)
    except external_sync.SyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/channels/{channel_id}/launch/confirm")
def confirm_channel_launch(
    channel_id: int, body: ChannelLaunchConfirmIn, request: Request,
) -> dict[str, Any]:
    try:
        return external_sync.confirm_launch(
            channel_id, request.state.user, body.owner_note
        )
    except (external_sync.SyncError, lifecycle.LifecycleError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/online-verification/sources")
def list_online_verification_sources() -> list[dict[str, Any]]:
    return [external_sync.source_out(row) for row in store.query(
        "SELECT * FROM online_verification_sources ORDER BY name,id")]


@app.post("/api/online-verification/sources")
def create_online_verification_source(
    body: OnlineVerificationSourceIn,
) -> dict[str, Any]:
    try:
        return external_sync.create_source(body.model_dump())
    except (external_sync.SyncError, egress.EgressDenied) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=409, detail="上线核验数据源名称已经存在") from exc
        raise


@app.delete("/api/online-verification/sources/{source_id}")
def delete_online_verification_source(source_id: int) -> dict[str, str]:
    source = store.get("online_verification_sources", source_id)
    if not source:
        raise HTTPException(status_code=404, detail="上线核验数据源不存在")
    used = store.query(
        "SELECT COUNT(*) n FROM channel_launches WHERE verification_source_id=?",
        (source_id,),
    )[0]["n"]
    if used:
        store.update("online_verification_sources", source_id, {
            "enabled": 0, "updated_at": time.time(),
        })
        return {"status": "archived"}
    store.delete("online_verification_sources", source_id)
    return {"status": "deleted"}


@app.post("/api/channels/{channel_id}/launch/verify")
async def verify_channel_online(channel_id: int, source_id: int) -> dict[str, Any]:
    try:
        return await external_sync.verify_online(channel_id, source_id)
    except (external_sync.SyncError, lifecycle.LifecycleError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/external-sync/feishu/settings")
def get_feishu_bitable_settings() -> dict[str, Any] | None:
    return external_sync.feishu_settings_out(store.get("feishu_bitable_settings", 1))


@app.put("/api/external-sync/feishu/settings")
def put_feishu_bitable_settings(
    body: FeishuBitableSettingsIn,
) -> dict[str, Any]:
    try:
        return external_sync.save_feishu_settings(body.model_dump())
    except external_sync.SyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/channels/{channel_id}/external-sync/feishu/preview")
async def preview_channel_feishu_sync(channel_id: int) -> dict[str, Any]:
    try:
        return await external_sync.preview_sync(channel_id)
    except external_sync.SyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/external-sync/jobs")
def list_external_sync_jobs() -> list[dict[str, Any]]:
    return [external_sync.sync_job_out(row) for row in store.query(
        "SELECT * FROM external_sync_jobs ORDER BY created_at DESC,id DESC LIMIT 200")]


@app.post("/api/external-sync/jobs/{job_id}/confirm")
def confirm_external_sync_job(job_id: int, request: Request) -> dict[str, Any]:
    try:
        return external_sync.confirm_sync(job_id, request.state.user["id"])
    except external_sync.SyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/external-sync/jobs/{job_id}/run")
async def run_external_sync_job(job_id: int) -> dict[str, Any]:
    try:
        return await external_sync.execute_sync(job_id)
    except external_sync.SyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------- 双端配对相对准入 ----------

def _paired_scope(
    task_id: int,
) -> tuple[dict[str, Any], dict[str, Any], int | None]:
    paired = store.get("paired_tasks", task_id, key="task_id")
    task = store.get("tasks", task_id)
    if not paired or not task:
        raise HTTPException(status_code=404, detail="双端准入任务不存在")
    target = store.get("targets", paired["candidate_target_id"])
    snapshot = store.loads(task.get("snapshot"), {})
    channel_id = (target or {}).get("channel_id")
    if channel_id is None:
        channel_id = (snapshot.get("candidate") or {}).get("channel_id")
    return paired, task, channel_id


def _paired_permissions(
    user: dict[str, Any], paired: dict[str, Any], channel_id: int | None,
) -> dict[str, bool]:
    task_id = int(paired["task_id"])
    def has(role: str) -> bool:
        granted = paired_access.has_role(
            user["id"], role, channel_id=channel_id, task_id=task_id
        )
        return granted or (role != "raw_export" and paired_access.has_role(
            user["id"], "admin", channel_id=channel_id, task_id=task_id
        ))
    operator = has("operator")
    creator_operator = paired.get("created_by") == user["id"] and operator
    viewer = has("viewer")
    fidelity_reviewer = has("fidelity_reviewer")
    admission_reviewer = has("admission_reviewer")
    admin = has("admin")
    raw_export = has("raw_export")
    report = creator_operator or viewer or admission_reviewer or admin
    raw = creator_operator or fidelity_reviewer or admission_reviewer or admin
    return {
        "state": report or operator or fidelity_reviewer,
        "report": report,
        "raw": raw,
        "full_raw_export": raw_export,
        "fidelity_only": fidelity_reviewer and not (
            creator_operator or admission_reviewer or admin
        ),
        "can_view_raw": raw,
        "can_export_full_raw": raw and raw_export,
        "can_decide_fidelity": fidelity_reviewer,
        "can_start_or_cancel_or_rerun": operator,
        "can_submit_conclusion": admission_reviewer,
        "can_recalculate": admin,
        "can_extend_retention": admin,
    }


def _paired_integrity(task_id: int, paired: dict[str, Any]) -> dict[str, Any]:
    return paired_admission.verify_integrity(task_id, paired)


def _paired_response(task_id: int, user: dict[str, Any]) -> dict[str, Any]:
    paired, _, channel_id = _paired_scope(task_id)
    permissions = _paired_permissions(user, paired, channel_id)
    if not permissions["state"]:
        raise HTTPException(status_code=403, detail="没有该双端任务的查看权限")
    result = paired_admission.get(task_id)
    result["permissions"] = {
        key: allowed for key, allowed in permissions.items() if key.startswith("can_")
    }
    result["task"] = {
        key: value for key, value in result["task"].items()
        if key not in {"report", "snapshot", "progress"}
    }
    integrity = result.get("integrity") or {"ok": True, "status": "collecting"}
    if paired["state"] in paired_admission.TERMINAL_STATES:
        integrity = _paired_integrity(task_id, paired)
    result["integrity"] = integrity
    raw_expired = float(paired["raw_expires_at"]) <= time.time() or bool(store.query(
        "SELECT 1 FROM paired_raw_blocks WHERE task_id=? AND deleted_at IS NOT NULL LIMIT 1",
        (task_id,),
    ))
    result["raw_evidence"] = {
        "status": "expired" if raw_expired else "available",
        "expires_at": paired["raw_expires_at"],
    }
    if not permissions["report"]:
        result["report"] = None
        result["conclusion"] = None
    elif not integrity["ok"]:
        result["report"] = None
        result["report_blocked_reason"] = "evidence_integrity_failed"
    return result


def _paired_summary(
    paired: dict[str, Any], task: dict[str, Any], user: dict[str, Any],
    channel_id: int | None,
) -> dict[str, Any]:
    permissions = _paired_permissions(user, paired, channel_id)
    integrity_status = str(paired.get("integrity_status") or "")
    if not integrity_status:
        integrity_status = "pending_verification" \
            if paired["state"] in paired_admission.TERMINAL_STATES else "collecting"
    return {
        "task": {
            key: value for key, value in task.items()
            if key not in {"report", "snapshot", "progress"}
        },
        "paired": {
            key: value for key, value in paired.items()
            if key != "secure_snapshot_ciphertext"
        },
        "snapshot": store.loads(task.get("snapshot"), {}),
        "progress": store.loads(task.get("progress"), {}),
        "integrity": {
            "ok": True if integrity_status == "verified"
            else False if integrity_status == "failed" else None,
            "status": integrity_status,
            "reason": paired.get("integrity_error") or "",
        },
        "permissions": {
            key: allowed for key, allowed in permissions.items() if key.startswith("can_")
        },
    }


def _paired_command_response(
    task_id: int, user: dict[str, Any], command_result: dict[str, Any],
) -> dict[str, Any]:
    result = _paired_response(task_id, user)
    result["command_replayed"] = bool(command_result.get("command_replayed"))
    return result


def _fidelity_evidence(
    paired: dict[str, Any], records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    fidelity_end = next((
        record["record_seq"] for record in records
        if record["record_type"] == "state_transition"
        and record["payload"].get("to") == "awaiting_fidelity_truth"
    ), None)
    if fidelity_end is not None:
        return [
            record for record in records
            if record["record_seq"] <= fidelity_end
            or record["record_type"] == "fidelity_human_decision"
        ]
    if paired["state"] in {"queued", "fidelity", "awaiting_fidelity_truth"} \
            and not paired.get("benchmark_target_id"):
        return records
    return [
        record for record in records
        if record["record_type"] in {"task_snapshot", "fidelity_truth_reused"}
    ]


def _configuration_row(configuration_id: int) -> dict[str, Any]:
    configuration = store.get("channel_configurations", configuration_id)
    if not configuration:
        raise HTTPException(status_code=404, detail="精确连接配置不存在")
    return configuration


def _configuration_has_role(user: dict[str, Any], role: str, configuration: dict[str, Any]) -> bool:
    channel_id = int(configuration["legacy_channel_id"])
    if paired_access.has_role(user["id"], "admin", channel_id=channel_id):
        return True
    if role == "viewer":
        return any(
            paired_access.has_role(user["id"], candidate, channel_id=channel_id)
            for candidate in ("viewer", "operator", "fidelity_reviewer", "admission_reviewer")
        )
    return paired_access.has_role(user["id"], role, channel_id=channel_id)


def _require_configuration_role(request: Request, role: str, configuration_id: int) -> dict[str, Any]:
    configuration = _configuration_row(configuration_id)
    user = request.state.user
    if not _configuration_has_role(user, role, configuration):
        raise HTTPException(status_code=403, detail=f"缺少 {role} 授权")
    return user


def _configuration_permissions(user: dict[str, Any], configuration_id: int) -> dict[str, bool]:
    configuration = _configuration_row(configuration_id)
    return {
        "edit_presentation": _configuration_has_role(user, "operator", configuration),
        "create_fidelity": _configuration_has_role(user, "operator", configuration),
        "create_admission_batch": _configuration_has_role(user, "operator", configuration),
        "mark_online": _configuration_has_role(user, "admin", configuration),
        "view_batch": _configuration_has_role(user, "viewer", configuration),
        "review_fidelity": _configuration_has_role(user, "fidelity_reviewer", configuration),
    }


def _with_configuration_permissions(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    for configuration in payload.get("configurations", []):
        configuration["permissions"] = _configuration_permissions(user, configuration["id"])
    return payload


def _with_batch_permissions(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    configuration_id = payload.get("configuration_id")
    if not configuration_id and payload.get("batch"):
        configuration_id = payload["batch"].get("configuration_id")
    if not configuration_id:
        return payload
    configuration = _configuration_row(int(configuration_id))
    permissions = {
        "can_conclude": _configuration_has_role(user, "admission_reviewer", configuration),
        "can_continue": _configuration_has_role(user, "admission_reviewer", configuration),
    }
    if payload.get("batch") is not None:
        payload["batch"]["permissions"] = permissions
    else:
        payload["permissions"] = permissions
    return payload


@app.get("/api/channel-configurations")
def list_channel_configurations(
    request: Request, eligible_for_admission: bool = False,
) -> dict[str, Any]:
    user = request.state.user
    data = channel_configurations.list_configurations(
        eligible_for_admission=eligible_for_admission
    )
    visible = [
        item for item in data["configurations"]
        if _configuration_has_role(user, "viewer", _configuration_row(item["id"]))
    ]
    data["configurations"] = visible
    data["summary"] = {
        "channels": len({item["channel_name"] for item in visible}),
        "configurations": len(visible),
        "pending_test": sum(item["business_status"] == "pending_test" for item in visible),
        "online": sum(item["business_status"] == "online" for item in visible),
        "attention": sum(item["attention_count"] for item in visible),
    }
    return _with_configuration_permissions(data, user)


@app.get("/api/channel-configurations/{configuration_id}")
def get_channel_configuration(configuration_id: int, request: Request) -> dict[str, Any]:
    user = _require_configuration_role(request, "viewer", configuration_id)
    result = channel_configurations.get_configuration(configuration_id)
    result["permissions"] = _configuration_permissions(user, configuration_id)
    return result


@app.post("/api/channel-configurations")
def create_channel_configuration(
    body: ChannelConfigurationIn, request: Request,
) -> dict[str, Any]:
    user = request.state.user
    if not (
        paired_access.has_role(user["id"], "operator")
        or paired_access.has_role(user["id"], "admin")
    ):
        raise HTTPException(status_code=403, detail="创建精确连接配置需要全局 operator 或 admin 授权")
    created = channel_configurations.create_configuration(body.model_dump(), user)
    created["permissions"] = _configuration_permissions(user, created["id"])
    return created


@app.patch("/api/channel-configurations/{configuration_id}")
def update_channel_configuration_presentation(
    configuration_id: int, body: ChannelConfigurationPresentationIn, request: Request,
) -> dict[str, Any]:
    user = _require_configuration_role(request, "operator", configuration_id)
    result = channel_configurations.update_presentation(
        configuration_id, body.display_name, body.note, user,
    )
    result["permissions"] = _configuration_permissions(user, configuration_id)
    return result


@app.post("/api/channel-configurations/{configuration_id}/connectivity-checks")
async def test_channel_configuration_connectivity(
    configuration_id: int, request: Request,
) -> dict[str, Any]:
    user = _require_configuration_role(request, "operator", configuration_id)
    return await channel_configurations.ordinary_connectivity_check(configuration_id, user)


@app.post("/api/channel-configurations/{configuration_id}/business-status")
def update_channel_configuration_business_status(
    configuration_id: int, body: ConfigurationBusinessStatusIn, request: Request,
) -> dict[str, Any]:
    user = _require_configuration_role(request, "admin", configuration_id)
    if body.to_status == "online":
        if body.reason_code != "external_business_verified":
            raise HTTPException(status_code=400, detail="标记已上线必须确认该配置已在外部业务稳定使用")
        return channel_configurations.start_online_check(
            configuration_id, user, reason_code=body.reason_code, note=body.note,
        )
    return channel_configurations.change_business_status(
        configuration_id, body.to_status, body.reason_code, body.note, user,
    )


@app.post("/api/channel-configurations/{configuration_id}/online-checks/cancel")
def cancel_channel_configuration_online_check(
    configuration_id: int, request: Request,
) -> dict[str, Any]:
    user = _require_configuration_role(request, "admin", configuration_id)
    return channel_configurations.cancel_online_check(configuration_id, user)


@app.post("/api/channel-configurations/{configuration_id}/fidelity-tasks")
async def create_configuration_fidelity_task(
    configuration_id: int, request: Request,
) -> dict[str, Any]:
    user = _require_configuration_role(request, "operator", configuration_id)
    task_id = channel_configurations.create_fidelity_task(configuration_id, user)
    paired = store.get("paired_tasks", task_id, key="task_id")
    if paired and paired["state"] == "queued":
        await runner.submit(task_id)
    return {"task_id": task_id, "configuration_id": configuration_id}


@app.post("/api/channel-configurations/fidelity-truths/{truth_id}/revoke")
def revoke_configuration_fidelity_truth(
    truth_id: int, body: FidelityTruthRevokeIn, request: Request,
) -> dict[str, Any]:
    truth = store.get("configuration_fidelity_truths", truth_id)
    if not truth:
        raise HTTPException(status_code=404, detail="有效精确配置保真定义不存在")
    user = _require_configuration_role(request, "fidelity_reviewer", int(truth["configuration_id"]))
    return channel_configurations.revoke_fidelity_truth(truth_id, body.reason, user)


@app.get("/api/admission-batches/preview")
def get_admission_batch_preview(configuration_id: int, request: Request) -> dict[str, Any]:
    user = _require_configuration_role(request, "viewer", configuration_id)
    return _with_batch_permissions(channel_configurations.batch_preview(configuration_id), user)


@app.post("/api/admission-batches")
async def create_admission_batch(
    body: AdmissionBatchIn, request: Request,
) -> dict[str, Any]:
    if not body.scale_confirmed:
        raise HTTPException(status_code=400, detail="启动前必须确认请求规模预览")
    user = _require_configuration_role(request, "operator", body.configuration_id)
    paired_admission.require_ready()
    return await channel_configurations.create_batch(
        body.configuration_id,
        [selection.model_dump() for selection in body.benchmark_selections],
        user,
    )


@app.get("/api/admission-batches/{batch_id}")
def get_admission_batch(batch_id: int, request: Request) -> dict[str, Any]:
    batch = store.get("admission_batches", batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="准入批次不存在")
    user = _require_configuration_role(request, "viewer", int(batch["configuration_id"]))
    return _with_batch_permissions(channel_configurations.batch_out(batch_id, refresh=True), user)


@app.post("/api/admission-batches/{batch_id}/conclusion")
def submit_admission_batch_conclusion(
    batch_id: int, body: AdmissionBatchConclusionIn, request: Request,
) -> dict[str, Any]:
    batch = store.get("admission_batches", batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="准入批次不存在")
    user = _require_configuration_role(request, "admission_reviewer", int(batch["configuration_id"]))
    return _with_batch_permissions(channel_configurations.submit_batch_conclusion(
        batch_id, body.verdict, body.reason, body.evidence_refs, body.report_versions,
        body.expected_conclusion_version, user,
    ), user)


@app.post("/api/admission-batches/{batch_id}/continue")
async def continue_admission_batch(
    batch_id: int, body: AdmissionBatchContinueIn, request: Request,
) -> dict[str, Any]:
    batch = store.get("admission_batches", batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="准入批次不存在")
    user = _require_configuration_role(request, "admission_reviewer", int(batch["configuration_id"]))
    paired_admission.require_ready()
    return _with_batch_permissions(
        await channel_configurations.continue_batch(batch_id, body.models, body.reason, user), user,
    )


@app.get("/api/paired-admission/readiness")
def paired_readiness() -> dict[str, Any]:
    return paired_admission.readiness()


@app.get("/api/paired-admission/estimate")
def paired_estimate(candidate_target_id: int, request: Request) -> dict[str, Any]:
    target = store.get("targets", candidate_target_id)
    if not target:
        raise HTTPException(status_code=404, detail="候选渠道不存在")
    paired_access.require(request, "operator", channel_id=target.get("channel_id"))
    try:
        estimate_result = paired_admission.estimate(candidate_target_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"候选渠道配置不可用：{exc}") from exc
    estimate_result["readiness"] = paired_admission.readiness()
    return estimate_result


@app.post("/api/paired-admission/tasks")
async def create_paired_task(body: PairedTaskIn, request: Request) -> dict[str, Any]:
    target = store.get("targets", body.candidate_target_id)
    if not target:
        raise HTTPException(status_code=404, detail="候选渠道不存在")
    user = paired_access.require(
        request, "operator", channel_id=target.get("channel_id")
    )
    created = paired_admission.create(
        body.candidate_target_id, user, idempotency_key=body.idempotency_key
    )
    if created["paired"]["state"] == "queued" and not created.get("command_replayed"):
        await runner.submit(created["task"]["id"])
    return _paired_command_response(created["task"]["id"], user, created)


@app.get("/api/paired-admission/tasks")
def list_paired_tasks(
    request: Request, state: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    sql = "SELECT task_id FROM paired_tasks"
    params: list[Any] = []
    if state:
        sql += " WHERE state=?"
        params.append(state)
    sql += " ORDER BY task_id DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    output: list[dict[str, Any]] = []
    for row in store.query(sql, tuple(params)):
        paired, task, channel_id = _paired_scope(row["task_id"])
        if _paired_permissions(request.state.user, paired, channel_id)["state"]:
            output.append(
                _paired_summary(paired, task, request.state.user, channel_id)
            )
    return output


@app.get("/api/paired-admission/tasks/{task_id}")
def get_paired_task(task_id: int, request: Request) -> dict[str, Any]:
    return _paired_response(task_id, request.state.user)


@app.post("/api/paired-admission/tasks/{task_id}/fidelity-decision")
def decide_paired_fidelity(
    task_id: int, body: FidelityDecisionIn, request: Request,
) -> dict[str, Any]:
    _, _, channel_id = _paired_scope(task_id)
    user = paired_access.require(
        request, "fidelity_reviewer", channel_id=channel_id, task_id=task_id,
    )
    result = paired_admission.decide_fidelity(
        task_id, body.value, body.reason, body.evidence_refs,
        body.expected_state_version, user, idempotency_key=body.idempotency_key,
    )
    return _paired_command_response(result["task"]["id"], user, result)


@app.post("/api/paired-admission/tasks/{task_id}/start")
async def start_paired_task(
    task_id: int, body: PairedStartIn, request: Request,
) -> dict[str, Any]:
    _, _, channel_id = _paired_scope(task_id)
    user = paired_access.require(
        request, "operator", channel_id=channel_id, task_id=task_id
    )
    if not body.scale_confirmed:
        raise HTTPException(status_code=400, detail="启动前必须确认请求规模预览")
    try:
        started = paired_admission.start_pair(
            task_id, body.benchmark_target_id,
            request_limit=body.request_limit, token_limit=body.token_limit,
            money_limit=body.money_limit,
            expected_state_version=body.expected_state_version,
            idempotency_key=body.idempotency_key, user=user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if started["paired"]["state"] == "queued" and not started.get("command_replayed"):
        await runner.submit(task_id)
    return _paired_command_response(started["task"]["id"], user, started)


@app.post("/api/paired-admission/tasks/{task_id}/cancel")
async def cancel_paired_task(
    task_id: int, body: PairedCancelIn, request: Request,
) -> dict[str, Any]:
    _, _, channel_id = _paired_scope(task_id)
    user = paired_access.require(
        request, "operator", channel_id=channel_id, task_id=task_id
    )
    result = await paired_admission.cancel(
        task_id, body.expected_state_version, user,
        idempotency_key=body.idempotency_key,
    )
    return _paired_command_response(task_id, user, result)


@app.post("/api/paired-admission/tasks/{task_id}/rerun")
async def rerun_paired_task(
    task_id: int, body: PairedRerunIn, request: Request,
) -> dict[str, Any]:
    paired, _, channel_id = _paired_scope(task_id)
    user = paired_access.require(
        request, "operator", channel_id=channel_id, task_id=task_id
    )
    if paired["state"] not in paired_admission.TERMINAL_STATES:
        raise HTTPException(status_code=400, detail="原任务尚未封存，不能完整重跑")
    try:
        created = paired_admission.rerun(
            task_id, user, idempotency_key=body.idempotency_key
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"候选渠道配置不可用：{exc}") from exc
    new_task_id = created["task"]["id"]
    if created["paired"]["state"] == "queued" and not created.get("command_replayed"):
        await runner.submit(new_task_id)
    return _paired_command_response(new_task_id, user, created)


@app.post("/api/paired-admission/tasks/{task_id}/conclusion")
async def submit_paired_conclusion(
    task_id: int, body: PairedConclusionIn, request: Request,
) -> dict[str, Any]:
    paired, _, channel_id = _paired_scope(task_id)
    user = paired_access.require(
        request, "admission_reviewer", channel_id=channel_id, task_id=task_id,
    )
    conclusion = paired_admission.submit_conclusion(
        task_id, verdict=body.verdict, reason=body.reason,
        evidence_refs=body.evidence_refs, report_version=body.report_version,
        expected_conclusion_version=body.expected_conclusion_version,
        idempotency_key=body.idempotency_key, user=user,
    )
    return conclusion


@app.post("/api/paired-admission/tasks/{task_id}/report-revisions")
def recalculate_paired_report(
    task_id: int, body: PairedReportRevisionIn, request: Request,
) -> dict[str, Any]:
    _, _, channel_id = _paired_scope(task_id)
    user = paired_access.require(
        request, "admin", channel_id=channel_id, task_id=task_id,
    )
    return paired_admission.recalculate_report(
        task_id, body.reason, user,
        expected_report_version=body.expected_report_version,
        idempotency_key=body.idempotency_key,
    )


@app.post("/api/paired-admission/tasks/{task_id}/retention")
def extend_paired_retention(
    task_id: int, body: PairedRetentionExtensionIn, request: Request,
) -> dict[str, Any]:
    _, _, channel_id = _paired_scope(task_id)
    user = paired_access.require(
        request, "admin", channel_id=channel_id, task_id=task_id,
    )
    return paired_admission.extend_retention(
        task_id,
        raw_expires_at=body.raw_expires_at,
        structured_expires_at=body.structured_expires_at,
        reason=body.reason,
        user=user,
        idempotency_key=body.idempotency_key,
    )


@app.get("/api/paired-admission/tasks/{task_id}/evidence")
def get_paired_evidence(
    task_id: int, request: Request, include_raw: bool = False,
    export_full: bool = False, purpose: str = "",
) -> dict[str, Any]:
    paired, _, channel_id = _paired_scope(task_id)
    user = request.state.user
    permissions = _paired_permissions(user, paired, channel_id)
    if not permissions["raw"]:
        raise HTTPException(status_code=403, detail="没有原始证据查看权限")
    if export_full and not permissions["full_raw_export"]:
        raise HTTPException(status_code=403, detail="完整证据导出需要逐任务 raw_export 权限")
    use = purpose.strip()
    if export_full and not use:
        raise HTTPException(status_code=400, detail="完整证据导出必须填写用途")
    if len(use) > 500:
        raise HTTPException(status_code=400, detail="证据访问用途不能超过 500 字符")
    use = use or ("查看保真证据" if permissions["fidelity_only"] else "查看任务证据")
    integrity = _paired_integrity(task_id, paired)
    try:
        evidence = paired_evidence.records(
            task_id, include_raw=include_raw or export_full
        )
    except paired_evidence.EvidenceError as exc:
        status_code = 410 if str(exc) == "raw_evidence_expired" else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    if permissions["fidelity_only"]:
        evidence = _fidelity_evidence(paired, evidence)
    auth.audit(
        user["id"], user["username"],
        "paired.evidence.export" if export_full else "paired.evidence.view",
        "paired_task", str(task_id), "success",
        detail={"include_raw": include_raw, "export_full": export_full, "purpose": use},
    )
    return {"task_id": task_id, "integrity": integrity, "records": evidence}


@app.post("/api/paired-admission/fidelity-truths/{truth_id}/revoke")
def revoke_paired_truth(
    truth_id: int, body: FidelityTruthRevokeIn, request: Request,
) -> dict[str, Any]:
    truth = store.get("paired_fidelity_truths", truth_id)
    if not truth:
        raise HTTPException(status_code=404, detail="有效保真定义不存在")
    user = paired_access.require(
        request, "fidelity_reviewer", channel_id=truth.get("channel_id")
    )
    return paired_admission.revoke_truth(truth_id, body.reason, user)


@app.get("/api/paired-admission/roles")
def list_paired_roles(request: Request, user_id: int | None = None) -> list[dict[str, Any]]:
    paired_access.require(request, "admin")
    if user_id:
        return paired_access.active_roles(user_id)
    return store.query(
        "SELECT roles.id,roles.user_id,users.username,roles.role,roles.scope_type,"
        "roles.scope_id,roles.granted_by,roles.reason,roles.created_at "
        "FROM user_roles roles JOIN users ON users.id=roles.user_id "
        "WHERE roles.revoked_at IS NULL ORDER BY users.username,roles.id"
    )


@app.post("/api/paired-admission/roles")
def grant_paired_role(body: RoleGrantIn, request: Request) -> dict[str, Any]:
    actor = paired_access.require(request, "admin")
    normalized_scope_id = body.scope_id
    if body.scope_type == "global" and body.scope_id != "*":
        raise HTTPException(status_code=400, detail="全局授权的 scope_id 必须为 *")
    if body.scope_type != "global":
        try:
            scope_id = int(body.scope_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="资源范围 ID 必须是正整数") from exc
        if scope_id < 1:
            raise HTTPException(status_code=400, detail="资源范围 ID 必须是正整数")
        normalized_scope_id = str(scope_id)
        if body.scope_type == "channel" and not store.get("channels", scope_id):
            raise HTTPException(status_code=404, detail="授权渠道不存在")
        if body.scope_type == "task" \
                and not store.get("paired_tasks", scope_id, key="task_id"):
            raise HTTPException(status_code=404, detail="授权双端任务不存在")
    if body.role == "raw_export" and body.scope_type != "task":
        raise HTTPException(status_code=400, detail="raw_export 只能逐任务授权")
    try:
        return paired_access.grant(
            actor, user_id=body.user_id, role=body.role,
            scope_type=body.scope_type, scope_id=normalized_scope_id, reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/paired-admission/roles/{role_id}")
def revoke_paired_role(
    role_id: int, body: RoleRevokeIn, request: Request,
) -> dict[str, Any]:
    actor = paired_access.require(request, "admin")
    try:
        return paired_access.revoke(actor, role_id, body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------- 静态前端 ----------

app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
