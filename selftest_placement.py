"""横向对比与分组推荐自测（步骤 4、5）。不依赖网络，直接调 placement。

用法：python selftest_placement.py
重点验证：余弦相似度会被"形状一致但水平低"骗过，而加权比值不会。
"""
import sys

from app import itembank as ib
from app import placement as p

fails: list[str] = []


def check(name: str, cond: bool, extra: object = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + ("" if cond else f"  <- {extra}"))
    if not cond:
        fails.append(name)


def cap(dims: dict[str, float], version: str | None = None) -> dict:
    """伪造一份四类能力评分。"""
    return {
        "pack_version": version or ib.PACK_VERSION,
        "dims": {k: {"score": v, "weight": ib.WEIGHTS[k], "graded": 4, "ungraded": 0}
                 for k, v in dims.items()},
    }


def group(gid: int, name: str, mult: float, dims: dict[str, float],
          version: str | None = None) -> dict:
    return {
        "id": gid, "name": name, "multiplier": mult,
        "benchmark": {
            "id": gid * 10, "name": f"{name}标杆",
            "pack_version": version or ib.PACK_VERSION,
            "dims": dims, "overall": None,
        },
    }


BENCH_2X = {"代码": 0.89, "结构化": 0.85, "推理": 0.91, "指令保持": 0.87}
BENCH_1X = {"代码": 0.71, "结构化": 0.68, "推理": 0.74, "指令保持": 0.70}
BENCH_05X = {"代码": 0.49, "结构化": 0.45, "推理": 0.52, "指令保持": 0.48}

G2 = group(3, "2x组", 2.0, BENCH_2X)
G1 = group(2, "1x组", 1.0, BENCH_1X)
G05 = group(1, "0.5x组", 0.5, BENCH_05X)
ALL = [G2, G1, G05]
PASS_GATES = {"passed": True, "failed": []}

print("=" * 56)
print("余弦相似度的陷阱（这是不用余弦做主判据的原因）")

check("模型与倍率生成稳定的自动分组名",
      p.model_rate_group_name("Claude-Sonnet5", 3.0) == "Claude-Sonnet5-3x组")
check("自动分组名可还原同一模型键",
      p.model_group_key(p.model_rate_group_name("Claude-Sonnet5", 3.0), 3.0)
      == "claude-sonnet5")

# 各维度都只有标杆的一半，但强弱结构完全一致
half = cap({k: round(v / 2, 4) for k, v in BENCH_2X.items()})
c = p.compare_one(half, G2)
check("余弦被骗过（≈1.0）", c["cosine"] is not None and c["cosine"] > 0.99, c["cosine"])
check("加权比值识别出水平只有一半", 0.45 < c["ratio"] < 0.55, c["ratio"])
check("不判够格", c["qualified"] is False)
print(f"       余弦 {c['cosine']}  vs  比值 {c['ratio']}  ← 主判据用比值")

print("\n" + "=" * 56)
print("正常落位")

# 与 2x 标杆基本持平
strong = cap({"代码": 0.88, "结构化": 0.84, "推理": 0.91, "指令保持": 0.86})
r = p.recommend(strong, ALL, PASS_GATES)
check("推荐进 2x 组", r["target_group_id"] == G2["id"], r.get("headline"))
check("状态为已推荐", r["status"] == "recommended", r["status"])
check("理由里给出比值",
      any("标杆的" in x and "%" in x for x in r["reasons"]), r["reasons"])
check("三个分组都做了对比", len(r["comparisons"]) == 3, len(r["comparisons"]))

# 明显只够 1x
mid = cap({"代码": 0.73, "结构化": 0.70, "推理": 0.76, "指令保持": 0.72})
r = p.recommend(mid, ALL, PASS_GATES)
check("推荐进 1x 组", r["target_group_id"] == G1["id"], r.get("headline"))
check("说明为什么没进 2x",
      any("未进2x组" in x for x in r["reasons"]), r["reasons"])

print("\n" + "=" * 56)
print("偏科：单维度只提示，综合分达标仍推荐")

