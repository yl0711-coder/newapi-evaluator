from __future__ import annotations

import json
from .domain import encode, fingerprint, VERSION


class Store:
    def __init__(self, registry):
        self.registry = registry
        with registry.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS daily_check_plan(id INTEGER PRIMARY KEY CHECK(id=1),
                spec TEXT NOT NULL,revision TEXT NOT NULL,enabled INTEGER NOT NULL,updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_check_batches(id TEXT PRIMARY KEY,start_at REAL NOT NULL,
                report_at REAL NOT NULL UNIQUE,spec TEXT NOT NULL,revision TEXT NOT NULL,status TEXT NOT NULL,
                report TEXT,created_at REAL NOT NULL,finalized_at REAL);
            CREATE TABLE IF NOT EXISTS daily_check_items(batch_id TEXT NOT NULL,channel_id INTEGER NOT NULL,
                family TEXT NOT NULL,spec TEXT NOT NULL,mt_job_id TEXT,usage_job_id TEXT,
                mt_reason TEXT NOT NULL,usage_reason TEXT NOT NULL,PRIMARY KEY(batch_id,channel_id,family));
            CREATE TABLE IF NOT EXISTS daily_check_messages(batch_id TEXT NOT NULL,part INTEGER NOT NULL,
                text TEXT NOT NULL,status TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(batch_id,part));
            """)

    def plan(self):
        with self.registry.connect() as db:
            row = db.execute("SELECT * FROM daily_check_plan WHERE id=1").fetchone()
        return {"enabled": bool(row["enabled"]), "revision": row["revision"], "updated_at": row["updated_at"],
                **json.loads(row["spec"])} if row else {"enabled": False, "revision": "none", "updated_at": 0,
                                                    "version": VERSION, "profiles": [], "notify": True}

    def save_plan(self, spec, enabled, now):
        revision = fingerprint({"spec": spec, "enabled": enabled, "saved_at": now})
        with self.registry.connect() as db:
            db.execute("INSERT OR REPLACE INTO daily_check_plan VALUES(1,?,?,?,?)", (encode(spec), revision, int(enabled), now))
        return self.plan()

    def pause(self, now):
        current = self.plan()
        return self.save_plan({k: current[k] for k in ("version", "profiles", "notify")}, False, now)

    def create_batch(self, slot, plan, items, now):
        batch_id = "daily-" + fingerprint([VERSION, slot["report_at"]])[:24]
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT revision,enabled FROM daily_check_plan WHERE id=1").fetchone()
            if current is None or not current["enabled"] or current["revision"] != plan["revision"]:
                return None
            if db.execute("SELECT 1 FROM daily_check_batches WHERE report_at=?", (slot["report_at"],)).fetchone():
                return None
            db.execute("INSERT INTO daily_check_batches VALUES(?,?,?,?,?,'collecting',NULL,?,NULL)",
                       (batch_id, slot["start_at"], slot["report_at"], encode(plan), plan["revision"], now))
            for row in items:
                db.execute("INSERT INTO daily_check_items VALUES(?,?,?,?,NULL,NULL,?,?)",
                           (batch_id, row["channel_id"], row["family"], encode(row), row["mt_reason"], row["usage_reason"]))
        return batch_id

    def has_batch(self, report_at):
        with self.registry.connect() as db:
            return db.execute("SELECT 1 FROM daily_check_batches WHERE report_at=?", (report_at,)).fetchone() is not None

    def prune(self, cutoff):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT b.id FROM daily_check_batches b WHERE b.status!='collecting' AND b.report_at<? AND NOT EXISTS (SELECT 1 FROM daily_check_messages m WHERE m.batch_id=b.id AND m.status!='sent')", (cutoff,)).fetchall()
            for row in rows:
                for table in ("daily_check_messages", "daily_check_items"):
                    db.execute(f"DELETE FROM {table} WHERE batch_id=?", (row[0],))
                db.execute("DELETE FROM daily_check_batches WHERE id=?", (row[0],))
        return len(rows)

    def items(self, batch_id):
        with self.registry.connect() as db:
            rows = db.execute("SELECT * FROM daily_check_items WHERE batch_id=? ORDER BY channel_id,family", (batch_id,)).fetchall()
        return [{**json.loads(r["spec"]), **{k: r[k] for k in ("batch_id", "mt_job_id", "usage_job_id", "mt_reason", "usage_reason")}} for r in rows]

    def update_item(self, item, method, *, job_id=None, reason=""):
        if method not in {"mt", "usage"}:
            raise ValueError("invalid_method")
        with self.registry.connect() as db:
            db.execute(f"UPDATE daily_check_items SET {method}_job_id=COALESCE(?,{method}_job_id),{method}_reason=? WHERE batch_id=? AND channel_id=? AND family=?",
                       (job_id, reason, item["batch_id"], item["channel_id"], item["family"]))

    def batches(self, *, collecting=False):
        with self.registry.connect() as db:
            rows = db.execute("SELECT * FROM daily_check_batches" + (" WHERE status='collecting'" if collecting else "") + " ORDER BY report_at DESC LIMIT 100").fetchall()
        return [{**dict(r), "spec": json.loads(r["spec"]), "report": json.loads(r["report"]) if r["report"] else None} for r in rows]

    def batch(self, batch_id):
        with self.registry.connect() as db:
            row = db.execute("SELECT * FROM daily_check_batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise KeyError("batch_not_found")
        return {**dict(row), "spec": json.loads(row["spec"]), "report": json.loads(row["report"]) if row["report"] else None}

    def finish(self, batch_id, report, messages, now, *, status="finalized"):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("UPDATE daily_check_batches SET status=?,report=?,finalized_at=? WHERE id=? AND status='collecting'",
                          (status, encode(report), now, batch_id)).rowcount != 1:
                return
            for part, message in enumerate(messages, 1):
                db.execute("INSERT INTO daily_check_messages VALUES(?,?,?,'pending','')", (batch_id, part, message))

    def messages(self, batch_id):
        with self.registry.connect() as db:
            return [dict(r) for r in db.execute("SELECT part,status,error FROM daily_check_messages WHERE batch_id=? ORDER BY part", (batch_id,))]

    def claim_message(self, now):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT m.* FROM daily_check_messages m JOIN daily_check_batches b ON b.id=m.batch_id WHERE m.status='pending' AND b.report_at<=? ORDER BY b.report_at,m.part LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE daily_check_messages SET status='sending' WHERE batch_id=? AND part=?", (row["batch_id"], row["part"]))
            return dict(row)

    def message_result(self, row, status, error=""):
        with self.registry.connect() as db:
            db.execute("UPDATE daily_check_messages SET status=?,error=? WHERE batch_id=? AND part=? AND status='sending'",
                       (status, error, row["batch_id"], row["part"]))

    def recover_notifications(self):
        with self.registry.connect() as db:
            db.execute("UPDATE daily_check_messages SET status='unknown',error='delivery_unconfirmed' WHERE status='sending'")

    def retry_notifications(self, batch_id):
        self.batch(batch_id)
        with self.registry.connect() as db:
            return db.execute("UPDATE daily_check_messages SET status='pending',error='' WHERE batch_id=? AND status IN ('failed','unknown','disabled')", (batch_id,)).rowcount
