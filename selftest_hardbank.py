"""HardcoreLogic 无解题的判分、结构和四类能力隔离自测。"""
import sys

from app import hardbank, itembank, packs, probes, report

fails: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        fails.append(name)


items = hardbank.all_items()
check("四道 HardcoreLogic 无解题", len(items) == 4, len(items))
check("只包含无解识别题库", hardbank.banks() == ["hardcore_unsolvable"], hardbank.banks())
check("全部使用结构化判分", all(item["scorer"] == "structured" for item in items))
check("题号唯一", len({item["id"] for item in items}) == len(items))
check("题面、答案和预算完整", all(
    item["prompt"] and item["expected"] and int(item["max_tokens"]) > 0
    for item in items))
check("标准答案全部判对", all(
    hardbank.score(item, f"<solution>{item['expected'][0]}</solution>") == 1.0
    for item in items))
check("错误结构全部判错", all(
    hardbank.score(item, '<solution>{"solvable":true,"solution":[]}</solution>') == 0.0
    for item in items))
check("硬题版本独立", hardbank.HARD_VERSION.startswith("hard-")
      and hardbank.HARD_VERSION != itembank.PACK_VERSION)
check("硬题不在准入内容题中", not (
    {item["id"] for item in items}
    & {item["id"] for _dimension, item in itembank.all_items()}))


def step(name: str, *, hard: bool = False, timeout: bool = False,
         dim: str | None = None) -> dict:
    score = 0.0 if hard else 1.0
    extra = {
        "hard": hard,
        "graded": True,
        "scored": score,
        "grade": {"status": "failed" if hard else "passed", "score": score},
        "score_domain": "hard" if hard else "ability",
        "eligibility": {"ability": "eligible"} if not hard else {},
        "item": name,
        "item_weight": 1.0,
        "level": 2,
    }
    if dim:
        extra["dim"] = dim
    return probes.result(
        name, not timeout and not hard,
        reason=probes.TIMEOUT if timeout else (probes.MISMATCH if hard else ""),
        latency=200.0 if hard else 1.0,
        usage={"prompt": 10, "completion": 10}, extra=extra)


base = [probes.result("连通", True, latency=1.0),
        step("能力题", dim="推理")]
with_hard = base + [step(f"硬题{i}", hard=True, timeout=True) for i in range(4)]
plain_metrics = report.compute_metrics(base, 2.0, 8.0)
hard_metrics = report.compute_metrics(with_hard, 2.0, 8.0)
check("硬题不改变成功率", hard_metrics["pass_rate"] == plain_metrics["pass_rate"])
check("硬题不改变超时率", hard_metrics["timeout_rate"] == plain_metrics["timeout_rate"])
check("硬题不改变四类能力综合分",
      hard_metrics["capability"]["overall"] == plain_metrics["capability"]["overall"])
check("硬题单独汇总", hard_metrics["hard"]["total"] == 4
      and hard_metrics["hard"]["timeout_count"] == 4)

admission = packs.estimate("admission", 2.0, 8.0)
capability_on = packs.estimate("capability", 2.0, 8.0, include_hard=True)
capability_off = packs.estimate("capability", 2.0, 8.0, include_hard=False)
check("默认准入不含硬题", not admission["has_hard"])
check("能力复测可选硬题", capability_on["has_hard"]
      and capability_off["requests"] == 7
      and capability_on["requests"] == capability_off["requests"] + 4)

print(f"通过 {not fails}，失败项：{fails if fails else '无'}")
sys.exit(1 if fails else 0)
