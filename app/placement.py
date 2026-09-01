"""横向标杆对比与分组推荐（步骤 4、5）。

从模型当前所在倍率组开始，只比较同一模型的标杆；达到当前档后逐级向上挑战。
主判据是加权综合分与标杆的比值，标杆容差默认为 10%。单维度差异和余弦
相似度只做风险提示，不阻断综合能力达标的推荐。人工确认后才真正改分组。
"""
import math
import re
from typing import Any

from . import itembank

# 标杆未单独设容差时的默认达标比值。
QUALIFY_RATIO = 0.90
# 单维度低于这个比值时在理由里点出，不参与达标判定。
WEAK_DIM_RATIO = 0.85
# 严格对比时，四类能力都必须高于标杆的 93%。
STRICT_ABILITY_RATIO = 0.93


def model_group_model(name: str, multiplier: float) -> str:
    """取倍率组的模型部分，例如 Claude-Sonnet5-2x组 -> Claude-Sonnet5。"""
    value = (name or "").strip()
    rate = format(float(multiplier), "g")
    value = re.sub(rf"(?:\s*[-·]\s*)?{re.escape(rate)}\s*x\s*(?:分?组)?$", "", value,
                   flags=re.I).strip(" -·")
    return value


def model_group_key(name: str, multiplier: float) -> str:
    """取倍率组的规范模型键。"""
    return re.sub(r"[\s_·]+", "-", model_group_model(name, multiplier).lower())


def model_rate_group_name(model: str, multiplier: float) -> str:
    return f"{model.strip()}-{format(float(multiplier), 'g')}x组"


def cosine(a: list[float], b: list[float], weights: list[float]) -> float | None:
    """加权余弦相似度，仅用于显示能力结构是否相似。"""
    if not a or len(a) != len(b) or len(a) != len(weights):
        return None
    num = sum(w * x * y for w, x, y in zip(weights, a, b))
    da = math.sqrt(sum(w * x * x for w, x in zip(weights, a)))
    db = math.sqrt(sum(w * y * y for w, y in zip(weights, b)))
    if da <= 0 or db <= 0:
        return None
    return round(num / (da * db), 4)


def compare_capability_to_benchmark(
    cap: dict[str, Any], benchmark: dict[str, Any],
    strict: bool = False,
) -> dict[str, Any] | None:
    """把新模型的四维能力分与一条标杆对比。可走严格阈值版本。"""
    base_dims: dict[str, Any] = benchmark.get("dims") or {}
    if not base_dims:
        return None

    if benchmark.get("pack_version") != cap.get("pack_version"):
        return {
            "comparable": False,
            "skip_reason": (
                f"标杆题库为 {benchmark.get('pack_version')}，"
                f"当前为 {cap.get('pack_version')}，版本不同不可比"
            ),
        }

    dims: dict[str, Any] = cap["dims"]
    tolerance = float(benchmark.get("tolerance")
                      if benchmark.get("tolerance") is not None else 1 - QUALIFY_RATIO)
    per_dim: dict[str, Any] = {}
    weak_dims: list[str] = []
    missing_dims: list[str] = []
    mine: list[float] = []
    theirs: list[float] = []
    weights: list[float] = []

    for name in itembank.DIM_ORDER:
        got = dims.get(name, {}).get("score")
        want = base_dims.get(name)
        w = itembank.DIMENSIONS.get(name, {}).get("weight", 0.0)
        if got is None or want is None:
            per_dim[name] = {"score": got, "bench": want, "ratio": None}
            if strict:
                missing_dims.append(name)
            continue
        ratio = round(got / want, 4) if want > 0 else None
        per_dim[name] = {"score": got, "bench": want, "ratio": ratio, "gap": round(got - want, 4)}
        mine.append(got)
        theirs.append(want)
        weights.append(w)
        threshold = STRICT_ABILITY_RATIO if strict else WEAK_DIM_RATIO
        if ratio is not None and ratio <= threshold:
            weak_dims.append(name)

    if not mine:
        return None

    den = sum(weights)
    mine_overall = sum(w * x for w, x in zip(weights, mine)) / den
    bench_overall = sum(w * y for w, y in zip(weights, theirs)) / den
    ratio = round(mine_overall / bench_overall, 4) if bench_overall > 0 else None
    qualify_ratio = max(0.0, 1.0 - tolerance)
    strict_pass = (
        not weak_dims
        and not missing_dims
        and ratio is not None
        and ratio > STRICT_ABILITY_RATIO
    )

    return {
        "comparable": True,
        "benchmark_id": benchmark.get("id"),
        "benchmark_name": benchmark.get("name"),
        "tolerance": round(tolerance, 4),
        "qualify_ratio": round(qualify_ratio, 4),
        "strict": strict,
        "overall": round(mine_overall, 4),
        "bench_overall": round(bench_overall, 4),
        "ratio": ratio,
        "cosine": cosine(mine, theirs, weights),
        "per_dim": per_dim,
        "weak_dims": weak_dims,
        "missing_dims": missing_dims,
        "status": ("recommended" if strict_pass else "pool")
                  if strict else
                  ("recommended" if ratio is not None and ratio >= qualify_ratio else "pool"),
        "qualified": bool(strict_pass if strict else
                          ratio is not None and ratio >= qualify_ratio),
    }