# 推理够 2x，代码明显拖后腿
lopsided = cap({"代码": 0.70, "结构化": 0.84, "推理": 0.92, "指令保持": 0.86})
r = p.recommend(lopsided, ALL, PASS_GATES)
check("综合分达标仍推荐 2x 组", r["target_group_id"] == G2["id"], r.get("headline"))
reason_text = " ".join(r["reasons"])
check("理由点名代码维度", "代码" in reason_text, r["reasons"])
check("理由带出低多少百分比", "%" in reason_text)
c2 = next(x for x in r["comparisons"] if x["group_id"] == G2["id"])
check("2x 对比里代码被标为短板", "代码" in c2["weak_dims"], c2["weak_dims"])
check("2x 对比里推理不算短板", "推理" not in c2["weak_dims"], c2["weak_dims"])
print(f"       {r['headline']}")
for x in r["reasons"]:
    print(f"         · {x}")

print("\n" + "=" * 56)
print("一档都不够 → 试玩池")

weak = cap({"代码": 0.20, "结构化": 0.25, "推理": 0.30, "指令保持": 0.28})
r = p.recommend(weak, ALL, PASS_GATES)
check("推荐试玩池", r["status"] == "pool", r["status"])
check("不给分组 id", r["target_group_id"] is None)
check("说明离最低档还差多少",
      any("最低档" in x for x in r["reasons"]), r["reasons"])
check("建议放入试玩池",
      any("试玩池" in x for x in r["reasons"]), r["reasons"])

print("\n" + "=" * 56)
print("运行门槛未过 → 作风险提示，不阻断软推荐")

r = p.recommend(strong, ALL, {"passed": False, "failed": ["长上下文"]})
check("仍然根据标杆推荐", r["target_group_id"] == G2["id"], r.get("headline"))
check("理由保留风险提示", any("长上下文" in x for x in r["reasons"]), r["reasons"])
check("仍完成分组对比", len(r["comparisons"]) == 3, r["comparisons"])

print("\n" + "=" * 56)
print("同模型倍率链：从 2x 起步，逐级挑战到 3x")

SONNET_2 = group(20, "Claude-Sonnet5-2x组", 2.0,
                 {k: 0.80 for k in BENCH_2X})
SONNET_3 = group(30, "Claude-Sonnet5-3x组", 3.0,
                 {k: 0.90 for k in BENCH_2X})
SONNET_5 = group(50, "Claude-Sonnet5-5x组", 5.0,
                 {k: 0.99 for k in BENCH_2X})
HAIKU_3 = group(31, "Claude-Haiku-3x组", 3.0,
                {k: 0.50 for k in BENCH_2X})
sonnet = cap({k: 0.86 for k in BENCH_2X})
r = p.recommend(sonnet, [HAIKU_3, SONNET_5, SONNET_2, SONNET_3], PASS_GATES,
                current_group_id=SONNET_2["id"])
check("先过 2x 再过 3x，推荐插入 3x",
      r["target_group_id"] == SONNET_3["id"], r.get("headline"))
check("不与其他模型标杆混比",
      all(c["group_id"] != HAIKU_3["id"] for c in r["comparisons"]), r["comparisons"])
check("逐级比到 5x 未达标即停止",
      [c["group_id"] for c in r["comparisons"]] == [20, 30, 50], r["comparisons"])
check("理由说明确认后自动改到 3x",
      any("自动调整" in x and "3x" in x for x in r["reasons"]), r["reasons"])

print("\n" + "=" * 56)
print("本轮评分不可信 → 终止，不进推荐")

BAD_TRUST = {"ok": False, "reasons": ["4 道题的回答里混进了上游注入的内容"]}
r = p.recommend(strong, ALL, PASS_GATES, BAD_TRUST)
check("状态为不可信", r["status"] == "untrusted", r["status"])
check("不给分组 id", r["target_group_id"] is None)
check("理由带出注入", any("注入" in x for x in r["reasons"]), r["reasons"])
check("说明分数不能定档",
      any("不代表模型能力" in x for x in r["reasons"]), r["reasons"])
