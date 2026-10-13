"""Durable paired permits, frozen configs and atomic principal-scoped Outbox."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager

from shared.registry import Conflict
from .domain import digest, empty_result, observation


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ScheduleStopped(RuntimeError):
    pass


class Store:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.path = directory / "token-usage.db"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS bindings(model TEXT,protocol TEXT,snapshot TEXT NOT NULL,
                revision TEXT NOT NULL,thresholds TEXT NOT NULL, PRIMARY KEY(model,protocol));
            CREATE TABLE IF NOT EXISTS configs(id INTEGER PRIMARY KEY AUTOINCREMENT,spec TEXT NOT NULL,
                enabled INTEGER NOT NULL,next_due REAL NOT NULL,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,principal TEXT NOT NULL,idem TEXT NOT NULL,
                payload_hash TEXT NOT NULL,event_id TEXT,spec TEXT NOT NULL,status TEXT NOT NULL,
                result TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,owner TEXT,lease_until REAL,
                cancelled INTEGER NOT NULL DEFAULT 0,config_id INTEGER,occurrence REAL,
                UNIQUE(principal,idem),UNIQUE(principal,event_id),UNIQUE(config_id,occurrence));
            CREATE TABLE IF NOT EXISTS attempts(job_id TEXT NOT NULL,step_id TEXT NOT NULL,
                request_id TEXT NOT NULL UNIQUE,state TEXT NOT NULL,result TEXT,
                PRIMARY KEY(job_id,step_id));
            CREATE TABLE IF NOT EXISTS events(cursor INTEGER PRIMARY KEY AUTOINCREMENT,principal TEXT NOT NULL,
                job_id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,created_at REAL NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        self.path.chmod(0o600)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def binding(self, model, protocol):
        with self.connect() as db:
            row = db.execute("SELECT * FROM bindings WHERE model=? AND protocol=?", (model, protocol)).fetchone()
        if not row:
            raise KeyError("trusted_reference_missing")
        return {"model": model, "protocol": protocol, "snapshot": json.loads(row["snapshot"]), "revision": row["revision"], "thresholds": json.loads(row["thresholds"])}

    def bindings(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM bindings ORDER BY model,protocol").fetchall()
        return [{**dict(row), "snapshot": json.loads(row["snapshot"]), "thresholds": json.loads(row["thresholds"])} for row in rows]

    def save_binding(self, model, protocol, snapshot, thresholds):
        revision = digest([snapshot, thresholds])
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO bindings VALUES(?,?,?,?,?)", (model, protocol, encode(snapshot), revision, encode(thresholds)))
        return self.binding(model, protocol)

    def submitted(self, *, principal, idem, event_id, payload_hash):
        with self.connect() as db:
            return self._submitted(db, principal, idem, event_id, payload_hash)

    def _submitted(self, db, principal, idem, event_id, payload_hash):
        rows = db.execute("SELECT * FROM jobs WHERE principal=? AND (idem=? OR (? IS NOT NULL AND event_id=?))",
                          (principal, idem, event_id, event_id)).fetchall()
        if any(row["payload_hash"] != payload_hash for row in rows) or len(rows) > 1:
            raise Conflict("idempotency_conflict")
        return self._job(rows[0]) if rows else None

    def enqueue(self, spec, *, principal, idem, event_id=None, config_id=None, occurrence=None, now=None, payload_hash=None):
        now = time.time() if now is None else now
        payload_hash = payload_hash or digest(spec)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            submitted = self._submitted(db, principal, idem, event_id, payload_hash)
            if submitted is not None:
                return submitted
            job_id = "tok-" + secrets.token_hex(12)
            db.execute("INSERT INTO jobs(id,principal,idem,payload_hash,event_id,spec,status,created_at,updated_at,config_id,occurrence) VALUES(?,?,?,?,?,?,'queued',?,?,?,?)",
                       (job_id, principal, idem, payload_hash, event_id, encode(spec), now, now, config_id, occurrence))
            for index in range(2):
                for side in ("candidate", "reference"):
                    step = f"token:{side}:{index}"
                    request_id = digest([job_id, step])
                    db.execute("INSERT INTO attempts VALUES(?,?,?,'not_sent',NULL)", (job_id, step, request_id))
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    @staticmethod
    def _job(row):
        spec = json.loads(row["spec"])
        return {"job_id": row["id"], "status": row["status"], "result": json.loads(row["result"]) if row["result"] else None,
                "created_at": row["created_at"], "updated_at": row["updated_at"], "source": spec["source"],
                "target": spec["candidate"], "reference": spec["reference"], "display_identity": spec.get("display_identity"),
                "config_id": row["config_id"], "occurrence": row["occurrence"]}

    def get(self, job_id, *, principal="workbench", administrative=False):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or (not administrative and row["principal"] != principal):
                raise KeyError("job_not_found")
            job = self._job(row)
            job["cancellation_requested"] = bool(row["cancelled"])
            job["attempts"] = [{**dict(a), "result": json.loads(a["result"]) if a["result"] else None}
                               for a in db.execute("SELECT step_id,request_id,state,result FROM attempts WHERE job_id=? ORDER BY step_id", (job_id,))]
            return job

    def list(self, *, principal="workbench", administrative=False):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200" if administrative else
                              "SELECT * FROM jobs WHERE principal=? ORDER BY created_at DESC LIMIT 200",
                              () if administrative else (principal,)).fetchall()
        return [self._job(row) for row in rows]

    def claim(self, owner, *, active=True, scheduled_only=False, active_only=False, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute("SELECT * FROM jobs WHERE (status='running' AND (lease_until IS NULL OR lease_until<?)) OR (status='queued' AND cancelled=1)", (now,)).fetchall()
            for row in expired:
                unknown = observation([], status="unknown", transport_status="lease_expired")
                db.execute("UPDATE attempts SET state='unknown',result=? WHERE job_id=? AND state='sending'", (encode(unknown), row["id"]))
                if row["cancelled"]:
                    spec = json.loads(row["spec"])
                    sent = db.execute("SELECT COUNT(*) FROM attempts WHERE job_id=? AND state IN ('confirmed','unknown','sending')", (row["id"],)).fetchone()[0]
                    result = empty_result("cancelled_unknown" if sent else "cancelled_not_sent", source=spec["source"], log_evidence=spec.get("log_evidence"))
                    result.update(target=spec["candidate"], reference=spec["reference"], display_identity=spec.get("display_identity"),
                                  condition_hash=spec["condition_hash"], frozen_hash=spec["frozen_hash"],
                                  thresholds=spec["thresholds"], occurrence=row["occurrence"], requests_sent=sent, new_requests_sent=0)
                    previous = json.loads(row["result"]) if row["result"] else None
                    if previous and previous.get("severity") == "high_risk":
                        result = {**previous, "verification_status": "cancelled_unknown", "requests_sent": max(sent, previous.get("requests_sent", 0)), "new_requests_sent": 0}
                    db.execute("UPDATE jobs SET status='cancelled',result=?,owner=NULL,lease_until=NULL,updated_at=? WHERE id=?", (encode(result), now, row["id"]))
                    db.execute("INSERT INTO events(principal,job_id,kind,payload,created_at) VALUES(?,?,'result',?,?)", (row["principal"], row["id"], encode(result), now))
                else:
                    db.execute("UPDATE jobs SET status='queued',owner=NULL,lease_until=NULL,updated_at=? WHERE id=?", (now, row["id"]))
            row = db.execute("SELECT * FROM jobs WHERE status='queued' AND cancelled=0" +
                             (" AND config_id IS NULL" if active_only else " AND config_id IS NOT NULL" if scheduled_only or not active else "") + " ORDER BY CASE WHEN config_id IS NULL THEN 0 ELSE 1 END,created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running',owner=?,lease_until=?,updated_at=? WHERE id=?", (owner, now + 90, now, row["id"]))
            return {**dict(row), "spec": json.loads(row["spec"])}

    def heartbeat(self, job_id, owner):
        with self.connect() as db:
            db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND owner=? AND status='running'", (time.time() + 90, job_id, owner))

    @staticmethod
    def _schedule_stop_reason(db, config_id, frozen_spec):
        if config_id is None:
            return None
        config = db.execute("SELECT enabled,spec FROM configs WHERE id=?", (config_id,)).fetchone()
        if config is None:
            return "schedule_config_changed"
        current, frozen = json.loads(config["spec"]), json.loads(frozen_spec)
        current.pop("display_identity", None)
        frozen.pop("display_identity", None)
        if current != frozen:
            return "schedule_config_changed"
        return None if config["enabled"] else "schedule_paused"

    def schedule_stop_reason(self, config_id, spec):
        with self.connect() as db:
            return self._schedule_stop_reason(db, config_id, encode(spec))

    def permit(self, job_id, owner, side, index):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["owner"] != owner or row["status"] != "running" or row["cancelled"] or row["lease_until"] < time.time():
                raise RuntimeError("send_not_permitted")
            reason = self._schedule_stop_reason(db, row["config_id"], row["spec"])
            if reason:
                raise ScheduleStopped(reason)
            changed = db.execute("UPDATE attempts SET state='sending' WHERE job_id=? AND step_id=? AND state='not_sent'",
                                 (job_id, f"token:{side}:{index}")).rowcount
            if changed != 1:
                raise RuntimeError("attempt_not_replayable")

    def attempt(self, job_id, owner, side, index, result):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner,status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row["owner"] != owner or row["status"] != "running":
                raise RuntimeError("lease_lost")
            state = "unknown" if result["status"] == "unknown" else "not_sent" if result["status"] == "not_sent" else "confirmed"
            db.execute("UPDATE attempts SET state=?,result=? WHERE job_id=? AND step_id=?", (state, encode(result), job_id, f"token:{side}:{index}"))

    def publish(self, job_id, owner, result, *, terminal=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row["owner"] != owner or row["status"] != "running":
                raise RuntimeError("lease_lost")
            previous = json.loads(row["result"]) if row["result"] else None
            if previous and previous.get("severity") == "high_risk" and result.get("severity") != "high_risk":
                result = {**previous, "verification_status": result.get("reason", "interrupted"),
                          "requests_sent": max(previous.get("requests_sent", 0), result.get("requests_sent", 0)),
                          "new_requests_sent": result.get("new_requests_sent", 0)}
            if row["cancelled"] and terminal:
                status = "cancelled"
            else:
                status = "completed" if terminal else "running"
            db.execute("UPDATE jobs SET result=?,status=?,updated_at=? WHERE id=?", (encode(result), status, time.time(), job_id))
            if terminal:
                db.execute("UPDATE attempts SET state='unknown',result=COALESCE(result,?) WHERE job_id=? AND state='sending'",
                           (encode(observation([], status="unknown", transport_status="execution_interrupted")), job_id))
                db.execute("UPDATE jobs SET owner=NULL,lease_until=NULL WHERE id=?", (job_id,))
            if terminal or (result.get("severity") == "high_risk" and (not previous or previous.get("severity") != "high_risk")):
                db.execute("INSERT INTO events(principal,job_id,kind,payload,created_at) VALUES(?,?,?,?,?)",
                           (row["principal"], job_id, "result" if terminal else "alert", encode(result), time.time()))

    def cancel(self, job_id, *, principal="workbench", administrative=False):
        self.get(job_id, principal=principal, administrative=administrative)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row["status"] == "queued":
                spec = json.loads(row["spec"])
                result = json.loads(row["result"]) if row["result"] else empty_result("cancelled_not_sent", source=spec["source"], log_evidence=spec["log_evidence"])
                result = {**result, "target": spec["candidate"], "reference": spec["reference"], "display_identity": spec.get("display_identity"),
                          "condition_hash": spec["condition_hash"], "frozen_hash": spec["frozen_hash"], "thresholds": spec["thresholds"], "occurrence": row["occurrence"]}
                db.execute("UPDATE jobs SET cancelled=1,status='cancelled',result=?,updated_at=? WHERE id=?", (encode(result), time.time(), job_id))
                db.execute("INSERT INTO events(principal,job_id,kind,payload,created_at) VALUES(?,?,'result',?,?)", (row["principal"],job_id,encode(result),time.time()))
            elif row["status"] == "running":
                db.execute("UPDATE jobs SET cancelled=1 WHERE id=?", (job_id,))
        return self.get(job_id, principal=principal, administrative=administrative)

    def events(self, principal, after, limit):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE cursor>? AND (principal=? OR principal='scheduled') ORDER BY cursor LIMIT ?", (after, principal, limit)).fetchall()
        events = [{"cursor": row["cursor"], "job_id": row["job_id"], "kind": row["kind"], "result": json.loads(row["payload"])} for row in rows]
        return {"events": events, "next_cursor": events[-1]["cursor"] if events else after}
