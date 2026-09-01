"""Versioned QA item instances backed by registered deterministic graders."""
from __future__ import annotations

import json
import math
from typing import Any

CATALOG_VERSION = "qa-simple-v2.0.0"
DEFAULT_SEED = 104729
INTELLIGENCE_REQUIRED_PASSES = 5
FIXED_SPEED_VERSION = "speed-v1.0.0"
FIXED_SPEED_MAX_TOKENS = 320

METHODS: dict[str, dict[str, Any]] = {
    "QA-PROTO-01": {"name": "鉴权与模型可达性", "domain": "protocol"},
    "QA-CODE-01": {"name": "代码执行追踪", "domain": "ability"},
    "QA-CODE-02": {"name": "可执行函数实现", "domain": "ability"},
    "QA-STR-01": {"name": "动态 JSON 约束", "domain": "ability"},
    "QA-REA-01": {"name": "干扰信息链式计算", "domain": "ability"},
    "QA-REA-02": {"name": "有向图最短路", "domain": "ability"},
    "QA-INS-01": {"name": "多条件指令保持", "domain": "ability"},
    **{f"QA-SPEED-{index:02d}": {"name": "固定速度题", "domain": "speed"}
       for index in range(1, 6)},
}


def _item(item_id: str, *, prompt: str, grader: str, config: dict[str, Any],
          slice_name: str, dimension: str | None, max_tokens: int,
          variant: int, seed: int, mock_answer: str,
          json_mode: bool = False,
          completion_policy: str = "whole_response_required") -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": item_id, "version": 1, "template_id": item_id,
        "template_version": 1, "variant": variant, "seed": seed,
        "slice": slice_name, "dim": dimension,
        "score_domain": "risk" if dimension is None else "ability",
        "level": 2, "weight": 1.0, "prompt": prompt,
        "max_tokens": max_tokens, "completion_policy": completion_policy,
        "grader": {"id": grader, "version": "1.0.0", "config": config},
        "comparison_key": f"{item_id}/L2/v1", "method_id": item_id,
        "mock_answer": mock_answer,
    }
    if json_mode:
        value["json_mode"] = True
    return value


def _code_trace(seed: int, variant: int) -> dict[str, Any]:
    instances = [
        ([['b', 3], ['a', 2], ['b', -1], ['c', 2], ['a', 1]], 2),
        ([['z', 5], ['x', 4], ['z', -2], ['y', 3], ['x', 1]], 3),
        ([['m', 2], ['n', 6], ['m', 4], ['p', 6], ['n', -1]], 5),
    ]
    rows, threshold = instances[(variant - 1) % len(instances)]
    totals: dict[str, int] = {}
    for name, amount in rows:
        totals[name] = totals.get(name, 0) + amount
    expected = [[name, amount] for name, amount in
                sorted(totals.items(), key=lambda entry: (-entry[1], entry[0]))
                if amount >= threshold]
    rows_literal = repr([(name, amount) for name, amount in rows])
    prompt = f"""下面的 Python 代码最终输出什么？只输出一行 JSON 数组，不要解释。

rows = {rows_literal}
totals = {{}}
for name, value in rows:
    totals[name] = totals.get(name, 0) + value
result = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
print([[name, value] for name, value in result if value >= {threshold}])"""
    return _item(
        "QA-CODE-01", prompt=prompt, grader="program_oracle",
        config={"oracle_id": "code_trace_oracle", "oracle_version": "1.0.0",
                "expected": expected, "comparator": "json_schema_exact"},
        slice_name="code",
        dimension="代码", max_tokens=220, variant=variant, seed=seed,
        mock_answer=json.dumps(expected, ensure_ascii=False, separators=(",", ":")),
        json_mode=True)


def _code_implementation(seed: int, variant: int) -> dict[str, Any]:
    function_name = ["normalize_windows", "normalize_ranges", "coalesce_segments"][(variant - 1) % 3]
    prompt = f"""请用 Python 实现 {function_name}(windows)。

输入是若干 [start, end]，表示左闭右开的整数区间，保证 start < end。
返回合并后的新区间列表，按 start 升序排列。重叠区间和首尾相接的区间都要合并；
不能修改输入；空输入返回 []。

示例：[[5, 8], [1, 3], [3, 4], [7, 10]] -> [[1, 4], [5, 10]]
只输出函数代码，不要调用第三方库。"""
    tests = [
        {"args": [[]], "expected": []},
        {"args": [[[2, 5]]], "expected": [[2, 5]]},
        {"args": [[[5, 8], [1, 3], [3, 4], [7, 10]]],
         "expected": [[1, 4], [5, 10]], "input_immutable": True},
        {"args": [[[1, 10], [2, 3], [10, 12]]], "expected": [[1, 12]]},
    ]
    mock = (f"def {function_name}(windows):\n    merged = []\n"
            "    for start, end in sorted(windows):\n"
            "        if merged and start <= merged[-1][1]:\n"
            "            merged[-1][1] = max(merged[-1][1], end)\n"
            "        else:\n            merged.append([start, end])\n"
            "    return merged")
    return _item(
        "QA-CODE-02", prompt=prompt, grader="executable_code",
        config={"language": "python", "function": function_name, "tests": tests},
        slice_name="code", dimension="代码", max_tokens=700,
        variant=variant, seed=seed, mock_answer=mock)


