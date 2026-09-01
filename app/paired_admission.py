"""双端准入的任务、人工保真、报告版本与人工结论服务。"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

from fastapi import HTTPException

from . import auth, grading, paired_assets, paired_evidence, paired_protocol, store
from .config import (DEFAULT_PRICE_IN, DEFAULT_PRICE_OUT, PAIRED_FIDELITY_WAIT_SECONDS,
                     PAIRED_RAW_RETENTION_SECONDS, PAIRED_SELFTEST_MODE,
                     PAIRED_STRUCTURED_RETENTION_SECONDS, PAIRED_TRUTH_TTL_SECONDS)
from .security import credential_fingerprint, decrypt, encrypt, mask, redact_url

PAIRED_KIND = "paired_admission"
TOKENIZER_VERSION = "paired-tokenizer-v1"
GRADER_VERSION = "deterministic-binary-v1"
TERMINAL_STATES = {"completed", "completed_with_insufficient_metrics", "stopped", "canceled"}
ACTIVE_STATES = {
    "queued", "fidelity", "warmup", "identity", "speed_round_1",
    "ability_base", "speed_round_2", "ability_retest", "finalizing",
}


def _json_hash(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _begin_command(
    user_id: int, command_name: str, idempotency_key: str,
    payload: dict[str, Any], *, task_id: int | None = None,
) -> tuple[int | None, dict[str, Any] | None]:
    payload_hash = _json_hash(payload)
    with store.cursor() as cur:
        existing = cur.execute(
            "SELECT * FROM paired_commands WHERE created_by=? AND idempotency_key=?",
            (user_id, idempotency_key),
        ).fetchone()
        if existing:
            if existing["command_name"] != command_name \
                    or existing["payload_hash"] != payload_hash:
                raise HTTPException(
                    status_code=409,
                    detail="幂等键已用于不同的命令参数",
                )
            if existing["status"] != "completed":
                raise HTTPException(status_code=409, detail="相同命令正在处理")
            return None, {
                **store.loads(existing["result_json"], {}),
                "command_replayed": True,
            }
        cur.execute(
            "INSERT INTO paired_commands "
            "(task_id,command_name,idempotency_key,payload_hash,status,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                task_id, command_name, idempotency_key, payload_hash,
                "processing", user_id, time.time(),
            ),
        )
        return int(cur.lastrowid), None


def _replay_command(
    user_id: int, command_name: str, idempotency_key: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    rows = store.query(
        "SELECT * FROM paired_commands WHERE created_by=? AND idempotency_key=?",
        (user_id, idempotency_key),
    )
    if not rows:
        return None
    command = rows[0]
    if command["command_name"] != command_name \
            or command["payload_hash"] != _json_hash(payload):
        raise HTTPException(status_code=409, detail="幂等键已用于不同的命令参数")
    if command["status"] != "completed":
        raise HTTPException(status_code=409, detail="相同命令正在处理")
    return {
        **store.loads(command["result_json"], {}),
        "command_replayed": True,
    }


def _complete_command(
    command_id: int, result: dict[str, Any], *, task_id: int,
) -> dict[str, Any]:
    paired = store.get("paired_tasks", task_id, key="task_id")
    store.update("paired_commands", command_id, {
        "task_id": task_id,
        "status": "completed",
        "result_json": store.dumps(result),
        "result_state_version": (paired or {}).get("state_version"),
        "completed_at": time.time(),
    })
    return result


def mark_evidence_storage_failed(task_id: int) -> None:
    now = time.time()
    with store.cursor() as cur:
        cur.execute(
            "UPDATE paired_tasks SET state='stopped',state_version=state_version+1,"
            "stop_reason='evidence_storage_failed',integrity_status='failed',"
            "integrity_error='evidence_storage_failed',updated_at=? WHERE task_id=?",
            (now, task_id),
        )
        cur.execute(
            "UPDATE tasks SET status='stopped',cancel_flag=1,finished_at=? WHERE id=?",
            (now, task_id),
        )
    store.add_event(
        task_id, "证据存储失败，任务已停止且禁止准入", stage="停止", level="error"
    )


def _complete_evidence_failure(command_id: int, task_id: int) -> dict[str, Any]:
    mark_evidence_storage_failed(task_id)
    return _complete_command(command_id, get(task_id), task_id=task_id)


def _target_secure_snapshot(target: dict[str, Any]) -> dict[str, Any]:
    key = decrypt(target.get("key_enc") or "") if target.get("key_enc") else ""
    snapshot = {
        "id": target["id"],
        "channel_id": target.get("channel_id"),
        "name": target["name"],
        "protocol": target["protocol"],
        "base_url": target["base_url"],
        "model": target["model"],
        "canonical_model": target.get("canonical_model") or target["model"],
        "route": target.get("route") or "",
        "group_name": target.get("group_name") or "",
        "env": target.get("env") or "",
        "upstream_multiplier": float(target.get("upstream_multiplier") or 1.0),
        "price_in": target.get("price_in"),
        "price_out": target.get("price_out"),
        "key": key,
        "key_masked": mask(key),
        "credential_fingerprint": credential_fingerprint(key),
        "snapshot_at": time.time(),
    }
    configuration_id = target.get("exact_configuration_id")
    if configuration_id:
        configuration = store.get("channel_configurations", int(configuration_id))
        if configuration:
            mappings = store.query(
                "SELECT canonical_model,request_model FROM configuration_model_mappings "
                "WHERE configuration_id=? ORDER BY sort_order,id",
                (configuration["id"],),
            )
            snapshot.update({
                "exact_configuration_id": configuration["id"],
                "configuration_fingerprint": configuration["configuration_fingerprint"],
                "configuration_business_status": configuration["business_status"],
                "exact_model_mappings": mappings,
                "route": configuration["route"],
                "group_name": configuration["group_name"],
                "upstream_multiplier": float(configuration["upstream_multiplier"]),
                "credential_fingerprint": configuration["credential_fingerprint"],
            })
    return snapshot


def public_target(snapshot: dict[str, Any]) -> dict[str, Any]:
    public = {
        key: value for key, value in snapshot.items()
        if key not in {"key", "credential_fingerprint"}
    }
    public["base_url"] = redact_url(str(snapshot["base_url"]))
    public["credential_fingerprint"] = snapshot["credential_fingerprint"]
    return public


def fidelity_fingerprint(snapshot: dict[str, Any]) -> str:
    if snapshot.get("exact_configuration_id"):
        return f"cf1:{snapshot['configuration_fingerprint']}"
    return f"f1:{_json_hash({
        'base_url': snapshot['base_url'].rstrip('/'),
        'protocol': snapshot['protocol'],
        'model': snapshot['model'],
        'route': snapshot.get('route') or '',
        'group': snapshot.get('group_name') or '',
        'upstream_multiplier': snapshot['upstream_multiplier'],
        'credential_fingerprint': snapshot['credential_fingerprint'],
    })}"


def resource_fingerprint(snapshot: dict[str, Any]) -> str:
    if snapshot.get("exact_configuration_id"):
        return f"configuration:{snapshot['configuration_fingerprint']}"
    return f"r1:{_json_hash({
        'base_url': snapshot['base_url'].rstrip('/'),
        'protocol': snapshot['protocol'],
        'model': snapshot['model'],
        'route': snapshot.get('route') or '',
        'group': snapshot.get('group_name') or '',
        'credential_fingerprint': snapshot['credential_fingerprint'],
    })}"


def secure_snapshots(paired: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(decrypt(paired["secure_snapshot_ciphertext"]))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("paired_secure_snapshot_invalid") from exc


def _valid_truth(candidate: dict[str, Any]) -> dict[str, Any] | None:
    now = time.time()
    configuration_id = candidate.get("exact_configuration_id")
    if configuration_id:
        if candidate.get("configuration_business_status") == "online":
            return {
                "id": None, "kind": "online", "source_task_id": None,
                "source_manifest_root": None,
            }
        rows = store.query(
            "SELECT * FROM configuration_fidelity_truths WHERE configuration_id=? "
            "AND configuration_fingerprint=? AND status='active' AND revoked_at IS NULL "
            "AND expires_at>? ORDER BY id DESC LIMIT 1",
            (configuration_id, candidate.get("configuration_fingerprint"), now),
        )
        if not rows:
            return None
        truth = {**rows[0], "kind": "configuration"}
        checked = paired_evidence.verify_manifest(
            truth["source_task_id"], truth["source_manifest_root"],
            expected_stage="fidelity",
        )
        return truth if checked["ok"] else None
    fingerprint = fidelity_fingerprint(candidate)
    rows = store.query(
        "SELECT * FROM paired_fidelity_truths WHERE target_id=? "
        "AND upstream_multiplier=? AND fidelity_fingerprint=? AND status='active' "
        "AND formal=1 AND revoked_at IS NULL AND expires_at>? ORDER BY id DESC LIMIT 1",
        (candidate["id"], candidate["upstream_multiplier"], fingerprint, now),
    )
    if not rows:
        return None
    truth = rows[0]
    source = store.get("paired_tasks", truth["source_task_id"], key="task_id")
    if not source or source.get("fidelity_manifest_root") != truth["source_manifest_root"]:
        return None
    checked = paired_evidence.verify_manifest(
        truth["source_task_id"], truth["source_manifest_root"],
        expected_stage="fidelity",
    )
    return {**truth, "kind": "legacy_target"} if checked["ok"] else None


def frozen_truth_valid(paired: dict[str, Any]) -> bool:
    kind = paired.get("truth_definition_kind") or "legacy_target"
    snapshots = secure_snapshots(paired)
    candidate = snapshots.get("candidate") or {}
    if kind == "online":
        configuration_id = candidate.get("exact_configuration_id")
        configuration = store.get("channel_configurations", configuration_id) if configuration_id else None
        return bool(configuration and configuration.get("business_status") == "online"
                    and configuration.get("configuration_fingerprint") == candidate.get("configuration_fingerprint"))
    truth_id = paired.get("truth_definition_id")
    if not truth_id:
        return False
    table = "configuration_fidelity_truths" if kind == "configuration" else "paired_fidelity_truths"
    truth = store.get(table, int(truth_id))
    if not truth or truth.get("status") != "active" or truth.get("revoked_at") \
            or float(truth["expires_at"]) <= time.time():
        return False
    if kind == "configuration":
        configuration = store.get("channel_configurations", truth["configuration_id"])
        if not configuration or configuration.get("configuration_fingerprint") != truth.get("configuration_fingerprint"):
            return False
    checked = paired_evidence.verify_manifest(
        int(truth["source_task_id"]), str(truth["source_manifest_root"]), expected_stage="fidelity",
    )
    return bool(checked["ok"])


def model_aliases(model: str) -> set[str]:
    aliases = {model}
    canonical_models = {model}
    for target in store.query(
        "SELECT canonical_model FROM targets WHERE model=? AND canonical_model!=''",
        (model,),
    ):
        canonical_models.add(str(target["canonical_model"]))
    aliases.update(canonical_models)
    for canonical_model in canonical_models:
        model_rows = store.query(
            "SELECT id,family_id FROM model_family_models WHERE model=? AND archived_at IS NULL",
            (canonical_model,),
        )
        for row in model_rows:
            aliases.update(
                str(alias["alias"]) for alias in store.query(
                    "SELECT alias FROM model_aliases WHERE model_id=? AND family_id=?",
                    (row["id"], row["family_id"]),
                )
            )
    return aliases


def models_equivalent(left: str, right: str) -> bool:
    return not model_aliases(left).isdisjoint(model_aliases(right))


def verify_integrity(
    task_id: int, paired: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_state = paired or store.get("paired_tasks", task_id, key="task_id")
    if not task_state:
        return {"ok": False, "status": "missing", "reason": "paired_task_missing"}
    fidelity_root = str(task_state.get("fidelity_manifest_root") or "")
    if task_state["state"] in TERMINAL_STATES:
        task_manifest = paired_evidence.verify_manifest(
            task_id, str(task_state.get("task_manifest_root") or ""),
            expected_stage="task",
        )
        result: dict[str, Any] = {
            "ok": bool(task_manifest["ok"]),
            "status": "verified" if task_manifest["ok"] else "failed",
            "task_manifest": task_manifest,
        }
        if fidelity_root:
            fidelity_manifest = paired_evidence.verify_manifest(
                task_id, fidelity_root, expected_stage="fidelity",
            )
            result["fidelity_manifest"] = fidelity_manifest
            result["ok"] = result["ok"] and bool(fidelity_manifest["ok"])
            if task_manifest.get("fidelity_manifest_root") != fidelity_root:
                result["ok"] = False
                result["reason"] = "task_manifest_fidelity_root"
            if not result["ok"]:
                result["status"] = "failed"
        return result
    if fidelity_root:
        fidelity_manifest = paired_evidence.verify_manifest(
            task_id, fidelity_root, expected_stage="fidelity",
        )
        return {
            "ok": bool(fidelity_manifest["ok"]),
            "status": "collecting" if fidelity_manifest["ok"] else "failed",
            "fidelity_manifest": fidelity_manifest,
        }
    chain = paired_evidence.verify(task_id)
    return {
        "ok": bool(chain["ok"]),
        "status": "collecting" if chain["ok"] else "failed",
        "chain": chain,
    }


def _pair_request_policy(
    candidate: dict[str, Any], benchmark: dict[str, Any],
    assets: dict[str, Any],
) -> dict[str, Any]:
    canonical_requests = [assets["warmup"], assets["identity"]]
    canonical_requests.extend({
        "id": item.get("instance_id") or item["id"],
        "messages": [{"role": "user", "content": item["prompt"]}],
        "max_tokens": int(item["max_tokens"]),
        "temperature": 0.0,
        "stream": True,
    } for item in [*assets["speed"], *assets["ability"]])
    omit_temperature = False
    for canonical in canonical_requests:
        adapted = [
            paired_protocol.adapt_request(
                side["protocol"], side["model"], canonical
            )
            for side in (candidate, benchmark)
        ]
        if "temperature" in canonical and any(
            "temperature" not in body for body in adapted
        ):
            omit_temperature = True
    return {
        "symmetric_omissions": ["temperature"] if omit_temperature else [],
        "validated_request_count": len(canonical_requests),
        "validated_at": time.time(),
    }


def readiness() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    assets_ok, asset_errors = paired_assets.verify()
    checks.append({"name": "assets", "ok": assets_ok, "detail": asset_errors})
    adapters_ok, adapter_errors = paired_protocol.adapter_selfcheck()
    checks.append({"name": "adapters", "ok": adapters_ok, "detail": adapter_errors})
    fidelity_adapter_errors: list[str] = []
    for protocol_name, model in (
        ("openai", "gpt-4o-mini"), ("anthropic", "claude-sonnet-5"),
    ):
        for item in paired_assets.instantiate_fidelity("readiness-selfcheck"):
            try:
                paired_protocol.adapt_request(protocol_name, model, item)
            except paired_protocol.AdapterError as exc:
                fidelity_adapter_errors.append(
                    f"{protocol_name}:{item['id']}:{exc}"
                )
    checks.append({
        "name": "fidelity_requests",
        "ok": not fidelity_adapter_errors,
        "detail": fidelity_adapter_errors,
    })
    manifest = paired_assets.manifest()
    required_graders = {
        str(item["grader"]["id"]) for item in [
            *manifest["fidelity"], *manifest["ability"], *manifest["speed"]
        ] if item.get("grader")
    }
    missing_graders = sorted(required_graders - set(grading.registered_graders()))
    checks.append({"name": "graders", "ok": not missing_graders, "detail": missing_graders})
    required_tables = {
        "paired_tasks", "paired_commands", "paired_fidelity_decisions", "paired_fidelity_truths",
        "paired_evidence_records", "paired_raw_blocks", "paired_report_revisions",
        "paired_conclusions", "user_roles",
    }
    present = {
        row["name"] for row in store.query(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing_tables = sorted(required_tables - present)
    checks.append({"name": "schema", "ok": not missing_tables, "detail": missing_tables})
    checks.append({"name": "evidence_hmac", "ok": bool(paired_evidence.canonical_json({"a": 1}))})
    ready = all(check["ok"] for check in checks)
    return {
        "ready": ready,
        "formal": not PAIRED_SELFTEST_MODE,
        "checks": checks,
        "asset_version": paired_assets.ASSET_VERSION,
        "asset_hash": paired_assets.manifest_hash(),
        "load_thresholds_used": False,
    }


def require_ready() -> None:
    state = readiness()
    if not state["ready"]:
        missing = ", ".join(check["name"] for check in state["checks"] if not check["ok"])
        raise HTTPException(status_code=503, detail=f"双端准入入口未就绪：{missing}")


def _candidate_snapshot(candidate_target_id: int) -> dict[str, Any]:
    require_ready()
    target = store.get("targets", candidate_target_id)
    if not target:
        raise HTTPException(status_code=404, detail="候选渠道不存在")
    try:
        paired_protocol.validate_protocol(target["protocol"])
        candidate = _target_secure_snapshot(target)
    except (paired_protocol.AdapterError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"候选渠道配置不可用：{exc}") from exc
    return candidate


def _insert_task(
    candidate_target_id: int, candidate: dict[str, Any],
    user: dict[str, Any], *, parent_task_id: int | None,
) -> int:
    truth = _valid_truth(candidate) if not PAIRED_SELFTEST_MODE else None
    now = time.time()
    state = "fidelity_skipped" if truth else "queued"
    manifest = paired_assets.manifest()
    fidelity_instance_seed = secrets.token_urlsafe(24) if not truth else ""
    fidelity_instances = paired_assets.instantiate_fidelity(fidelity_instance_seed) \
        if fidelity_instance_seed else []
    frozen_assets = {**manifest, "fidelity": fidelity_instances,
                     "fidelity_instance_seed": fidelity_instance_seed} \
        if fidelity_instances else manifest
    public_snapshot = {
        "candidate": public_target(candidate),
        "benchmark": None,
        "fidelity": {
            "fingerprint": fidelity_fingerprint(candidate),
            "truth_reused": bool(truth),
            "truth_definition_id": truth.get("id") if truth else None,
            "truth_definition_kind": truth.get("kind") if truth else None,
            "source_task_id": truth.get("source_task_id") if truth else None,
            "instance_seed_hash": fidelity_instances[0]["instance_seed_hash"]
            if fidelity_instances else None,
            "instance_hashes": {
                item["instance_id"]: item["content_hash"] for item in fidelity_instances
            },
        },
        "asset_version": paired_assets.ASSET_VERSION,
        "asset_hash": paired_assets.manifest_hash(),
        "upstream_catalog_version": manifest["upstream_catalog_version"],
        "item_hashes": manifest["item_hashes"],
        "adapter_versions": paired_protocol.ADAPTER_VERSIONS,
        "tokenizer_version": TOKENIZER_VERSION,
        "grader_version": GRADER_VERSION,
        "timeouts": {"fidelity_warmup_identity": 60, "formal": 180, "active_task": 3600},
        "fixed_cooldown_seconds": 5,
        "retention": {
            "raw_seconds": PAIRED_RAW_RETENTION_SECONDS,
            "structured_seconds": PAIRED_STRUCTURED_RETENTION_SECONDS,
        },
        "formal": not PAIRED_SELFTEST_MODE,
        "parent_task_id": parent_task_id,
    }
    task_id = store.insert("tasks", {
        "kind": PAIRED_KIND,
        "target_id": candidate_target_id,
        "target_name": candidate["name"],
        "pack_name": "双端配对相对准入",
        "pack_version": paired_assets.ASSET_VERSION,
        "status": state,
        "parent_task_id": parent_task_id,
        "snapshot": store.dumps(public_snapshot),
        "progress": store.dumps({"done": 0, "total": 16, "current": "等待保真检查"}),
        "include_hard": 0,
        "created_at": now,
    })
    secure = {"candidate": candidate, "benchmark": None, "assets": frozen_assets}
    store.insert("paired_tasks", {
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "candidate_target_id": candidate_target_id,
        "created_by": user["id"],
        "state": state,
        "state_version": 1,
        "idempotency_key": secrets.token_urlsafe(24),
        "ready_at": now if not truth else None,
        "truth_definition_id": truth.get("id") if truth else None,
        "truth_definition_kind": truth.get("kind", "legacy_target") if truth else "legacy_target",
        "formal": 0 if PAIRED_SELFTEST_MODE else 1,
        "secure_snapshot_ciphertext": encrypt(json.dumps(secure, ensure_ascii=False)),
        "asset_version": paired_assets.ASSET_VERSION,
        "asset_hash": paired_assets.manifest_hash(),
        "adapter_versions_json": store.dumps(paired_protocol.ADAPTER_VERSIONS),
        "tokenizer_version": TOKENIZER_VERSION,
        "grader_version": GRADER_VERSION,
        "raw_expires_at": now + PAIRED_RAW_RETENTION_SECONDS,
        "structured_expires_at": now + PAIRED_STRUCTURED_RETENTION_SECONDS,
        "created_at": now,
        "updated_at": now,
    })
    try:
        paired_evidence.append(
            task_id, "task_snapshot", public_snapshot,
            raw=paired_evidence.canonical_json(frozen_assets), block_type="asset_manifest",
        )
        if truth:
            paired_evidence.append(task_id, "fidelity_truth_reused", {
                "truth_definition_id": truth.get("id"),
                "truth_definition_kind": truth.get("kind"),
                "source_task_id": truth.get("source_task_id"),
                "source_manifest_root": truth.get("source_manifest_root"),
                "validated_at": now,
            })
            transition(
                task_id, "awaiting_pair_start",
                reason="精确命中有效人工保真定义",
            )
            store.add_event(task_id, "命中有效人工保真定义，可选择标杆并启动双端测试", stage="保真")
        else:
            store.add_event(task_id, "未命中有效人工保真定义，候选端进入自动保真检查", stage="保真")
    except paired_evidence.EvidenceError:
        mark_evidence_storage_failed(task_id)
        return task_id
    if parent_task_id is not None:
        paired_evidence.append(task_id, "rerun_link", {
            "parent_task_id": parent_task_id,
            "created_by": user["id"],
        })
        store.add_event(task_id, f"由双端任务 #{parent_task_id} 发起完整重跑", stage="补测")
    return task_id


def create(
    candidate_target_id: int, user: dict[str, Any], *, idempotency_key: str,
) -> dict[str, Any]:
    command_payload = {"candidate_target_id": candidate_target_id}
    replay = _replay_command(
        user["id"], "create", idempotency_key, command_payload
    )
    if replay is not None:
        return replay
    candidate = _candidate_snapshot(candidate_target_id)
    command_id, replay = _begin_command(
        user["id"], "create", idempotency_key, command_payload,
    )
    if replay is not None:
        return replay
    assert command_id is not None
    task_id = _insert_task(
        candidate_target_id, candidate, user, parent_task_id=None
    )
    return _complete_command(command_id, get(task_id), task_id=task_id)


def rerun(
    source_task_id: int, user: dict[str, Any], *, idempotency_key: str,
) -> dict[str, Any]:
    command_payload = {"source_task_id": source_task_id}
    replay = _replay_command(
        user["id"], "rerun", idempotency_key, command_payload
    )
    if replay is not None:
        return replay
    source = store.get("paired_tasks", source_task_id, key="task_id")
    if not source:
        raise HTTPException(status_code=404, detail="来源双端准入任务不存在")
    if source["state"] not in TERMINAL_STATES:
        raise HTTPException(status_code=400, detail="只有已结束任务可以发起完整重跑")
    candidate = secure_snapshots(source)["candidate"]
    paired_protocol.validate_protocol(candidate["protocol"])
    require_ready()
    command_id, replay = _begin_command(
        user["id"], "rerun", idempotency_key,
        command_payload, task_id=source_task_id,
    )
    if replay is not None:
        return replay
    assert command_id is not None
    task_id = _insert_task(
        int(source["candidate_target_id"]), candidate, user,
        parent_task_id=source_task_id,
    )
    return _complete_command(command_id, get(task_id), task_id=task_id)


def get(task_id: int) -> dict[str, Any]:
    paired = store.get("paired_tasks", task_id, key="task_id")
    task = store.get("tasks", task_id)
    if not paired or not task:
        raise HTTPException(status_code=404, detail="双端准入任务不存在")
    truth_kind = paired.get("truth_definition_kind") or "legacy_target"
    truth_table = "configuration_fidelity_truths" \
        if truth_kind == "configuration" else "paired_fidelity_truths"
    truth = store.get(truth_table, paired["truth_definition_id"]) \
        if paired.get("truth_definition_id") else None
    report = store.task_report(task, None)
    conclusion_rows = store.query(
        "SELECT * FROM paired_conclusions WHERE task_id=? "
        "ORDER BY conclusion_version DESC LIMIT 1", (task_id,),
    )
    fidelity_checks = [
        record["payload"] for record in paired_evidence.records(task_id)
        if record["record_type"] == "derived_result"
        and record["payload"].get("stage") == "fidelity"
    ]
    integrity = verify_integrity(task_id, paired)
    if paired["state"] in TERMINAL_STATES:
        store.update(
            "paired_tasks", task_id,
            {
                "integrity_status": "verified" if integrity["ok"] else "failed",
                "integrity_error": "" if integrity["ok"] else str(
                    integrity.get("reason") or "integrity_failed"
                ),
            },
            key="task_id",
        )
    return {
        "task": task,
        "paired": {
            key: value for key, value in paired.items()
            if key != "secure_snapshot_ciphertext"
        },
        "snapshot": store.loads(task["snapshot"], {}),
        "progress": store.loads(task["progress"], {}),
        "events": store.list_events(task_id),
        "fidelity_checks": fidelity_checks,
        "truth": truth,
        "report": report,
        "conclusion": conclusion_rows[0] if conclusion_rows else None,
        "integrity": integrity,
    }


def transition(
    task_id: int, state: str, *, expected_version: int | None = None,
    reason: str = "", task_status: str | None = None,
) -> dict[str, Any]:
    now = time.time()
    with store.cursor() as cur:
        current = cur.execute(
            "SELECT state,state_version FROM paired_tasks WHERE task_id=?", (task_id,),
        ).fetchone()
        if not current:
            raise RuntimeError("paired_task_missing")
        if expected_version is not None and current["state_version"] != expected_version:
            raise HTTPException(status_code=409, detail="任务状态版本已变化，请刷新后重试")
        next_version = int(current["state_version"]) + 1
        cur.execute(
            "UPDATE paired_tasks SET state=?,state_version=?,idempotency_key=?,updated_at=? "
            "WHERE task_id=? AND state_version=?",
            (state, next_version, secrets.token_urlsafe(24), now, task_id,
             current["state_version"]),
        )
        if cur.rowcount != 1:
            raise HTTPException(status_code=409, detail="任务状态并发冲突")
        cur.execute(
            "UPDATE tasks SET status=? WHERE id=?", (task_status or state, task_id)
        )
    paired_evidence.append(task_id, "state_transition", {
        "from": current["state"], "to": state, "state_version": next_version,
        "reason": reason, "idempotency_key_changed": True,
    })
    return store.get("paired_tasks", task_id, key="task_id") or {}


def _create_fidelity_stop_report(task_id: int, reason: str, title: str) -> dict[str, Any]:
    paired = store.get("paired_tasks", task_id, key="task_id")
    task = store.get("tasks", task_id)
    assert paired and task
    checked = paired_evidence.verify(task_id)
    input_root = paired_evidence.seal_manifest(task_id, "fidelity_stop")
    report = {
        "task_id": task_id,
        "kind": PAIRED_KIND,
        "status": "stopped",
        "complete": False,
        "stop_reason": reason,
        "title": title,
        "snapshot": store.loads(task["snapshot"], {}),
        "integrity": {"verified": checked["ok"], "input_manifest_root": input_root},
        "conclusion": {
            "code": "manual_restricted",
            "verdict": "仅可选择暂不准入或继续补测",
            "allowed": ["do_not_admit", "continue_testing"],
        },
        "report_version": 1,
        "created_at": time.time(),
    }
    save_report_revision(task_id, report, "初始停止报告", None)
    task_root = paired_evidence.seal_manifest(task_id, "task")
    store.update(
        "paired_tasks", task_id, {
            "task_manifest_root": task_root,
            "integrity_status": "verified",
            "integrity_error": "",
        }, key="task_id"
    )
    return report


def decide_fidelity(
    task_id: int, value: str, reason: str, evidence_refs: list[str],
    expected_state_version: int, user: dict[str, Any], *, idempotency_key: str,
) -> dict[str, Any]:
    paired = store.get("paired_tasks", task_id, key="task_id")
    if not paired:
        raise HTTPException(status_code=404, detail="双端准入任务不存在")
    command_payload = {
        "task_id": task_id,
        "value": value,
        "reason": reason.strip(),
        "evidence_refs": evidence_refs,
        "expected_state_version": expected_state_version,
    }
    replay = _replay_command(
        user["id"], "fidelity_decision", idempotency_key, command_payload
    )
    if replay is not None:
        return replay
    if paired["state_version"] != expected_state_version:
        raise HTTPException(status_code=409, detail="任务状态版本已变化，请刷新后重试")
    if paired["state"] not in {"awaiting_fidelity_truth", "stopped"}:
        raise HTTPException(status_code=400, detail="当前任务不在人工保真判断阶段")
    if paired["state"] == "stopped" and paired.get("stop_reason") != "fidelity_unconfirmed":
        raise HTTPException(status_code=400, detail="该停止任务不能再形成保真定义")
    checked = paired_evidence.verify_manifest(
        task_id, str(paired.get("fidelity_manifest_root") or ""),
        expected_stage="fidelity",
    )
    if not checked["ok"]:
        raise HTTPException(status_code=409, detail="保真证据完整性校验失败")
    command_id, replay = _begin_command(
        user["id"], "fidelity_decision", idempotency_key,
        command_payload,
        task_id=task_id,
    )
    if replay is not None:
        return replay
    assert command_id is not None
    formal = bool(paired["formal"])
    decision_id = store.insert("paired_fidelity_decisions", {
        "task_id": task_id, "value": value, "reason": reason.strip(),
        "evidence_refs_json": store.dumps(evidence_refs),
        "user_id": user["id"], "actor": user["username"],
        "formal": int(formal), "created_at": time.time(),
    })
    try:
        paired_evidence.append(task_id, "fidelity_human_decision", {
            "decision_id": decision_id, "value": value, "reason": reason.strip(),
            "evidence_refs": evidence_refs, "user_id": user["id"],
            "actor": user["username"], "formal": formal,
            "idempotency_key": idempotency_key,
        })
    except paired_evidence.EvidenceError:
        return _complete_evidence_failure(command_id, task_id)
    auth.audit(
        user["id"], user["username"], "paired.fidelity.decide", "paired_task",
        str(task_id), "success", detail={"value": value, "reason": reason.strip()},
    )
    if value == "false":
        try:
            transition(
                task_id, "stopped", expected_version=expected_state_version,
                reason="保真人工否定",
            )
        except paired_evidence.EvidenceError:
            return _complete_evidence_failure(command_id, task_id)
        store.update(
            "paired_tasks", task_id,
            {"stop_reason": "fidelity_human_false"}, key="task_id",
        )
        try:
            _create_fidelity_stop_report(task_id, "fidelity_human_false", "保真人工否定")
        except paired_evidence.EvidenceError:
            return _complete_evidence_failure(command_id, task_id)
        store.add_event(task_id, "人工判定本次保真证据不真；只终止本任务", stage="保真", level="warn")
        return _complete_command(command_id, get(task_id), task_id=task_id)

    snapshots = secure_snapshots(paired)
    candidate = snapshots["candidate"]
    truth_id = None
    truth_kind = "legacy_target"
    if formal:
        now = time.time()
        if candidate.get("exact_configuration_id"):
            truth_kind = "configuration"
            with store.cursor() as cur:
                previous = cur.execute(
                    "SELECT id FROM configuration_fidelity_truths WHERE configuration_id=? "
                    "AND status='active' AND revoked_at IS NULL",
                    (candidate["exact_configuration_id"],),
                ).fetchall()
                cur.execute(
                    "UPDATE configuration_fidelity_truths SET status='replaced' "
                    "WHERE configuration_id=? AND status='active' AND revoked_at IS NULL",
                    (candidate["exact_configuration_id"],),
                )
                cur.execute(
                    "INSERT INTO configuration_fidelity_truths "
                    "(configuration_id,configuration_fingerprint,source_task_id,"
                    "source_manifest_root,status,reason,user_id,actor,valid_from,"
                    "expires_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        candidate["exact_configuration_id"],
                        candidate["configuration_fingerprint"], task_id,
                        paired["fidelity_manifest_root"], "active", reason.strip(),
                        user["id"], user["username"], now,
                        now + PAIRED_TRUTH_TTL_SECONDS, now,
                    ),
                )
                truth_id = int(cur.lastrowid)
                if previous:
                    cur.execute(
                        "UPDATE configuration_fidelity_truths SET replaced_by=? "
                        f"WHERE id IN ({','.join('?' for _ in previous)})",
                        (truth_id, *(row["id"] for row in previous)),
                    )
        else:
            with store.cursor() as cur:
                previous = cur.execute(
                    "SELECT id FROM paired_fidelity_truths WHERE target_id=? "
                    "AND upstream_multiplier=? AND status='active' AND revoked_at IS NULL",
                    (candidate["id"], candidate["upstream_multiplier"]),
                ).fetchall()
                cur.execute(
                    "UPDATE paired_fidelity_truths SET status='replaced' "
                    "WHERE target_id=? AND upstream_multiplier=? "
                    "AND status='active' AND revoked_at IS NULL",
                    (candidate["id"], candidate["upstream_multiplier"]),
                )
                cur.execute(
                    "INSERT INTO paired_fidelity_truths "
                    "(target_id,channel_id,upstream_multiplier,fidelity_fingerprint,"
                    "source_task_id,source_manifest_root,user_id,actor,status,reason,"
                    "valid_from,expires_at,formal,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        candidate["id"], candidate.get("channel_id"),
                        candidate["upstream_multiplier"], fidelity_fingerprint(candidate),
                        task_id, paired["fidelity_manifest_root"], user["id"],
                        user["username"], "active", reason.strip(), now,
                        now + PAIRED_TRUTH_TTL_SECONDS, 1, now,
                    ),
                )
                truth_id = int(cur.lastrowid)
                if previous:
                    cur.execute(
                        "UPDATE paired_fidelity_truths SET replaced_by=? "
                        f"WHERE id IN ({','.join('?' for _ in previous)})",
                        (truth_id, *(row["id"] for row in previous)),
                    )
    store.update(
        "paired_tasks", task_id, {
            "truth_definition_id": truth_id,
            "truth_definition_kind": truth_kind,
        }, key="task_id"
    )
    if paired["state"] != "stopped":
        try:
            transition(
                task_id, "awaiting_pair_start", expected_version=expected_state_version,
                reason="人工定义为真",
            )
        except paired_evidence.EvidenceError:
            return _complete_evidence_failure(command_id, task_id)
        store.add_event(task_id, "保真已人工定义为真，可选择标杆并启动双端测试", stage="保真")
    else:
        store.add_event(task_id, "已基于超时任务证据建立保真定义；原任务不恢复", stage="保真")
    return _complete_command(command_id, get(task_id), task_id=task_id)


def start_pair(
    task_id: int, benchmark_target_id: int, *, request_limit: int | None,
    token_limit: int | None, money_limit: float | None,
    expected_state_version: int, user: dict[str, Any], idempotency_key: str,
) -> dict[str, Any]:
    paired = store.get("paired_tasks", task_id, key="task_id")
    task = store.get("tasks", task_id)
    if not paired or not task:
        raise HTTPException(status_code=404, detail="双端准入任务不存在")
    command_payload = {
        "task_id": task_id,
        "benchmark_target_id": benchmark_target_id,
        "request_limit": request_limit,
        "token_limit": token_limit,
        "money_limit": money_limit,
        "expected_state_version": expected_state_version,
    }
    replay = _replay_command(
        user["id"], "start", idempotency_key, command_payload
    )
    if replay is not None:
        return replay
    require_ready()
    if paired["state"] != "awaiting_pair_start":
        raise HTTPException(status_code=400, detail="任务尚未通过人工保真或已经启动")
    if paired["state_version"] != expected_state_version:
        raise HTTPException(status_code=409, detail="任务状态版本已变化，请刷新后重试")
    if paired["formal"] and not frozen_truth_valid(paired):
        raise HTTPException(status_code=409, detail="本次启动前保真定义已失效，请重新执行保真")
    benchmark_row = store.get("targets", benchmark_target_id)
    if not benchmark_row:
        raise HTTPException(status_code=404, detail="标杆渠道不存在")
    if benchmark_target_id == paired["candidate_target_id"]:
        raise HTTPException(status_code=400, detail="候选端和标杆端必须是不同渠道配置")
    paired_protocol.validate_protocol(benchmark_row["protocol"])
    snapshots = secure_snapshots(paired)
    candidate = snapshots["candidate"]
    benchmark = _target_secure_snapshot(benchmark_row)
    if candidate.get("channel_id") is not None \
            and candidate.get("channel_id") == benchmark.get("channel_id"):
        same_channel_differences = (
            candidate["upstream_multiplier"] != benchmark["upstream_multiplier"],
            candidate["credential_fingerprint"] != benchmark["credential_fingerprint"],
            candidate["base_url"].rstrip("/") != benchmark["base_url"].rstrip("/"),
            candidate.get("group_name") != benchmark.get("group_name"),
            candidate.get("env") != benchmark.get("env"),
        )
        if not any(same_channel_differences):
            raise HTTPException(
                status_code=400,
                detail="同渠道双端配置必须在倍率、凭据、路由或分组中至少有一项不同",
            )
    if not models_equivalent(candidate["model"], benchmark["model"]):
        raise HTTPException(status_code=400, detail="双端配置模型不属于已确认的同一模型或精确别名")
    if money_limit is not None and any(
        side.get("price_in") is None or side.get("price_out") is None
        for side in (candidate, benchmark)
    ):
        raise HTTPException(status_code=400, detail="双端缺少可靠价格，不能启用金额硬预算")
    request_policy = _pair_request_policy(
        candidate, benchmark, snapshots["assets"]
    )
    snapshots["benchmark"] = benchmark
    snapshots["request_policy"] = request_policy
    public_snapshot = store.loads(task["snapshot"], {})
    public_snapshot["benchmark"] = public_target(benchmark)
    public_snapshot["identity_aliases"] = {
        "candidate": sorted(model_aliases(candidate["model"])),
        "benchmark": sorted(model_aliases(benchmark["model"])),
    }
    public_snapshot["budgets"] = {
        "request_limit": request_limit,
        "token_limit": token_limit,
        "money_limit": money_limit,
    }
    public_snapshot["request_policy"] = request_policy
    command_id, replay = _begin_command(
        user["id"], "start", idempotency_key, command_payload, task_id=task_id
    )
    if replay is not None:
        return replay
    assert command_id is not None
    state_key = secrets.token_urlsafe(24)
    now = time.time()
    secure_ciphertext = encrypt(json.dumps(snapshots, ensure_ascii=False))
    with store.cursor() as cur:
        cur.execute(
            "UPDATE paired_tasks SET benchmark_target_id=?,secure_snapshot_ciphertext=?,"
            "request_limit=?,token_limit=?,money_limit=?,ready_at=?,state='queued',"
            "state_version=?,idempotency_key=?,updated_at=? "
            "WHERE task_id=? AND state='awaiting_pair_start' AND state_version=?",
            (
                benchmark_target_id, secure_ciphertext, request_limit, token_limit,
                money_limit, now, expected_state_version + 1, state_key, now,
                task_id, expected_state_version,
            ),
        )
        if cur.rowcount != 1:
            raise HTTPException(status_code=409, detail="任务状态并发冲突")
        cur.execute(
            "UPDATE tasks SET target_name=?,snapshot=?,progress=?,status='queued' "
            "WHERE id=?",
            (
                f"{candidate['name']} ↔ {benchmark['name']}",
                store.dumps(public_snapshot),
                store.dumps({"done": 0, "total": 16, "current": "等待双端执行"}),
                task_id,
            ),
        )
    try:
        paired_evidence.append(task_id, "pair_configuration_snapshot", {
            "candidate": public_target(candidate),
            "benchmark": public_target(benchmark),
            "identity_aliases": public_snapshot["identity_aliases"],
            "budgets": public_snapshot["budgets"],
            "request_policy": request_policy,
            "scale_preview_confirmed": True,
            "confirmed_by": user["id"],
        })
        paired_evidence.append(task_id, "state_transition", {
            "from": "awaiting_pair_start", "to": "queued",
            "state_version": expected_state_version + 1,
            "reason": "用户确认规模并启动双端测试",
            "idempotency_key": state_key,
        })
    except paired_evidence.EvidenceError:
        return _complete_evidence_failure(command_id, task_id)
    auth.audit(
        user["id"], user["username"], "paired.start", "paired_task", str(task_id),
        "success", detail={"benchmark_target_id": benchmark_target_id},
    )
    return _complete_command(command_id, get(task_id), task_id=task_id)


def estimate(candidate_target_id: int) -> dict[str, Any]:
    target = store.get("targets", candidate_target_id)
    if not target:
        raise HTTPException(status_code=404, detail="候选渠道不存在")
    candidate = _target_secure_snapshot(target)
    truth = _valid_truth(candidate) if not PAIRED_SELFTEST_MODE else None
    maximum_requests = 516 if truth else 516 + (
        paired_assets.FIDELITY_ITEM_COUNT * paired_assets.FIDELITY_MAX_ATTEMPTS
    )
    manifest = paired_assets.manifest()
    formal_output = sum(item["max_tokens"] for item in manifest["speed"]) * 3 \
        + sum(item["max_tokens"] for item in manifest["ability"]) * 9
    fidelity_output = 0 if truth else sum(
        int(item["max_tokens"]) for item in manifest["fidelity"]
    ) * paired_assets.FIDELITY_MAX_ATTEMPTS
    auxiliary_output = fidelity_output + (32 + 64) * 6 + 32 * 56 * 3
    maximum_output_tokens = formal_output + auxiliary_output
    return {
        "asset_version": paired_assets.ASSET_VERSION,
        "truth_reused": bool(truth),
        "fidelity_logical_items": 0 if truth else paired_assets.FIDELITY_ITEM_COUNT,
        "maximum_fidelity_endpoint_requests": 0 if truth else (
            paired_assets.FIDELITY_ITEM_COUNT * paired_assets.FIDELITY_MAX_ATTEMPTS
        ),
        "base_logical_pairs": 16,
        "maximum_formal_pair_attempts": 84,
        "maximum_endpoint_requests": maximum_requests,
        "maximum_output_tokens": maximum_output_tokens,
        "concurrency": 2,
        "fixed_cooldown_seconds": 5,
        "active_time_limit_seconds": 3600,
        "budgets_optional": True,
    }


def save_report_revision(
    task_id: int, report: dict[str, Any], reason: str,
    created_by: int | None,
) -> dict[str, Any]:
    paired = store.get("paired_tasks", task_id, key="task_id")
    if not paired:
        raise RuntimeError("paired_task_missing")
    version = int(paired.get("report_version") or 0) + 1
    parent = version - 1 if version > 1 else None
    input_hashes = [
        row["record_hash"] for row in store.query(
            "SELECT record_hash FROM paired_evidence_records "
            "WHERE task_id=? ORDER BY record_seq", (task_id,),
        )
    ]
    frozen = {
        **report,
        "report_version": version,
        "integrity": {
            **(report.get("integrity") or {}),
            "input_evidence_hashes": input_hashes,
        },
    }
    report_hash = _json_hash(frozen)
    row_id = store.insert("paired_report_revisions", {
        "task_id": task_id, "version": version, "parent_version": parent,
        "reason": reason.strip(), "report_json": store.dumps(frozen),
        "input_manifest_root": str((frozen.get("integrity") or {}).get(
            "input_manifest_root") or paired.get("task_manifest_root") or ""),
        "algorithm_versions_json": store.dumps({
            "tokenizer": paired["tokenizer_version"],
            "grader": paired["grader_version"],
            "report": "paired-report-v1.0.0",
        }),
        "report_hash": report_hash, "created_by": created_by,
        "created_at": time.time(),
    })
    store.update(
        "paired_tasks", task_id, {"report_version": version}, key="task_id"
    )
    store.update("tasks", task_id, {"report": store.dumps(frozen)})
    return store.get("paired_report_revisions", row_id) or {}


def submit_conclusion(
    task_id: int, *, verdict: str, reason: str, evidence_refs: list[str],
    report_version: int, expected_conclusion_version: int,
    user: dict[str, Any], idempotency_key: str,
) -> dict[str, Any]:
    paired = store.get("paired_tasks", task_id, key="task_id")
    if not paired:
        raise HTTPException(status_code=404, detail="双端准入任务不存在")
    command_payload = {
        "task_id": task_id,
        "verdict": verdict,
        "reason": reason.strip(),
        "evidence_refs": evidence_refs,
        "report_version": report_version,
        "expected_conclusion_version": expected_conclusion_version,
    }
    replay = _replay_command(
        user["id"], "conclusion", idempotency_key, command_payload
    )
    if replay is not None:
        return replay
    if not paired["formal"]:
        raise HTTPException(status_code=400, detail="开发自检任务不能形成正式准入结论")
    integrity = verify_integrity(task_id, paired)
    if not integrity["ok"]:
        store.update(
            "paired_tasks", task_id,
            {
                "integrity_status": "failed",
                "integrity_error": str(integrity.get("reason") or "integrity_failed"),
            }, key="task_id",
        )
        raise HTTPException(status_code=409, detail="证据完整性失败，不能提交人工结论")
    report_rows = store.query(
        "SELECT * FROM paired_report_revisions WHERE task_id=? AND version=?",
        (task_id, report_version),
    )
    if not report_rows or report_version != paired["report_version"]:
        raise HTTPException(status_code=409, detail="报告版本已变化，请刷新后重新确认")
    current_rows = store.query(
        "SELECT conclusion_version FROM paired_conclusions WHERE task_id=? "
        "ORDER BY conclusion_version DESC LIMIT 1", (task_id,),
    )
    current = int(current_rows[0]["conclusion_version"]) if current_rows else 0
    if current != expected_conclusion_version:
        raise HTTPException(status_code=409, detail="人工结论版本已变化，请刷新后重试")
    if verdict == "admit" and paired["state"] not in {
        "completed", "completed_with_insufficient_metrics",
    }:
        raise HTTPException(status_code=400, detail="停止或不完整任务不能选择准入")
    if verdict == "admit" and not evidence_refs:
        raise HTTPException(status_code=400, detail="选择准入时必须引用报告区域或逐题证据")
    raw_expired = float(paired["raw_expires_at"]) <= time.time() or bool(store.query(
        "SELECT 1 FROM paired_raw_blocks WHERE task_id=? AND deleted_at IS NOT NULL LIMIT 1",
        (task_id,),
    ))
    if verdict == "admit" and raw_expired:
        raise HTTPException(status_code=400, detail="原始证据已到期，不能新增准入结论")
    command_id, replay = _begin_command(
        user["id"], "conclusion", idempotency_key,
        command_payload, task_id=task_id,
    )
    if replay is not None:
        return replay
    assert command_id is not None
    next_version = current + 1
    self_review = int(paired.get("created_by") == user["id"])
    row_id = store.insert("paired_conclusions", {
        "task_id": task_id, "report_version": report_version,
        "conclusion_version": next_version, "verdict": verdict,
        "reason": reason.strip(), "evidence_refs_json": store.dumps(evidence_refs),
        "self_review": self_review, "user_id": user["id"],
        "actor": user["username"], "created_at": time.time(),
    })
    auth.audit(
        user["id"], user["username"], "paired.conclusion.submit", "paired_task",
        str(task_id), "success", detail={
            "verdict": verdict, "report_version": report_version,
            "conclusion_version": next_version, "self_review": bool(self_review),
        },
    )
    row = store.get("paired_conclusions", row_id)
    assert row is not None
    return _complete_command(command_id, row, task_id=task_id)


def recalculate_report(
    task_id: int, reason: str, user: dict[str, Any], *,
    expected_report_version: int, idempotency_key: str,
) -> dict[str, Any]:
    paired = store.get("paired_tasks", task_id, key="task_id")
    task = store.get("tasks", task_id)
    command_payload = {
        "task_id": task_id,
        "reason": reason.strip(),
        "expected_report_version": expected_report_version,
    }
    replay = _replay_command(
        user["id"], "recalculate_report", idempotency_key, command_payload
    )
    if replay is not None:
        return replay
    if not paired or not task or paired["state"] not in TERMINAL_STATES:
        raise HTTPException(status_code=400, detail="只有已封存任务可以生成新报告版本")
    if int(paired.get("report_version") or 0) != expected_report_version:
        raise HTTPException(status_code=409, detail="报告版本已变化，请刷新后重试")
    if float(paired["raw_expires_at"]) <= time.time() or store.query(
        "SELECT 1 FROM paired_raw_blocks WHERE task_id=? AND deleted_at IS NOT NULL LIMIT 1",
        (task_id,),
    ):
        raise HTTPException(status_code=410, detail="原始证据已到期，不能重新计算报告")
    checked = verify_integrity(task_id, paired)
    if not checked["ok"]:
        raise HTTPException(status_code=409, detail="证据完整性失败，不能重新计算报告")
    command_id, replay = _begin_command(
        user["id"], "recalculate_report", idempotency_key,
        command_payload, task_id=task_id,
    )
    if replay is not None:
        return replay
    assert command_id is not None
    from . import paired_engine
    records = [
        record["payload"] for record in paired_evidence.records(task_id)
        if record["record_type"] == "derived_result"
    ]
    complete = paired["state"] in {"completed", "completed_with_insufficient_metrics"}
    report = paired_engine._build_report(
        task_id, records, complete=complete, status=paired["state"],
        stop_reason=paired.get("stop_reason") or "",
        task_root=paired["task_manifest_root"],
    )
    revision = save_report_revision(task_id, report, reason, user["id"])
    auth.audit(
        user["id"], user["username"], "paired.report.recalculate", "paired_task",
        str(task_id), "success", detail={
            "reason": reason.strip(), "report_version": revision["version"],
        },
    )
    return _complete_command(command_id, revision, task_id=task_id)


def extend_retention(
    task_id: int, *, raw_expires_at: float | None,
    structured_expires_at: float | None, reason: str,
    user: dict[str, Any], idempotency_key: str,
) -> dict[str, Any]:
    paired = store.get("paired_tasks", task_id, key="task_id")
    if not paired:
        raise HTTPException(status_code=404, detail="双端准入任务不存在")
    command_payload = {
        "task_id": task_id,
        "raw_expires_at": raw_expires_at,
        "structured_expires_at": structured_expires_at,
        "reason": reason.strip(),
    }
    replay = _replay_command(
        user["id"], "extend_retention", idempotency_key, command_payload
    )
    if replay is not None:
        return replay
    if raw_expires_at is None and structured_expires_at is None:
        raise HTTPException(status_code=400, detail="至少填写一种新的保留到期时间")
    now = time.time()
    if raw_expires_at is not None:
        if float(paired["raw_expires_at"]) <= now or store.query(
            "SELECT 1 FROM paired_raw_blocks WHERE task_id=? AND deleted_at IS NOT NULL LIMIT 1",
            (task_id,),
        ):
            raise HTTPException(status_code=410, detail="原始证据已经到期，不能恢复或延长")
        if raw_expires_at <= float(paired["raw_expires_at"]):
            raise HTTPException(status_code=400, detail="原始证据保留期只能延长")
    if structured_expires_at is not None:
        if float(paired["structured_expires_at"]) <= now:
            raise HTTPException(status_code=410, detail="结构化审计数据已经到期，不能恢复或延长")
        if structured_expires_at <= float(paired["structured_expires_at"]):
            raise HTTPException(status_code=400, detail="结构化审计数据保留期只能延长")
    command_id, replay = _begin_command(
        user["id"], "extend_retention", idempotency_key,
        command_payload, task_id=task_id,
    )
    if replay is not None:
        return replay
    assert command_id is not None
    updates: dict[str, Any] = {}
    with store.cursor() as cur:
        if raw_expires_at is not None:
            updates["raw_expires_at"] = raw_expires_at
            cur.execute(
                "UPDATE paired_raw_blocks SET expires_at=? "
                "WHERE task_id=? AND deleted_at IS NULL",
                (raw_expires_at, task_id),
            )
        if structured_expires_at is not None:
            updates["structured_expires_at"] = structured_expires_at
            cur.execute(
                "UPDATE paired_evidence_records SET retention_until=? WHERE task_id=?",
                (structured_expires_at, task_id),
            )
        assignments = ",".join(f"{key}=?" for key in updates)
        cur.execute(
            f"UPDATE paired_tasks SET {assignments},updated_at=? WHERE task_id=?",
            (*updates.values(), now, task_id),
        )
    auth.audit(
        user["id"], user["username"], "paired.retention.extend", "paired_task",
        str(task_id), "success", detail={**updates, "reason": reason.strip()},
    )
    result = {
        "task_id": task_id,
        "raw_expires_at": raw_expires_at or paired["raw_expires_at"],
        "structured_expires_at": structured_expires_at or paired["structured_expires_at"],
    }
    return _complete_command(command_id, result, task_id=task_id)


def revoke_truth(truth_id: int, reason: str, user: dict[str, Any]) -> dict[str, Any]:
    truth = store.get("paired_fidelity_truths", truth_id)
    if not truth or truth["status"] != "active" or truth.get("revoked_at"):
        raise HTTPException(status_code=404, detail="有效保真定义不存在")
    now = time.time()
    store.update("paired_fidelity_truths", truth_id, {
        "status": "revoked", "revoked_at": now, "reason": reason.strip(),
    })
    auth.audit(
        user["id"], user["username"], "paired.fidelity.revoke", "fidelity_truth",
        str(truth_id), "success", detail={"reason": reason.strip()},
    )
    return {**truth, "status": "revoked", "revoked_at": now, "reason": reason.strip()}


async def cancel(
    task_id: int, expected_state_version: int, user: dict[str, Any], *,
    idempotency_key: str,
) -> dict[str, Any]:
    paired = store.get("paired_tasks", task_id, key="task_id")
    task = store.get("tasks", task_id)
    if not paired or not task:
        raise HTTPException(status_code=404, detail="双端准入任务不存在")
    command_payload = {
        "task_id": task_id,
        "expected_state_version": expected_state_version,
    }
    replay = _replay_command(
        user["id"], "cancel", idempotency_key, command_payload
    )
    if replay is not None:
        return replay
    if paired["state"] in TERMINAL_STATES:
        raise HTTPException(status_code=400, detail="任务已经结束")
    if paired["state_version"] != expected_state_version:
        raise HTTPException(status_code=409, detail="任务状态版本已变化，请刷新后重试")
    command_id, replay = _begin_command(
        user["id"], "cancel", idempotency_key, command_payload, task_id=task_id
    )
    if replay is not None:
        return replay
    assert command_id is not None
    store.update("tasks", task_id, {"cancel_flag": 1})
    try:
        paired_evidence.append(task_id, "cancel_requested", {
            "expected_state_version": expected_state_version,
            "requested_by": user["id"],
            "actor": user["username"],
            "idempotency_key": idempotency_key,
        })
    except paired_evidence.EvidenceError:
        return _complete_evidence_failure(command_id, task_id)
    auth.audit(
        user["id"], user["username"], "paired.cancel", "paired_task",
        str(task_id), "success",
    )
    from . import paired_engine
    paired_engine.cancel_runtime(task_id)
    if paired["state"] in {"queued", "awaiting_fidelity_truth", "awaiting_pair_start"}:
        await paired_engine.cancel(task_id)
    return _complete_command(command_id, get(task_id), task_id=task_id)


def expire_waiting_truth(now: float | None = None) -> int:
    moment = now if now is not None else time.time()
    cutoff = moment - PAIRED_FIDELITY_WAIT_SECONDS
    rows = store.query(
        "SELECT * FROM paired_tasks WHERE state='awaiting_fidelity_truth' "
        "AND awaiting_truth_at IS NOT NULL AND awaiting_truth_at<=?", (cutoff,),
    )
    for row in rows:
        transition(row["task_id"], "stopped", reason="保真等待超过 24 小时")
        store.update(
            "paired_tasks", row["task_id"],
            {"stop_reason": "fidelity_unconfirmed"}, key="task_id",
        )
        _create_fidelity_stop_report(row["task_id"], "fidelity_unconfirmed", "保真未确认")
        store.update("tasks", row["task_id"], {"finished_at": moment})
    return len(rows)


def prices(snapshot: dict[str, Any]) -> tuple[float, float]:
    return (
        float(snapshot.get("price_in") or DEFAULT_PRICE_IN),
        float(snapshot.get("price_out") or DEFAULT_PRICE_OUT),
    )
