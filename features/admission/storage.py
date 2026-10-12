from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from shared.config import DATA_DIR
from features.model_coverage.workflow import WorkflowQueue
from shared.registry import Conflict


REPORT_DIR = DATA_DIR / "admission"
DB_PATH = REPORT_DIR / "reports.db"
MAX_REPORTS = 30
MAX_REPORT_BYTES = 4 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at REAL NOT NULL,
  reported_at TEXT NOT NULL,
  status TEXT NOT NULL,
  candidate_url TEXT NOT NULL,
  candidate_model TEXT NOT NULL,
  reference_name TEXT NOT NULL,
  reference_model TEXT NOT NULL,
  report_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admission_reports_created ON reports(created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS feishu_admission_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_url TEXT NOT NULL,
  test_group TEXT NOT NULL,
  status TEXT NOT NULL,
  fields_json TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  feishu_record_id TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admission_feishu_status
  ON feishu_admission_outbox(status,created_at,id);
"""

_connection: sqlite3.Connection | None = None
_lock = threading.RLock()


def init() -> None:
    global _connection
    with _lock:
        if _connection is not None:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            DB_PATH.parent.chmod(0o700)
        except OSError:
            pass
        _connection = sqlite3.connect(DB_PATH, check_same_thread=False)
        _connection.row_factory = sqlite3.Row
        _connection.execute("PRAGMA journal_mode=WAL")
        _connection.execute("PRAGMA secure_delete=ON")
        _connection.execute("PRAGMA busy_timeout=5000")
        _connection.executescript(SCHEMA)
        _connection.commit()
        try:
            DB_PATH.chmod(0o600)
        except OSError:
            pass


def close() -> None:
    global _connection
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    init()
    assert _connection is not None
    with _lock:
        current = _connection.cursor()
        try:
            yield current
            _connection.commit()
        except Exception:
            _connection.rollback()
            raise
        finally:
            current.close()


def _summary(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "id": int(row["id"]),
        "created_at": float(row["created_at"]),
        "reported_at": row["reported_at"],
        "status": row["status"],
        "candidate_url": row["candidate_url"],
        "candidate_model": row["candidate_model"],
        "reference_name": row["reference_name"],
        "reference_model": row["reference_model"],
    }
    if "report_json" in row.keys():
        try:
            report = json.loads(row["report_json"])
            summary = report.get("summary") if isinstance(report, dict) else {}
            counts = summary.get("counts", {}) if isinstance(summary, dict) else {}
            evidence = summary.get("evidence", {}) if isinstance(summary, dict) else {}
            result.update(
                overall_label=summary.get("overall", {}).get("label", "") if isinstance(summary, dict) else "",
                candidate_only=int(counts.get("candidate_only") or 0),
                shared=int(counts.get("shared") or 0),
                valid_pairs=int(evidence.get("valid_pairs") or 0),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return result


def save_report(report: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("准入报告超过 4 MiB，未保存到历史；仍可在当前页面下载")
    candidate = report["candidate"]
    reference = report["reference"]
    with cursor() as cur:
        cur.execute(
            "INSERT INTO reports(created_at,reported_at,status,candidate_url,candidate_model,"
            "reference_name,reference_model,report_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                time.time(), report["created_at"], report["status"],
                candidate["base_url"], candidate["model"],
                reference.get("name") or reference.get("base_url") or "参照端",
                reference["model"], encoded,
            ),
        )
        report_id = int(cur.lastrowid)
        cur.execute(
            "DELETE FROM reports WHERE id IN ("
            "SELECT id FROM reports ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET ?)",
            (MAX_REPORTS,),
        )
        row = cur.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    assert row is not None
    return _summary(row)


def list_reports(limit: int = MAX_REPORTS) -> list[dict[str, Any]]:
    limit = min(MAX_REPORTS, max(1, int(limit)))
    with cursor() as cur:
        rows = cur.execute(
            "SELECT id,created_at,reported_at,status,candidate_url,candidate_model,"
            "reference_name,reference_model,report_json FROM reports "
            "ORDER BY created_at DESC,id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_summary(row) for row in rows]


def get_report(report_id: int) -> dict[str, Any] | None:
    with cursor() as cur:
        row = cur.execute("SELECT id,report_json FROM reports WHERE id=?", (report_id,)).fetchone()
    if row is None:
        return None
    report = json.loads(row["report_json"])
    return {"id": int(row["id"]), **report}


def delete_report(report_id: int) -> bool:
    with cursor() as cur:
        return cur.execute("DELETE FROM reports WHERE id=?", (report_id,)).rowcount > 0


def _feishu_entry(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    result["fields"] = json.loads(result.pop("fields_json"))
    return result


def enqueue_feishu_record(
    channel_url: str,
    test_group: str,
    fields: dict[str, str],
    *,
    delivery_configured: bool,
) -> dict[str, Any]:
    now = time.time()
    status = "pending" if delivery_configured else "not_configured"
    encoded = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    with cursor() as cur:
        cur.execute(
            "INSERT INTO feishu_admission_outbox(channel_url,test_group,status,fields_json,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (channel_url, test_group, status, encoded, now, now),
        )
        entry_id = int(cur.lastrowid)
        row = cur.execute(
            "SELECT * FROM feishu_admission_outbox WHERE id=?", (entry_id,)
        ).fetchone()
    result = _feishu_entry(row)
    assert result is not None
    return result


def get_feishu_record(entry_id: int) -> dict[str, Any] | None:
    with cursor() as cur:
        row = cur.execute(
            "SELECT * FROM feishu_admission_outbox WHERE id=?", (entry_id,)
        ).fetchone()
    return _feishu_entry(row)


def latest_feishu_record() -> dict[str, Any] | None:
    with cursor() as cur:
        row = cur.execute(
            "SELECT * FROM feishu_admission_outbox ORDER BY created_at DESC,id DESC LIMIT 1"
        ).fetchone()
    return _feishu_entry(row)


def list_unsynced_feishu_records(limit: int = 100) -> list[dict[str, Any]]:
    with cursor() as cur:
        rows = cur.execute(
            "SELECT * FROM feishu_admission_outbox WHERE status!='synced' "
            "ORDER BY created_at,id LIMIT ?",
            (min(500, max(1, int(limit))),),
        ).fetchall()
    return [item for row in rows if (item := _feishu_entry(row)) is not None]


def recover_feishu_deliveries() -> int:
    now = time.time()
    with cursor() as cur:
        return cur.execute(
            "UPDATE feishu_admission_outbox SET status='failed',"
            "last_error='上次写入未完成，可在重启后重试',updated_at=? WHERE status='sending'",
            (now,),
        ).rowcount


def begin_feishu_delivery(entry_id: int) -> dict[str, Any]:
    now = time.time()
    with cursor() as cur:
        row = cur.execute(
            "SELECT * FROM feishu_admission_outbox WHERE id=?", (entry_id,)
        ).fetchone()
        if row is None:
            raise KeyError("飞书准入记录不存在")
        if row["status"] == "synced":
            result = _feishu_entry(row)
            assert result is not None
            return result
        if row["status"] == "sending":
            raise RuntimeError("飞书准入记录正在写入")
        cur.execute(
            "UPDATE feishu_admission_outbox SET status='sending',attempts=attempts+1,"
            "last_error='',updated_at=? WHERE id=?",
            (now, entry_id),
        )
        updated = cur.execute(
            "SELECT * FROM feishu_admission_outbox WHERE id=?", (entry_id,)
        ).fetchone()
    result = _feishu_entry(updated)
    assert result is not None
    return result


def finish_feishu_delivery(entry_id: int, record_id: str) -> dict[str, Any]:
    now = time.time()
    with cursor() as cur:
        changed = cur.execute(
            "UPDATE feishu_admission_outbox SET status='synced',feishu_record_id=?,"
            "last_error='',updated_at=? WHERE id=?",
            (record_id, now, entry_id),
        ).rowcount
        if changed != 1:
            raise KeyError("飞书准入记录不存在")
    result = get_feishu_record(entry_id)
    assert result is not None
    return result


def fail_feishu_delivery(entry_id: int, message: str) -> dict[str, Any]:
    now = time.time()
    with cursor() as cur:
        changed = cur.execute(
            "UPDATE feishu_admission_outbox SET status='failed',last_error=?,updated_at=? "
            "WHERE id=?",
            (message[:300], now, entry_id),
        ).rowcount
        if changed != 1:
            raise KeyError("飞书准入记录不存在")
    result = get_feishu_record(entry_id)
    assert result is not None
    return result


JOB_TYPE = "admission:compare"
MAX_JOB_EVENTS = 2048
JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS admission_jobs (
  task_id INTEGER PRIMARY KEY,
  configuration_json TEXT NOT NULL,
  report_json TEXT NOT NULL,
  report_id INTEGER,
  remaining_seconds REAL NOT NULL,
  execution_deadline REAL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS admission_steps (
  task_id INTEGER NOT NULL,
  step_id TEXT NOT NULL,
  state TEXT NOT NULL,
  PRIMARY KEY(task_id,step_id)
);
CREATE TABLE IF NOT EXISTS admission_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  event_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admission_events_task ON admission_events(task_id,id);
CREATE TABLE IF NOT EXISTS admission_step_results (
  task_id INTEGER NOT NULL,
  step_id TEXT NOT NULL,
  result_json TEXT NOT NULL,
  PRIMARY KEY(task_id,step_id)
);
"""


class JobStore:
    """Queue metadata and report checkpoints share one SQLite transaction boundary."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self.queue = WorkflowQueue(self)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA + JOB_SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        self.path.chmod(0o600)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _encode(report):
        encoded = json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > MAX_REPORT_BYTES:
            raise ValueError("准入报告超过 4 MiB")
        return encoded

    def create(self, task_id, configuration, report, steps):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO admission_jobs(task_id,configuration_json,report_json,remaining_seconds,updated_at) VALUES(?,?,?,?,?)",
                         (task_id, json.dumps(configuration, ensure_ascii=False), self._encode(report),
                          self.queue.get(task_id)["budget_seconds"], time.time()))
            conn.executemany("INSERT OR IGNORE INTO admission_steps(task_id,step_id,state) VALUES(?,?,'not_sent')",
                             [(task_id, step) for step in steps])

    def get(self, task_id):
        task = self.queue.get(task_id)
        if task["task_type"] != JOB_TYPE:
            raise KeyError("准入任务不存在")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM admission_jobs WHERE task_id=?", (task_id,)).fetchone()
            steps = conn.execute("SELECT step_id,state FROM admission_steps WHERE task_id=?", (task_id,)).fetchall()
            results = conn.execute("SELECT step_id,result_json FROM admission_step_results WHERE task_id=?", (task_id,)).fetchall()
            position = conn.execute("SELECT COUNT(*) FROM eval_workflow_tasks WHERE task_type=? AND state IN ('queued','running','waiting_credentials') AND (created_at<? OR (created_at=? AND id<=?))",
                                    (JOB_TYPE, task["created_at"], task["created_at"], task_id)).fetchone()[0]
        if row is None:
            raise KeyError("准入任务快照不存在")
        return {**task, "configuration": json.loads(row["configuration_json"]),
                "report": json.loads(row["report_json"]), "report_id": row["report_id"],
                "remaining_seconds": row["remaining_seconds"], "execution_deadline": row["execution_deadline"],
                "steps": {step["step_id"]: step["state"] for step in steps},
                "step_results": {item["step_id"]: json.loads(item["result_json"]) for item in results},
                "queue_position": int(position) if task["state"] in {"queued", "running", "waiting_credentials"} else None}

    def list(self, limit=MAX_REPORTS):
        with self.connect() as conn:
            ids = conn.execute("SELECT j.task_id FROM admission_jobs j JOIN eval_workflow_tasks t ON t.id=j.task_id WHERE t.state IN ('queued','running','waiting_credentials') ORDER BY t.created_at,t.id").fetchall()
            ids += conn.execute("SELECT j.task_id FROM admission_jobs j JOIN eval_workflow_tasks t ON t.id=j.task_id WHERE t.state IN ('succeeded','failed','cancelled') ORDER BY t.created_at DESC,t.id DESC LIMIT ?", (min(500, max(1, limit)),)).fetchall()
        return [self.get(row[0]) for row in ids]

    @staticmethod
    def _event(conn, task_id, event):
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 256 * 1024:
            raise ValueError("准入事件超过大小限制")
        conn.execute("INSERT INTO admission_events(task_id,event_json) VALUES(?,?)", (task_id, encoded))
        conn.execute("DELETE FROM admission_events WHERE task_id=? AND id NOT IN (SELECT id FROM admission_events WHERE task_id=? ORDER BY id DESC LIMIT ?)",
                     (task_id, task_id, MAX_JOB_EVENTS))

    def checkpoint(self, task_id, report, event, *, step_id=None, state=None, result=None):
        encoded = self._encode(report)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE admission_jobs SET report_json=?,updated_at=? WHERE task_id=?", (encoded, time.time(), task_id))
            if step_id is not None:
                conn.execute("UPDATE admission_steps SET state=? WHERE task_id=? AND step_id=?", (state, task_id, step_id))
                if result is not None:
                    conn.execute("INSERT OR REPLACE INTO admission_step_results(task_id,step_id,result_json) VALUES(?,?,?)", (task_id, step_id, json.dumps(result, ensure_ascii=False, allow_nan=False)))
            self._event(conn, task_id, event)

    def begin_step(self, task_id, step_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT t.state,t.cancel_requested,j.execution_deadline FROM eval_workflow_tasks t JOIN admission_jobs j ON t.id=j.task_id WHERE t.id=?", (task_id,)).fetchone()
            if row is None or row["state"] != "running" or row["cancel_requested"] or (row["execution_deadline"] is not None and time.time() >= row["execution_deadline"]):
                return False
            return conn.execute("UPDATE admission_steps SET state='sending' WHERE task_id=? AND step_id=? AND state='not_sent'", (task_id, step_id)).rowcount == 1

    def skip_unused_token_steps(self, task_id):
        with self.connect() as conn:
            conn.execute("UPDATE admission_steps SET state='not_needed' WHERE task_id=? AND step_id LIKE 'token:%' AND state='not_sent'", (task_id,))

    def start_budget(self, task_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT remaining_seconds FROM admission_jobs WHERE task_id=?", (task_id,)).fetchone()
            seconds = max(0, row[0])
            conn.execute("UPDATE admission_jobs SET execution_deadline=? WHERE task_id=?", (time.time() + seconds, task_id))
        return seconds

    def recover(self):
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT t.id,t.cancel_requested,j.report_json,j.execution_deadline,j.remaining_seconds FROM eval_workflow_tasks t JOIN admission_jobs j ON t.id=j.task_id WHERE t.task_type=? AND (t.state IN ('running','queued','waiting_credentials') OR (t.state='cancelled' AND j.report_id IS NULL))", (JOB_TYPE,)).fetchall()
            for row in rows:
                remaining = min(row["remaining_seconds"], max(0, row["execution_deadline"] - now)) if row["execution_deadline"] is not None else row["remaining_seconds"]
                conn.execute("UPDATE admission_jobs SET remaining_seconds=?,execution_deadline=NULL WHERE task_id=?", (remaining, row["id"]))
                conn.execute("UPDATE admission_steps SET state='unknown' WHERE task_id=? AND state='sending'", (row["id"],))
                if row["cancel_requested"]:
                    report = json.loads(row["report_json"])
                    report["status"] = "canceled"
                    self._finish_transaction(conn, row["id"], report, "cancelled", "", now)
                    continue
                conn.execute("UPDATE eval_workflow_tasks SET state='waiting_credentials',updated_at=? WHERE id=?", (now, row["id"]))
                self._event(conn, row["id"], {"type": "credentials_required", "reason": "process_restarted"})
            # A process can end between queue creation and the credential-free snapshot write.
            conn.execute("UPDATE eval_workflow_tasks SET state='failed',error='任务创建未完成',updated_at=?,finished_at=? WHERE task_type=? AND state IN ('queued','running','waiting_credentials') AND id NOT IN (SELECT task_id FROM admission_jobs)", (now, now, JOB_TYPE))
        return len(rows)

    def supply(self, task_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("UPDATE eval_workflow_tasks SET state='queued',updated_at=? WHERE id=? AND task_type=? AND state='waiting_credentials' AND cancel_requested=0", (time.time(), task_id, JOB_TYPE)).rowcount
            if not changed:
                raise Conflict("任务当前不能补充凭据")

    def _finish_transaction(self, conn, task_id, report, state, error, now):
        conn.execute("UPDATE admission_steps SET state='unknown' WHERE task_id=? AND state='sending'", (task_id,))
        unresolved = conn.execute("SELECT step_id FROM admission_steps WHERE task_id=? AND state='unknown' AND step_id NOT LIKE 'token:%'", (task_id,)).fetchall()
        measured = {(row["round"], row["question_id"], row["side"]) for row in report.get("measurements", [])}
        for step in unresolved:
            number, question_id, side = step[0].split(":")
            identity = (int(number), question_id, side)
            if identity not in measured:
                report.setdefault("measurements", []).append({"type": "side_finished", "round": int(number), "question_id": question_id,
                    "side": side, "pair_id": f"{report['run_id']}:{number}:{question_id}",
                    "round_role": "warmup" if int(number) <= report.get("warmup_rounds", 0) else "evaluated",
                    "ok": False, "status": "unknown", "transport_completed": False, "error_category": "unknown",
                    "error": "请求已发送但结果未确认，未重放"})
        encoded = self._encode(report)
        candidate, reference = report["candidate"], report["reference"]
        cur = conn.execute("INSERT INTO reports(created_at,reported_at,status,candidate_url,candidate_model,reference_name,reference_model,report_json) VALUES(?,?,?,?,?,?,?,?)",
                           (now, report["created_at"], report["status"], candidate["base_url"], candidate["model"], reference.get("name") or reference["base_url"], reference["model"], encoded))
        report_id = cur.lastrowid
        conn.execute("UPDATE admission_jobs SET report_json=?,report_id=?,execution_deadline=NULL,updated_at=? WHERE task_id=?", (encoded, report_id, now, task_id))
        conn.execute("UPDATE eval_workflow_tasks SET state=?,result_json=?,error=?,updated_at=?,finished_at=? WHERE id=?",
                     (state, json.dumps({"report_id": report_id}), error, now, now, task_id))
        self._event(conn, task_id, {"type": "run_finished", "status": report["status"], "report_id": report_id})
        conn.execute("DELETE FROM reports WHERE id IN (SELECT id FROM reports ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET ?)", (MAX_REPORTS,))
        conn.execute("UPDATE admission_jobs SET report_json='{}' WHERE task_id IN (SELECT id FROM eval_workflow_tasks WHERE state IN ('succeeded','failed','cancelled')) AND report_id NOT IN (SELECT id FROM reports)")

    def finish(self, task_id, report, state, error=""):
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM eval_workflow_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError("准入任务不存在")
            if row["state"] not in {"running", "cancelled"}:
                raise Conflict("任务当前不能完成")
            saved = conn.execute("SELECT report_id FROM admission_jobs WHERE task_id=?", (task_id,)).fetchone()
            if saved and saved[0] is not None:
                return self.get(task_id)
            if row["cancel_requested"]:
                state, report["status"] = "cancelled", "canceled"
            self._finish_transaction(conn, task_id, report, state, error, now)
        return self.get(task_id)

    def events(self, task_id, after=0, limit=200):
        self.get(task_id)
        with self.connect() as conn:
            first = conn.execute("SELECT MIN(id) FROM admission_events WHERE task_id=?", (task_id,)).fetchone()[0]
            rows = conn.execute("SELECT id,event_json FROM admission_events WHERE task_id=? AND id>? ORDER BY id LIMIT ?", (task_id, after, min(500, max(1, limit)))).fetchall()
        return {"events": [{"id": row["id"], **json.loads(row["event_json"])} for row in rows],
                "after": rows[-1]["id"] if rows else after, "truncated": bool(first and after and after < first - 1)}
