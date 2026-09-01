"""本地压力执行器的配对、密钥封装、租约领取和签名结果回传。"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import store
from .security import scrub

PAIRING_TTL_SECONDS = 10 * 60
RUNNER_ONLINE_SECONDS = 90
JOB_LEASE_SECONDS = 120
MIN_RUNNER_VERSION = "1.0.0"


class RunnerError(ValueError):
    pass


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split(".")[:3])
    except ValueError:
        return (0,)


def runner_out(row: dict[str, Any]) -> dict[str, Any]:
    online = bool(
        not row["revoked_at"] and row["last_seen_at"]
        and time.time() - row["last_seen_at"] <= RUNNER_ONLINE_SECONDS
    )
    return {
        "id": row["id"], "name": row["name"],
        "status": "online" if online else ("revoked" if row["revoked_at"] else "offline"),
        "version": row["version"],
        "minimum_version": MIN_RUNNER_VERSION,
        "update_available": _version_tuple(row["version"]) < _version_tuple(MIN_RUNNER_VERSION),
        "capabilities": store.loads(row["capabilities_json"], {}),
        "last_seen_at": row["last_seen_at"], "created_at": row["created_at"],
    }


def create_pairing_code(name: str, created_by: int) -> dict[str, Any]:
    clean = name.strip()
    if not clean:
        raise RunnerError("请填写执行器名称")
    code = f"{secrets.token_hex(3).upper()}-{secrets.token_hex(3).upper()}"
    now = time.time()
    code_id = store.insert("runner_pairing_codes", {
        "runner_name": clean, "code_hash": _digest(code),
        "expires_at": now + PAIRING_TTL_SECONDS, "created_by": created_by,
        "created_at": now,
    })
    return {"id": code_id, "runner_name": clean, "pairing_code": code,
            "expires_at": now + PAIRING_TTL_SECONDS}


def pair(
    pairing_code: str, encryption_public_key: str, signing_public_key: str,
    version: str, capabilities: dict[str, Any],
) -> dict[str, Any]:
    rows = store.query(
        "SELECT * FROM runner_pairing_codes WHERE code_hash=? AND used_at IS NULL "
        "AND expires_at>?", (_digest(pairing_code.strip().upper()), time.time()),
    )
    if not rows:
        raise RunnerError("配对码无效、已使用或已过期")
    try:
        X25519PublicKey.from_public_bytes(_unb64(encryption_public_key))
        Ed25519PublicKey.from_public_bytes(_unb64(signing_public_key))
    except Exception as exc:
        raise RunnerError("执行器公钥格式无效") from exc
    token = secrets.token_urlsafe(40)
    now = time.time()
    runner_id = store.insert("paired_runners", {
        "name": rows[0]["runner_name"],
        "encryption_public_key": encryption_public_key,
        "signing_public_key": signing_public_key,
        "token_hash": _digest(token), "status": "online", "version": version,
        "capabilities_json": store.dumps(capabilities), "last_seen_at": now,
        "created_at": now, "updated_at": now,
    })
    store.update("runner_pairing_codes", rows[0]["id"], {"used_at": now})
    return {"runner_id": runner_id, "runner_token": token,
            "minimum_version": MIN_RUNNER_VERSION}


def authenticate(runner_id: int, authorization: str) -> dict[str, Any]:
    row = store.get("paired_runners", runner_id)
    token = authorization.removeprefix("Bearer ").strip()
    if not row or row["revoked_at"] or not token \
            or not secrets.compare_digest(row["token_hash"], _digest(token)):
        raise RunnerError("执行器身份校验失败")
    return row


def heartbeat(
    runner: dict[str, Any], version: str, capabilities: dict[str, Any],
) -> dict[str, Any]:
    store.update("paired_runners", runner["id"], {
        "status": "online", "version": version,
        "capabilities_json": store.dumps(capabilities),
        "last_seen_at": time.time(), "updated_at": time.time(),
    })
    updated = store.get("paired_runners", runner["id"])
    assert updated is not None
    return runner_out(updated)


def _seal(public_key_text: str, payload: dict[str, Any]) -> dict[str, str]:
    recipient = X25519PublicKey.from_public_bytes(_unb64(public_key_text))
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(recipient)
    salt = secrets.token_bytes(16)
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=salt,
        info=b"api-evaluator-local-runner-job-v1",
    ).derive(shared)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(
        nonce, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), None
    )
    ephemeral_public = ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return {"version": "x25519-aesgcm-v1", "ephemeral_public_key": _b64(ephemeral_public),
            "salt": _b64(salt), "nonce": _b64(nonce), "ciphertext": _b64(ciphertext)}


def create_job(
    task_id: int, runner_id: int, target: dict[str, Any], api_key: str,
    job_config: dict[str, Any],
) -> dict[str, Any]:
    runner = store.get("paired_runners", runner_id)
    if not runner or runner["revoked_at"]:
        raise RunnerError("本地执行器不存在或已撤销")
    status = runner_out(runner)["status"]
    if status != "online":
        raise RunnerError("本地执行器当前离线，请启动执行器后再提交")
    expires_at = time.time() + 3600
    sealed = _seal(runner["encryption_public_key"], {
        "base_url": target["base_url"], "protocol": target["protocol"],
        "model": target["model"], "api_key": api_key, "expires_at": expires_at,
    })
    now = time.time()
    job_id = store.insert("runner_jobs", {
        "task_id": task_id, "runner_id": runner_id, "status": "queued",
        "encrypted_credentials_json": store.dumps(sealed),
        "job_config_json": store.dumps(job_config),
        "result_nonce": secrets.token_urlsafe(24), "result_json": "{}",
        "telemetry_json": "{}", "created_at": now, "updated_at": now,
    })
    store.update("tasks", task_id, {
        "progress": store.dumps({"stage": "等待本地执行器领取", "runner_id": runner_id})
    })
    store.add_event(task_id, f"压力任务已分配给本地执行器 {runner['name']}", stage="本地执行器")
    return job_out(store.get("runner_jobs", job_id))


def job_out(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        raise RunnerError("本地执行任务不存在")
    return {
        "id": row["id"], "task_id": row["task_id"], "runner_id": row["runner_id"],
        "status": row["status"], "config": store.loads(row["job_config_json"], {}),
        "result": store.loads(row["result_json"], {}),
        "telemetry": store.loads(row["telemetry_json"], {}),
        "lease_expires_at": row["lease_expires_at"], "claimed_at": row["claimed_at"],
        "started_at": row["started_at"], "finished_at": row["finished_at"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def poll(runner: dict[str, Any]) -> dict[str, Any] | None:
    now = time.time()
    store.execute(
        "UPDATE runner_jobs SET status='queued',lease_expires_at=NULL,updated_at=? "
        "WHERE runner_id=? AND status='claimed' AND lease_expires_at<?",
        (now, runner["id"], now),
    )
    rows = store.query(
        "SELECT * FROM runner_jobs WHERE runner_id=? AND status='queued' "
        "ORDER BY created_at,id LIMIT 1", (runner["id"],),
    )
    heartbeat(runner, runner["version"], store.loads(runner["capabilities_json"], {}))
    if not rows:
        return None
    job = rows[0]
    lease = now + JOB_LEASE_SECONDS
    store.update("runner_jobs", job["id"], {
        "status": "claimed", "claimed_at": now, "started_at": now,
        "lease_expires_at": lease, "updated_at": now,
    })
    task = store.get("tasks", job["task_id"])
    if task:
        store.update("tasks", task["id"], {
            "status": "running", "started_at": task["started_at"] or now,
            "progress": store.dumps({"stage": "本地执行器运行中", "runner_id": runner["id"]}),
        })
    return {
        "job_id": job["id"], "task_id": job["task_id"],
        "result_nonce": job["result_nonce"], "lease_expires_at": lease,
        "encrypted_credentials": store.loads(job["encrypted_credentials_json"], {}),
        "config": store.loads(job["job_config_json"], {}),
    }


def result_signing_bytes(
    job_id: int, result_nonce: str, status: str,
    report: dict[str, Any], telemetry: dict[str, Any],
) -> bytes:
    return json.dumps({
        "job_id": job_id, "result_nonce": result_nonce, "status": status,
        "report": report, "telemetry": telemetry,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _reject_sensitive(value: Any, path: str = "result") -> None:
    forbidden = {"api_key", "key", "authorization", "prompt", "messages",
                 "raw_response", "response_body", "request_body"}
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in forbidden:
                raise RunnerError(f"结果包含禁止字段：{path}.{key}")
            _reject_sensitive(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive(item, f"{path}[{index}]")


def submit_result(
    runner: dict[str, Any], job_id: int, status: str, report: dict[str, Any],
    telemetry: dict[str, Any], signature: str,
) -> dict[str, Any]:
    job = store.get("runner_jobs", job_id)
    if not job or job["runner_id"] != runner["id"]:
        raise RunnerError("任务不属于该执行器")
    if job["status"] not in {"claimed", "cancel_requested"}:
        raise RunnerError("任务不处于可回传状态")
    _reject_sensitive(report)
    _reject_sensitive(telemetry, "telemetry")
    try:
        public = Ed25519PublicKey.from_public_bytes(_unb64(runner["signing_public_key"]))
        public.verify(
            _unb64(signature),
            result_signing_bytes(job_id, job["result_nonce"], status, report, telemetry),
        )
    except Exception as exc:
        raise RunnerError("结果签名校验失败") from exc
    now = time.time()
    safe_report = _standard_report(report, telemetry, status, now)
    store.update("runner_jobs", job_id, {
        "status": status, "encrypted_credentials_json": "{}",
        "result_json": store.dumps(report), "telemetry_json": store.dumps(telemetry),
        "lease_expires_at": None, "finished_at": now, "updated_at": now,
    })
    task_status = "success" if status == "success" else status
    store.update("tasks", job["task_id"], {
        "status": task_status, "report": store.dumps(safe_report),
        "progress": store.dumps({"stage": "本地压力测试已完成", "runner_id": runner["id"]}),
        "finished_at": now,
    })
    store.add_event(job["task_id"], f"本地执行器签名结果已验证：{status}", stage="本地执行器")
    return job_out(store.get("runner_jobs", job_id))


def _standard_report(
    result: dict[str, Any], telemetry: dict[str, Any], status: str, finished_at: float,
) -> dict[str, Any]:
    load = result.get("load") or {"rounds": result.get("rounds", []),
                                  "safe_concurrency": result.get("safe_concurrency", 0)}
    rounds = load.get("rounds") or []
    requests = sum(int(row.get("requests") or 0) for row in rounds)
    successes = sum(round(float(row.get("success_rate") or 0) * int(row.get("requests") or 0))
                    for row in rounds)
    return {
        "summary": "本地压力测试完成" if status == "success" else "本地压力测试未完成",
        "metrics": {
            "total": requests, "passed": successes,
            "pass_rate": successes / requests if requests else 0,
            "avg_latency": 0, "p95_latency": 0, "avg_first_token": 0,
            "avg_tokens_per_second": 0, "cost": float(result.get("cost") or 0),
            "load": load, "local_runner": telemetry,
        },
        "conclusion": {
            "code": "load_complete" if status == "success" else "load_incomplete",
            "verdict": "本地压力测试完成" if status == "success" else "本地压力测试失败",
            "reasons": ["压力流量由配对本地执行器产生，云端未执行压力请求"],
            "actions": ["结合本地 CPU、网络和发生器饱和指标复核容量拐点"],
        },
        "advice": ["结合本地 CPU、网络和发生器饱和指标复核容量拐点"],
        "steps": [], "timeline": [], "layers": {},
        "versions": {"local_runner_protocol": "1"}, "finished_at": finished_at,
        "security": {"credentials_cleared": True, "signature_verified": True},
    }


def request_cancel(task_id: int) -> None:
    rows = store.query("SELECT * FROM runner_jobs WHERE task_id=?", (task_id,))
    if rows and rows[0]["status"] in {"queued", "claimed"}:
        store.update("runner_jobs", rows[0]["id"], {
            "status": "cancel_requested", "updated_at": time.time(),
        })


def runner_job_status(runner: dict[str, Any], job_id: int) -> dict[str, Any]:
    job = store.get("runner_jobs", job_id)
    if not job or job["runner_id"] != runner["id"]:
        raise RunnerError("任务不属于该执行器")
    return {"job_id": job_id, "status": job["status"],
            "cancel_requested": job["status"] == "cancel_requested"}
