"""异步执行器：提交即返回任务号，worker 在后台跑，单条失败不拖垮整个任务。"""
import asyncio
import secrets
import time
from typing import Any

import httpx

from . import (egress, evaluation_packs, hardbank, itembank, lifecycle,
               packs, paired_admission, paired_engine, placement, probes, report,
               scheduled_measurement, specialty, store,
               test_catalog, workbench)
from .config import HTTP_TIMEOUT, WORKER_COUNT
from .security import decrypt, mask

_queue: asyncio.Queue[int] | None = None
_workers: list[asyncio.Task] = []
_enqueued_ids: set[int] = set()
_active_ids: set[int] = set()
_retry_timers: set[asyncio.Task] = set()
_owner_id = secrets.token_hex(16)
LEASE_SECONDS = 30
LEASE_HEARTBEAT_SECONDS = 10


def queue_size() -> int:
    return _queue.qsize() if _queue else 0


async def start() -> None:
    """应用启动时拉起 worker 池，并把上次中断的排队任务捡回来。"""
    global _queue
    if _workers:
        return
    _queue = asyncio.Queue()
    store.execute("DELETE FROM job_leases WHERE lease_until<?", (time.time(),))
    interrupted = store.query(
        "SELECT tasks.id,tasks.kind FROM tasks LEFT JOIN job_leases ON job_leases.task_id=tasks.id "
        "WHERE tasks.status='running' AND job_leases.id IS NULL"
    )
    if interrupted:
        now = time.time()
        for row in interrupted:
            if row["kind"] == paired_admission.PAIRED_KIND:
                await paired_engine.fail_execution(
                    row["id"], "worker_lease_lost", "执行节点失联"
                )
                continue
            store.update("tasks", row["id"], {
                "status": "interrupted", "finished_at": now,
            })
            store.add_event(
                row["id"], "平台重启时任务仍在执行，已明确标记为中断",
                stage="恢复", level="warn",
            )
    cancelled = store.query(
        "SELECT id FROM tasks WHERE status='queued' AND cancel_flag=1"
    )
    for row in cancelled:
        store.update("tasks", row["id"], {
            "status": "cancelled", "finished_at": time.time(),
        })
    queued = store.query(
        "SELECT id FROM tasks WHERE status='queued' AND cancel_flag=0 ORDER BY id"
    )
    for row in queued:
        task_id = int(row["id"])
        delay = max(0.0, float(row.get("next_attempt_at") or 0) - time.time())
        if delay:
            _track_retry_timer(task_id, delay)
        else:
            _enqueued_ids.add(task_id)
            await _queue.put(task_id)
        store.add_event(task_id, "平台重启后已恢复持久排队", stage="恢复")
    for i in range(WORKER_COUNT):
        _workers.append(asyncio.create_task(_worker(i), name=f"worker-{i}"))


async def stop() -> None:
    interrupted = store.query(
        "SELECT tasks.id,tasks.kind FROM tasks JOIN job_leases ON job_leases.task_id=tasks.id "
        "WHERE tasks.status='running' AND job_leases.owner_id=?", (_owner_id,)
    )
    for w in _workers:
        w.cancel()
    if _workers:
        await asyncio.gather(*_workers, return_exceptions=True)
    for row in interrupted:
        current = store.get("tasks", row["id"])
        if current and current["status"] == "running":
            if row["kind"] == paired_admission.PAIRED_KIND:
                await paired_engine.fail_execution(
                    row["id"], "worker_lease_lost", "执行节点停止"
                )
                continue
            store.update("tasks", row["id"], {
                "status": "interrupted", "finished_at": time.time(),
            })
            store.add_event(
                row["id"], "平台停止，任务已标记为中断",
                stage="恢复", level="warn",
            )
    _workers.clear()
    for timer in tuple(_retry_timers):
        timer.cancel()
    if _retry_timers:
        await asyncio.gather(*_retry_timers, return_exceptions=True)
    _retry_timers.clear()
    _enqueued_ids.clear()
    _active_ids.clear()


def _claim_lease(task_id: int) -> bool:
    now = time.time()
    with store.cursor() as cur:
        row = cur.execute(
            "SELECT * FROM job_leases WHERE task_id=?", (task_id,)
        ).fetchone()
        if row and row["lease_until"] > now and row["owner_id"] != _owner_id:
            return False
        if row:
            cur.execute(
                "UPDATE job_leases SET owner_id=?,lease_until=?,heartbeat_at=?,acquired_at=? "
                "WHERE task_id=?", (_owner_id, now + LEASE_SECONDS, now, now, task_id),
            )
        else:
            cur.execute(
                "INSERT INTO job_leases "
                "(task_id,owner_id,lease_until,heartbeat_at,acquired_at) VALUES (?,?,?,?,?)",
                (task_id, _owner_id, now + LEASE_SECONDS, now, now),
            )
    return True


async def _heartbeat_lease(task_id: int) -> None:
    while True:
        await asyncio.sleep(LEASE_HEARTBEAT_SECONDS)
        now = time.time()
        store.execute(
            "UPDATE job_leases SET lease_until=?,heartbeat_at=? "
            "WHERE task_id=? AND owner_id=?",
            (now + LEASE_SECONDS, now, task_id, _owner_id),
        )


