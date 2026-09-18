import json
from pathlib import Path
import sqlite3
from contextlib import closing


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.path = directory / "reports.db"
        with closing(self.connect()) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS protocol_reports (id TEXT PRIMARY KEY, created_at REAL NOT NULL, body TEXT NOT NULL)")
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def save(self, report):
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO protocol_reports VALUES (?,?,?)", (report["id"], report["created_at"], json.dumps(report, ensure_ascii=False)))
            conn.execute("DELETE FROM protocol_reports WHERE id IN (SELECT id FROM protocol_reports ORDER BY created_at DESC LIMIT -1 OFFSET 30)")

    def get(self, identifier):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT body FROM protocol_reports WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return json.loads(row[0])

    def list(self):
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT body FROM protocol_reports ORDER BY created_at DESC").fetchall()
        return [json.loads(row[0]) for row in rows]