def _structured_json(seed: int, variant: int) -> dict[str, Any]:
    word, left, right, letter = [
        ("lantern", 18, 30, "n"), ("copper", 24, 36, "p"),
        ("matrix", 21, 35, "a")][(variant - 1) % 3]
    divisor = math.gcd(left, right)
    count = word.count(letter)
    product = len(word) * divisor
    expected = {"r": word[::-1], "g": divisor, "c": count, "p": product,
                "q": count + product if divisor % 2 == 0 else count - product}
    prompt = f"""只输出一行合法 JSON，不要 Markdown，不要解释，也不要额外字段。
字段必须且只能是 r、g、c、p、q：
- r：字符串 {word} 的反转；
- g：{left} 和 {right} 的最大公约数；
- c：字母 {letter} 在 {word} 中出现的次数；
- p：字符串长度乘以 g；
- q：如果 g 是偶数则为 c+p，否则为 c-p。"""
    return _item(
        "QA-STR-01", prompt=prompt, grader="program_oracle",
        config={"oracle_id": "dynamic_json_oracle", "oracle_version": "1.0.0",
                "expected": expected, "comparator": "json_schema_exact"},
        slice_name="structured_output", dimension="结构化", max_tokens=220,
        variant=variant, seed=seed,
        mock_answer=json.dumps(expected, ensure_ascii=False, separators=(",", ":")),
        json_mode=True)


def _chain_reasoning(seed: int, variant: int) -> dict[str, Any]:
    a, b = [(7, 4), (6, 5), (8, 3)][(variant - 1) % 3]
    c = a + b
    d = c * b
    e = d - a
    expected = e + c
    prompt = f"""根据以下记录计算 F。只输出一个整数。

A={a}
B={b}
仓库里另有 37 箱水，与计算无关。
C=A+B
D=C*B
E=D-A
F=E+C
备用记录 Z=A*B，已作废，不参与计算。"""
    return _item(
        "QA-REA-01", prompt=prompt, grader="program_oracle",
        config={"oracle_id": "chain_reasoning_oracle", "oracle_version": "1.0.0",
                "expected": expected, "comparator": "exact_scalar"},
        slice_name="objective_reasoning",
        dimension="推理", max_tokens=160, variant=variant, seed=seed,
        mock_answer=str(expected))


def _graph_reasoning(seed: int, variant: int) -> dict[str, Any]:
    start, a, b, c, target = [
        ("S", "A", "B", "C", "T"), ("P", "K", "L", "M", "Q"),
        ("U", "D", "E", "F", "V")][(variant - 1) % 3]
    edges = [[start, a, 4], [start, b, 2], [b, a, 1], [a, c, 3],
             [b, c, 7], [c, target, 2], [a, target, 8], [b, target, 12]]
    lines = "\n".join(f"{left}->{right}:{cost}" for left, right, cost in edges)
    expected = {"cost": 8, "path": [start, b, a, c, target]}
    prompt = (f"道路都是单向的。求 {start} 到 {target} 的最低总成本。\n\n{lines}\n\n"
              "只输出 JSON：{\"cost\":整数,\"path\":[按顺序经过的节点]}，不要解释。")
    return _item(
        "QA-REA-02", prompt=prompt, grader="graph_solution",
        config={"edges": edges, "start": start, "target": target, "expected_cost": 8},
        slice_name="objective_reasoning", dimension="推理", max_tokens=220,
        variant=variant, seed=seed,
        mock_answer=json.dumps(expected, ensure_ascii=False, separators=(",", ":")),
        json_mode=True)


def _constraint_total(variant: int) -> tuple[int, list[int]]:
    totals: list[tuple[int, list[int]]] = []
    for total in range(18, 60):
        solutions = []
        for first in range(1, 15):
            fourth = first * 2
            for second in range(first + 1, fourth):
                for third in range(second + 1, fourth):
                    if first + second + third + fourth == total:
                        solutions.append([first, second, third, fourth])
        if len(solutions) >= 2:
            totals.append((total, solutions[0]))
    return totals[(variant - 1) % len(totals)]


