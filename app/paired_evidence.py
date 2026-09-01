"""双端准入原始证据的加密数据块、顺序哈希链和封存根。"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import store
from .security import audit_hmac, decrypt, encrypt


class EvidenceError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _encrypted_raw(raw: str) -> tuple[str, str]:
    data_key = Fernet.generate_key()
    return encrypt(data_key.decode("ascii")), Fernet(data_key).encrypt(
        raw.encode("utf-8")
    ).decode("ascii")


def append(
    task_id: int, record_type: str, payload: dict[str, Any], *,
    raw: str | None = None, block_type: str = "",
) -> dict[str, Any]:
    wall_time = time.time()
    monotonic_time = time.monotonic()
    payload_json = canonical_json(payload)
    try:
        with store.cursor() as cur:
            task = cur.execute(
                "SELECT raw_expires_at,structured_expires_at FROM paired_tasks "
                "WHERE task_id=?", (task_id,),
            ).fetchone()
            if not task:
                raise EvidenceError("paired_task_missing")
            previous = cur.execute(
                "SELECT record_seq,record_hash FROM paired_evidence_records "
                "WHERE task_id=? ORDER BY record_seq DESC LIMIT 1", (task_id,),
            ).fetchone()
            record_seq = int(previous["record_seq"] + 1) if previous else 1
            previous_hash = str(previous["record_hash"]) if previous else "0" * 64
            raw_block_id = None
            raw_hash = ""
            if raw is not None:
                raw_hash = _sha256(raw)
                key_ciphertext, ciphertext = _encrypted_raw(raw)
                cur.execute(
                    "INSERT INTO paired_raw_blocks "
                    "(task_id,block_type,key_ciphertext,ciphertext,content_hash,created_at,expires_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (task_id, block_type or record_type, key_ciphertext, ciphertext,
                     raw_hash, wall_time, task["raw_expires_at"]),
                )
                raw_block_id = int(cur.lastrowid)
            record_material = canonical_json({
                "task_id": task_id,
                "record_seq": record_seq,
                "record_type": record_type,
                "payload": json.loads(payload_json),
                "raw_hash": raw_hash,
                "previous_record_hash": previous_hash,
                "wall_time": wall_time,
                "monotonic_time": monotonic_time,
            })
            record_hash = _sha256(record_material)
            record_hmac = audit_hmac("paired-evidence", record_hash)
            cur.execute(
                "INSERT INTO paired_evidence_records "
                "(task_id,record_seq,record_type,payload_json,raw_block_id,"
                "previous_record_hash,record_hash,record_hmac,wall_time,monotonic_time,"
                "retention_until) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, record_seq, record_type, payload_json, raw_block_id,
                 previous_hash, record_hash, record_hmac, wall_time, monotonic_time,
                 task["structured_expires_at"]),
            )
    except Exception as exc:
        raise EvidenceError(type(exc).__name__) from exc
    return {
        "task_id": task_id,
        "record_seq": record_seq,
        "record_hash": record_hash,
        "raw_block_id": raw_block_id,
        "wall_time": wall_time,
        "monotonic_time": monotonic_time,
    }


def read_raw(raw_block_id: int) -> str:
    row = store.get("paired_raw_blocks", raw_block_id)
    if not row or row.get("deleted_at") or float(row["expires_at"]) <= time.time() \
            or not row.get("ciphertext"):
        raise EvidenceError("raw_evidence_expired")
    try:
        data_key = decrypt(row["key_ciphertext"]).encode("ascii")
        raw = Fernet(data_key).decrypt(row["ciphertext"].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise EvidenceError("raw_evidence_decryption_failed") from exc
    if _sha256(raw) != row["content_hash"]:
        raise EvidenceError("raw_evidence_hash_mismatch")
    return raw


def records(task_id: int, *, include_raw: bool = False) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT * FROM paired_evidence_records WHERE task_id=? ORDER BY record_seq",
        (task_id,),
    )
    output: list[dict[str, Any]] = []
    for row in rows:
        item = {
            "record_seq": row["record_seq"],
            "record_type": row["record_type"],
            "payload": store.loads(row["payload_json"], {}),
            "record_hash": row["record_hash"],
            "previous_record_hash": row["previous_record_hash"],
            "wall_time": row["wall_time"],
            "monotonic_time": row["monotonic_time"],
            "raw_block_id": row["raw_block_id"],
        }
        if include_raw and row["raw_block_id"]:
            item["raw"] = read_raw(row["raw_block_id"])
        output.append(item)
    return output


def verify(task_id: int, *, verify_raw: bool = True) -> dict[str, Any]:
    rows = store.query(
        "SELECT * FROM paired_evidence_records WHERE task_id=? ORDER BY record_seq",
        (task_id,),
    )
    previous_hash = "0" * 64
    for expected_seq, row in enumerate(rows, 1):
        if row["record_seq"] != expected_seq or row["previous_record_hash"] != previous_hash:
            return {"ok": False, "record_seq": expected_seq, "reason": "sequence_or_link"}
        raw_hash = ""
        if row["raw_block_id"]:
            raw_row = store.get("paired_raw_blocks", row["raw_block_id"])
            if not raw_row:
                return {"ok": False, "record_seq": expected_seq, "reason": "raw_missing"}
            raw_hash = str(raw_row["content_hash"])
            if verify_raw and not raw_row.get("deleted_at") \
                    and float(raw_row["expires_at"]) > time.time():
                try:
                    read_raw(row["raw_block_id"])
                except EvidenceError as exc:
                    return {"ok": False, "record_seq": expected_seq, "reason": str(exc)}
        material = canonical_json({
            "task_id": task_id,
            "record_seq": row["record_seq"],
            "record_type": row["record_type"],
            "payload": store.loads(row["payload_json"], {}),
            "raw_hash": raw_hash,
            "previous_record_hash": row["previous_record_hash"],
            "wall_time": row["wall_time"],
            "monotonic_time": row["monotonic_time"],
        })
        record_hash = _sha256(material)
        if record_hash != row["record_hash"] \
                or audit_hmac("paired-evidence", record_hash) != row["record_hmac"]:
            return {"ok": False, "record_seq": expected_seq, "reason": "hash_or_hmac"}
        previous_hash = record_hash
    return {"ok": True, "count": len(rows), "last_hash": previous_hash}


def seal_manifest(task_id: int, stage: str, start_seq: int = 1) -> str:
    checked = verify(task_id)
    if not checked["ok"]:
        raise EvidenceError(f"integrity:{checked['reason']}:{checked.get('record_seq', 0)}")
    rows = store.query(
        "SELECT record_seq,record_hash FROM paired_evidence_records "
        "WHERE task_id=? AND record_seq>=? ORDER BY record_seq", (task_id, start_seq),
    )
    end_seq = rows[-1]["record_seq"] if rows else start_seq - 1
    last_hash = rows[-1]["record_hash"] if rows else "0" * 64
    paired = store.get("paired_tasks", task_id, key="task_id") or {}
    manifest = {
        "task_id": task_id,
        "stage": stage,
        "start_seq": start_seq,
        "end_seq": end_seq,
        "last_hash": last_hash,
        "fidelity_manifest_root": str(paired.get("fidelity_manifest_root") or "")
        if stage == "task" else "",
    }
    material = canonical_json(manifest)
    return canonical_json({
        **manifest,
        "manifest_hash": _sha256(material),
        "manifest_hmac": audit_hmac("paired-manifest", material),
        "version": "paired-manifest-v1",
    })


def verify_manifest(
    task_id: int, root: str, *, expected_stage: str | None = None,
) -> dict[str, Any]:
    checked = verify(task_id)
    if not checked["ok"]:
        return checked
    try:
        manifest = json.loads(root)
        if not isinstance(manifest, dict):
            raise ValueError("manifest_not_object")
        start_seq = int(manifest["start_seq"])
        end_seq = int(manifest["end_seq"])
        stage = str(manifest["stage"])
        last_hash = str(manifest["last_hash"])
        material = canonical_json({
            "task_id": int(manifest["task_id"]),
            "stage": stage,
            "start_seq": start_seq,
            "end_seq": end_seq,
            "last_hash": last_hash,
            "fidelity_manifest_root": str(manifest["fidelity_manifest_root"]),
        })
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {"ok": False, "reason": "manifest_format"}
    if int(manifest["task_id"]) != task_id:
        return {"ok": False, "reason": "manifest_task"}
    if expected_stage is not None and stage != expected_stage:
        return {"ok": False, "reason": "manifest_stage"}
    if manifest.get("version") != "paired-manifest-v1" \
            or manifest.get("manifest_hash") != _sha256(material) \
            or manifest.get("manifest_hmac") != audit_hmac("paired-manifest", material):
        return {"ok": False, "reason": "manifest_authentication"}
    rows = store.query(
        "SELECT record_seq,record_hash FROM paired_evidence_records "
        "WHERE task_id=? AND record_seq BETWEEN ? AND ? ORDER BY record_seq",
        (task_id, start_seq, end_seq),
    )
    expected_count = max(0, end_seq - start_seq + 1)
    if len(rows) != expected_count:
        return {"ok": False, "reason": "manifest_coverage"}
    observed_last = rows[-1]["record_hash"] if rows else "0" * 64
    if observed_last != last_hash:
        return {"ok": False, "reason": "manifest_last_hash"}
    return {
        "ok": True, "stage": stage, "start_seq": start_seq,
        "end_seq": end_seq, "last_hash": last_hash,
        "fidelity_manifest_root": str(manifest["fidelity_manifest_root"]),
    }


def purge_expired_raw(now: float | None = None) -> int:
    cutoff = now if now is not None else time.time()
    rows = store.query(
        "SELECT id,task_id,content_hash FROM paired_raw_blocks "
        "WHERE deleted_at IS NULL AND expires_at<=?", (cutoff,),
    )
    for row in rows:
        store.update("paired_raw_blocks", row["id"], {
            "key_ciphertext": "", "ciphertext": "", "deleted_at": cutoff,
        })
        store.insert("audit_events", {
            "user_id": None, "actor": "system", "action": "paired.raw.expire",
            "object_type": "paired_raw_block", "object_id": str(row["id"]),
            "result": "success", "request_id": "", "ip": "",
            "detail_json": store.dumps({
                "task_id": row["task_id"], "content_hash": row["content_hash"],
                "deleted_at": cutoff,
            }),
            "created_at": cutoff,
        })
    return len(rows)