def compare_one(
    cap: dict[str, Any], group: dict[str, Any],
) -> dict[str, Any] | None:
    """把新模型的四维能力分与一个分组的标杆对比。"""
    bench = group.get("benchmark") or {}
    base = compare_capability_to_benchmark(cap, {
        "id": bench.get("id"),
        "name": bench.get("name"),
        "pack_version": bench.get("pack_version"),
        "dims": bench.get("dims") or {},
        "overall": bench.get("overall"),
        "tolerance": bench.get("tolerance"),
    })
    if not base:
        return None
    return {
        **base,
        "group_id": group["id"], "group_name": group["name"],
        "multiplier": group["multiplier"],
    }


def recommend(
    cap: dict[str, Any] | None, groups: list[dict[str, Any]],
    gates: dict[str, Any] | None = None,
    trust: dict[str, Any] | None = None,
    current_group_id: int | None = None,
) -> dict[str, Any]:
    """步骤 4+5：在同模型倍率链上逐级对比并输出软推荐。"""
    gates = gates or {"passed": True, "failed": []}
    if trust and not trust.get("ok"):
        return {
            "status": "untrusted",
            "target_group_id": None,
            "headline": "本轮评分不可信，不进入分组推荐",
            "reasons": (trust.get("reasons") or [])
                       + ["分数不代表模型能力，定档前必须先解决上游转发问题"],
            "comparisons": [],
        }
    if not cap or not cap.get("dims"):
        return {
            "status": "no_data", "target_group_id": None,
            "headline": "本次没有四维能力评分，无法做横向对比",
            "reasons": ["需要先跑一次带四维能力评分的接入检测或能力评测"],
            "comparisons": [],
        }
    if not groups:
        return {
            "status": "no_group", "target_group_id": None,
            "headline": "还没有配置模型分组",
            "reasons": ["请先创建分组并为每个分组绑定一条标杆记录"],
            "comparisons": [],
        }

    current = next((g for g in groups if g["id"] == current_group_id), None)
    candidates = groups
    if current:
        key = model_group_key(current["name"], float(current["multiplier"]))
        candidates = [g for g in groups
                      if model_group_key(g["name"], float(g["multiplier"])) == key
                      and float(g["multiplier"]) >= float(current["multiplier"])]

    ordered = sorted(candidates, key=lambda g: float(g["multiplier"]))
    comparisons: list[dict[str, Any]] = []
    hit: dict[str, Any] | None = None
    for g in ordered:
        c = compare_one(cap, g)
        if c is None:
            if current:
                break
            continue
        comparisons.append(c)
        if not c.get("comparable"):
            if current:
                break
            continue
        if c["qualified"]:
            hit = c
            continue
        if current:
            break

    usable = [c for c in comparisons if c.get("comparable")]
    if not usable:
        return {
            "status": "no_comparable", "target_group_id": None,
            "headline": "没有可比的标杆",
            "reasons": [c.get("skip_reason", "标杆不可用")
                        for c in comparisons] or ["所有分组都还没绑定有效标杆"],
            "comparisons": comparisons,
        }

    if not current:
        hit = next((c for c in reversed(usable) if c["qualified"]), None)
    if hit:
        return _build_hit(hit, usable, comparisons, gates, current)
    return _build_miss(usable, comparisons, gates, current)


