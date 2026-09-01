"""Aggregate eligible deterministic grades into capability slices."""
from typing import Any

from . import hardbank, itembank, test_catalog
from .config import TIMEOUT_RATE_LIMIT


def aggregate_item_trials(
    steps: list[dict[str, Any]], domains: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve equivalent variants at the evidence layer using an explicit majority."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for step in steps:
        extra = step["extra"]
        item_id = str(extra.get("item") or "")
        domain = str(extra.get("score_domain") or "")
        if not item_id or (domains is not None and domain not in domains):
            continue
        grouped.setdefault(item_id, []).append(step)
    output: list[dict[str, Any]] = []
    for item_id, trials in grouped.items():
        eligible = [trial for trial in trials
                    if (trial["extra"].get("eligibility") or {}).get("ability")
                    == "eligible"
                    or trial["extra"].get("score_domain") == "risk"
                    and (trial["extra"].get("eligibility") or {}).get("risk")
                    == "eligible"]
        valid = [trial for trial in eligible
                 if (trial["extra"].get("grade") or {}).get("status")
                 in {"passed", "failed", "partial"}]
        votes = [1 if (trial["extra"]["grade"]["status"] == "passed") else 0
                 for trial in valid]
        if not votes:
            status, score = "not_graded", None
        elif len(votes) == 1:
            status = "passed" if votes[0] else "failed"
            score = float(valid[0]["extra"]["grade"].get("score") or 0.0)
        elif sum(votes) > len(votes) / 2:
            status, score = "passed", 1.0
        elif sum(votes) < len(votes) / 2:
            status, score = "failed", 0.0
        else:
            status, score = "not_graded", None
        representative = trials[0]["extra"]
        output.append({
            "item": item_id, "domain": representative.get("score_domain"),
            "dim": representative.get("dim"), "level": representative.get("level"),
            "weight": float(representative.get("item_weight") or 1.0),
            "status": status, "score": score, "trials": len(trials),
            "valid_trials": len(valid), "votes": votes,
            "variants": [int(trial["extra"].get("variant") or 1) for trial in trials],
        })
    return sorted(output, key=lambda value: value["item"])


def dimension_scores(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """从执行步骤里汇总各维度得分。"""
    buckets: dict[str, list[tuple[float, float]]] = {}
    ungraded: dict[str, int] = {}
    attempted_weight: dict[str, float] = {}
    items: dict[str, float] = {}
    levels: dict[str, dict[str, Any]] = {}

    for aggregate in aggregate_item_trials(steps, {"ability"}):
        dim = aggregate.get("dim")
        if not dim:
            continue
        item_weight = float(aggregate.get("weight") or 1.0)
        attempted_weight[dim] = attempted_weight.get(dim, 0.0) + item_weight
        if aggregate["status"] not in {"passed", "failed", "partial"}:
            ungraded[dim] = ungraded.get(dim, 0) + 1
            continue
        score = float(aggregate.get("score") or 0.0)
        buckets.setdefault(dim, []).append((score, item_weight))
        item_id = str(aggregate["item"])
        items[item_id] = round(score, 4)
        lv = str(aggregate.get("level", ""))
        if lv:
            bucket = levels.setdefault(lv, {"sum": 0.0, "weight": 0.0})
            bucket["sum"] += score * item_weight
            bucket["weight"] += item_weight

    dims: dict[str, Any] = {}
    for dim, scores in buckets.items():
        earned = sum(score * weight for score, weight in scores)
        graded_weight = sum(weight for _score, weight in scores)
        expected_weight = attempted_weight.get(dim, 0.0)
        dims[dim] = {
            "score": round(earned / graded_weight, 4) if graded_weight else None,
            "graded": len(scores),
            "ungraded": ungraded.get(dim, 0),
            "coverage": round(graded_weight / expected_weight, 4)
            if expected_weight else 1.0,
            "weight": itembank.DIMENSIONS.get(dim, {}).get("weight", 0.0),
        }
    # 整个维度全都没判上分，也要留个位，报告里能看出是"没测到"而不是"0 分"
    for dim, n in ungraded.items():
        if dim not in dims:
            dims[dim] = {
                "score": None, "graded": 0, "ungraded": n,
                "coverage": 0.0,
                "weight": itembank.DIMENSIONS.get(dim, {}).get("weight", 0.0),
            }
    for dim in itembank.ABILITY_DIM_ORDER:
        dims.setdefault(dim, {
            "score": None, "graded": 0, "ungraded": 0,
            "coverage": 0.0, "weight": itembank.WEIGHTS[dim],
        })
    dims = {dim: dims[dim] for dim in itembank.ABILITY_DIM_ORDER}
    by_level = {k: round(v["sum"] / v["weight"], 4)
                for k, v in levels.items() if v["weight"]}

    # 内容题截断时记为未测到（probe 里 graded=False），避免把 thinking
    # 烧穿预算、正文为空误写成能力 0 分，并在报告中保留题号供复核。
    trunc = [s for s in steps if s["extra"].get("score_domain") == "ability"
             and s["extra"].get("truncated")]
    trunc_unscored = [s for s in trunc
                      if (s["extra"].get("grade") or {}).get("status") == "not_graded"]

    return {
        "dims": dims, "items": items, "by_level": by_level,
        "truncated": len(trunc),
        "truncated_unscored": len(trunc_unscored),
        "truncated_items": [str(s["extra"].get("item")) for s in trunc_unscored],
    }


def overall(dims: dict[str, Any]) -> float | None:
    """按权重算总分。只在有分的维度间归一化，避免缺维度时总分虚低。"""
    num = 0.0
    den = 0.0
    for cfg in dims.values():
        if cfg["score"] is None:
            continue
        num += cfg["score"] * cfg["weight"]
        den += cfg["weight"]
    return round(num / den, 4) if den else None


def gate_results(
    steps: list[dict[str, Any]], metrics: dict[str, Any],
    required: list[str] | None = None,
    method_gate: bool = False,
) -> dict[str, Any]:
    """步骤 1 硬门槛汇总。任一未过则 passed=False，分组推荐会被拦住。

    门槛直接从真实硬性条件算（必过项 + 超时率），不依赖"门槛题"。
    以前是靠一道长上下文埋针题撑起这个字段的，那道题删掉后如果还按题算，
    passed 会永远为 True，鉴权失败和超时超标就拦不住了。
    """
    items: list[dict[str, Any]] = []
    by_step = {s["step"]: s for s in steps}

    for name in (required or []):
        s = by_step.get(name)
        if s is None:
            continue
        grade = s["extra"].get("grade") or {}
        passed = bool(s["ok"]) and (
            not grade or grade.get("status") == "passed")
        items.append({
            "id": f"required.{name}", "name": name,
            "passed": passed,
            "detail": s["detail"] if not passed else "通过",
            "reason": s["reason"],
        })

    if method_gate:
        admission_methods = set(test_catalog.admission_methods())
        selected = [s for s in steps
                    if not s["extra"].get("analysis")
                    and s["extra"].get("method_id") in admission_methods]
        if selected:
            ok_count = sum(1 for s in selected if s["ok"])
            total = len(selected)
            rate = ok_count / total
            severity = "recommend" if rate >= 0.8 else "observe" if rate >= 0.6 else "reject"
            items.append({
                "id": "method.selection", "name": "准入方法通过率",
                "passed": rate >= 0.8,
                "detail": f"{ok_count}/{total}（{rate:.0%}）",
                "reason": "" if rate >= 0.6 else "准入方法通过率过低",
                "severity": severity,
            })
            if rate < 0.6:
                items[-1]["passed"] = False
                items[-1]["detail"] = f"{ok_count}/{total}（{rate:.0%}）低于 60%"
            elif 0.6 <= rate < 0.8:
                items[-1]["detail"] = f"{ok_count}/{total}（{rate:.0%}）在观察区间"

    rate = float(metrics.get("timeout_rate", 0.0))
    ok = rate <= TIMEOUT_RATE_LIMIT
    items.append({
        "id": "rate.timeout", "name": "超时率",
        "passed": ok,
        "detail": f"{rate:.1%}（{metrics.get('timeout_count', 0)}/"
                  f"{metrics.get('request_total', 0)} 次请求超时，"
                  f"门槛 {TIMEOUT_RATE_LIMIT:.0%}）",
        "reason": "" if ok else "超时",
    })

    return {
        "items": items,
        "passed": all(g["passed"] for g in items),
        "failed": [g["name"] for g in items if not g["passed"]],
    }


def trust_check(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """判断这一轮的分数还能不能用。

    两种情况会让整轮评分失去意义，必须拦在分组推荐之前：
      1. 上游把自己的 system prompt 泄漏进正文（我们只发 user 消息，不该出现）
      2. 不同题目拿到几乎一样的回答，说明上游根本没在回答我们的题
    分数低是能力问题，可以照常判档；这两种是**测量本身无效**，性质完全不同。
    """
    graded = [s for s in steps
              if (s["extra"].get("grade") or {}).get("status")
              in {"passed", "failed", "partial"}
              or s["extra"].get("graded")]
    injected = [(s["extra"].get("item"), s["extra"]["injected"])
                for s in graded if s["extra"].get("injected")]

    # 重复检测只在核心能力题里算门槛，硬题不参与。
    # 硬题会把 graded 从 14 道抬到 36 道，dup_min 跟着从 7 涨到 18，
    # 等于因为「加了题」反而更难发现上游答非所问 —— 那是把检测能力做没了。
    # 注入检测仍然覆盖硬题（上面那行用的是全部 graded），多几十个样本只会更灵敏。
    dup_pool = [s for s in graded if not s["extra"].get("hard")
                and s["extra"].get("score_domain") != "risk"]

    # 同一份回答出现在多道不同的题上。
    # 门槛定在 3 道以上：2 道撞车在正常情况下也可能出现（比如模型连着拒答两题），
    # 拿它去否定整轮评分会误伤。3 道以上才说明上游根本没在按题作答。
    seen: dict[str, list[str]] = {}
    for s in dup_pool:
        reply = (s["extra"].get("reply") or "").strip()
        if len(reply) < 20:      # 太短的回答（如"北京"）本来就容易重复，不算异常
            continue
        seen.setdefault(reply[:80], []).append(str(s["extra"].get("item")))
    dup_min = max(3, (len(dup_pool) + 1) // 2) if dup_pool else 3
    dupes = {k: v for k, v in seen.items() if len(v) >= dup_min}

    reasons: list[str] = []
    if injected:
        items = "、".join(i for i, _ in injected[:3])
        reasons.append(
            f"{len(injected)} 道题的回答里混进了上游注入的内容"
            f"（命中特征「{injected[0][1]}」，例如 {items}）")
    if dupes:
        worst = max(dupes.values(), key=len)
        reasons.append(
            f"{len(worst)} 道不同的题拿到了几乎相同的回答（{'、'.join(worst[:3])}），"
            f"上游可能没有真正处理请求")

    return {
        "ok": not reasons,
        "reasons": reasons,
        "injected_items": [i for i, _ in injected],
        "duplicate_groups": [v for v in dupes.values()],
    }


def vector(dims: dict[str, Any]) -> list[float | None]:
    """按固定顺序生成四维能力标杆向量。"""
    return [dims.get(d, {}).get("score") for d in itembank.DIM_ORDER]


def hard_scores(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """硬题汇总：总正确率 + 按题库 + 按学科分组。没跑硬题时返回 None。

    **独立于四维能力总分**，不参与 overall / vector，也不进 itembank.WEIGHTS。
    所以加硬题不会让已有的四维标杆失效 —— 这是第二把尺子，不是把原来的尺子改了。

    判分失败的题（超时、5xx）记进 ungraded，不算 0 分。硬题超时比核心题常见得多
    （输出长），把它算成答错会系统性地低估慢渠道的能力。
    """
    hard = [s for s in steps if s["extra"].get("hard")]
    if not hard:
        return None

    graded = [s for s in hard if s["extra"].get("graded")]
    correct = [s for s in graded if float(s["extra"].get("scored", 0.0)) >= 0.999]

    banks: dict[str, dict[str, Any]] = {}
    groups: dict[str, dict[str, Any]] = {}
    for s in hard:
        e = s["extra"]
        for key, bucket in (("bank", banks), ("group", groups)):
            name = str(e.get(key) or "")
            if not name:
                continue
            b = bucket.setdefault(name, {"correct": 0, "graded": 0, "ungraded": 0})
            if not e.get("graded"):
                b["ungraded"] += 1
                continue
            b["graded"] += 1
            if float(e.get("scored", 0.0)) >= 0.999:
                b["correct"] += 1

    for bucket in (banks, groups):
        for b in bucket.values():
            b["rate"] = round(b["correct"] / b["graded"], 4) if b["graded"] else None

    for name, b in banks.items():
        b["name"] = hardbank.bank_name(name)

    # 截断统计。分两种，性质完全不同：
    #   truncated_unscored —— 撞上 token 上限且抽不到答案，这道题没测到
    #   truncated_scored   —— 撞上上限但答案已经写完了，照常判分，只是留个记录
    # 前者多说明预算不适配这个模型（思考 token 算在预算里），分数要打问号。
    trunc_unscored = [s for s in hard
                      if s["extra"].get("truncated") and not s["extra"].get("graded")]
    trunc_scored = [s for s in hard
                    if s["extra"].get("truncated") and s["extra"].get("graded")]
    think = [int(s["extra"].get("reasoning_tokens") or 0) for s in hard]
    think_max = max(think) if think else 0

    return {
        "hard_version": hardbank.HARD_VERSION,
        "total": len(hard),
        "graded": len(graded),
        "ungraded": len(hard) - len(graded),
        "correct": len(correct),
        "rate": round(len(correct) / len(graded), 4) if graded else None,
        "truncated_unscored": len(trunc_unscored),
        "truncated_scored": len(trunc_scored),
        "reasoning_tokens_max": think_max,
        "banks": banks,
        "groups": groups,
        # 逐题明细：留证据，报告技术明细里逐条列出来
        "items": {
            str(s["extra"].get("item")): {
                "bank": s["extra"].get("bank", ""),
                "group": s["extra"].get("group", ""),
                "graded": bool(s["extra"].get("graded")),
                "correct": bool(float(s["extra"].get("scored", 0.0)) >= 0.999),
                "answer": s["extra"].get("answer", ""),
                "expected": s["extra"].get("expected", ""),
                "reason": s["reason"],
                "truncated": bool(s["extra"].get("truncated")),
                "reasoning_tokens": int(s["extra"].get("reasoning_tokens") or 0),
                "budget": int(s["extra"].get("budget") or 0),
            } for s in hard
        },
    }
