import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory / "diagnosis.db"
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY, created_at REAL NOT NULL, features TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, preview_id TEXT UNIQUE NOT NULL, created_at REAL NOT NULL,
                    state TEXT NOT NULL, stop_reason TEXT NOT NULL, snapshot TEXT NOT NULL, results TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def import_cases(self, cases):
        with self.connect() as conn:
            if conn.execute("SELECT count(*) FROM cases").fetchone()[0] + len(cases) > 1000:
                raise ValueError("案例库已满，请删除不再需要的案例")
            values = [{"id": uuid.uuid4().hex, "created_at": time.time(), **case.model_dump()} for case in cases]
            conn.executemany("INSERT INTO cases VALUES(?,?,?)", [
                (row["id"], row["created_at"], json.dumps(row)) for row in values])
        return values

    def cases(self):
        with self.connect() as conn:
            return [json.loads(row[0]) for row in conn.execute("SELECT features FROM cases ORDER BY created_at DESC")]

    def case(self, identifier):
        with self.connect() as conn:
            row = conn.execute("SELECT features FROM cases WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError("未找到案例")
        return json.loads(row[0])

    def delete_case(self, identifier):
        with self.connect() as conn:
            conn.execute("DELETE FROM cases WHERE id=?", (identifier,))

    def create_run(self, preview_id, snapshot):
        identifier = uuid.uuid4().hex
        with self.connect() as conn:
            if conn.execute("SELECT count(*) FROM runs").fetchone()[0] >= 500:
                raise ValueError("运行记录已达 500 条，请先清理已结束记录")
            rows = [{**row, "outcome": "pending"} for row in snapshot["attempts"]]
            conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?)", (
                identifier, preview_id, time.time(), "running", "", json.dumps(snapshot), json.dumps(rows)))
        return self.run(identifier)

    def run(self, identifier=None, *, preview_id=None):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE " + ("preview_id=?" if preview_id else "id=?"),
                               (preview_id or identifier,)).fetchone()
        if row is None:
            raise KeyError("未找到运行")
        return {"id": row["id"], "created_at": row["created_at"], "state": row["state"],
                "stop_reason": row["stop_reason"], "plan": json.loads(row["snapshot"]),
                "results": json.loads(row["results"])}

    def runs(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id,created_at,state,stop_reason FROM runs ORDER BY created_at DESC LIMIT 100")]

    def update(self, run):
        with self.connect() as conn:
            conn.execute("UPDATE runs SET state=?,stop_reason=?,results=? WHERE id=?", (
                run["state"], run["stop_reason"], json.dumps(run["results"]), run["id"]))

    def interrupt_stale(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT id,results FROM runs WHERE state='running'").fetchall()
            for row in rows:
                results = json.loads(row["results"])
                for result in results:
                    if result["outcome"] == "running":
                        result["outcome"] = "unknown"
                    elif result["outcome"] == "pending":
                        result["outcome"] = "not_sent"
                conn.execute("UPDATE runs SET state='interrupted',stop_reason='process_interrupted',results=? WHERE id=?",
                             (json.dumps(results), row["id"]))

    def delete_run(self, identifier):
        with self.connect() as conn:
            conn.execute("DELETE FROM runs WHERE id=? AND state!='running'", (identifier,))
