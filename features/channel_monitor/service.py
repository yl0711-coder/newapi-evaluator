from __future__ import annotations

import asyncio
import math
import time

from features.daily_checks.domain import display
from features.integrity.execution import build_requests, resolve_registry_target
from features.integrity.scoring import project_observation, score_strategy
from features.integrity.strategies import get_strategy
from features.integrity.transport import send_probe
from features.model_coverage.catalog import Catalog
from shared.feishu_channels import ChannelSync
from shared.registry import get_registry
from . import domain
from .newapi import NewAPI
from .storage import Store


class Service:
    def __init__(self, registry, *, clock=time.time, sender=send_probe, client_factory=NewAPI,
                 inventory_reader=None, sleep=asyncio.sleep):
        self.registry, self.clock, self.sender = registry, clock, sender
        self.store, self.catalog = Store(registry), Catalog(registry)
        self.client_factory = client_factory
        self.inventory_reader, self.sleep = inventory_reader, sleep
        self.worker = None
        self.action_lock = asyncio.Lock()
        self.started = False

    def status(self):
        return {"running": bool(self.worker and not self.worker.done()), "plan": self.store.plan(),
                "circuit": self.store.circuit()}

    def meta(self):
        models = [{"model": m["model"], "protocol": m["protocol"]} for m in self.catalog.models()
                  if m["protocol"] in {"responses", "openai"}]
        return {**domain.contract(), "instance": self.store.instance(), "executor": self.status(),
                "bindings": self.store.bindings(), "models": models, "expected_models": domain.expected_models(),
                "channels": [{"id": c["id"], "enabled": c["enabled"], "credential_status": c["credential_status"]}
                             for c in self.registry.list()]}

    def save_instance(self, base_url, token, user_id):
        old = self.store.instance(secret=True)
        base_url = domain.endpoint(base_url)
        if not token and old and base_url == old["base_url"]:
            token = old["token"]
        if (not token or len(token) > 4096 or any(not 33 <= ord(c) <= 126 for c in token)
                or token in base_url):
            raise ValueError("请填写有效的管理员访问令牌")
        return self.store.save_instance({"base_url": base_url, "token": token, "user_id": user_id})

    def client(self):
        config = self.store.instance(secret=True)
        if not config:
            raise ValueError("请先配置 New API 管理连接")
        return self.client_factory(config)

    async def add_binding(self, *, remote_id, registry_id, model, protocol, expected, confirm_live=False,
                          confirm_same_credential=False):
        if confirm_live is not True or confirm_same_credential is not True:
            raise ValueError("请确认读取渠道，并核对两边使用同一把上游 Key")
        if self.store.plan()["enabled"]:
            raise ValueError("请先暂停监控")
        if domain.expected_model(expected) not in domain.expected_models():
            raise ValueError("指定模型不在 ModelTrace 参考库中")
        revision = self.store.plan()["revision"]
        target = resolve_registry_target(self.registry, registry_id, model, protocol)
        client = self.client()
        await client.verify_version()
        remote = await client.channel(remote_id)
        domain.validate_mapping(remote, target, model)
        if remote["status"] != 1:
            raise ValueError("请只绑定已启用渠道；人工停用渠道不会被自动接管")
        secrets = [target.channel["api_key"], self.store.instance(secret=True)["token"]]
        if any(display(v, secrets) != v for v in remote["groups"] + remote["models"]):
            raise ValueError("渠道模型或分组资料无效")
        if self.store.plan()["revision"] != revision or target.snapshot != resolve_registry_target(self.registry, registry_id, model, protocol).snapshot:
            raise ValueError("读取期间配置变化，请重试")
        expected = domain.expected_model(expected)
        identity = domain.condition_hash(target.snapshot, remote["config_hash"], model, expected)
        return self.store.add(remote_id, registry_id, model, protocol, expected,
                              target.snapshot, remote["config_hash"], identity)

    def enable(self, enabled, *, confirm_live=False, confirm_manage=False, confirm_inventory=False):
        if enabled and (confirm_live is not True or confirm_manage is not True or confirm_inventory is not True):
            raise ValueError("请确认定时模型请求、自动启停及飞书上线名单读取")
        if enabled:
            self.client()
        return self.store.set_enabled(enabled, self.clock())

    async def fresh(self, run, binding, client):
        self.store.guard(run, binding, self.clock())
        remote = await client.channel(binding["remote_id"])
        target = self.store.guard(run, binding, self.clock())
        domain.validate_mapping(remote, target, binding["model"])
        if (remote["config_hash"] != binding["remote_hash"] or
                domain.condition_hash(target.snapshot, remote["config_hash"], binding["model"], binding["expected"]) != binding["condition_hash"]):
            raise ValueError("渠道配置或检测合同变化，请重新绑定")
        return target, remote

    async def delay(self, run, seconds):
        if self.clock() + seconds >= run["deadline"]:
            raise ValueError("本轮窗口不足，不延长窗口")
        await self.sleep(seconds)

    async def question(self, run, binding, client, probe, *, manifest=None, attempts=domain.MAX_ATTEMPTS):
        for attempt in range(attempts):
            if domain.RETRY_DELAYS[attempt]:
                await self.delay(run, domain.RETRY_DELAYS[attempt])
            target, remote = await self.fresh(run, binding, client)
            wanted = 2 if binding["owned_reason"] else 1
            if remote["status"] != wanted:
                raise ValueError("渠道状态已被外部修改")
            permitted = False

            async def before_send():
                nonlocal permitted
                if permitted:
                    raise ValueError("重复发送许可")
                fresh, current = await self.fresh(run, binding, client)
                if fresh.snapshot != target.snapshot or current["status"] != wanted:
                    raise ValueError("发送前目标变化")
                self.store.reserve(run, binding, probe["id"], attempt, self.clock())
                permitted = True

            remaining = run["deadline"] - self.clock()
            if remaining <= 0:
                raise ValueError("本轮窗口已结束")
            try:
                raw = await asyncio.wait_for(self.sender(target.channel, probe, before_send=before_send), remaining)
            except asyncio.TimeoutError:
                raise ValueError("本轮窗口已结束") from None
            status = raw.get("status")
            if not permitted:
                if status not in {"network_error", "timeout"}:
                    raise ValueError("请求未获得发送许可")
                # DNS/connect preflight failures are observed failures, while an
                # interrupted permitted request remains unknown in the ledger.
                await before_send()
                sent = False
            else:
                sent = True
            allowed = domain.RETRYABLE | {"completed", "invalid_response", "tool_calls", "refused", "truncated", "egress_denied"}
            if status not in allowed:
                raise ValueError("执行器返回未知状态")
            valid = status == "completed" and raw.get("valid") is True
            if status == "completed" and not valid:
                raise ValueError("执行器返回冲突状态")
            answer = domain.grade_candy(raw.get("text")) if manifest is None and valid else None
            observation = project_observation(manifest, probe["id"], raw) if manifest is not None else None
            metrics = {"status": status, "request_sent": sent, "answer_correct": answer,
                       "mt_valid": observation["valid"] if observation else None}
            for field in ("input_tokens_reported", "output_tokens_reported", "reasoning_tokens_reported", "latency_ms"):
                value = raw.get(field)
                metrics[field] = value if type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 2**63-1 else None
            self.store.complete_attempt(run["id"], binding["id"], probe["id"], attempt, metrics, self.clock())
            raw.pop("text", None)
            if status == "egress_denied":
                raise ValueError("出站策略拒绝")
            if valid or not domain.retryable(status):
                return {"completed": valid, "answer": answer, "observation": observation, "exhausted": False}
        return {"completed": False, "answer": None, "observation": observation, "exhausted": True}

    async def evaluate(self, run, binding, client):
        candy = await self.question(run, binding, client, domain.candy_probe())
        result = {"candy_completed": candy["completed"], "candy_correct": candy["answer"], "modeltrace": None,
                  "outcome": "inconclusive", "condition_hash": binding["condition_hash"]}
        if candy["exhausted"]:
            return {**result, "outcome": "transport_failure"}
        if not candy["completed"]:
            return result
        manifest = get_strategy("modeltrace")
        observations, not_run = [], []
        requests = build_requests(manifest, {"reasoning_effort": "low"})
        exhausted = False
        for index, request in enumerate(requests):
            item = await self.question(run, binding, client, request.probe, manifest=manifest)
            observations.append(item["observation"])
            if item["exhausted"]:
                exhausted = True
                not_run = [r.probe["id"] for r in requests[index+1:]]
                break
        score = score_strategy(manifest, observations, expected_model=binding["expected"])
        result["modeltrace"] = {k: score[k] for k in ("status", "source_verdict", "prediction", "probability",
                                                       "p_expected", "valid_answers", "invalid_answers", "results",
                                                       "requested_answers", "missing_answers") if k in score}
        result["modeltrace"]["not_run"] = not_run
        verdict = domain.mt_verdict(score)
        result["mt_verdict"] = verdict
        if exhausted:
            result["outcome"] = "transport_failure"
        elif candy["answer"] is True and verdict == "match":
            result["outcome"] = "healthy"
        elif candy["answer"] is True and verdict == "mismatch":
            result["outcome"] = "mt_mismatch"
        elif candy["answer"] is False and verdict == "mismatch":
            result["outcome"] = "quality_failure"
        return result

    async def act(self, run, binding, client, desired, reason):
        async with self.action_lock:
            target, remote = await self.fresh(run, binding, client)
            if remote["status"] != (1 if desired == 2 else 2):
                raise ValueError("动作前渠道状态变化")
            await client.verify_version()
            if desired == 1:
                _, remote = await self.fresh(run, binding, client)
                if remote["status"] != 2:
                    raise ValueError("恢复前渠道状态变化")
            if desired == 2:
                inventory = await client.channels()
                current = next((c for c in inventory if c["id"] == remote["id"]), None)
                if not current or current["config_hash"] != remote["config_hash"] or current["status"] != 1:
                    raise ValueError("保护检查期间渠道配置变化")
                deficient = domain.protection(remote, inventory)
                if deficient:
                    self.store.event(run["id"], binding["id"], "minimum_channels", {"deficient": deficient}, self.clock())
                    return
                _, check = await self.fresh(run, binding, client)
                if check["status"] != 1:
                    raise ValueError("保护检查期间渠道状态变化")
            action = self.store.reserve_action(run, binding, desired, reason, self.clock())
            try:
                await client.set_status(binding["remote_id"], desired)
                after = await client.channel(binding["remote_id"])
                if after["status"] != desired or after["config_hash"] != remote["config_hash"]:
                    raise ValueError("状态回读未确认")
                self.store.guard(run, binding, self.clock())
                if desired == 1:
                    self.store.inventory_guard(run, binding, self.clock())
            except BaseException:
                self.store.finish_action(action, "unknown", self.clock())
                self.store.event(run["id"], binding["id"], "management_action_unconfirmed", {"action_id": action}, self.clock())
                raise
            self.store.finish_action(action, "confirmed", self.clock())
            self.store.event(run["id"], binding["id"], "channel_disabled" if desired == 2 else "channel_enabled",
                             {"action_id": action, "reason": reason}, self.clock())
            return True

    async def binding_run(self, run, binding, client):
        item = {"binding": binding, "state": "partial", "result": {}, "desired": None, "reason": None}
        if binding["locked"]:
            return {**item, "state": "locked"}
        try:
            _, remote = await self.fresh(run, binding, client)
            if binding["owned_reason"] and remote["status"] == 1:
                self.store.release_ownership(binding["id"])
                self.store.event(run["id"], binding["id"], "externally_enabled", {}, self.clock())
                return {**item, "state": "external"}
            if remote["status"] != (2 if binding["owned_reason"] else 1):
                self.store.event(run["id"], binding["id"], "external_status", {"status": remote["status"]}, self.clock())
                return {**item, "state": "external"}
            if binding["owned_reason"]:
                self.store.inventory_guard(run, binding, self.clock())
            result = await self.evaluate(run, binding, client)
            if binding["owned_reason"]:
                self.store.inventory_guard(run, binding, self.clock())
            streak = self.store.result(run, binding, result, self.clock())
            item.update(state="tested", result=result)
            if binding["owned_reason"]:
                if result["outcome"] == "healthy":
                    item.update(desired=1, reason=binding["owned_reason"])
            elif result["outcome"] == "transport_failure":
                item.update(desired=2, reason="transport")
            elif result["outcome"] in domain.QUALITY_ROUNDS and streak >= domain.QUALITY_ROUNDS[result["outcome"]]:
                item.update(desired=2, reason="quality")
            return item
        except (ValueError, KeyError):
            self.store.event(run["id"], binding["id"], "binding_unavailable", {}, self.clock())
            return item

    async def floor_recheck(self, run, binding, client):
        _, remote = await self.fresh(run, binding, client)
        inventory = await client.channels()
        self.store.guard(run, binding, self.clock())
        if not domain.protection(remote, inventory):
            return False
        await self.delay(run, domain.FLOOR_RECHECK_DELAY)
        probe = {**domain.candy_probe(), "id": "candy-recheck"}
        check = await self.question(run, binding, client, probe, attempts=1)
        self.store.event(run["id"], binding["id"], "floor_connectivity_recheck",
                         {"completed": check["completed"], "delay_seconds": domain.FLOOR_RECHECK_DELAY}, self.clock())
        return check["completed"]

    async def apply_results(self, run, items, client):
        if self.store.circuit()["opened"]:
            for item in items:
                if item["state"] != "tested":
                    continue
                try:
                    _, remote = await self.fresh(run, item["binding"], client)
                    if remote["status"] != (2 if item["binding"]["owned_reason"] else 1):
                        raise ValueError("检测后状态变化")
                except (ValueError, KeyError):
                    item.update(state="partial", desired=None)
        opened = self.store.circuit_for_round(run, items, self.clock())
        partial, recovery_failed = False, False
        for item in items:
            if item["desired"] != 1:
                continue
            try:
                await self.act(run, item["binding"], client, 1, item["reason"])
            except (ValueError, KeyError):
                partial, recovery_failed = True, True
                self.store.event(run["id"], item["binding"]["id"], "recovery_unavailable", {}, self.clock())
        for item in items:
            if item["desired"] != 2:
                continue
            binding = item["binding"]
            if opened or recovery_failed:
                self.store.event(run["id"], binding["id"], "disable_suspended",
                                 {"circuit": opened, "recovery_unconfirmed": recovery_failed}, self.clock())
                continue
            try:
                if item["reason"] == "transport" and await self.floor_recheck(run, binding, client):
                    continue
                await self.act(run, binding, client, 2, item["reason"])
            except (ValueError, KeyError):
                partial = True
                self.store.event(run["id"], binding["id"], "binding_unavailable", {}, self.clock())
        return partial or any(item["state"] == "partial" for item in items)

    async def run(self, run):
        status = "completed"
        tasks = []
        try:
            client = self.client()
            await client.verify_version()
            bindings = self.store.bindings()
            if any(b["owned_reason"] and not b["locked"] for b in bindings):
                try:
                    snapshot = await asyncio.wait_for(ChannelSync(self.registry).read_online_inventory(self.inventory_reader),
                                                      max(.001, run["deadline"] - self.clock()))
                    self.store.save_inventory(run, snapshot, self.clock())
                except ValueError:
                    self.store.event(run["id"], None, "online_inventory_unavailable", {}, self.clock())
                    status = "partial"
            tasks = [asyncio.create_task(self.binding_run(run, b, client)) for b in bindings]
            results = await asyncio.wait_for(asyncio.gather(*tasks), max(.001, run["deadline"] - self.clock()))
            if any(item["state"] == "partial" for item in results):
                status = "partial"
            if await asyncio.wait_for(self.apply_results(run, results, client), max(.001, run["deadline"] - self.clock())):
                status = "partial"
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception:
            status = "partial"
            self.store.event(run["id"], None, "round_interrupted", {}, self.clock())
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.store.finish(run["id"], status, self.clock())

    def recover(self):
        if not self.started:
            self.store.recover(self.clock())
            self.started = True

    async def tick(self, now=None):
        self.recover()
        if self.worker and not self.worker.done():
            return
        run = self.store.begin(self.clock() if now is None else now)
        if run:
            self.worker = asyncio.create_task(self.run(run), name=f"newapi-channel-monitor-{run['id']}")

    async def run_now(self):
        self.recover()
        run = self.store.begin(self.clock(), manual=True)
        if not run:
            raise ValueError("监控暂停或已有轮次运行")
        self.worker = asyncio.create_task(self.run(run), name=f"newapi-channel-monitor-{run['id']}")
        return {"round_id": run["id"], "status": "queued"}

    async def stop(self):
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        self.worker = None


_service = None


def get_service():
    global _service
    registry = get_registry()
    if _service is None or _service.registry is not registry:
        _service = Service(registry)
    return _service
