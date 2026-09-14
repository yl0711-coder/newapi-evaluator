import fcntl
import hashlib
import sqlite3
import time

from .config import data_path


class Checkpoint:
    """One executor per checkpoint DB. Confirmed steps are immutable and durable."""
    def __init__(self, path, task_id, fingerprint, steps):
        path = data_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = path.with_suffix('.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ValueError('Checkpoint already has an active executor') from None
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, expected INTEGER NOT NULL,
              first_failure_step INTEGER, recovery_count INTEGER NOT NULL DEFAULT 0,
              recovery_seconds REAL NOT NULL DEFAULT 0, failure_since REAL);
            CREATE TABLE IF NOT EXISTS steps (
              task_id TEXT NOT NULL, step INTEGER NOT NULL, step_id TEXT NOT NULL UNIQUE,
              attempts INTEGER NOT NULL DEFAULT 0, confirmed INTEGER NOT NULL DEFAULT 0,
              output_hash TEXT, output_units INTEGER, PRIMARY KEY(task_id, step));
        ''')
        row = self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row and (row['fingerprint'] != fingerprint or row['expected'] != steps):
            self.close()
            raise ValueError('Checkpoint configuration mismatch')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO tasks(id,fingerprint,expected) VALUES(?,?,?)', (task_id, fingerprint, steps))
            for i in range(1, steps + 1):
                step_id = hashlib.sha256(f'{task_id}:{i}'.encode()).hexdigest()
                self.db.execute('INSERT OR IGNORE INTO steps(task_id,step,step_id) VALUES(?,?,?)', (task_id, i, step_id))
        self.task_id = task_id

    def step(self, number):
        return dict(self.db.execute('SELECT * FROM steps WHERE task_id=? AND step=?', (self.task_id, number)).fetchone())

    def begin(self, number):
        if self.step(number)['confirmed']:
            raise ValueError('Confirmed steps must not execute again')
        with self.db:
            self.db.execute('UPDATE steps SET attempts=attempts+1 WHERE task_id=? AND step=?', (self.task_id, number))
        return self.step(number)

    def failed(self, number):
        with self.db:
            self.db.execute('UPDATE tasks SET first_failure_step=COALESCE(first_failure_step,?), failure_since=COALESCE(failure_since,?) WHERE id=?',
                            (number, time.time(), self.task_id))

    def confirm(self, number, result):
        if not result.success or not result.complete:
            raise ValueError('Cannot checkpoint an incomplete response')
        with self.db:
            self.db.execute('UPDATE steps SET confirmed=1,output_hash=?,output_units=? WHERE task_id=? AND step=? AND confirmed=0',
                            (result.output_sha256, result.output_units, self.task_id, number))
            task = self.db.execute('SELECT failure_since FROM tasks WHERE id=?', (self.task_id,)).fetchone()
            if task['failure_since'] is not None:
                self.db.execute('UPDATE tasks SET recovery_count=recovery_count+1,recovery_seconds=recovery_seconds+?,failure_since=NULL WHERE id=?',
                                (max(0, time.time() - task['failure_since']), self.task_id))

    def summary(self):
        task = dict(self.db.execute('SELECT * FROM tasks WHERE id=?', (self.task_id,)).fetchone())
        rows = [dict(r) for r in self.db.execute('SELECT step,step_id,confirmed,attempts,output_hash,output_units FROM steps WHERE task_id=? ORDER BY step', (self.task_id,))]
        complete = len(rows) == task['expected'] and all(r['confirmed'] and r['output_hash'] and r['output_units'] > 0 for r in rows)
        digest = hashlib.sha256(''.join(r['output_hash'] or '' for r in rows).encode()).hexdigest()
        return {'expected_steps': task['expected'], 'confirmed_steps': sum(r['confirmed'] for r in rows),
                'first_failure_step': task['first_failure_step'], 'recovery_count': task['recovery_count'],
                'total_recovery_time': task['recovery_seconds'], 'final_complete': complete,
                'final_digest': digest, 'steps': rows}

    def close(self):
        self.db.close()
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