def _instruction_constraints(seed: int, variant: int) -> dict[str, Any]:
    total, solution = _constraint_total(variant)
    tags = [["amber", "lime", "navy", "rose"],
            ["cedar", "mint", "ocean", "ruby"],
            ["cloud", "leaf", "stone", "sun"]][(variant - 1) % 3]
    prompt = f"""严格输出 4 行，不要标题、解释、空行或代码块。
每行格式必须是 K序号=整数;tag=单词，且不能出现空格。
序号依次为 1、2、3、4；四个整数均为正数、严格递增、总和为 {total}；
第 4 个整数是第 1 个的 2 倍；tag 必须把 {tags[0]}、{tags[1]}、{tags[2]}、{tags[3]} 各用一次；
第 2 行的 tag 必须是 {tags[1]}，第 4 行的 tag 必须是 {tags[3]}。"""
    config = {"validator": "numbered_line_records",
              "line_pattern": r"K(?P<index>\d+)=(?P<value>\d+);tag=(?P<tag>[a-z]+)",
              "line_count": 4, "index_sequence": [1, 2, 3, 4],
              "strictly_increasing": "value",
              "sum": {"field": "value", "expected": total},
              "relation": {"left_index": 3, "right_index": 0,
                           "field": "value", "multiplier": 2},
              "set": {"field": "tag", "expected": tags},
              "fixed": [{"index": 1, "field": "tag", "expected": tags[1]},
                        {"index": 3, "field": "tag", "expected": tags[3]}]}
    mock = "\n".join(f"K{index + 1}={number};tag={tags[index]}"
                     for index, number in enumerate(solution))
    return _item(
        "QA-INS-01", prompt=prompt, grader="constraint_set", config=config,
        slice_name="instruction_following", dimension="指令保持", max_tokens=220,
        variant=variant, seed=seed, mock_answer=mock)


_ADMISSION_BUILDERS = (_code_trace, _code_implementation, _structured_json,
                       _chain_reasoning, _graph_reasoning, _instruction_constraints)


def admission_items(model: str = "", seed: int = DEFAULT_SEED,
                    variant: int = 1) -> list[dict[str, Any]]:
    del model
    normalized_variant = min(max(int(variant), 1), 3)
    return [builder(seed, normalized_variant) for builder in _ADMISSION_BUILDERS]


def fixed_speed_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, start in enumerate((101, 301, 501, 701, 901), 1):
        expected = list(range(start, start + 60))
        item_id = f"QA-SPEED-{index:02d}"
        items.append({
            "id": item_id, "version": 1, "template_id": item_id,
            "template_version": 1, "slice": "fixed_speed",
            "score_domain": "speed", "prompt": (
                f"按升序输出 {start} 到 {start + 59} 的整数，使用英文逗号分隔，"
                "不要解释。"),
            "max_tokens": FIXED_SPEED_MAX_TOKENS,
            "completion_policy": "whole_response_required",
            "method_id": item_id, "performance_probe": True,
            "grader": {"id": "sequence_exact", "version": "1.0.0",
                       "config": {"expected": expected, "separator": ",",
                                  "element_type": "integer"}},
            "mock_answer": ",".join(str(value) for value in expected),
            "comparison_key": f"{item_id}/{FIXED_SPEED_VERSION}",
        })
    return items


def paired_speed_items() -> list[dict[str, Any]]:
    """双端准入清单冻结的两轮速度实例；顺序由调用方按轮次组织。"""
    starts_by_round = (
        (101, 301, 501, 701, 901),
        (161, 361, 561, 761, 841),
    )
    items: list[dict[str, Any]] = []
    for round_number, starts in enumerate(starts_by_round, 1):
        for index, start in enumerate(starts, 1):
            expected = list(range(start, start + 60))
            item_id = f"QA-SPEED-{index:02d}"
            items.append({
                "id": item_id,
                "instance_id": f"{item_id}-R{round_number}",
                "version": 1,
                "template_id": item_id,
                "template_version": 1,
                "slice": "fixed_speed",
                "score_domain": "speed",
                "round": round_number,
                "prompt": (
                    f"按升序输出 {start} 到 {start + 59} 的整数，使用英文逗号分隔，"
                    "不要解释。"
                ),
                "max_tokens": 320,
                "completion_policy": "whole_response_required",
                "method_id": item_id,
                "performance_probe": True,
                "grader": {
                    "id": "sequence_exact",
                    "version": "1.0.0",
                    "config": {
                        "expected": expected,
                        "separator": ",",
                        "element_type": "integer",
                    },
                },
                "mock_answer": ",".join(str(value) for value in expected),
                "comparison_key": f"{item_id}/R{round_number}/{FIXED_SPEED_VERSION}",
            })
    return items


def estimate_tokens(model: str = "") -> int:
    return sum(int(candidate["max_tokens"])
               for candidate in [*admission_items(model, DEFAULT_SEED),
                                  *fixed_speed_items()])


def method_catalog() -> dict[str, Any]:
    return {"version": CATALOG_VERSION, "admission": list(METHODS),
            "methods": METHODS,
            "registered_item_count": len(_ADMISSION_BUILDERS) + len(fixed_speed_items())}


def admission_methods() -> set[str]:
    return set(METHODS)


def analysis_results(steps: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    del steps, model
    return []
