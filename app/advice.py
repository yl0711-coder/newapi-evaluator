"""建议引擎：可解释规则优先。每条结论都能指回具体证据，不做黑盒判断。"""
from typing import Any

from . import itembank, probes
from .config import TIMEOUT_RATE_LIMIT  # noqa: F401  (main.py 从这里读判定口径)

# 五档业务结论
RECOMMEND = ("recommend", "推荐准入")
OBSERVE = ("observe", "建议观察")
DOWNGRADE = ("downgrade", "建议降级")
REJECT = ("reject", "暂不准入")
MANUAL = ("manual", "需要人工复核")

_SEVERITY = {"recommend": 0, "observe": 1, "manual": 2, "downgrade": 3, "reject": 4}


def _worse(a: tuple[str, str], b: tuple[str, str]) -> tuple[str, str]:
    return a if _SEVERITY[a[0]] >= _SEVERITY[b[0]] else b


def evaluate(
    kind: str, pack: dict[str, Any], steps: list[dict[str, Any]],
    metrics: dict[str, Any], baseline: dict[str, Any] | None = None,
    benchmark: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回 {code, verdict, reasons, actions}。reasons 是给人看的判断依据。"""
    verdict = RECOMMEND
    reasons: list[str] = []
    actions: list[str] = []
    # 用 gate 那份（不含硬题）。硬题失败不能经由失败分布绕过隔离去改结论 ——
    # 报告刚把硬题排除在成功率与超时门槛之外，这里再让它触发「建议降级」就自相矛盾了。
    # 硬题自己的失败在 hard 块里逐项可查，不会被藏起来。
    counts: dict[str, int] = metrics.get("reason_counts_gate", {})
    by_step = {s["step"]: s for s in steps}

    # --- 硬性失败：必过项 ---
    for need in pack.get("required", []):
        step = by_step.get(need)
        if step and not step["ok"]:
            verdict = _worse(verdict, REJECT)
            reasons.append(f"必过项「{need}」失败：{step['detail']}")

    # --- 鉴权 ---
    if counts.get(probes.AUTH):
        verdict = _worse(verdict, REJECT)
        reasons.append(f"出现 {counts[probes.AUTH]} 次鉴权失败")
        actions.append("检查 API Key 是否正确、是否过期、是否有该模型的权限")

    # --- 配置 / 模型不存在 ---
    if counts.get(probes.CFG):
        verdict = _worse(verdict, REJECT)
        reasons.append(f"出现 {counts[probes.CFG]} 次配置类失败（地址或模型名）")
        actions.append("核对上游地址与模型名，确认模型已在该渠道开通")

    # --- 断流 ---
    if metrics.get("stream_break_rate", 0) > 0:
        verdict = _worse(verdict, DOWNGRADE)
        reasons.append("存在断流，流式输出不稳定")
        actions.append("检查上游 SSE 实现与中间层超时配置，必要时联系上游")

    # --- 限流 / 超时 ---
    if counts.get(probes.LIMIT):
        verdict = _worse(verdict, OBSERVE)
        reasons.append(f"出现 {counts[probes.LIMIT]} 次限流")
        actions.append("降低并发或申请提高上游速率限制")
    if counts.get(probes.TIMEOUT):
        verdict = _worse(verdict, DOWNGRADE)
        reasons.append(f"出现 {counts[probes.TIMEOUT]} 次超时")
        actions.append("确认上游链路与网络稳定性，超时集中出现时先降级观察")

    # --- 上游 5xx ---
    if counts.get(probes.UPSTREAM):
        verdict = _worse(verdict, DOWNGRADE)
        reasons.append(f"上游返回 {counts[probes.UPSTREAM]} 次 5xx 错误")
        actions.append("联系上游确认服务状态，暂时不要放量")

    # --- 协议兼容 ---
    if counts.get(probes.PROTO):
        verdict = _worse(verdict, OBSERVE)
        reasons.append(f"{counts[probes.PROTO]} 次协议不兼容或响应格式异常")
        actions.append("核对协议类型（openai / anthropic）与上游实际实现")

    return _finish(kind, verdict, reasons, actions, steps, metrics, baseline,
                   by_step, benchmark)


def _finish(
    kind: str, verdict: tuple[str, str], reasons: list[str], actions: list[str],
    steps: list[dict[str, Any]], metrics: dict[str, Any],
    baseline: dict[str, Any] | None, by_step: dict[str, dict[str, Any]],
    benchmark: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """成功率、防作假、延迟、计费字段、基线对比。"""
    pass_rate = metrics.get("pass_rate", 0.0)

    # --- 整体成功率 ---
    # 分母为 0 时不判。metrics["total"] 只统计可用性步骤，答错与截断都被排除在外，
    # 所以有可能一个都不剩（例如只跑了计分题、且全部撞上 token 上限）。
    # 那时 pass_rate 会是默认的 0.0 —— 拿它当「0% 成功率」会判成暂不准入，
    # 而真实情况是「这一轮没有可用性数据」，两件事不能混。
    if metrics.get("total", 0) <= 0:
        reasons.append("本轮没有可用性类测试项，成功率不参与判定")
    elif pass_rate < 0.5:
        verdict = _worse(verdict, REJECT)
        reasons.append(f"成功率仅 {pass_rate:.0%}，基础可用性不达标")
    elif pass_rate < 0.8:
        verdict = _worse(verdict, OBSERVE)
        reasons.append(f"成功率 {pass_rate:.0%}，存在偶发失败")

    # --- 防作假：静默兜底、模型名不一致 ---
    if any(s["extra"].get("silent_fallback") for s in steps):
        verdict = _worse(verdict, REJECT)
        reasons.append("请求不存在的模型仍返回成功，上游存在静默兜底（疑似作假）")
        actions.append("要求上游说明路由策略，确认没有把请求转到其它模型")
    mismatch = [s for s in steps if s["extra"].get("model_mismatch")]
    if mismatch:
        verdict = _worse(verdict, MANUAL)
        actual = mismatch[0]["extra"].get("actual_model", "?")
        reasons.append(f"上游返回的模型名是 {actual}，与申报不一致（疑似换模型）")
        actions.append("人工核对上游是否用其它模型代答，必要时暂停该渠道")

    # --- 计费字段完整性 ---
    chats = [s for s in steps if s["usage"].get("prompt", 0) or s["usage"].get("completion", 0)
             or "usage_complete" in s["extra"]]
    if chats and any(s["extra"].get("usage_complete") is False for s in chats):
        verdict = _worse(verdict, OBSERVE)
        reasons.append("部分响应缺少 usage 字段，成本核算不完整")
        actions.append("要求上游补齐 usage，否则按估算计费会有偏差")

    # --- 方法级准入质量（准入任务）：60-80% 视为观察，低于 60% 直接否决 ---
    gates = metrics.get("gates", {}).get("items") if isinstance(metrics.get("gates"), dict) else []
    method_gate = next((g for g in gates if isinstance(g, dict) and g.get("id") == "method.selection"), None)
    if method_gate:
        severity = method_gate.get("severity", "observe" if not method_gate.get("passed") else "recommend")
        if not method_gate.get("passed") and severity == "reject":
            verdict = _worse(verdict, REJECT)
            reasons.append(f"准入方法通过率 {method_gate.get('detail', '')}（低于 60%）")
            actions.append("降低问题范围后重跑，或核对目标方法口径是否均为通道能力问题")
        elif severity == "observe":
            verdict = _worse(verdict, OBSERVE)
            reasons.append(f"准入方法通过率 {method_gate.get('detail', '')}，处于 60%~80% 观察区间")

    admission = metrics.get("admission") or {}
    if kind == "admission" and admission:
        status = admission.get("status")
        if status == "failed":
            verdict = _worse(verdict, REJECT)
            if not admission.get("intelligence_passed"):
                reasons.append(
                    f"能力题通过 {admission.get('passed_items', 0)}/"
                    f"{admission.get('expected_items', 6)}，未达到智力门槛")
            if not admission.get("stability_passed"):
                reasons.append(
                    f"全部流式题稳定完成率 {admission.get('stability_rate', 0):.0%}")
        elif status == "insufficient_evidence":
            verdict = _worse(verdict, MANUAL)
            reasons.append(
                f"能力题形成 {admission.get('valid_items', 0)}/"
                f"{admission.get('expected_items', 6)} 项结果，固定速度题形成 "
                f"{admission.get('speed_samples', 0)}/"
                f"{admission.get('expected_speed_samples', 5)} 个可比样本")
            actions.append("补齐缺失请求后重新运行同一固定测试")
        else:
            reasons.append(
                f"智力门槛通过（{admission.get('passed_items', 0)}/"
                f"{admission.get('expected_items', 6)}），"
                f"全部流式题稳定完成率 {admission.get('stability_rate', 0):.0%}")

    # --- 延迟 ---
    p95 = metrics.get("p95_latency", 0.0)
    first = metrics.get("avg_first_token", 0.0)
    if kind != "admission" and p95 > 20:
        verdict = _worse(verdict, DOWNGRADE)
        reasons.append(f"P95 延迟 {p95:.1f}s，明显偏慢")
        actions.append("与上游确认链路，或换用更快的渠道")
    elif kind != "admission" and p95 > 10:
        verdict = _worse(verdict, OBSERVE)
        reasons.append(f"P95 延迟 {p95:.1f}s，偏高但可用")
    if kind != "admission" and first > 8:
        verdict = _worse(verdict, OBSERVE)
        reasons.append(f"首 token 平均 {first:.1f}s，交互体感偏慢")

    load = metrics.get("load")
    if kind == "load" and load:
        safe = int(load.get("safe_concurrency") or 0)
        rounds = load.get("rounds") or []
        if safe:
            reasons.append(f"最大稳定并发为 {safe}")
            actions.append(f"初始路由并发上限建议设为 {safe}，放量后继续观察")
        else:
            verdict = _worse(verdict, DOWNGRADE)
            reasons.append("本次没有找到满足成功率与速度衰减要求的稳定并发档")
            actions.append("保持低权重，降低起始并发后重新测试")
        if rounds and not rounds[-1].get("stable"):
            verdict = _worse(verdict, OBSERVE)
            reasons.append(
                f"并发 {rounds[-1]['concurrency']} 触发停止条件："
                f"成功率 {rounds[-1]['success_rate']:.0%}，"
                f"生成速度下降 {rounds[-1]['speed_decline']:.0%}")

    agent = metrics.get("agent_stability")
    if kind == "agent_stability" and agent:
        if agent.get("graded", 0) < agent.get("expected", 0):
            verdict = _worse(verdict, MANUAL)
            reasons.append("Agent 稳定性包存在未形成判分的请求")
        elif agent.get("pass_rate") is not None and agent["pass_rate"] < 1.0:
            verdict = _worse(verdict, OBSERVE)
            reasons.append(f"Agent 固定任务通过率 {agent['pass_rate']:.0%}")
        else:
            reasons.append("Agent 结构化、工具和工作流固定负载均完成")

    development = metrics.get("development_speed")
    if kind == "development_speed" and development:
        if development.get("graded", 0) < development.get("expected", 0):
            verdict = _worse(verdict, MANUAL)
            reasons.append("开发速度包存在未完成的固定负载，不混入速度分布")
        elif development.get("pass_rate") is not None \
                and development["pass_rate"] < 1.0:
            verdict = _worse(verdict, MANUAL)
            reasons.append("部分开发固定负载未完整达成，其耗时不进入可比速度分布")
        else:
            reasons.append("已按短、中、长固定负载分开形成速度证据")

    long_context = metrics.get("long_context")
    if kind == "long_context" and long_context:
        if long_context.get("reliable_context_tier") != "8k":
            verdict = _worse(verdict, OBSERVE)
            reasons.append("8K 档位未形成全部可靠召回证据")
        if long_context.get("cache_status") == "unverified":
            actions.append("上游未报告可验证的缓存 usage 字段，缓存能力保持“尚未验证”")

    verdict = _gate_rules(verdict, reasons, actions, metrics)

    # 本轮评分不可信时，跳过所有基于分数的判定。
    # 分数不代表模型能力，拿它去判"某维度不可用"会把上游的转发问题算到模型头上。
    trust = metrics.get("trust")
    if kind != "admission" and not (trust and not trust.get("ok")):
        verdict = _baseline_rules(kind, verdict, reasons, actions, metrics, baseline)
        verdict = _capability_rules(verdict, reasons, actions, metrics, benchmark)

    if verdict[0] == "recommend" and not reasons:
        reasons.append("全部测试项通过，延迟与计费字段正常")
    if not actions:
        actions.append("暂无需要处理的问题，可按正常节奏使用并保持巡检")

    if kind == "load":
        label = {
            "recommend": "压力测试通过",
            "observe": "建议限制并发",
            "manual": "压力结果待复核",
            "downgrade": "压力测试不稳定",
            "reject": "压力测试不通过",
        }[verdict[0]]
        verdict = (verdict[0], label)

    return {
        "code": verdict[0], "verdict": verdict[1],
        "reasons": reasons, "actions": _dedup(actions),
    }


def _baseline_rules(
    kind: str, verdict: tuple[str, str], reasons: list[str], actions: list[str],
    metrics: dict[str, Any], baseline: dict[str, Any] | None,
) -> tuple[str, str]:
    """纵向降智判定：本次四维能力分与该渠道自己的历史基线对比。

    与横向标杆对比（_capability_rules）是两件事：
      纵向 = 跟自己的过去比，看有没有变差；
      横向 = 跟别的模型比，看该定哪一档倍率。
    """
    cap = metrics.get("capability")
    if not cap or cap.get("overall") is None:
        return verdict
    rate = cap["overall"]

    if not baseline or baseline.get("overall") is None:
        reasons.append(f"综合得分 {rate:.0%}，暂无历史基线可比（本次结果将作为基线）")
        return verdict

    # 题库换了就不能比分，说清楚而不是给个错答案
    if baseline.get("pack_version") and \
            baseline["pack_version"] != cap.get("pack_version"):
        verdict = _worse(verdict, MANUAL)
        reasons.append(
            f"历史基线用的题库是 {baseline['pack_version']}，"
            f"本次是 {cap.get('pack_version')}，版本不同不能比分")
        actions.append("用当前题库重跑一次接入检测，重建纵向基线")
        return verdict

    base_rate = baseline["overall"]
    drop = base_rate - rate

    # 先点出掉得最狠的那个维度，比只报总分更有指向性
    worst = ""
    base_dims = baseline.get("dims") or {}
    diffs = []
    for dim, v in cap["dims"].items():
        want = base_dims.get(dim)
        if v["score"] is None or want is None:
            continue
        diffs.append((want - v["score"], dim, v["score"], want))
    if diffs:
        diffs.sort(reverse=True)
        d, dim, got, want = diffs[0]
        if d >= 0.15:
            worst = f"，其中{dim}掉得最多（{got:.0%} vs 基线 {want:.0%}）"

    if drop >= 0.30:
        verdict = _worse(verdict, DOWNGRADE)
        reasons.append(
            f"综合得分 {rate:.0%}，历史基线 {base_rate:.0%}，"
            f"下降 {drop:.0%}{worst}，判定为确认降智")
        actions.append("暂停放量并联系上游核实模型版本，必要时切换渠道")
    elif drop >= 0.15:
        verdict = _worse(verdict, OBSERVE)
        reasons.append(
            f"综合得分 {rate:.0%} 低于历史基线 {base_rate:.0%}"
            f"（下降 {drop:.0%}）{worst}，疑似波动，建议再复核一次")
        actions.append("隔日再跑一次降智复核，确认是波动还是持续下降")
    else:
        reasons.append(
            f"综合得分 {rate:.0%}，与历史基线 {base_rate:.0%} 基本一致，未见降智")
    return verdict


def _gate_rules(
    verdict: tuple[str, str], reasons: list[str], actions: list[str],
    metrics: dict[str, Any],
) -> tuple[str, str]:
    """步骤 1 的硬门槛：超时率。未过则终止分组推荐。

    这里只判超时率。必过项失败由上面的「必过项」循环负责报，
    metrics["gates"] 只是给报告和 placement 用的汇总，不在这里再报一遍 ——
    否则同一个失败会在依据里出现两三次。
    """
    rate = metrics.get("timeout_rate", 0.0)
    if rate > TIMEOUT_RATE_LIMIT:
        verdict = _worse(verdict, REJECT)
        reasons.append(
            f"超时率 {rate:.1%} 超过硬门槛 {TIMEOUT_RATE_LIMIT:.0%}"
            f"（{metrics.get('timeout_count', 0)}/{metrics.get('request_total', 0)} 次请求超时）")
        actions.append("先解决上游超时问题，稳定性不过关不进入分组推荐")

    # 测量本身无效：这一轮的分数不能用来判档
    trust = metrics.get("trust")
    if trust and not trust.get("ok"):
        verdict = _worse(verdict, MANUAL)
        for r in trust.get("reasons", []):
            reasons.append(f"本轮评分不可信：{r}")
        actions.append(
            "上游疑似在转发链路上注入了 system prompt 或改写了请求，"
            "本轮分数不能作为定档依据。先与上游确认转发是否透传，再重测")
    return verdict


def _capability_rules(
    verdict: tuple[str, str], reasons: list[str], actions: list[str],
    metrics: dict[str, Any], benchmark: dict[str, Any] | None,
) -> tuple[str, str]:
    """能力评测判定：逐维度与标杆对比，取最严的一档。

    标杆是相对尺子：标杆本身在某维度弱，那个维度就看不出问题。
    """
    cap = metrics.get("capability")
    if not cap or not cap.get("dims"):
        return verdict
    dims: dict[str, Any] = cap["dims"]
    got_overall = cap.get("overall")

    # 没判上分的维度先说清楚，别让人误读成 0 分
    blind = [d for d, v in dims.items() if v["score"] is None]
    if blind:
        verdict = _worse(verdict, MANUAL)
        reasons.append(f"维度「{'、'.join(blind)}」全部请求失败，这次没测到，不是 0 分")
        actions.append("先解决这些维度的请求失败，再重跑一次评测")

    weak_coverage = [
        (name, float(value.get("coverage") or 0.0))
        for name, value in dims.items()
        if name in itembank.ABILITY_DIM_ORDER
        and float(value.get("coverage") or 0.0) < 0.85
    ]
    if cap.get("coverage_enforced") and weak_coverage:
        verdict = _worse(verdict, MANUAL)
        detail = "、".join(f"{name} {coverage:.0%}" for name, coverage in weak_coverage)
        reasons.append(f"题目有效覆盖率不足（{detail}），不能用剩余简单题正常定档")
        actions.append("先解决空回答、协议或预算问题，确保每个能力维度至少完成 85% 的加权题目")

    # 截断：思考 token 烧穿了内容题的预算。
    #
    # 这件事必须拦住判档，不能只记一句。
    # 而**思考 token 算在 max_tokens 里** —— 推理模型光 thinking 就能烧穿，
    # 正文返回空的。那些题记成未测到，于是 overall 只由剩下的题算出来，
    # 拿这个数去和标杆比、去落位，比的是两个不同的题目子集，结论没有意义。
    trunc = int(cap.get("truncated_unscored") or 0)
    if trunc:
        verdict = _worse(verdict, MANUAL)
        items = "、".join(cap.get("truncated_items") or [])
        reasons.append(
            f"{trunc} 道内容能力题因撞上 token 上限没测到（{items}）—— "
            f"思考 token 也算在预算里，这个模型的思考量超出了内容题预算。"
            f"本次四维能力分只由剩下的题算出，不能用于判档")
        actions.append(
            "这是推理模型：内容能力题的预算是按简短作答定的，不适配。"
            f"改用硬题分横向对比，或调高题目预算后以新版本重跑（已有 {cap.get('pack_version')} "
            "标杆不能与新版本混比）")

    # 某维度得分接近 0，说明该能力基本不可用，不用等标杆也能判
    for dim, v in dims.items():
        if v["score"] is not None and v["score"] <= 0.05:
            verdict = _worse(verdict, DOWNGRADE)
            reasons.append(f"维度「{dim}」得分 {v['score']:.0%}，该能力基本不可用")
            actions.append(f"确认上游是否完整支持{dim}相关能力")

    if not benchmark:
        if got_overall is not None:
            reasons.append(
                f"综合得分 {got_overall:.0%}，尚未设定标杆，"
                f"本次结果可存为标杆供日后对比")
            actions.append("确认这条渠道当前状态可信后，把本次结果设为标杆")
        return verdict

    # 题库版本不一致，拒绝对比而不是给个错答案
    if benchmark.get("pack_version") != cap.get("pack_version"):
        verdict = _worse(verdict, MANUAL)
        reasons.append(
            f"标杆用的题库是 {benchmark.get('pack_version')}，"
            f"本次是 {cap.get('pack_version')}，版本不同不能直接比分")
        actions.append("用当前题库重新设一份标杆，再来对比")
        return verdict

    tol = float(benchmark.get("tolerance") or 0.10)
    base_dims: dict[str, Any] = benchmark.get("dims") or {}
    base_overall = benchmark.get("overall")
    name = benchmark.get("name") or "标杆"

    # 逐维度对比
    weak: list[str] = []
    for dim, v in dims.items():
        if v["score"] is None:
            continue
        want = base_dims.get(dim)
        if want is None:
            reasons.append(f"维度「{dim}」得分 {v['score']:.0%}，标杆里没有这一项，跳过对比")
            continue
        gap = want - v["score"]
        if gap <= tol:
            continue
        weak.append(f"{dim}（{v['score']:.0%} vs 标杆 {want:.0%}）")
        if gap > 0.25:
            verdict = _worse(verdict, DOWNGRADE)
        else:
            verdict = _worse(verdict, OBSERVE)

    if weak:
        reasons.append(f"低于{name}容差 {tol:.0%} 的维度：" + "；".join(weak))
        actions.append("按维度定位问题：先确认上游模型版本，再看是不是渠道转发实现不完整")

    # 综合分
    if got_overall is not None and base_overall is not None:
        gap = base_overall - got_overall
        if gap > tol:
            verdict = _worse(verdict, DOWNGRADE if gap > 0.2 else OBSERVE)
            reasons.append(
                f"综合得分 {got_overall:.0%}，{name} {base_overall:.0%}，"
                f"低 {gap:.0%}，判定为能力不达标")
        else:
            reasons.append(
                f"综合得分 {got_overall:.0%}，与{name} {base_overall:.0%} "
                f"相差在容差 {tol:.0%} 内，判定为能力达标")
    return verdict


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out
