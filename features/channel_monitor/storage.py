from __future__ import annotations

import json

from features.integrity.execution import resolve_registry_target
from features.integrity.strategies import canonical_hash
from shared.feishu_channels import ChannelSync
from .domain import CIRCUIT_FAILURES, INTERVAL, QUALITY_ROUNDS, ROUND_WINDOW, next_due


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, registry):
        self.registry = registry
        ChannelSync(registry)
        with registry.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS channel_monitor_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), instance_enc TEXT,
                    revision INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 0,
                    next_due REAL);
                INSERT OR IGNORE INTO channel_monitor_settings(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS channel_monitor_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, remote_id INTEGER NOT NULL UNIQUE,
                    registry_id INTEGER NOT NULL, model TEXT NOT NULL, protocol TEXT NOT NULL,
                    expected TEXT NOT NULL, snapshot TEXT NOT NULL, remote_hash TEXT NOT NULL,
                    condition_hash TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                    locked INTEGER NOT NULL DEFAULT 0, quality_streak INTEGER NOT NULL DEFAULT 0,
                    last_slot REAL, owned_reason TEXT NOT NULL DEFAULT '', owner_action INTEGER,
                    last_result TEXT);
                CREATE TABLE IF NOT EXISTS channel_monitor_rounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, due REAL NOT NULL, source TEXT NOT NULL,
                    revision INTEGER NOT NULL, started REAL NOT NULL, deadline REAL NOT NULL,
                    finished REAL, status TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS channel_monitor_scheduled_slot
                    ON channel_monitor_rounds(due) WHERE source='scheduled';
                CREATE TABLE IF NOT EXISTS channel_monitor_attempts (
                    round_id INTEGER NOT NULL, binding_id INTEGER NOT NULL, probe TEXT NOT NULL,
                    attempt INTEGER NOT NULL, status TEXT NOT NULL, started REAL NOT NULL,
                    finished REAL, result TEXT,
                    PRIMARY KEY(round_id,binding_id,probe,attempt));
                CREATE TABLE IF NOT EXISTS channel_monitor_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER NOT NULL,
                    binding_id INTEGER NOT NULL, binding_revision INTEGER NOT NULL,
                    desired INTEGER NOT NULL, reason TEXT NOT NULL,
                    target TEXT NOT NULL,
                    started REAL NOT NULL, finished REAL, status TEXT NOT NULL,
                    UNIQUE(round_id,binding_id));
                CREATE TABLE IF NOT EXISTS channel_monitor_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER, binding_id INTEGER,
                    kind TEXT NOT NULL, created REAL NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS channel_monitor_inventories (
                    round_id INTEGER PRIMARY KEY, checked_at REAL NOT NULL, snapshot TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS channel_monitor_results (
                    round_id INTEGER NOT NULL, binding_id INTEGER NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(round_id,binding_id));
                CREATE TABLE IF NOT EXISTS channel_monitor_circuit (
                    id INTEGER PRIMARY KEY CHECK(id=1), opened INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0, round_id INTEGER, changed_at REAL);
                INSERT OR IGNORE INTO channel_monitor_circuit(id) VALUES(1);
            """)

    def plan(self):
        with self.registry.connect() as db:
            row = dict(db.execute("SELECT revision,enabled,next_due FROM channel_monitor_settings WHERE id=1").fetchone())
        row["enabled"] = bool(row["enabled"])
        return row

    def instance(self, *, secret=False):
        with self.registry.connect() as db:
            value = db.execute("SELECT instance_enc FROM channel_monitor_settings WHERE id=1").fetchone()[0]
        if not value:
            return None
        try:
            config = json.loads(self.registry._cipher.decrypt(value.encode()).decode())
        except Exception:
            raise ValueError("管理凭据不可用，请重新保存") from None
        if secret:
            return config
        return {"base_url": config["base_url"], "user_id": config.get("user_id"), "token_configured": True}

    def save_instance(self, config):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT enabled FROM channel_monitor_settings WHERE id=1").fetchone()[0]:
                raise ValueError("请先暂停监控")
            old = self.instance(secret=True)
            if old and old["base_url"] != config["base_url"] and db.execute("SELECT 1 FROM channel_monitor_bindings LIMIT 1").fetchone():
                raise ValueError("更换管理实例前请先移除旧映射")
            db.execute("UPDATE channel_monitor_settings SET instance_enc=?,revision=revision+1 WHERE id=1",
                       (self.registry.encrypt(encode(config)),))
        return self.instance()

    @staticmethod
    def _binding(row):
        result = dict(row)
        result["snapshot"] = json.loads(result["snapshot"])
        result["last_result"] = json.loads(result["last_result"]) if result["last_result"] else None
        result["locked"] = bool(result["locked"])
        return result

    def bindings(self):
        with self.registry.connect() as db:
            return [self._binding(r) for r in db.execute("SELECT * FROM channel_monitor_bindings ORDER BY id")]

    def binding(self, binding_id):
        with self.registry.connect() as db:
            row = db.execute("SELECT * FROM channel_monitor_bindings WHERE id=?", (binding_id,)).fetchone()
        if not row:
            raise KeyError("映射不存在")
        return self._binding(row)

    def add(self, remote_id, registry_id, model, protocol, expected, snapshot, remote_hash, condition_hash):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT enabled FROM channel_monitor_settings WHERE id=1").fetchone()[0]:
                raise ValueError("请先暂停监控")
            if db.execute("SELECT COUNT(*) FROM channel_monitor_bindings").fetchone()[0] >= 100:
                raise ValueError("最多配置100条渠道映射")
            if db.execute("SELECT 1 FROM channel_monitor_bindings WHERE remote_id=?", (remote_id,)).fetchone():
                raise ValueError("该 New API 渠道已有映射")
            cur = db.execute("""INSERT INTO channel_monitor_bindings
                (remote_id,registry_id,model,protocol,expected,snapshot,remote_hash,condition_hash)
                VALUES(?,?,?,?,?,?,?,?)""", (remote_id, registry_id, model, protocol, expected,
                                           encode(snapshot), remote_hash, condition_hash))
            binding_id = cur.lastrowid
            db.execute("UPDATE channel_monitor_settings SET revision=revision+1 WHERE id=1")
        return self.binding(binding_id)

    def set_enabled(self, enabled, now):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if enabled and not db.execute("SELECT 1 FROM channel_monitor_bindings WHERE locked=0 LIMIT 1").fetchone():
                raise ValueError("请先配置可用渠道映射")
            db.execute("UPDATE channel_monitor_settings SET enabled=?,revision=revision+1,next_due=? WHERE id=1",
                       (int(enabled), next_due(now) if enabled else None))
            db.execute("UPDATE channel_monitor_bindings SET quality_streak=0,last_slot=NULL")
        return self.plan()

    def lock(self, binding_id, locked, *, takeover=False):
        if takeover and not locked:
            raise ValueError("人工接管必须保持锁定")
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute("""UPDATE channel_monitor_bindings SET locked=?,revision=revision+1,
                quality_streak=0,last_slot=NULL WHERE id=?""", (int(locked), binding_id))
            if not cur.rowcount:
                raise KeyError("映射不存在")
            if takeover:
                db.execute("UPDATE channel_monitor_bindings SET owned_reason='',owner_action=NULL WHERE id=?", (binding_id,))
        return self.binding(binding_id)

    def remove(self, binding_id):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owned_reason FROM channel_monitor_bindings WHERE id=?", (binding_id,)).fetchone()
            if not row:
                raise KeyError("映射不存在")
            if self.plan()["enabled"] or row[0]:
                raise ValueError("请先暂停；系统停用的渠道须先人工接管")
            db.execute("DELETE FROM channel_monitor_bindings WHERE id=?", (binding_id,))
            db.execute("UPDATE channel_monitor_settings SET revision=revision+1 WHERE id=1")

    def begin(self, now, *, manual=False):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = dict(db.execute("SELECT * FROM channel_monitor_settings WHERE id=1").fetchone())
            if not plan["enabled"] or db.execute("SELECT 1 FROM channel_monitor_rounds WHERE status='running' LIMIT 1").fetchone():
                return None
            if manual:
                due = now
            else:
                due = (int(now) // INTERVAL) * INTERVAL
                if plan["next_due"] is None or due < plan["next_due"]:
                    return None
                db.execute("UPDATE channel_monitor_settings SET next_due=? WHERE id=1", (due + INTERVAL,))
                if now >= due + ROUND_WINDOW:
                    return None
            deadline = due + ROUND_WINDOW
            if not manual and db.execute("SELECT 1 FROM channel_monitor_rounds WHERE source='scheduled' AND due=?", (due,)).fetchone():
                return None
            cur = db.execute("""INSERT INTO channel_monitor_rounds(due,source,revision,started,deadline,status)
                VALUES(?,?,?,?,?,'running')""", (due, "manual" if manual else "scheduled", plan["revision"], now, deadline))
            if manual:
                db.execute("UPDATE channel_monitor_bindings SET quality_streak=0,last_slot=NULL")
            return dict(db.execute("SELECT * FROM channel_monitor_rounds WHERE id=?", (cur.lastrowid,)).fetchone())

    def guard(self, run, binding, now, *, conn=None):
        if conn is None:
            with self.registry.connect() as current:
                return self.guard(run, binding, now, conn=current)
        plan = conn.execute("SELECT enabled,revision FROM channel_monitor_settings WHERE id=1").fetchone()
        row = conn.execute("SELECT locked,revision FROM channel_monitor_bindings WHERE id=?", (binding["id"],)).fetchone()
        state = conn.execute("SELECT status FROM channel_monitor_rounds WHERE id=?", (run["id"],)).fetchone()
        if (not plan["enabled"] or plan["revision"] != run["revision"] or not row or row["locked"]
                or row["revision"] != binding["revision"] or now >= run["deadline"] or not state or state[0] != "running"):
            raise ValueError("监控已暂停、配置变化或本轮窗口已结束")
        target = resolve_registry_target(self.registry, binding["registry_id"], binding["model"],
                                         binding["protocol"], conn=conn)
        if target.snapshot != binding["snapshot"]:
            raise ValueError("直测连接或模型映射变化，请重新绑定")
        return target

    def reserve(self, run, binding, probe, attempt, now):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(run, binding, now, conn=db)
            if binding["owned_reason"]:
                self.inventory_guard(run, binding, now, conn=db)
            db.execute("""INSERT INTO channel_monitor_attempts
                (round_id,binding_id,probe,attempt,status,started) VALUES(?,?,?,?,'sending',?)""",
                       (run["id"], binding["id"], probe, attempt, now))

    def complete_attempt(self, run_id, binding_id, probe, attempt, result, now):
        with self.registry.connect() as db:
            db.execute("""UPDATE channel_monitor_attempts SET status='confirmed',finished=?,result=?
                WHERE round_id=? AND binding_id=? AND probe=? AND attempt=? AND status='sending'""",
                       (now, encode(result), run_id, binding_id, probe, attempt))

    def result(self, run, binding, result, now):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(run, binding, now, conn=db)
            current = db.execute("SELECT quality_streak,last_slot,last_result FROM channel_monitor_bindings WHERE id=?", (binding["id"],)).fetchone()
            previous = json.loads(current["last_result"]) if current["last_result"] else {}
            prior = current["quality_streak"] if (current["last_slot"] == run["due"] - INTERVAL
                                                  and previous.get("outcome") == result["outcome"]) else 0
            streak = prior + 1 if result["outcome"] in QUALITY_ROUNDS else 0
            # Manual diagnostics may recover a channel, but cannot advance scheduled streaks.
            if run["source"] != "scheduled":
                streak = 0
            db.execute("UPDATE channel_monitor_bindings SET last_result=?,quality_streak=?,last_slot=? WHERE id=?",
                       (encode(result), streak, run["due"] if run["source"] == "scheduled" else None, binding["id"]))
            db.execute("INSERT INTO channel_monitor_results(round_id,binding_id,result) VALUES(?,?,?)",
                       (run["id"], binding["id"], encode(result)))
        return streak

    def event(self, run_id, binding_id, kind, detail, now):
        with self.registry.connect() as db:
            db.execute("INSERT INTO channel_monitor_events(round_id,binding_id,kind,created,detail) VALUES(?,?,?,?,?)",
                       (run_id, binding_id, kind, now, encode(detail)))

    def save_inventory(self, run, snapshot, now):
        with self.registry.connect() as db:
            db.execute("INSERT INTO channel_monitor_inventories(round_id,checked_at,snapshot) VALUES(?,?,?)",
                       (run["id"], now, encode(snapshot)))

    def inventory_guard(self, run, binding, now, *, conn=None):
        if conn is None:
            with self.registry.connect() as db:
                return self.inventory_guard(run, binding, now, conn=db)
        self.guard(run, binding, now, conn=conn)
        record = conn.execute("SELECT snapshot FROM channel_monitor_inventories WHERE round_id=?", (run["id"],)).fetchone()
        if not record:
            raise ValueError("本轮无完整飞书白名单")
        snapshot = json.loads(record[0])
        settings = ChannelSync._load(conn)
        row = conn.execute("SELECT fingerprint,enabled,status FROM channels WHERE id=?", (binding["registry_id"],)).fetchone()
        member = next((m for m in snapshot["members"] if m["registry_id"] == binding["registry_id"]), None)
        source_binding = conn.execute("SELECT channel_id FROM feishu_channel_bindings WHERE source_id=? AND record_id=?",
                                      (snapshot["source_id"], member["record_id"] if member else "")).fetchone()
        if (settings["version"] != snapshot["settings_version"] or settings.get("source_id") != snapshot["source_id"]
                or settings.get("generation") != snapshot["generation"] or settings.get("lease_id")
                or not row or not row["enabled"] or row["status"] != "online" or not member
                or member["fingerprint"] != row["fingerprint"] or not source_binding
                or source_binding[0] != binding["registry_id"]):
            raise ValueError("渠道不在本轮有效飞书上线白名单")
        return canonical_hash(snapshot)

    def circuit(self):
        with self.registry.connect() as db:
            row = dict(db.execute("SELECT * FROM channel_monitor_circuit WHERE id=1").fetchone())
        row["opened"] = bool(row["opened"])
        return row

    def circuit_for_round(self, run, items, now):
        failures = {item["binding"]["remote_id"] for item in items
                    if item.get("result", {}).get("outcome") == "transport_failure"}
        tested = [item for item in items if item["state"] != "locked"]
        complete = (len(tested) >= CIRCUIT_FAILURES and all(item["state"] == "tested"
                    and item["result"].get("outcome") == "healthy" for item in tested))
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT opened FROM channel_monitor_circuit WHERE id=1").fetchone()[0]
            plan = db.execute("SELECT enabled,revision FROM channel_monitor_settings WHERE id=1").fetchone()
            state = db.execute("SELECT status FROM channel_monitor_rounds WHERE id=?", (run["id"],)).fetchone()
            complete = (complete and plan["enabled"] and plan["revision"] == run["revision"]
                        and state and state[0] == "running" and now < run["deadline"])
            live = {r["id"]: r["revision"] for r in db.execute("SELECT id,revision FROM channel_monitor_bindings WHERE locked=0")}
            complete = complete and set(live) == {item["binding"]["id"] for item in tested}
            if complete:
                try:
                    for item in tested:
                        self.guard(run, item["binding"], now, conn=db)
                except (ValueError, KeyError):
                    complete = False
            opened = True if len(failures) >= CIRCUIT_FAILURES else (False if complete and run["source"] == "scheduled" else bool(current))
            if opened != bool(current):
                db.execute("UPDATE channel_monitor_circuit SET opened=?,revision=revision+1,round_id=?,changed_at=? WHERE id=1",
                           (int(opened), run["id"], now))
                db.execute("INSERT INTO channel_monitor_events(round_id,kind,created,detail) VALUES(?,?,?,?)",
                           (run["id"], "circuit_opened" if opened else "circuit_closed", now,
                            encode({"failure_remote_ids": sorted(failures)})))
        return opened

    def reset_circuit(self, revision, now):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT enabled FROM channel_monitor_settings WHERE id=1").fetchone()[0]:
                raise ValueError("请先暂停监控并核对故障原因")
            changed = db.execute("UPDATE channel_monitor_circuit SET opened=0,revision=revision+1,changed_at=? WHERE id=1 AND revision=? AND opened=1",
                                 (now, revision))
            if not changed.rowcount:
                raise ValueError("熔断状态已变化，请刷新")
            db.execute("INSERT INTO channel_monitor_events(kind,created,detail) VALUES('circuit_reset',?,'{}')", (now,))
        return self.circuit()

    def reserve_action(self, run, binding, desired, reason, now):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(run, binding, now, conn=db)
            current_result = db.execute("SELECT result FROM channel_monitor_results WHERE round_id=? AND binding_id=?",
                                        (run["id"], binding["id"])).fetchone()
            if not current_result:
                raise ValueError("本轮无已确认检测结果")
            inventory_hash = None
            if desired == 1:
                inventory_hash = self.inventory_guard(run, binding, now, conn=db)
                owner = db.execute("""SELECT a.status,a.desired FROM channel_monitor_bindings b
                    JOIN channel_monitor_actions a ON a.id=b.owner_action AND a.binding_id=b.id
                    WHERE b.id=? AND b.owned_reason!=''""", (binding["id"],)).fetchone()
                if not owner or owner["status"] != "confirmed" or owner["desired"] != 2:
                    raise ValueError("没有可确认的自有停用记录")
            elif db.execute("SELECT opened FROM channel_monitor_circuit WHERE id=1").fetchone()[0]:
                raise ValueError("多渠道故障熔断中，禁止自动停用")
            config = self.instance(secret=True)
            target = {key: binding[key] for key in ("remote_id", "registry_id", "model", "protocol", "expected", "condition_hash")}
            target["instance_fingerprint"] = canonical_hash({"base_url": config["base_url"], "user_id": config.get("user_id")})
            current = db.execute("SELECT last_result,quality_streak FROM channel_monitor_bindings WHERE id=?", (binding["id"],)).fetchone()
            result = json.loads(current_result[0])
            if current["last_result"] != current_result[0]:
                raise ValueError("动作依据已变化")
            target.update(trigger_outcome=result.get("outcome"), streak=current["quality_streak"],
                          threshold=QUALITY_ROUNDS.get(result.get("outcome")))
            if desired == 1:
                if (result.get("candy_correct") is not True or result.get("mt_verdict") != "match"
                        or result.get("outcome") != "healthy" or result.get("modeltrace", {}).get("valid_answers") != 3):
                    raise ValueError("本轮未同时答对糖果且MT匹配")
                target["online_inventory_hash"] = inventory_hash
            cur = db.execute("""INSERT INTO channel_monitor_actions
                (round_id,binding_id,binding_revision,desired,reason,target,started,status) VALUES(?,?,?,?,?,?,?,'sending')""",
                             (run["id"], binding["id"], binding["revision"], desired, reason, encode(target), now))
            return cur.lastrowid

    def finish_action(self, action_id, status, now):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            action = db.execute("SELECT * FROM channel_monitor_actions WHERE id=? AND status='sending'", (action_id,)).fetchone()
            if not action:
                return
            db.execute("UPDATE channel_monitor_actions SET status=?,finished=? WHERE id=?", (status, now, action_id))
            if status == "confirmed":
                current = db.execute("SELECT revision FROM channel_monitor_bindings WHERE id=?", (action["binding_id"],)).fetchone()
                if current and current[0] == action["binding_revision"]:
                    db.execute("""UPDATE channel_monitor_bindings SET owned_reason=?,owner_action=?,quality_streak=0
                        WHERE id=?""", (action["reason"] if action["desired"] == 2 else "",
                                         action_id if action["desired"] == 2 else None, action["binding_id"]))
                else:
                    db.execute("UPDATE channel_monitor_bindings SET locked=1 WHERE id=?", (action["binding_id"],))
            else:
                db.execute("UPDATE channel_monitor_bindings SET locked=1,revision=revision+1 WHERE id=?", (action["binding_id"],))

    def release_ownership(self, binding_id):
        with self.registry.connect() as db:
            db.execute("UPDATE channel_monitor_bindings SET owned_reason='',owner_action=NULL,quality_streak=0 WHERE id=?", (binding_id,))

    def finish(self, run_id, status, now):
        with self.registry.connect() as db:
            db.execute("UPDATE channel_monitor_rounds SET status=?,finished=? WHERE id=? AND status='running'", (status, now, run_id))
            db.execute("UPDATE channel_monitor_attempts SET status='unknown' WHERE round_id=? AND status='sending'", (run_id,))

    def recover(self, now):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE channel_monitor_bindings SET locked=1,revision=revision+1
                WHERE id IN(SELECT binding_id FROM channel_monitor_actions WHERE status='sending')""")
            db.execute("UPDATE channel_monitor_actions SET status='unknown',finished=? WHERE status='sending'", (now,))
            db.execute("UPDATE channel_monitor_attempts SET status='unknown' WHERE status='sending'")
            db.execute("UPDATE channel_monitor_rounds SET status='interrupted',finished=? WHERE status='running'", (now,))

    def history(self):
        with self.registry.connect() as db:
            return {"rounds": [dict(r) for r in db.execute("SELECT * FROM channel_monitor_rounds ORDER BY id DESC LIMIT 50")],
                    "actions": [{**dict(r), "target": json.loads(r["target"])} for r in
                                db.execute("SELECT * FROM channel_monitor_actions ORDER BY id DESC LIMIT 100")],
                    "events": [{**dict(r), "detail": json.loads(r["detail"])} for r in
                               db.execute("SELECT * FROM channel_monitor_events ORDER BY id DESC LIMIT 100")]}

    def attempts(self, run_id):
        with self.registry.connect() as db:
            return [{**dict(r), "result": json.loads(r["result"]) if r["result"] else None}
                    for r in db.execute("SELECT * FROM channel_monitor_attempts WHERE round_id=? ORDER BY started,binding_id,probe,attempt", (run_id,))]
