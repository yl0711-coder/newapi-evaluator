"""Durable request permits and bounded local media, never credentials or prompts."""
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time
import uuid

MAX_REPORTS = 30
MAX_MEDIA_BYTES = 512 * 1024 * 1024
MAX_MEDIA_FILES = 200
LOCAL_ID = re.compile(r"^[a-f0-9]{32}$")
ACTIVE = {"queued", "running"}


class Conflict(ValueError):
    pass


def key_digest(salt, key):
    return hashlib.sha256(bytes.fromhex(salt) + key.encode()).hexdigest()


class Store:
    def __init__(self, directory, *, clock=time.time):
        self._lock_fd = None
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.directory / ".admission.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            raise Conflict("此验收目录已由另一个执行器占用") from None
        self._lock_fd = descriptor
        try:
            self._initialize(clock)
        except BaseException:
            self.close()
            raise

    def _initialize(self, clock):
        self.directory.chmod(0o700)
        self.media_directory = self.directory / "media"
        self.media_directory.mkdir(mode=0o700, exist_ok=True)
        self.media_directory.chmod(0o700)
        self.path = self.directory / "admission.db"
        self.clock = clock
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, idempotency TEXT UNIQUE NOT NULL, salt TEXT NOT NULL, key_digest TEXT NOT NULL, data TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS media (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, bytes INTEGER NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS reservations (item_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, bytes INTEGER NOT NULL)")
        self.recover()

    def close(self):
        if self._lock_fd is not None:
            descriptor, self._lock_fd = self._lock_fd, None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def __del__(self):
        self.close()

    @contextmanager
    def connect(self):
        if self._lock_fd is None:
            raise Conflict("验收目录执行锁已释放")
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
            for name in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
                if name.exists():
                    name.chmod(0o600)

    def recover(self):
        """A process restart consumes all unsent permits; only known video IDs resume."""
        with self.connect() as conn:
            for row in conn.execute("SELECT id,data FROM runs").fetchall():
                run = json.loads(row["data"])
                if run["status"] not in ACTIVE and not any(item["status"] == "sending" for item in run["items"]):
                    continue
                for item in run["items"]:
                    if item["status"] == "sending":
                        item.update(status="unknown", error_category="process_interrupted")
                    elif item["status"] == "pending":
                        item.update(status="not_sent", error_category="process_interrupted")
                    elif item["status"] == "polling":
                        item["status"] = "submitted"
                run.update(status="interrupted", updated_at=self.clock())
                conn.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), run["id"]))
            # Recovery never sends; query-only resume must obtain a fresh reservation.
            conn.execute("DELETE FROM reservations")

    def by_idempotency(self, value):
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM runs WHERE idempotency=?", (hashlib.sha256(value.encode()).hexdigest(),)).fetchone()
        return json.loads(row["data"]) if row else None

    def create(self, run, idempotency, key):
        salt = secrets.token_hex(32)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] >= 1000:
                raise Conflict("验收记录达到上限，请先整理外置运行目录")
            conn.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (run["id"], hashlib.sha256(idempotency.encode()).hexdigest(), salt, key_digest(salt, key), json.dumps(run)))
        self.prune()
        return run

    def matches_key(self, run_id, key):
        with self.connect() as conn:
            row = conn.execute("SELECT salt,key_digest FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return hmac.compare_digest(row["key_digest"], key_digest(row["salt"], key))

    def get(self, run_id):
        if not LOCAL_ID.fullmatch(run_id):
            raise KeyError(run_id)
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row["data"])

    def runs(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT data FROM runs ORDER BY rowid DESC LIMIT 1000").fetchall()
        return [json.loads(row["data"]) for row in rows]

    def update(self, run_id, changes=None, *, item_id=None, item_changes=None):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            run = json.loads(row["data"])
            if changes:
                run.update(changes)
            if item_id is not None:
                found = next(item for item in run["items"] if item["item_id"] == item_id)
                found.update(item_changes or {})
            run["updated_at"] = self.clock()
            conn.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), run_id))
        return run

    def _capacity(self, conn):
        files = list(self.media_directory.iterdir())
        reserved_files, reserved_bytes = conn.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM reservations").fetchone()
        return {"used_bytes": sum(path.stat().st_size for path in files if path.is_file()), "used_files": len(files),
                "reserved_bytes": reserved_bytes, "reserved_files": reserved_files,
                "max_bytes": MAX_MEDIA_BYTES, "max_files": MAX_MEDIA_FILES,
                "free_disk_bytes": shutil.disk_usage(self.directory).free}

    def capacity(self):
        with self.connect() as conn:
            return self._capacity(conn)

    def _reserve(self, conn, run_id, item_id, maximum):
        if conn.execute("SELECT 1 FROM reservations WHERE item_id=?", (item_id,)).fetchone():
            return True
        state = self._capacity(conn)
        if (state["used_files"] + state["reserved_files"] >= MAX_MEDIA_FILES
                or state["used_bytes"] + state["reserved_bytes"] + maximum > MAX_MEDIA_BYTES
                or state["reserved_bytes"] + maximum + 1024 * 1024 > state["free_disk_bytes"]):
            return False
        conn.execute("INSERT INTO reservations VALUES (?,?,?)", (item_id, run_id, maximum))
        return True

    def reserve(self, run_id, item_id, maximum):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._reserve(conn, run_id, item_id, maximum)

    def release(self, run_id, item_id=None):
        with self.connect() as conn:
            if item_id is None:
                conn.execute("DELETE FROM reservations WHERE run_id=?", (run_id,))
            else:
                conn.execute("DELETE FROM reservations WHERE run_id=? AND item_id=?", (run_id, item_id))

    def acquire(self, run_id, item_id, maximum):
        """Commit sending BEFORE a POST; no other state may consume this permit."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            run = json.loads(row["data"]) if row else None
            if not run or run["status"] != "running":
                return False
            item = next(item for item in run["items"] if item["item_id"] == item_id)
            if item["status"] != "pending":
                return False
            if not self._reserve(conn, run_id, item_id, maximum):
                item.update(status="not_sent", error_category="media_capacity_unavailable")
                conn.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), run_id))
                return False
            item["status"] = "sending"
            item["started_at"] = self.clock()
            run["updated_at"] = self.clock()
            conn.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), run_id))
        return True

    def stop(self, run_id):
        run = self.get(run_id)
        if run["status"] in ACTIVE:
            for item in run["items"]:
                if item["status"] == "pending":
                    item.update(status="not_sent", error_category="stopped_before_send")
            self.update(run_id, {"status": "stopped", "items": run["items"]})
        return self.get(run_id)

    def save_media(self, run_id, kind, content, *, item_id=None):
        if kind not in {"image", "video"} or not content:
            raise ValueError("media_invalid")
        self.get(run_id)
        media_id = uuid.uuid4().hex
        suffix = ".png" if kind == "image" else ".mp4"
        path = self.media_directory / (media_id + suffix)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Include orphaned files left by a crash between fsync and SQLite commit.
            state = self._capacity(conn)
            reservation = conn.execute("SELECT bytes FROM reservations WHERE item_id=? AND run_id=?", (item_id, run_id)).fetchone()
            reserved = reservation[0] if reservation else 0
            if (state["used_files"] + state["reserved_files"] - bool(reservation) >= MAX_MEDIA_FILES
                    or state["used_bytes"] + state["reserved_bytes"] - reserved + len(content) > MAX_MEDIA_BYTES
                    or reservation and len(content) > reserved):
                raise ValueError("media_capacity_exceeded")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                conn.execute("INSERT INTO media VALUES (?,?,?,?)", (media_id, run_id, kind, len(content)))
                conn.execute("DELETE FROM reservations WHERE run_id=? AND item_id=?", (run_id, item_id))
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        return media_id

    def cleanup_media(self, run_id):
        run = self.get(run_id)
        if run["status"] in ACTIVE:
            raise Conflict("运行中的验收不能清理本地样片")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute("SELECT id,kind FROM media WHERE run_id=?", (run_id,)).fetchall():
                if LOCAL_ID.fullmatch(row["id"]):
                    (self.media_directory / (row["id"] + (".png" if row["kind"] == "image" else ".mp4"))).unlink(missing_ok=True)
            conn.execute("DELETE FROM media WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM reservations WHERE run_id=?", (run_id,))
            for item in run["items"]:
                if item.get("media_id"):
                    item.update(media_id=None, media_bytes=None, media_status="removed")
            run["updated_at"] = self.clock()
            conn.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), run_id))
        return run

    def media(self, run_id, media_id):
        if not LOCAL_ID.fullmatch(run_id) or not LOCAL_ID.fullmatch(media_id):
            raise KeyError(media_id)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM media WHERE id=? AND run_id=?", (media_id, run_id)).fetchone()
        if not row:
            raise KeyError(media_id)
        path = self.media_directory / (media_id + (".png" if row["kind"] == "image" else ".mp4"))
        if path.is_symlink() or not path.is_file() or path.resolve().parent != self.media_directory:
            raise KeyError(media_id)
        return path, "image/png" if row["kind"] == "image" else "video/mp4"

    def prune(self):
        terminal = [run for run in self.runs() if run["status"] not in ACTIVE]
        for run in terminal[MAX_REPORTS:]:
            # Only this store's generated media names can be removed.
            with self.connect() as conn:
                rows = conn.execute("SELECT id,kind FROM media WHERE run_id=?", (run["id"],)).fetchall()
                for row in rows:
                    if LOCAL_ID.fullmatch(row["id"]):
                        path = self.media_directory / (row["id"] + (".png" if row["kind"] == "image" else ".mp4"))
                        path.unlink(missing_ok=True)
                conn.execute("DELETE FROM media WHERE run_id=?", (run["id"],))
                conn.execute("DELETE FROM runs WHERE id=?", (run["id"],))
                conn.execute("DELETE FROM reservations WHERE run_id=?", (run["id"],))