def _release_lease(task_id: int) -> None:
    store.execute(
        "DELETE FROM job_leases WHERE task_id=? AND owner_id=?", (task_id, _owner_id)
    )


async def submit(task_id: int) -> None:
    assert _queue is not None, "runner 未启动"
    if task_id in _enqueued_ids or task_id in _active_ids:
        return
    task = store.get("tasks", task_id)
    if not task:
        raise ValueError(f"任务不存在：{task_id}")
    if task["status"] == "running":
        return
    store.update("tasks", task_id, {"status": "queued", "next_attempt_at": None})
    store.add_event(task_id, "任务已提交，等待执行", stage="队列")
    _enqueued_ids.add(task_id)
    await _queue.put(task_id)


def _track_retry_timer(task_id: int, delay: float) -> None:
    timer = asyncio.create_task(_enqueue_after(task_id, delay), name=f"retry-{task_id}")
    _retry_timers.add(timer)
    timer.add_done_callback(_retry_timers.discard)


async def _enqueue_after(task_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    task = store.get("tasks", task_id)
    if not task or task["status"] != "queued" or task["cancel_flag"]:
        return
    assert _queue is not None
    if task_id not in _enqueued_ids and task_id not in _active_ids:
        _enqueued_ids.add(task_id)
        await _queue.put(task_id)


def _handle_executor_failure(task_id: int, exc: Exception) -> None:
    task = store.get("tasks", task_id)
    if not task or task["status"] not in {"queued", "running"}:
        return
    attempt = int(task.get("attempt_count") or 0) + 1
    maximum = int(task.get("max_attempts") or 3)
    safe_error = mask(str(exc))[:500]
    if attempt < maximum:
        delay = min(300.0, float(2 ** (attempt - 1) * 5))
        store.update("tasks", task_id, {
            "status": "queued", "attempt_count": attempt,
            "next_attempt_at": time.time() + delay,
            "last_executor_error": safe_error, "started_at": None,
        })
        store.add_event(
            task_id, f"执行器异常，第 {attempt}/{maximum} 次；{delay:g} 秒后重试：{safe_error}",
            stage="可靠性", level="warn",
        )
        _track_retry_timer(task_id, delay)
        return
    now = time.time()
    store.update("tasks", task_id, {
        "status": "failed", "attempt_count": attempt,
        "last_executor_error": safe_error, "dead_lettered_at": now,
        "finished_at": now,
    })
    store.add_event(
        task_id, f"执行器连续失败，已进入失败队列：{safe_error}",
        stage="可靠性", level="error",
    )
    store.record_system_alert(
        f"task-dead-letter:{task_id}", "task_dead_letter",
        f"任务 #{task_id} 进入失败队列", safe_error, "critical",
    )


async def _worker(idx: int) -> None:
    assert _queue is not None
    while True:
        task_id = await _queue.get()
        try:
            _enqueued_ids.discard(task_id)
            task = store.get("tasks", task_id)
            if not task or task["status"] != "queued" or task["cancel_flag"]:
                continue
            if not _claim_lease(task_id):
                continue
            _active_ids.add(task_id)
            heartbeat = asyncio.create_task(_heartbeat_lease(task_id))
            try:
                await _run_task(task_id)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                _release_lease(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 兜底：worker 不能死
            _handle_executor_failure(task_id, exc)
        finally:
            _active_ids.discard(task_id)
            _queue.task_done()


def _expand(pack: dict[str, Any], include_hard: bool = True, *,
            model: str = "", seed: int = 1,
            variant: int = 1) -> list[dict[str, Any]]:
    """把测试包展开成扁平的执行步骤，好在提交前就算出总数。

    include_hard=False 时跳过硬题（任务上的开关关掉了），其余步骤不变 ——
    所以关掉硬题的接入检测与加硬题之前完全等价，费用也回到原来的水平。
    """
    out: list[dict[str, Any]] = []
    for s in pack["steps"]:
        if s["kind"] in {"agent_stability", "development_speed", "long_context"}:
            for item in evaluation_packs.items_for(s["kind"]):
                out.append({"kind": "package_eval", "item": item,
                            "package": s["kind"], "method_id": item["method_id"]})
            continue
        if s["kind"] == "eval":
            for item in test_catalog.admission_items(model, seed, variant):
                out.append({
                    "kind": "eval", "dim": item.get("dim") or "风险", "item": item,
                    "max_tokens": int(item["max_tokens"]),
                    "method_id": item["method_id"],
                })
            continue
        if s["kind"] == "fixed_speed":
            for item in test_catalog.fixed_speed_items():
                out.append({"kind": "fixed_speed", "item": item,
                            "method_id": item["method_id"]})
            continue
        if s["kind"] == "hard":
            if not include_hard:
                continue
            for item in hardbank.all_items(s.get("banks")):
                out.append({"kind": "hard", "item": item, "method_id": "T62"})
            continue
        if s["kind"] != "capability":
            out.append(s)
            continue
        only = s.get("only")
        for item in probes.CAPABILITY_PROBES:
            if only and item["name"] not in only:
                continue
            out.append({"kind": "capability", "item": item})
    return out


async def _one_step(
    cli: httpx.AsyncClient, cfg: dict[str, Any], spec: dict[str, Any],
) -> dict[str, Any]:
    """按步骤类型分派到对应 probe。"""
    kind = spec["kind"]
    if kind == "auth":
        return await probes.probe_auth(cli, cfg)
    if kind == "stream":
        return await probes.probe_stream(
            cli, cfg, step=spec.get("step", "流式输出"),
            prompt=spec.get("prompt", "从 1 数到 20，用逗号分隔。"),
            max_tokens=int(spec.get("max_tokens") or 200),
            performance_probe=bool(spec.get("performance_probe")),
        )
    if kind == "error":
        return await probes.probe_error_handling(cli, cfg)
    if kind == "capability":
        return await probes.probe_capability(cli, cfg, spec["item"])
    if kind == "hard":
        return await probes.probe_hard_item(cli, cfg, spec["item"])
    if kind == "eval":
        item = spec["item"]
        streamed = await probes.probe_stream(
            cli, cfg, step=f"{spec['dim']}·{item['id']}", prompt=item["prompt"],
            max_tokens=int(item["max_tokens"]))
        return probes.attach_evaluation(
            streamed, item, text=str(streamed["extra"].get("grade_text") or ""))
    if kind == "fixed_speed":
        item = spec["item"]
        streamed = await probes.probe_stream(
            cli, cfg, step=f"固定速度·{item['id']}", prompt=item["prompt"],
            max_tokens=int(item["max_tokens"]), performance_probe=True)
        return probes.attach_evaluation(
            streamed, item, text=str(streamed["extra"].get("grade_text") or ""))
    if kind == "package_eval":
        item = spec["item"]
        if spec["package"] == "development_speed":
            streamed = await probes.probe_stream(
                cli, cfg, step=f"development_speed·{item['id']}",
                prompt=item["prompt"], max_tokens=int(item["max_tokens"]),
                performance_probe=True)
            streamed["extra"].update({
                "item": item["id"], "method_id": item["method_id"],
                "evaluation_package": item["package"],
                "score_domain": item["score_domain"],
                "workload": item.get("workload"), "variant": 1,
            })
            return probes.attach_evaluation(
                streamed, item, text=str(streamed["extra"].get("grade_text") or ""))
        evaluated = await probes.probe_eval_item(
            cli, cfg, spec["package"], item, int(item["max_tokens"]))
        return evaluated if evaluated["extra"].get("grade") else \
            probes.attach_evaluation(evaluated, item)
    if kind == "specialty":
        item = spec["item"]
        streamed = await probes.probe_stream(
            cli, cfg, step=f"专项·{spec['profile']}·{item['id']}",
            prompt=item["prompt"], max_tokens=int(item["max_tokens"]))
        return probes.attach_evaluation(
            streamed, item, text=str(streamed["extra"].get("grade_text") or ""))
    opts: dict[str, Any] = {"step": spec.get("step", "基础问答"),
                            "max_tokens": int(spec.get("max_tokens") or 256)}
    if spec.get("prompt"):
        opts["prompt"] = spec["prompt"]
    if spec.get("json_mode"):
        opts["json_mode"] = True
    res = await probes.probe_chat(cli, cfg, **opts)
    return res


async def _run_repeated_inspect(
    cli: httpx.AsyncClient, cfg: dict[str, Any], task: dict[str, Any],
    specs: list[dict[str, Any]], snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    rounds = int(snapshot.get("inspect_rounds") or 1)
    interval = float(snapshot.get("round_interval_seconds") or 0)
    steps: list[dict[str, Any]] = []
    progress_lock = asyncio.Lock()
    started = time.perf_counter()

    async def run_round(round_number: int) -> None:
        if round_number > 1:
            await asyncio.sleep(interval * (round_number - 1))
        if _cancelled(task["id"]):
            return
        results = await asyncio.gather(*(_one_step(cli, cfg, spec) for spec in specs))
        async with progress_lock:
            for spec, res in zip(specs, results):
                res["extra"]["inspect_round"] = round_number
                if spec.get("method_id"):
                    res["extra"]["method_id"] = spec["method_id"]
                steps.append(res)
                store.add_event(
                    task["id"], f"第 {round_number} 轮 · {_event_text(res)}",
                    stage=res["step"], level="info" if res["ok"] else "warn")
            metrics = report.compute_metrics(
                steps, snapshot.get("price_in"), snapshot.get("price_out"))
            done = len(steps)
            total = len(specs) * rounds
            elapsed = time.perf_counter() - started
            eta = (elapsed / done) * (total - done) if done else 0.0
            last_error = next(
                (step["detail"] for step in reversed(steps) if not step["ok"]), "")
            store.update("tasks", task["id"], {"progress": store.dumps({
                "done": done, "total": total, "failed": metrics["failed"],
                "current": f"第 {round_number} 轮", "eta": round(eta, 1),
                "tokens": metrics["tokens"], "cost": metrics["cost"],
                "last_error": last_error,
            })})

    await asyncio.gather(*(run_round(round_number) for round_number in range(1, rounds + 1)))
    return steps


def _cancelled(task_id: int) -> bool:
    row = store.get("tasks", task_id)
    return bool(row and row["cancel_flag"])


def _event_text(res: dict[str, Any]) -> str:
    """Render transport completion and content grade as separate facts."""
    grade = res["extra"].get("grade") or {}
    if grade.get("status") == "passed":
        return f"{res['step']}：内容通过"
    if grade.get("status") == "partial":
        return f"{res['step']}：内容部分通过 - {res['detail']}"
    if grade.get("status") == "failed":
        return f"{res['step']}：内容未通过 - {res['detail']}"
    if grade.get("status") in {"not_graded", "grader_error"}:
        return f"{res['step']}：未形成内容判分 - {res['detail']}"
    if res["ok"]:
        return f"{res['step']}：通过"
    return f"{res['step']}：失败 - {res['detail']}"


async def _run_load_rounds(
    cli: httpx.AsyncClient, cfg: dict[str, Any], task: dict[str, Any],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    settings = snapshot.get("load") or {}
    levels = settings.get("levels") or [10, 20, 30, 40, 50]
    per_level = int(settings.get("requests_per_level") or 20)
    mode = settings.get("mode") or "closed"
    profile = settings.get("prompt_profile") or "simple"
    stream = bool(settings.get("stream", True))
    interval = float(settings.get("interval_seconds") or 0.0)
    burst_period = float(settings.get("burst_period_seconds") or 1.0)
    max_in_flight = int(settings.get("max_in_flight") or 250)
    profile_defaults = {"simple": 64, "reasoning": 512, "coding": 1600}
    max_tokens = int(settings.get("max_tokens") or profile_defaults[profile])
    steps: list[dict[str, Any]] = []
    baseline_speed: float | None = None
    baseline_ttft: float | None = None

    def prompt_for(level: int, index: int, repeat: bool) -> str:
        if profile == "reasoning":
            base = "鸡兔同笼，共有35个头、94只脚。鸡和兔各多少只？请简要推理并给出答案。"
        elif profile == "coding":
            base = "用 Python 实现带指数退避的异步上游请求函数，处理超时和 429，给出完整代码。"
        else:
            base = "用一句话说明 API 渠道稳定性为什么重要。"
        return base if repeat else f"{base}\n本次唯一探针编号：{level}-{index}-{task['id']}。"

    async def request_one(level: int, round_index: int, index: int,
                          repeated: int) -> dict[str, Any]:
        repeat = index < repeated
        prompt = prompt_for(level, index, repeat)
        if stream:
            res = await probes.probe_stream(
                cli, cfg, step=f"{mode} {level}·{'旧题' if repeat else '新题'} #{index + 1}",
                prompt=prompt, max_tokens=max_tokens)
        else:
            res = await probes.probe_chat(
                cli, cfg, step=f"{mode} {level}·{'旧题' if repeat else '新题'} #{index + 1}",
                prompt=prompt, max_tokens=max_tokens)
        res["extra"].update({
            "load": True, "load_level": int(level), "load_round": round_index,
            "load_mode": mode, "prompt_profile": profile,
            "cache_candidate": repeat, "load_methods": [f"T{i}" for i in range(73, 91)],
        })
        if interval and mode == "closed":
            await asyncio.sleep(interval)
        return res

    for round_index, level in enumerate(levels, 1):
        if _cancelled(task["id"]):
            break
        repeated = per_level // 2
        started = time.perf_counter()
        if mode == "closed":
            semaphore = asyncio.Semaphore(int(level))
            async def closed_one(index: int) -> dict[str, Any]:
                async with semaphore:
                    return await request_one(level, round_index, index, repeated)
            batch = await asyncio.gather(*(closed_one(i) for i in range(per_level)))
        else:
            in_flight = 0
            counter_lock = asyncio.Lock()
            async def open_one(index: int) -> dict[str, Any]:
                nonlocal in_flight
                if burst_period > 1:
                    batch_size = max(1, round(level * burst_period))
                    planned = (index // batch_size) * burst_period
                else:
                    planned = index / max(level, 1)
                await asyncio.sleep(max(0.0, planned - (time.perf_counter() - started)))
                async with counter_lock:
                    if in_flight >= max_in_flight:
                        saturated = probes.result(
                            f"open {level}·#{index + 1}", False, reason=probes.PLATFORM,
                            detail="压测机最大在飞已满，请提高上限或更换压测机",
                            extra={"load": True, "load_level": int(level),
                                   "load_round": round_index, "load_mode": mode,
                                   "prompt_profile": profile, "generator_saturated": True,
                                   "cache_candidate": index < repeated,
                                   "load_methods": [f"T{i}" for i in range(73, 91)]})
                        return saturated
                    in_flight += 1
                try:
                    return await request_one(level, round_index, index, repeated)
                finally:
                    async with counter_lock:
                        in_flight -= 1
            batch = await asyncio.gather(*(open_one(i) for i in range(per_level)))
        elapsed = time.perf_counter() - started
        for res in batch:
            res["extra"]["round_elapsed"] = round(elapsed, 3)
        steps.extend(batch)

        success = sum(1 for res in batch if res["ok"]) / len(batch)
        valid_batch = [res for res in batch if report.has_usable_model_response(res)]
        speeds = [float(res["extra"].get("tokens_per_second") or 0.0)
                  for res in valid_batch if res["extra"].get("tokens_per_second")]
        speed = sum(speeds) / len(speeds) if speeds else 0.0
        ttfts = [res["first_token"] for res in valid_batch if res["first_token"] > 0]
        ttft = sorted(ttfts)[len(ttfts) // 2] if ttfts else 0.0
        if baseline_speed is None:
            baseline_speed = speed
        if baseline_ttft is None and ttft:
            baseline_ttft = ttft
        decline = (baseline_speed - speed) / baseline_speed if baseline_speed else 0.0
        ttft_decline = (ttft - baseline_ttft) / baseline_ttft if baseline_ttft and ttft else 0.0
        combined_decline = max(0.0, decline) * 0.5 + max(0.0, ttft_decline) * 0.5
        store.add_event(
            task["id"],
            f"并发 {level} 完成：成功率 {success:.0%}，吞吐 {len(batch) / elapsed:.2f} req/s，"
            f"综合衰减 {combined_decline:.0%}",
            stage="压力测试", level="info" if success >= 0.95 else "warn")
        store.update("tasks", task["id"], {"progress": store.dumps({
            "done": round_index,
            "total": len(levels),
            "failed": sum(1 for res in steps if not res["ok"]),
            "current": f"并发 {level} 已完成",
            "eta": 0,
            "tokens": sum(max(res["usage"].get("prompt", 0), 0)
                          + max(res["usage"].get("completion", 0), 0) for res in steps),
            "cost": report.compute_metrics(
                steps, snapshot.get("price_in"), snapshot.get("price_out"))["cost"],
            "last_error": next((res["detail"] for res in reversed(steps)
                                if not res["ok"]), ""),
        })})

        metrics = report.compute_metrics(
            steps, snapshot.get("price_in"), snapshot.get("price_out"))
        if task.get("cost_limit") and metrics["cost"] > task["cost_limit"]:
            store.add_event(task["id"], "达到费用上限，停止后续并发轮次",
                            stage="费用", level="warn")
            break
        if success < 0.95 or combined_decline > 0.55:
            store.add_event(
                task["id"], "达到压力测试停止条件，不再提高并发",
                stage="压力测试", level="warn")
            break
        cooldown = float(settings.get("cooldown_seconds") or 0)
        if cooldown and round_index < len(levels):
            store.add_event(task["id"], f"冷却 {cooldown:g} 秒后进入下一档", stage="压力测试")
            await asyncio.sleep(cooldown)
    return steps


async def _run_task(task_id: int) -> None:
    """执行一个任务。每步结果即时落库，进度可下钻，失败继续跑后面的步骤。"""
    task = store.get("tasks", task_id)
    if not task or task["cancel_flag"]:
        return
    if task["kind"] == "load":
        store.update("tasks", task_id, {
            "status": "failed", "finished_at": time.time(),
            "progress": store.dumps({"stage": "云端禁止执行压力测试，请使用配对本地执行器"}),
        })
        store.add_event(
            task_id, "安全边界拒绝云端压力测试", stage="本地执行器", level="error"
        )
        return
    if task["kind"] == paired_admission.PAIRED_KIND:
        store.update("tasks", task_id, {"status": "running", "started_at": time.time()})
        store.add_event(task_id, "双端准入执行器取得任务", stage="执行")
        try:
            await paired_engine.execute(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await paired_engine.fail_execution(
                task_id, "worker_execution_failed",
                f"执行器异常停止：{type(exc).__name__}",
            )
        return
    if task["kind"] == scheduled_measurement.TASK_KIND:
        await scheduled_measurement.run(task_id)
        return
    pack = packs.get_pack(task["kind"])
    snapshot = store.loads(task["snapshot"], {})
    target = store.get("targets", task["target_id"]) if task["target_id"] else None

    store.update("tasks", task_id, {"status": "running", "started_at": time.time()})
    store.add_event(task_id, f"开始执行：{pack['name']} {pack['version']}", stage="执行")

    # 真 Key 只在内存里用，快照里始终是脱敏值
    key_enc = (target or {}).get("key_enc") or snapshot.get("key_enc", "")
    try:
        real_key = decrypt(key_enc) if key_enc else ""
    except Exception:
        store.add_event(task_id, "密钥解密失败，请重新填写连接信息", stage="配置", level="error")
        store.update("tasks", task_id, {"status": "failed", "finished_at": time.time()})
        return

    cfg = {
        "protocol": snapshot.get("protocol", "openai"),
        "base_url": snapshot.get("base_url", ""),
        "model": snapshot.get("model", ""),
        "key": real_key,
    }
    # 硬题开关存在任务行上，重试同一个任务时口径不变，历史可比
    include_hard = bool(task["include_hard"]) \
        if task["include_hard"] is not None else True
    variant = int(snapshot.get("item_variant") or ((task_id - 1) % 3 + 1))
    if snapshot.get("item_variant") != variant:
        snapshot["item_variant"] = variant
        store.update("tasks", task_id, {"snapshot": store.dumps(snapshot)})
    specs = _expand(pack, include_hard, model=cfg["model"], seed=task_id,
                    variant=variant)
    specs.extend(specialty.expand(list(snapshot.get("specialty_profiles") or [])))
    steps: list[dict[str, Any]] = []
    cost_limit = task["cost_limit"] or 0.0
    t_start = time.perf_counter()

    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT, follow_redirects=True,
        max_redirects=egress.EGRESS_MAX_REDIRECTS,
        event_hooks=egress.event_hooks(),
    ) as cli:
        if task["kind"] == "inspect" and int(snapshot.get("inspect_rounds") or 1) > 1:
            steps = await _run_repeated_inspect(cli, cfg, task, specs, snapshot)
            if _cancelled(task_id):
                store.update("tasks", task_id, {
                    "status": "cancelled", "finished_at": time.time()})
                _save_partial(task_id, task, pack, steps, snapshot)
                return
            _finalize(task_id, task, pack, steps, snapshot, target)
            return
        for i, spec in enumerate(specs):
            if _cancelled(task_id):
                store.add_event(task_id, "收到取消指令，停止后续测试", stage="执行", level="warn")
                store.update("tasks", task_id, {
                    "status": "cancelled", "finished_at": time.time()})
                _save_partial(task_id, task, pack, steps, snapshot)
                return

            res = await _one_step(cli, cfg, spec)
            if spec.get("method_id"):
                res["extra"]["method_id"] = spec["method_id"]
            steps.append(res)
            store.add_event(task_id, _event_text(res),
                            stage=res["step"],
                            level="info" if res["ok"] else "warn")

            m = report.compute_metrics(steps, snapshot.get("price_in"),
                                       snapshot.get("price_out"))
            elapsed = time.perf_counter() - t_start
            done = i + 1
            eta = (elapsed / done) * (len(specs) - done) if done else 0.0
            last_err = next((s["detail"] for s in reversed(steps) if not s["ok"]), "")
            store.update("tasks", task_id, {"progress": store.dumps({
                "done": done, "total": len(specs),
                "failed": m["failed"], "current": res["step"],
                "eta": round(eta, 1), "tokens": m["tokens"], "cost": m["cost"],
                "last_error": last_err,
            })})

            # 费用上限：超了就停，已完成的部分仍然出报告
            if cost_limit and m["cost"] > cost_limit:
                store.add_event(
                    task_id, f"已达费用上限 ¥{cost_limit}，停止后续测试",
                    stage="费用", level="warn")
                break

    if pack.get("catalog_analysis"):
        analyses = test_catalog.analysis_results(steps, cfg["model"])
        steps.extend(analyses)
        for res in analyses:
            store.add_event(task_id, _event_text(res), stage=res["step"],
                            level="info" if res["ok"] else "warn")

    _finalize(task_id, task, pack, steps, snapshot, target)


def _load_benchmark(task: dict[str, Any]) -> dict[str, Any] | None:
    """取任务指定标杆；未指定时使用目标平台组的同模型标杆。"""
    bid = task.get("benchmark_id")
    if not bid and task.get("target_id"):
        tgt = store.get("targets", task["target_id"])
        benchmark = workbench.benchmark_for_target(tgt or {}) if tgt else None
        bid = (benchmark or {}).get("id")
    if not bid:
        return None
    row = store.get("benchmarks", int(bid))
    if not row:
        return None
    return {
        "id": row["id"], "name": row["name"],
        "pack_version": row["pack_version"],
        "dims": store.loads(row["dims"], {}),
        "overall": row["overall"],
        "tolerance": row["tolerance"],
    }


def _save_partial(
    task_id: int, task: dict[str, Any], pack: dict[str, Any],
    steps: list[dict[str, Any]], snapshot: dict[str, Any],
) -> None:
    """取消时也保留已完成部分的报告，历史不丢。"""
    if not steps:
        return
    rep = report.build({**task, "finished_at": time.time()}, pack, steps, snapshot)
    store.update("tasks", task_id, {"report": store.dumps(rep)})


def _finalize(
    task_id: int, task: dict[str, Any], pack: dict[str, Any],
    steps: list[dict[str, Any]], snapshot: dict[str, Any],
    target: dict[str, Any] | None,
) -> None:
    """生成报告、判定状态、回写目标状态与基线。"""
    finished = time.time()
    baseline = store.loads((target or {}).get("baseline"), None)
    benchmark = _load_benchmark(task)
    try:
        rep = report.build({**task, "finished_at": finished}, pack, steps, snapshot,
                           baseline, benchmark)
    except Exception as exc:
        # 报告生成失败不能连带丢掉任务与已有数据
        store.add_event(task_id, f"报告生成失败：{exc}", stage="报告", level="error")
        store.update("tasks", task_id, {"status": "partial", "finished_at": finished})
        return

    basic_steps = [step for step in steps if not step["extra"].get("specialty_profile")]
    ok_count = sum(1 for step in basic_steps if step["ok"])
    if not basic_steps:
        status = "failed"
    elif ok_count == len(basic_steps):
        status = "success"
    elif ok_count == 0:
        status = "failed"
    else:
        status = "partial"

    # 先写报告、目标状态和分组推荐，最后才把状态翻成终态。
    # 顺序反了的话，轮询到"已完成"的客户端可能读到还没写入的推荐，前端会闪一下空白。
    store.update("tasks", task_id, {
        "finished_at": finished, "report": store.dumps(rep)})
    if target:
        if task["kind"] not in {"agent_stability", "development_speed", "long_context"}:
            _update_target(target, task, rep, steps)
        _make_recommendation(target, task, rep)
        _save_specialty_labels(target, task, rep)
        if task["kind"] == "admission" and target.get("channel_id"):
            channel = store.get("channels", int(target["channel_id"]))
            if channel and channel["lifecycle_status"] == "candidate":
                lifecycle.transition_channel(
                    int(channel["id"]), "tested", "准入测试已产生报告",
                    {"id": None, "username": "system"},
                )

    store.update("tasks", task_id, {"status": status})
    store.add_event(task_id, f"执行完成：{report.summary_line(rep)}", stage="完成")


def _save_specialty_labels(
    target: dict[str, Any], task: dict[str, Any], rep: dict[str, Any],
) -> None:
    for code, result in (rep["metrics"].get("specialties") or {}).items():
        store.execute(
            "INSERT INTO target_usage_labels "
            "(task_id,target_id,usage_profile,pack_version,status,score,evidence_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task_id,usage_profile) DO UPDATE SET "
            "status=excluded.status,score=excluded.score,evidence_json=excluded.evidence_json",
            (task["id"], target["id"], code, result["version"], result["status"],
             result["score"], store.dumps(result), time.time()),
        )


def _make_recommendation(
    target: dict[str, Any], task: dict[str, Any], rep: dict[str, Any],
) -> None:
    """跑完带四维能力评分的任务后，对比所选平台组的同模型标杆。"""
    if task["kind"] not in ("admission", "capability"):
        return
    cap = rep["metrics"].get("capability")
    if not cap:
        return
    if target.get("platform_group_id"):
        _make_platform_recommendation(target, task, rep, cap)
        return
    platform = workbench.platform_group(target["platform_group_id"]) \
        if target.get("platform_group_id") else None
    benchmark_row = workbench.benchmark_for_target(target)
    trust = rep["metrics"].get("trust") or {"ok": True, "reasons": []}
    if not platform:
        result = {
            "status": "unassigned", "target_group_id": None,
            "headline": "尚未选择平台倍率组",
            "reasons": ["请在工作台为这个模型选择目标平台倍率组"],
            "comparisons": [],
        }
    elif not trust.get("ok"):
        result = {
            "status": "untrusted", "target_group_id": platform["id"],
            "target_group_name": platform["label"],
            "headline": "本轮评分不可信，未进行标杆比较",
            "reasons": trust.get("reasons") or ["需要先排除上游转发异常"],
            "comparisons": [],
        }
    elif not benchmark_row:
        result = {
            "status": "no_benchmark", "target_group_id": platform["id"],
            "target_group_name": platform["label"],
            "headline": f"{platform['label']} 的 {target['model']} 尚未设置标杆",
            "reasons": ["本次结果已保留，可在工作台将可信结果设为标杆"],
            "comparisons": [],
        }
    else:
        benchmark = {
            "id": benchmark_row["id"], "name": benchmark_row["name"],
            "pack_version": benchmark_row["pack_version"],
            "dims": store.loads(benchmark_row["dims"], {}),
            "overall": benchmark_row["overall"],
            "tolerance": benchmark_row["tolerance"],
        }
        comparison = placement.compare_capability_to_benchmark(cap, benchmark)
        if not comparison:
            result = {
                "status": "no_comparable", "target_group_id": platform["id"],
                "target_group_name": platform["label"],
                "headline": "目标标杆缺少可比维度",
                "reasons": ["请重新设置完整标杆"], "comparisons": [],
            }
        else:
            qualified = bool(comparison.get("qualified"))
            reason = comparison.get("skip_reason") if not comparison.get("comparable") else (
                f"综合能力为标杆的 {(comparison.get('ratio') or 0):.0%}，"
                f"达标线为 {comparison.get('qualify_ratio', 0):.0%}"
            )
            result = {
                "status": "target_met" if qualified else "target_missed",
                "target_group_id": platform["id"],
                "target_group_name": platform["label"],
                "headline": (
                    f"达到 {platform['label']} 的 {target['model']} 标杆"
                    if qualified else
                    f"未达到 {platform['label']} 的 {target['model']} 标杆"
                ),
                "reasons": [reason],
                "comparisons": [{
                    **comparison, "group_id": platform["id"],
                    "group_name": platform["label"],
                    "multiplier": platform["online_multiplier"],
                }],
            }
    store.insert("recommendations", {
        "task_id": task["id"], "target_id": target["id"],
        "status": "pending", "result": store.dumps(result),
        "suggested_group_id": None,
        "created_at": time.time(),
    })
    store.add_event(task["id"], f"分组推荐：{result['headline']}", stage="推荐")


def _make_platform_recommendation(
    target: dict[str, Any], task: dict[str, Any], rep: dict[str, Any],
    cap: dict[str, Any],
) -> None:
    """同一次同版本结果先比较意向组，再比较同家族其他有效标杆组。"""
    intended_id = int(target["platform_group_id"])
    intended = workbench.platform_group(intended_id)
    trust = rep["metrics"].get("trust") or {"ok": True, "reasons": []}
    comparisons = []
    if intended:
        candidates = [group for group in workbench.list_platform_groups()
                      if group["family_id"] == intended["family_id"]]
        candidates.sort(key=lambda group: (
            group["id"] != intended_id, -float(group["online_multiplier"]), group["id"]
        ))
        for group in candidates:
            slot = next((item for item in group["models"]
                         if item["model"] == target["model"]), None)
            benchmark = (slot or {}).get("benchmark")
            if not benchmark or benchmark.get("stale"):
                continue
            comparable = placement.compare_capability_to_benchmark(cap, {
                "id": benchmark["id"], "name": benchmark["name"],
                "pack_version": benchmark["pack_version"],
                "dims": benchmark["dims"], "overall": benchmark["overall"],
                "tolerance": benchmark["tolerance"],
            })
            if comparable:
                comparisons.append({
                    **comparable, "group_id": group["id"],
                    "group_name": group["label"],
                    "multiplier": group["online_multiplier"],
                    "is_intended": group["id"] == intended_id,
                })
    intended_cmp = next((item for item in comparisons if item["is_intended"]), None)
    alternatives = sorted(
        (item for item in comparisons if item["qualified"] and not item["is_intended"]),
        key=lambda item: (-float(item["multiplier"]), item["group_id"]),
    )
    conclusion_code = (rep.get("conclusion") or {}).get("code")
    suggested_id = None
    if not trust.get("ok"):
        status = "untrusted"
        headline = "本轮证据不可信，不能给出技术分组建议"
        reasons = trust.get("reasons") or ["需要先排除上游转发异常"]
    elif conclusion_code in {"reject", "manual"}:
        status = "reject"
        headline = "基础准入未满足可用性要求，建议拒绝或人工复核"
        reasons = list((rep.get("conclusion") or {}).get("reasons") or [])
    elif intended_cmp and intended_cmp.get("qualified"):
        status = "target_met"
        suggested_id = intended_id
        headline = f"达到意向组 {intended['label']} 的 {target['model']} 标杆"
        reasons = [
            f"综合能力为意向组标杆的 {(intended_cmp.get('ratio') or 0):.0%}，"
            f"达标线为 {intended_cmp.get('qualify_ratio', 0):.0%}"
        ]
    elif alternatives:
        best = alternatives[0]
        status = "alternate_group"
        suggested_id = int(best["group_id"])
        headline = f"未达到意向组，建议改入 {best['group_name']}"
        reasons = [
            "同一次、同题库版本结果未达到意向组，但达到另一组有效标杆",
            "上游采购倍率未参与本次技术建议，最终仍由用户确认",
        ]
    elif intended_cmp:
        status = "observe_pool"
        headline = "未达到任何有效技术组，建议进入观察池"
        reasons = [
            "当前结果未达到意向组，也没有达到其他具备同版本有效标杆的组",
            "可保留结果并在补齐标杆或稳定性证据后复核",
        ]
    else:
        status = "no_benchmark"
        headline = "没有同模型、同版本的有效标杆，暂不能给出技术组建议"
        reasons = ["本次结果已保留，可先设定可信标杆或进入观察池"]
    result = {
        "status": status, "target_group_id": intended_id,
        "target_group_name": intended["label"] if intended else "",
        "suggested_group_id": suggested_id, "headline": headline,
        "reasons": reasons, "comparisons": comparisons,
        "commercial_boundary": "上游采购倍率只展示，不参与技术推荐；平台不计算利润。",
        "decision_required": True,
    }
    store.insert("recommendations", {
        "task_id": task["id"], "target_id": target["id"],
        "status": "pending", "result": store.dumps(result),
        "suggested_group_id": None,
        "target_platform_group_id": intended_id,
        "suggested_platform_group_id": suggested_id,
        "created_at": time.time(),
    })
    store.add_event(task["id"], f"技术分组建议：{headline}", stage="推荐")


def _update_target(
    target: dict[str, Any], task: dict[str, Any],
    rep: dict[str, Any], steps: list[dict[str, Any]],
) -> None:
    """保存检测建议；配置可用性不由评分结论决定。"""
    code = rep["conclusion"]["code"]
    patch: dict[str, Any] = {
        "last_task_id": task["id"],
        "last_verdict": rep["conclusion"]["verdict"],
        "status": code,
        "updated_at": time.time(),
    }
    # 纵向基线：首次跑出四维向量时存下来，供日后降智复核对比
    cap = rep["metrics"].get("capability")
    if cap and cap.get("overall") is not None \
            and not store.loads(target.get("baseline"), None):
        patch["baseline"] = store.dumps({
            "pack_version": cap.get("pack_version"),
            "dims": {k: v["score"] for k, v in cap["dims"].items()
                     if v["score"] is not None},
            "overall": cap["overall"],
            "p95_latency": rep["metrics"]["p95_latency"],
            "task_id": task["id"],
            "at": time.time(),
        })

    # 硬题纵向基线单独存一份。与四维基线分开的原因：硬题可能这趟跑了下趟没跑，
    # 混在一个字段里会出现「基线有四维没硬题」这种半残状态，比较时要处处判空。
    hard = rep["metrics"].get("hard")
    if hard and hard.get("rate") is not None \
            and not store.loads(target.get("hard_baseline"), None):
        patch["hard_baseline"] = store.dumps({
            "hard_version": hard.get("hard_version"),
            "rate": hard["rate"],
            "correct": hard.get("correct"),
            "graded": hard.get("graded"),
            "banks": {k: v.get("rate") for k, v in (hard.get("banks") or {}).items()},
            "task_id": task["id"],
            "at": time.time(),
        })
    store.update("targets", target["id"], patch)