def _dim_phrase(c: dict[str, Any]) -> str:
    """把短板维度写成人能读的一句话。"""
    bits = []
    for name in c["weak_dims"]:
        d = c["per_dim"].get(name) or {}
        if d.get("ratio") is None:
            continue
        bits.append(f"{name}分低于{c['group_name']}标杆 {(1 - d['ratio']) * 100:.0f}%")
    return "；".join(bits)


def _build_hit(
    hit: dict[str, Any], usable: list[dict[str, Any]], allc: list[dict[str, Any]],
    gates: dict[str, Any], current: dict[str, Any] | None,
) -> dict[str, Any]:
    """综合能力达标：推荐通过准入并插入逐级挑战到的最高档。"""
    reasons = [
        f"综合得分 {hit['overall']:.0%}，达到{hit['group_name']}标杆的 "
        f"{hit['ratio']:.0%}（允许差异 {hit['tolerance']:.0%}）"
    ]
    higher = [c for c in usable if float(c["multiplier"]) > float(hit["multiplier"])]
    for c in higher:
        if c["ratio"] is not None:
            reasons.append(
                f"未进{c['group_name']}：综合仅达其标杆 {c['ratio']:.0%}，"
                f"低于允许下限 {c['qualify_ratio']:.0%}"
            )
        if c["weak_dims"]:
            reasons.append(f"{c['group_name']}风险提示：{_dim_phrase(c)}")
    if current and hit["group_id"] != current["id"]:
        reasons.append(
            f"已通过{current['name']}标杆并向上挑战成功，"
            f"确认插入后将自动调整到{hit['group_name']}"
        )
    if hit["weak_dims"]:
        reasons.append(f"单维度风险提示：{_dim_phrase(hit)}")
    if not gates.get("passed"):
        reasons.append(
            f"运行风险提示：{'、'.join(gates.get('failed') or [])}未过，"
            "不阻断本次相对标杆推荐"
        )
    if hit.get("cosine") is not None:
        reasons.append(
            f"与{hit['group_name']}标杆的能力结构相似度 {hit['cosine']:.0%}"
            f"（仅供参考，不参与落位判定）")
    return {
        "status": "recommended",
        "target_group_id": hit["group_id"],
        "target_group_name": hit["group_name"],
        "multiplier": hit["multiplier"],
        "headline": f"综合能力达标，推荐准入并插入 {hit['group_name']}",
        "reasons": reasons,
        "comparisons": allc,
    }


def _build_miss(
    usable: list[dict[str, Any]], allc: list[dict[str, Any]],
    gates: dict[str, Any], current: dict[str, Any] | None,
) -> dict[str, Any]:
    """起始档未达标：不推荐插入，保留相对差距供人工判断。"""
    lowest = min(usable, key=lambda c: float(c["multiplier"]))
    reasons = []
    if lowest["ratio"] is not None:
        label = current["name"] if current else f"最低档{lowest['group_name']}"
        reasons.append(
            f"{label}未达标：综合仅达其标杆 "
            f"{lowest['ratio']:.0%}，允许下限 {lowest['qualify_ratio']:.0%}"
        )
    if lowest["weak_dims"]:
        reasons.append(f"短板维度：{_dim_phrase(lowest)}")
    reasons.append("建议先放入试玩池观察，或补测确认是否为偶发波动")
    if not gates.get("passed"):
        reasons.append(f"同时存在运行风险：{'、'.join(gates.get('failed') or [])}")
    return {
        "status": "pool",
        "target_group_id": None,
        "headline": "暂不入组，建议放入试玩池",
        "reasons": reasons,
        "comparisons": allc,
    }
