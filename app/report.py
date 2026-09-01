"""报告：先结论，再证据。原始日志不作为报告正文。"""
from typing import Any

from . import advice, itembank, probes, scoring, specialty, test_catalog
from .config import DEFAULT_PRICE_IN, DEFAULT_PRICE_OUT


def _pct(values: list[float], q: float) -> float:
    """简单百分位，数据量小，不引入 numpy。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(int(round(q * (len(ordered) - 1))), len(ordered) - 1)
    return round(ordered[idx], 3)


def has_usable_model_response(step: dict[str, Any]) -> bool:
    """是否拿到了可用于速度统计的完整模型响应。"""
    if not step["ok"] and step["reason"] != probes.MISMATCH:
        return False
    if step["extra"].get("stream_break"):
        return False
    usage = step.get("usage") or {}
    has_output = bool(
        step["extra"].get("reply")
        or step["extra"].get("tool_calls")
        or step["extra"].get("chunks")
        or usage.get("completion", 0) > 0
    )
    return has_output


def compute_metrics(
    steps: list[dict[str, Any]], price_in: float | None, price_out: float | None,
) -> dict[str, Any]:
    """核心指标：成功率、延迟分位、断流率、错误分布、tokens 与成本。

    成功率只统计「可用性」步骤：能力题答错属于质量问题，不算连不通。
    能力题因超时/5xx/鉴权而失败时仍计入可用性，因为那确实是掉线。
    """
    # 硬题一律不参与可用性、超时门槛与延迟分位。
    #
    # 理由是这三项都是「渠道能不能用」的判据，而硬题是故意选的高失败区题目，
    # 输出又长又慢（max_tokens 8192）。让它进去会有三个假信号：
    #   1. 答错拉低成功率 —— 但答错是能力问题，已经在硬题块里记着了
    #   2. 偶发超时撑爆 5% 超时门槛 → 直接判暂不准入
    #   3. 单题几十秒把 P95 顶到 20s 以上 → 直接判建议降级
    # 三者都会把一条完全可用的渠道误杀，所以硬题只算自己的分，不碰这些门槛。
    def _is_hard(s: dict[str, Any]) -> bool:
        return bool(s["extra"].get("hard"))

    def _is_specialty(s: dict[str, Any]) -> bool:
        return bool(s["extra"].get("specialty_profile"))

    gate_steps = [s for s in steps if not _is_hard(s) and not _is_specialty(s)]
    hard_steps = [s for s in steps if _is_hard(s)]
    response_steps = [s for s in gate_steps if has_usable_model_response(s)]

    def _is_avail(s: dict[str, Any]) -> bool:
        eligibility = s["extra"].get("eligibility") or {}
        if eligibility:
            return eligibility.get("stability") == "eligible"
        if "scored" not in s["extra"]:
            return True
        return not s["ok"] and s["reason"] not in (probes.MISMATCH, probes.TRUNCATED)

    avail = [s for s in gate_steps if _is_avail(s)]
    total = len(avail)
    ok = sum(1 for s in avail if s["ok"])
    latencies = [s["latency"] for s in response_steps if s["latency"] > 0]
    firsts = [s["first_token"] for s in response_steps if s["first_token"] > 0]
    speeds = [float(s["extra"].get("tokens_per_second") or 0.0)
              for s in response_steps if s["extra"].get("tokens_per_second")]

    # 失败分布分两份：
    #   reason_counts      —— 全部步骤，给报告展示用（硬题的失败也要看得见）
    #   reason_counts_gate —— 只含非硬题，**建议引擎用这一份**
    #
    # 必须分开，否则硬题会经由这里绕过隔离去改结论：advice 靠 reason_counts 判
    # 上游错误/超时/限流 → 建议降级。一道硬题 5xx 就能把渠道判成建议降级，
    # 而上面刚刚才把硬题排除在成功率与超时门槛之外 —— 同一个失败在一个门槛里
    # 不算、在另一个门槛里算，口径自相矛盾。
    # 硬题自己的失败情况在 hard 块里有完整记录（未测到/超时/截断逐项都在），不会被藏起来。
    reason_counts: dict[str, int] = {}
    reason_counts_gate: dict[str, int] = {}
    for s in steps:
        if not s["ok"] and s["reason"]:
            reason_counts[s["reason"]] = reason_counts.get(s["reason"], 0) + 1
            if not _is_hard(s) and not _is_specialty(s):
                reason_counts_gate[s["reason"]] = \
                    reason_counts_gate.get(s["reason"], 0) + 1

    stream_steps = [s for s in steps if "chunks" in s["extra"] or s["extra"].get("stream_break")]
    breaks = sum(1 for s in stream_steps if s["extra"].get("stream_break"))
    performance_candidates = [s for s in stream_steps
                              if s["extra"].get("performance_probe")]
    performance_steps = [s for s in performance_candidates
                         if has_usable_model_response(s)
                         and (s["extra"].get("grade") or {}).get("status") == "passed"]

    # 超时率按**除硬题以外的所有**请求算 —— 步骤 1 的硬门槛要看整体稳定性，
    # 但硬题超时是「答得慢」不是「连不上」，混进来会撑爆 5% 门槛直接判暂不准入。
    # 硬题自己的超时数单独记在 hard 块里，不会被藏起来。
    timeouts = sum(1 for s in gate_steps if s["reason"] == probes.TIMEOUT)

    tok_in = sum(max(s["usage"].get("prompt", 0), 0) for s in steps)
    tok_out = sum(max(s["usage"].get("completion", 0), 0) for s in steps)
    pin = price_in if price_in is not None else DEFAULT_PRICE_IN
    pout = price_out if price_out is not None else DEFAULT_PRICE_OUT
    cost = (tok_in / 1_000_000) * pin + (tok_out / 1_000_000) * pout

    caps = [s for s in steps if s["extra"].get("grade") or "scored" in s["extra"]]
    metrics: dict[str, Any] = {
        "total": total, "ok": ok, "failed": total - ok,
        "pass_rate": round(ok / total, 4) if total else 0.0,
        "p50_latency": _pct(latencies, 0.5),
        "p95_latency": _pct(latencies, 0.95),
        "avg_first_token": round(sum(firsts) / len(firsts), 3) if firsts else 0.0,
        "avg_tokens_per_second": round(sum(speeds) / len(speeds), 2) if speeds else 0.0,
        "stream_break_rate": round(breaks / len(stream_steps), 4) if stream_steps else 0.0,
        "reason_counts": reason_counts,
        "reason_counts_gate": reason_counts_gate,
        "request_total": len(gate_steps),
        "request_total_all": len(steps),
        "timeout_count": timeouts,
        "timeout_rate": round(timeouts / len(gate_steps), 4) if gate_steps else 0.0,
        "tokens_in": tok_in, "tokens_out": tok_out, "tokens": tok_in + tok_out,
        "cost": round(cost, 6),
        "price_in": pin, "price_out": pout,
    }
    if performance_steps:
        performance_latency = [s["latency"] for s in performance_steps if s["latency"] > 0]
        performance_ttft = [s["first_token"] for s in performance_steps if s["first_token"] > 0]
        performance_speed = [float(s["extra"]["tokens_per_second"])
                             for s in performance_steps
                             if s["extra"].get("tokens_per_second")]
        performance = {
            "samples": len(performance_steps),
            "expected_samples": len(performance_candidates),
            "success_rate": round(len(performance_steps) / len(performance_candidates), 4)
            if performance_candidates else 0.0,
            "p50_ttft": _pct(performance_ttft, 0.50),
            "p95_ttft": _pct(performance_ttft, 0.95),
            "p50_latency": _pct(performance_latency, 0.50),
            "p95_latency": _pct(performance_latency, 0.95),
            "median_tokens_per_second": _pct(performance_speed, 0.50)
            if performance_speed else None,
            "granular_samples": len(performance_speed),
        }
        metrics["performance"] = performance
        metrics["fixed_speed"] = dict(performance)
    probe_scores = [s for s in caps if "dim" not in s["extra"]
                    and not s["extra"].get("hard") and not s["extra"].get("analysis")
                    and (s["extra"].get("grade") or {}).get("status")
                    in {"passed", "failed", "partial"}]
    if probe_scores:
        metrics["capability_score"] = sum(float(
            (s["extra"].get("grade") or {}).get("score") or 0.0) for s in probe_scores)
        metrics["capability_total"] = len(probe_scores)

    # 结果可信度：上游注入 / 答非所问会让整轮分数失去意义
    trust = scoring.trust_check(gate_steps)
    if not trust["ok"]:
        metrics["trust"] = trust

    # 能力评测：四维分 + 加权总分，横向对比用这一块
    ability_steps = [step for step in steps
                     if not _is_specialty(step)
                     and step["extra"].get("score_domain") == "ability"]
    if ability_steps:
        agg = scoring.dimension_scores(ability_steps)
        attempted_item_ids = {
            str(s["extra"].get("item"))
            for s in ability_steps
            if s["extra"].get("dim") and s["extra"].get("item")
        }
        coverage_enforced = len(attempted_item_ids) >= int(len(itembank.all_items()) * 0.8)
        metrics["capability"] = {
            "pack_version": itembank.PACK_VERSION,
            "dims": agg["dims"],
            "items": agg["items"],
            "by_level": agg["by_level"],
            "overall": scoring.overall(agg["dims"]),
            "vector": scoring.vector(agg["dims"]),
            "dim_order": itembank.DIM_ORDER,
            "coverage": round(sum(v.get("coverage", 1.0) for name, v in agg["dims"].items()
                                  if name in itembank.ABILITY_DIM_ORDER)
                              / len(itembank.ABILITY_DIM_ORDER), 4),
            "coverage_enforced": coverage_enforced,
            # 截断（思考 token 烧穿预算）导致没测到的题，报告里要说清
            "truncated": agg["truncated"],
            "truncated_unscored": agg["truncated_unscored"],
            "truncated_items": agg["truncated_items"],
        }

    ability_items = scoring.aggregate_item_trials(steps, {"ability"})
    speed_steps = [step for step in steps
                   if step["extra"].get("score_domain") == "speed"]
    if ability_items and speed_steps:
        valid_ability = [item for item in ability_items
                         if item["status"] in {"passed", "failed", "partial"}]
        passed_ability = [item for item in valid_ability if item["status"] == "passed"]
        expected_ability = len(test_catalog.admission_items())
        required_ability = test_catalog.INTELLIGENCE_REQUIRED_PASSES
        intelligence_passed = len(valid_ability) == expected_ability \
            and len(passed_ability) >= required_ability
        question_steps = [step for step in steps
                          if step["extra"].get("score_domain") in {"ability", "speed"}]
        completed_streams = [step for step in question_steps
                             if step["ok"] and not step["extra"].get("stream_break")]
        all_streamed = all("chunks" in step["extra"]
                           or step["extra"].get("stream_break") for step in question_steps)
        stability_rate = len(completed_streams) / len(question_steps) if question_steps else 0.0
        stability_passed = all_streamed and stability_rate == 1.0
        auth_steps = [step for step in steps
                      if step["extra"].get("method_id") == "QA-PROTO-01"]
        auth_passed = len(auth_steps) == 1 and auth_steps[0]["ok"]
        speed = metrics.get("fixed_speed") or {}
        speed_complete = int(speed.get("samples") or 0) == len(speed_steps)
        evidence_complete = len(valid_ability) == expected_ability and speed_complete
        if not evidence_complete:
            admission_status = "insufficient_evidence"
        elif auth_passed and intelligence_passed and stability_passed:
            admission_status = "passed"
        else:
            admission_status = "failed"
        metrics["admission"] = {
            "status": admission_status,
            "intelligence_passed": intelligence_passed,
            "intelligence_score": round(len(passed_ability) / expected_ability, 4),
            "intelligence_required_passes": required_ability,
            "passed_items": len(passed_ability),
            "valid_items": len(valid_ability), "expected_items": expected_ability,
            "stability_passed": stability_passed,
            "stability_rate": round(stability_rate, 4),
            "streamed_questions": len(question_steps) if all_streamed else 0,
            "expected_streamed_questions": len(question_steps),
            "auth_passed": auth_passed,
            "speed_samples": int(speed.get("samples") or 0),
            "expected_speed_samples": len(speed_steps),
            "items": ability_items,
        }

    package_steps = [step for step in steps
                     if step["extra"].get("evaluation_package")]
    if package_steps:
        package_name = str(package_steps[0]["extra"]["evaluation_package"])
        graded = [step for step in package_steps
                  if (step["extra"].get("grade") or {}).get("status")
                  in {"passed", "failed", "partial"}]
        passed = [step for step in graded
                  if step["extra"]["grade"]["status"] == "passed"]
        rates: dict[str, Any] = {
            "graded": len(graded), "expected": len(package_steps),
            "pass_rate": round(len(passed) / len(graded), 4) if graded else None,
        }
        if package_name == "agent_stability":
            for label, prefix in (("json", "AS-JSON"), ("tool", "AS-TOOL"),
                                  ("workflow", "AS-WF")):
                selected = [step for step in graded
                            if str(step["extra"].get("item")).startswith(prefix)]
                rates[f"{label}_pass_rate"] = round(sum(
                    step["extra"]["grade"]["status"] == "passed" for step in selected
                ) / len(selected), 4) if selected else None
            metrics["agent_stability"] = rates
        elif package_name == "development_speed":
            workloads: dict[str, Any] = {}
            for workload in ("short", "medium", "long"):
                selected = [step for step in passed
                            if step["extra"].get("workload") == workload]
                workloads[workload] = {
                    "completed": len(selected),
                    "p50_ttft": _pct([step["first_token"] for step in selected
                                       if step["first_token"] > 0], 0.5),
                    "p95_total": _pct([step["latency"] for step in selected
                                        if step["latency"] > 0], 0.95),
                    "median_tokens_per_second": _pct([
                        float(step["extra"].get("tokens_per_second") or 0)
                        for step in selected
                        if step["extra"].get("tokens_per_second")], 0.5),
                }
            rates["workloads"] = workloads
            metrics["development_speed"] = rates
        elif package_name == "long_context":
            recalls = [step for step in graded
                       if str(step["extra"].get("item")).startswith("LC-")
                       and not str(step["extra"].get("item")).startswith("LC-CACHE")]
            recall_passed = sum(step["extra"]["grade"]["status"] == "passed"
                                for step in recalls)
            cache_usage_reported = any(
                int(step["usage"].get("cached_read", -1)) >= 0
                or int(step["usage"].get("cached_write", -1)) >= 0
                for step in package_steps)
            cache_steps = [step for step in package_steps
                           if step["extra"].get("cache_sequence")]
            cached_reads = [int(step["usage"].get("cached_read", -1))
                            for step in cache_steps
                            if int(step["usage"].get("cached_read", -1)) >= 0]
            rates.update({
                "reliable_context_tier": "8k" if recalls and recall_passed == len(recalls)
                else "below_8k_or_unproven",
                "recall_pass_rate": round(recall_passed / len(recalls), 4)
                if recalls else None,
                "cache_status": "measured" if cache_usage_reported else "unverified",
                "cache_read_tokens": sum(cached_reads) if cached_reads else None,
                "cache_hit_rate": round(sum(value > 0 for value in cached_reads)
                                        / len(cached_reads), 4)
                if cached_reads else None,
            })
            metrics["long_context"] = rates

    # 硬题：独立计分块，不进 capability，也不参与任何门槛
    hard = scoring.hard_scores(steps)
    if hard:
        hard_lat = [s["latency"] for s in hard_steps
                    if has_usable_model_response(s) and s["latency"] > 0]
        hard["p95_latency"] = _pct(hard_lat, 0.95)
        hard["timeout_count"] = sum(
            1 for s in hard_steps if s["reason"] == probes.TIMEOUT)
        metrics["hard"] = hard

    load_steps = [s for s in steps if s["extra"].get("load")]
    if load_steps:
        rounds: list[dict[str, Any]] = []
        base_speed: float | None = None
        for level in sorted({int(s["extra"]["load_level"]) for s in load_steps}):
            group = [s for s in load_steps if int(s["extra"]["load_level"]) == level]
            valid_group = [s for s in group if has_usable_model_response(s)]
            lat = [s["latency"] for s in valid_group if s["latency"] > 0]
            first = [s["first_token"] for s in valid_group if s["first_token"] > 0]
            speed_values = [float(s["extra"].get("tokens_per_second") or 0.0)
                            for s in valid_group if s["extra"].get("tokens_per_second")]
            speed = sum(speed_values) / len(speed_values) if speed_values else 0.0
            if base_speed is None:
                base_speed = speed
            decline = (base_speed - speed) / base_speed if base_speed else 0.0
            elapsed = max(float(s["extra"].get("round_elapsed") or 0.0)
                          for s in group)
            repeated = [s["latency"] for s in valid_group
                        if s["extra"].get("cache_candidate") and s["latency"] > 0]
            fresh = [s["latency"] for s in valid_group
                     if not s["extra"].get("cache_candidate") and s["latency"] > 0]
            repeat_p50 = _pct(repeated, 0.50)
            fresh_p50 = _pct(fresh, 0.50)
            cache_ratio = repeat_p50 / fresh_p50 if repeat_p50 and fresh_p50 else None
            cache_signal = "疑似有缓存" if cache_ratio is not None and cache_ratio <= 0.50 \
                else "无明显缓存证据"
            generator_saturated = sum(1 for s in group if s["extra"].get("generator_saturated"))
            sent = [s for s in group if not s["extra"].get("generator_saturated")]
            success = sum(1 for s in sent if s["ok"]) / len(sent) if sent else 0.0
            error_counts = {
                "429": sum(1 for s in sent if s["reason"] == probes.LIMIT),
                "timeout": sum(1 for s in sent if s["reason"] == probes.TIMEOUT),
                "5xx": sum(1 for s in sent if s["reason"] == probes.UPSTREAM),
                "fake_200": sum(1 for s in sent if s["extra"].get("embedded_error")),
            }
            rounds.append({
                "concurrency": level,
                "requests": len(group),
                "sent_requests": len(sent),
                "success_rate": round(success, 4),
                "throughput": round(sum(1 for s in sent if s["ok"]) / elapsed, 3) if elapsed else 0.0,
                "p50_latency": _pct(lat, 0.50),
                "p95_latency": _pct(lat, 0.95),
                "p99_latency": _pct(lat, 0.99),
                "p50_ttft": _pct(first, 0.50),
                "p95_ttft": _pct(first, 0.95),
                "p99_ttft": _pct(first, 0.99),
                "tokens_per_second": round(speed, 2),
                "speed_decline": round(max(decline, 0.0), 4),
                "repeat_p50": repeat_p50,
                "fresh_p50": fresh_p50,
                "cache_latency_ratio": round(cache_ratio, 4)
                if cache_ratio is not None else None,
                # 只凭延迟差不能证明命中缓存，因此最高只标“疑似”。
                "cache_signal": cache_signal,
                "generator_saturated": generator_saturated,
                "errors": error_counts,
                "stable": success >= 0.99 and error_counts["429"] == 0
                          and generator_saturated == 0 and decline <= 0.55,
            })
        knee_index = None
        for index, row in enumerate(rounds):
            if index == 0:
                if not row["stable"]:
                    knee_index = -1
                continue
            previous = rounds[index - 1]
            plateau = row["throughput"] <= previous["throughput"] * 1.05
            tail_rise = row["p99_latency"] > previous["p99_latency"] * 1.20
            if row["errors"]["429"] or (plateau and tail_rise) or row["generator_saturated"]:
                knee_index = index - 1
                row["stable"] = False
                break
        stable_rounds = [row for row in rounds if row["stable"]]
        safe = rounds[knee_index]["concurrency"] if knee_index is not None and knee_index >= 0 \
            else stable_rounds[-1]["concurrency"] if stable_rounds else 0
        generator_limited = any(row["generator_saturated"] for row in rounds)
        limited = any(row["errors"]["429"] or row["errors"]["fake_200"] for row in rounds)
        metrics["load"] = {
            "rounds": rounds,
            "safe_concurrency": safe,
            "stopped_early": bool(rounds and not rounds[-1]["stable"]),
            "knee_index": knee_index,
            "capacity_verdict": "压测机自身在飞上限" if generator_limited else
                                "上游限流" if limited else
                                "发现上游饱和拐点" if knee_index is not None else "扫描范围内未见饱和",
            "cache_signal": "疑似有缓存" if any(
                row["cache_signal"] == "疑似有缓存" for row in rounds
            ) else "无明显缓存证据",
        }
    return metrics


def build(
    task: dict[str, Any], pack: dict[str, Any], steps: list[dict[str, Any]],
    snapshot: dict[str, Any], baseline: dict[str, Any] | None = None,
    benchmark: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装完整报告。三种视图共用一份数据，只是呈现深度不同。"""
    metrics = compute_metrics(steps, snapshot.get("price_in"), snapshot.get("price_out"))
    selected_specialties = list(snapshot.get("specialty_profiles") or [])
    recommended_specialties = list(snapshot.get("recommended_specialty_profiles") or [])
    if selected_specialties or recommended_specialties:
        metrics["specialties"] = specialty.aggregate(
            steps, selected_specialties, recommended_specialties)
    # 步骤 1 硬门槛：必过项 + 超时率。放在这里算是因为要用到 pack["required"]。
    method_gate = bool(pack.get("method_ids"))
    metrics["gates"] = scoring.gate_results(
        steps, metrics, pack.get("required"), method_gate=method_gate)
    conclusion = advice.evaluate(task["kind"], pack, steps, metrics, baseline, benchmark)

    items = []
    for step in steps:
        grade = step["extra"].get("grade") or {}
        graded = grade.get("status") in {"passed", "failed", "partial"}
        passed = grade.get("status") == "passed" if graded else step["ok"]
        failure_codes = list(grade.get("failure_codes") or [])
        items.append({
            "step": step["step"], "ok": passed,
            "reason": "判题未通过" if graded and not passed else step["reason"],
            "detail": "、".join(failure_codes) if failure_codes else step["detail"],
            "latency": step["latency"], "grade": grade or None,
            "hard": bool(step["extra"].get("hard")),
        })

    evidence = [{
        "step": s["step"], "ok": s["ok"],
        "model": snapshot.get("model", ""),
        "protocol": snapshot.get("protocol", ""),
        "actual_model": s["extra"].get("actual_model", ""),
        "latency": s["latency"], "first_token": s["first_token"],
        "usage": s["usage"],
        "reply_summary": s["extra"].get("reply", ""),
        "attempts": int(s["extra"].get("attempts") or 1),
        "retry_reasons": list(s["extra"].get("retry_reasons") or []),
        "effective_parameters": dict(s["extra"].get("effective_parameters") or {}),
        "reason": s["reason"], "detail": s["detail"],
        "completion_status": s["extra"].get("completion_status"),
        "attribution": s["extra"].get("attribution"),
        "eligibility": s["extra"].get("eligibility"),
        "grade": s["extra"].get("grade"),
        "item_id": s["extra"].get("item"),
        "item_version": s["extra"].get("item_version"),
        "template_id": s["extra"].get("template_id"),
        "template_version": s["extra"].get("template_version"),
        "seed": s["extra"].get("item_seed"),
        "variant": s["extra"].get("item_variant"),
        "comparison_key": s["extra"].get("comparison_key"),
        "completion_policy": s["extra"].get("completion_policy"),
    } for s in steps]

    return {
        "task_id": task["id"],
        "kind": task["kind"],
        "pack": {"name": pack["name"], "version": pack["version"]},
        "target": {
            "name": snapshot.get("name", ""),
            "base_url": snapshot.get("base_url", ""),
            "model": snapshot.get("model", ""),
            "protocol": snapshot.get("protocol", ""),
            "group": snapshot.get("group_name", ""),
            "env": snapshot.get("env", ""),
            "key_masked": snapshot.get("key_masked", ""),
        },
        "conclusion": conclusion,
        "metrics": metrics,
        "items": items,
        "evidence": evidence,
        "benchmark": {
            "id": benchmark.get("id"), "name": benchmark.get("name"),
            "pack_version": benchmark.get("pack_version"),
            "dims": benchmark.get("dims") or {},
            "overall": benchmark.get("overall"),
            "tolerance": benchmark.get("tolerance"),
        } if benchmark else None,
        "created_at": task.get("created_at"),
        "finished_at": task.get("finished_at"),
    }


def summary_line(rep: dict[str, Any]) -> str:
    """一句话结论，任务列表与通知里用。"""
    m = rep["metrics"]
    p95 = (m.get("fixed_speed") or {}).get("p95_latency", m["p95_latency"])
    return (f"{rep['conclusion']['verdict']}｜成功率 {m['pass_rate']:.0%}"
            f"｜固定速度题 P95 {p95}s｜¥{m['cost']:.4f}")
