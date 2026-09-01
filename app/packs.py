"""Versioned evaluation packs and their request budgets."""
from typing import Any

from . import hardbank, itembank, test_catalog

def _admission_steps() -> tuple[int, int]:
    return 1 + len(test_catalog.admission_items("", test_catalog.DEFAULT_SEED)) \
        + len(test_catalog.fixed_speed_items()), 256 + test_catalog.estimate_tokens("")

PERFORMANCE_STEPS = [
    {"kind": "stream", "step": "性能基线 1", "performance_probe": True,
     "prompt": "请连续写出 32 个从 101 开始的整数，用英文逗号分隔，不要解释。",
     "max_tokens": 160},
    {"kind": "stream", "step": "性能基线 2", "performance_probe": True,
     "prompt": "请连续写出 32 个从 301 开始的整数，用英文逗号分隔，不要解释。",
     "max_tokens": 160},
    {"kind": "stream", "step": "性能基线 3", "performance_probe": True,
     "prompt": "请连续写出 32 个从 501 开始的整数，用英文逗号分隔，不要解释。",
     "max_tokens": 160},
]

PACKS: dict[str, dict[str, Any]] = {
    "admission": {
        "name": "简单渠道对比测试",
        "version": itembank.PACK_VERSION,
        "desc": "固定执行鉴权、6 道流式能力题和 5 道流式速度题。",
        "steps": [
            {"kind": "auth", "method_id": "QA-PROTO-01"},
            {"kind": "eval"},
            {"kind": "fixed_speed"},
        ],
        "required": ["QA-PROTO-01 鉴权与模型可达性"],
        "est_requests": _admission_steps()[0],
        "est_tokens": _admission_steps()[1],
        "concurrency": 1,
    },
    "inspect": {
        "name": "定期巡检",
        "version": "ins-v2",
        "desc": "已接入渠道的每日轻量抽检:连通性、流式、模型一致性、计费字段完整性。",
        "steps": [
            {"kind": "chat", "step": "连通抽检"},
            *PERFORMANCE_STEPS,
            {"kind": "capability", "only": ["数学计算", "指令遵循"]},
        ],
        "required": ["连通抽检"],
        "est_requests": 6,
        "est_tokens": 1_300,
        "concurrency": 1,
    },
    "degrade": {
        "name": "降智复核",
        "version": f"deg-{itembank.PACK_VERSION}",
        "desc": "重跑能力保真与风险证据，与该渠道自己的历史基线纵向对比。",
        "steps": [
            {"kind": "chat", "step": "连通确认"},
            {"kind": "eval"},
        ],
        "required": ["连通确认"],
        "est_requests": 1 + len(test_catalog.admission_items()),
        "est_tokens": 256 + test_catalog.estimate_tokens(),
        "concurrency": 1,
    },
    "capability": {
        "name": "能力评测",
        "version": f"{itembank.PACK_VERSION}+{hardbank.HARD_VERSION}",
        "desc": "重跑能力保真题并可选执行独立硬题，用于与同版本标杆比较。",
        "steps": [
            {"kind": "chat", "step": "连通确认"},
            {"kind": "eval"},
            {"kind": "hard"},
        ],
        "required": ["连通确认"],
        "est_requests": 1 + len(test_catalog.admission_items()) + hardbank.count(),
        "est_tokens": 256 + test_catalog.estimate_tokens() + hardbank.estimate_tokens(),
        "est_requests_no_hard": 1 + len(test_catalog.admission_items()),
        "est_tokens_no_hard": 256 + test_catalog.estimate_tokens(),
        "concurrency": 1,
    },
    "agent_stability": {
        "name": "Agent 稳定性测试", "version": "as-v1.0.0",
        "desc": "连续结构化请求、工具参数和三条状态机工作流。",
        "steps": [{"kind": "agent_stability"}], "required": [],
        "est_requests": 15, "est_tokens": 9_000, "concurrency": 1,
    },
    "development_speed": {
        "name": "开发速度测试", "version": "ds-v1.0.0",
        "desc": "3 个短响应、2 个中等代码响应和 1 个长代码输出。",
        "steps": [{"kind": "development_speed"}], "required": [],
        "est_requests": 6, "est_tokens": 5_000, "concurrency": 1,
    },
    "long_context": {
        "name": "长上下文与缓存测试", "version": "lc-v1.0.0",
        "desc": "按需验证可靠上下文档位和缓存写入、读取与失效。",
        "steps": [{"kind": "long_context"}], "required": [],
        "est_requests": 10, "est_tokens": 90_000, "concurrency": 1,
    },
    "load": {
        "name": "压力测试",
        "version": "load-v1",
        "desc": "准入通过后按阶梯并发测量吞吐、TTFT、生成速度、错误率与缓存倾向。",
        "steps": [{"kind": "load"}],
        "required": [],
        "est_requests": 60,
        "est_tokens": 12_000,
        "concurrency": 20,
    },
}


def get_pack(kind: str) -> dict[str, Any]:
    if kind not in PACKS:
        raise KeyError(f"未知测试包：{kind}")
    return PACKS[kind]


def _cost_of(tokens: int, price_in: float, price_out: float, hard: bool) -> float:
    """按输入/输出占比估费用。

    硬题的成本结构与其它步骤相反:题面短、输出长(max_tokens 给到 8192),
    所以按 2:8 拆;其余步骤沿用原来的 8:2。用同一个比例会把硬题费用估低好几倍。
    """
    r_in = 0.2 if hard else 0.8
    return (tokens * r_in / 1_000_000) * price_in \
        + (tokens * (1 - r_in) / 1_000_000) * price_out


def estimate(
    kind: str, price_in: float, price_out: float, include_hard: bool = True,
) -> dict[str, Any]:
    """提交前的预估:请求数、tokens、并发、预计费用。

    带硬题的包会同时给出"关掉硬题"的口径,前端好把差价摆给用户看。
    硬题按 max_tokens 满打满算,实际通常远低于此 —— 宁可估高,别让人被账单意外。
    """
    pack = get_pack(kind)
    base_tokens = pack.get("est_tokens_no_hard", pack["est_tokens"])
    base_requests = pack.get("est_requests_no_hard", pack["est_requests"])
    has_hard = "est_tokens_no_hard" in pack

    hard_tokens = pack["est_tokens"] - base_tokens if has_hard else 0
    hard_requests = pack["est_requests"] - base_requests if has_hard else 0
    use_hard = bool(has_hard and include_hard)

    tokens = base_tokens + (hard_tokens if use_hard else 0)
    requests = base_requests + (hard_requests if use_hard else 0)
    cost = _cost_of(base_tokens, price_in, price_out, hard=False)
    if use_hard:
        cost += _cost_of(hard_tokens, price_in, price_out, hard=True)

    out = {
        "pack": pack["name"],
        "version": pack["version"],
        "requests": requests,
        "tokens": tokens,
        "concurrency": pack["concurrency"],
        "cost": round(cost, 6),
        "has_hard": has_hard,
        "include_hard": use_hard,
    }
    if has_hard:
        out["hard_requests"] = hard_requests
        out["hard_tokens"] = hard_tokens
        out["hard_cost"] = round(
            _cost_of(hard_tokens, price_in, price_out, hard=True), 6)
        out["cost_no_hard"] = round(
            _cost_of(base_tokens, price_in, price_out, hard=False), 6)
        out["requests_no_hard"] = base_requests
        out["tokens_no_hard"] = base_tokens
    return out
