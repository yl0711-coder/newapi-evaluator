"""业务库与指标库的加密备份、恢复演练和容量守卫。"""
import asyncio
import hashlib
import io
import json
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import store
from .config import (BACKUP_DIR, BACKUP_HOUR_UTC, BACKUP_KEY_PATH, BASE_DIR,
                     DB_PATH, METRIC_DB_PATH, SQLITE_MIGRATION_BYTES)

_task: asyncio.Task | None = None
CHECK_SECONDS = 300
REPORT_ARCHIVE_DAYS = 90


def _fernet() -> Fernet:
    if not BACKUP_KEY_PATH.exists():
        BACKUP_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_KEY_PATH.write_bytes(Fernet.generate_key())
        try:
            BACKUP_KEY_PATH.chmod(0o600)
        except OSError:
            pass
    return Fernet(BACKUP_KEY_PATH.read_bytes())


def _online_copy(source_path: Path, destination: Path) -> None:
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _integrity(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def run_backup(run_date: str | None = None) -> dict[str, Any]:
    date = run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = store.query(
        "SELECT * FROM backup_runs WHERE run_date=? AND kind='daily'", (date,)
    )
    if existing and existing[0]["status"] == "success":
        return {**existing[0], "manifest": store.loads(existing[0]["manifest_json"], {})}
    started = time.time()
    if existing:
        run_id = existing[0]["id"]
        store.update("backup_runs", run_id, {
            "status": "running", "error": "", "started_at": started,
        })
    else:
        run_id = store.insert("backup_runs", {
            "run_date": date, "kind": "daily", "status": "running",
            "started_at": started,
        })
    try:
        with tempfile.TemporaryDirectory(prefix="evaluator-backup-") as temp_name:
            temp_dir = Path(temp_name)
            copies = {
                "business.db": temp_dir / "business.db",
                "metrics.db": temp_dir / "metrics.db",
            }
            _online_copy(DB_PATH, copies["business.db"])
            _online_copy(METRIC_DB_PATH, copies["metrics.db"])
            manifest = {
                "version": 1, "created_at": time.time(), "run_date": date,
                "databases": {name: {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size, "integrity": _integrity(path),
                } for name, path in copies.items()},
            }
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for name, path in copies.items():
                    bundle.write(path, name)
                bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            encrypted = _fernet().encrypt(archive.getvalue())
            output = BACKUP_DIR / f"evaluator-{date}.backup.enc"
            output.write_bytes(encrypted)
        finished = time.time()
        store.update("backup_runs", run_id, {
            "status": "success", "backup_path": str(output),
            "manifest_json": store.dumps(manifest), "finished_at": finished,
        })
        archive_result = store.archive_task_reports(
            time.time() - REPORT_ARCHIVE_DAYS * 86400
        )
        return {"id": run_id, "status": "success", "backup_path": str(output),
                "manifest": manifest, "report_archive": archive_result,
                "finished_at": finished}
    except Exception as exc:
        store.update("backup_runs", run_id, {
            "status": "failed", "error": str(exc)[:500], "finished_at": time.time(),
        })
        store.record_system_alert(
            f"backup:{date}", "backup", "每日加密备份失败", str(exc)[:500], "critical",
        )
        raise


def restore_drill(backup_path: str | None = None) -> dict[str, Any]:
    if backup_path:
        source = Path(backup_path).resolve()
    else:
        completed = store.query(
            "SELECT * FROM backup_runs WHERE kind='daily' AND status='success' "
            "ORDER BY finished_at DESC LIMIT 1"
        )
        if not completed:
            raise ValueError("尚无成功备份可供恢复演练")
        source = Path(completed[0]["backup_path"]).resolve()
    if source.parent != BACKUP_DIR and BACKUP_DIR not in source.parents:
        raise ValueError("恢复演练只允许使用配置备份目录内的文件")
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    run_id = store.insert("backup_runs", {
        "run_date": run_date, "kind": "restore_drill", "status": "running",
        "backup_path": str(source), "started_at": time.time(),
    })
    try:
        try:
            payload = _fernet().decrypt(source.read_bytes())
        except InvalidToken as exc:
            raise ValueError("备份密钥不匹配或文件已损坏") from exc
        with tempfile.TemporaryDirectory(prefix="evaluator-restore-") as temp_name:
            temp_dir = Path(temp_name)
            with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
                if set(bundle.namelist()) != {"business.db", "metrics.db", "manifest.json"}:
                    raise ValueError("备份文件清单不完整")
                bundle.extractall(temp_dir)
            manifest = json.loads((temp_dir / "manifest.json").read_text(encoding="utf-8"))
            checks = {}
            for name in ("business.db", "metrics.db"):
                path = temp_dir / name
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
                expected = manifest["databases"][name]["sha256"]
                integrity = _integrity(path)
                checks[name] = {"sha256_match": sha == expected, "integrity": integrity}
                if sha != expected or integrity != "ok":
                    raise ValueError(f"{name} 恢复校验失败")
        result = {"status": "success", "checks": checks, "finished_at": time.time()}
        store.update("backup_runs", run_id, {
            "status": "success", "manifest_json": store.dumps(result),
            "finished_at": result["finished_at"],
        })
        return {"id": run_id, **result}
    except Exception as exc:
        store.update("backup_runs", run_id, {
            "status": "failed", "error": str(exc)[:500], "finished_at": time.time(),
        })
        store.record_system_alert(
            "restore-drill", "backup", "备份恢复演练失败", str(exc)[:500], "critical",
        )
        raise


def capacity_status() -> dict[str, Any]:
    rows = []
    for name, path in (("business", DB_PATH), ("metrics", METRIC_DB_PATH)):
        size = path.stat().st_size if path.exists() else 0
        rows.append({"database": name, "bytes": size,
                     "threshold_bytes": SQLITE_MIGRATION_BYTES,
                     "migration_required": size >= SQLITE_MIGRATION_BYTES})
    return {"databases": rows, "postgresql_migration_required": any(
        row["migration_required"] for row in rows
    )}


def status() -> dict[str, Any]:
    latest = store.query("SELECT * FROM backup_runs ORDER BY id DESC LIMIT 10")
    try:
        offsite = BACKUP_DIR != BASE_DIR and BASE_DIR not in BACKUP_DIR.parents
    except ValueError:
        offsite = False
    return {"backup_dir": str(BACKUP_DIR), "offsite_configured": offsite,
            "latest_runs": latest, **capacity_status()}


async def _loop() -> None:
    while True:
        now = datetime.now(timezone.utc)
        if now.hour == BACKUP_HOUR_UTC:
            await asyncio.to_thread(run_backup, now.strftime("%Y-%m-%d"))
        await asyncio.sleep(CHECK_SECONDS)


async def start() -> None:
    global _task
    if not _task or _task.done():
        _task = asyncio.create_task(_loop(), name="encrypted-backups")


async def stop() -> None:
    if _task:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