check("不做任何对比", r["comparisons"] == [], r["comparisons"])

# 满分模型也一样拦住 —— 分数好不好不影响"测量无效"这个判断
r = p.recommend(cap({k: 1.0 for k in BENCH_2X}), ALL, PASS_GATES, BAD_TRUST)
check("满分也拦住", r["status"] == "untrusted", r["status"])

# trust 正常时不影响
r = p.recommend(strong, ALL, PASS_GATES, {"ok": True, "reasons": []})
check("trust 正常不受影响", r["target_group_id"] == G2["id"], r.get("headline"))
r = p.recommend(strong, ALL, PASS_GATES, None)
check("不传 trust 也正常", r["target_group_id"] == G2["id"], r.get("headline"))

print("\n" + "=" * 56)
print("边界")

r = p.recommend(strong, [], PASS_GATES)
check("没有分组池时说清楚", r["status"] == "no_group", r["status"])

r = p.recommend(None, ALL, PASS_GATES)
check("没有四类能力分时说清楚", r["status"] == "no_data", r["status"])

# 标杆题库版本不符
incompatible = group(9, "异版标杆组", 1.0, BENCH_1X, version="cap-v1")
r = p.recommend(strong, [incompatible], PASS_GATES)
check("版本不符时不可比", r["status"] == "no_comparable", r["status"])
check("理由说明版本不同",
      any("版本不同" in x for x in r["reasons"]), r["reasons"])
c3 = r["comparisons"][0]
check("对比项标记为不可比", c3["comparable"] is False)

# 只测到部分维度
partial = cap({"推理": 0.90, "代码": 0.92})
c4 = p.compare_one(partial, G2)
check("缺维度仍能比（按共有维度归一化）", c4 is not None and c4["ratio"] is not None,
      c4)
check("缺的维度 ratio 为空",
      c4["per_dim"]["结构化"]["ratio"] is None,
      c4["per_dim"]["结构化"])

strict_benchmark = {
    "id": 99, "name": "手选标杆", "pack_version": ib.PACK_VERSION,
    "dims": {name: 1.0 for name in ib.DIM_ORDER},
}
strict_ok = p.compare_capability_to_benchmark(
    cap({name: 0.94 for name in ib.DIM_ORDER}),
    strict_benchmark, strict=True)
check("手选标杆对比在四类能力均高于 93% 时推荐",
      strict_ok["qualified"] is True, strict_ok)
strict_edge = p.compare_capability_to_benchmark(
    cap({name: 0.93 for name in ib.DIM_ORDER}),
    strict_benchmark, strict=True)
check("能力比值等于 93% 边界时不推荐",
      strict_edge["qualified"] is False
      and set(strict_edge["weak_dims"]) == set(ib.DIM_ORDER), strict_edge)
strict_partial = p.compare_capability_to_benchmark(partial, strict_benchmark, strict=True)
check("手选标杆对比缺失维度时不推荐",
      strict_partial["qualified"] is False
      and "结构化" in strict_partial["missing_dims"], strict_partial)

# 分组未绑标杆
nob = {"id": 8, "name": "空组", "multiplier": 1.0, "benchmark": None}
check("未绑标杆的组被跳过", p.compare_one(strong, nob) is None)

print("\n" + "=" * 56)
print("排序：取够格的最高档")

# 三档都够格，应该选 2x
top = cap({"代码": 0.94, "结构化": 0.92, "推理": 0.95, "指令保持": 0.93})
r = p.recommend(top, [G05, G1, G2], PASS_GATES)   # 故意乱序传入
check("乱序传入也取最高档", r["target_group_id"] == G2["id"], r.get("headline"))
check("对比结果按倍率逐级升序",
      [x["multiplier"] for x in r["comparisons"]] == [0.5, 1.0, 2.0],
      [x["multiplier"] for x in r["comparisons"]])

print("\n" + "=" * 56)
print(f"通过 {len(fails) == 0}，失败项：{fails if fails else '无'}")
sys.exit(1 if fails else 0)
