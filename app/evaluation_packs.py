"""Deterministic item catalogs for post-admission evaluation packages."""
from __future__ import annotations

import json
from typing import Any

AGENT_VERSION = "as-v1.0.0"
DEVELOPMENT_VERSION = "ds-v1.0.0"
LONG_CONTEXT_VERSION = "lc-v1.0.0"


def _item(item_id: str, package: str, prompt: str, grader_id: str,
          config: dict[str, Any], mock_answer: str, *, max_tokens: int = 256,
          tools: list[dict[str, Any]] | None = None,
          completion_policy: str = "whole_response_required",
          workload: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": item_id, "version": 1, "template_id": item_id,
        "template_version": 1, "seed": 104729, "slice": package,
        "score_domain": package, "package": package, "prompt": prompt,
        "max_tokens": max_tokens, "completion_policy": completion_policy,
        "grader": {"id": grader_id, "version": "1.0.0", "config": config},
        "comparison_key": f"{item_id}/v1", "method_id": item_id,
        "mock_answer": mock_answer, "workload": workload,
    }
    if tools:
        item["tools"] = tools
    return item


def _agent_json(index: int) -> dict[str, Any]:
    left, right = 17 + index, 29 + index
    expected = {"request_id": f"R-{index:02d}", "sum": left + right,
                "retry": index % 2 == 0}
    prompt = ("只输出一行 JSON，字段必须且只能是 request_id、sum、retry。"
              f"request_id=R-{index:02d}，sum={left}+{right}，"
              f"retry={'true' if index % 2 == 0 else 'false'}。")
    return _item(
        f"AS-JSON-{index:02d}", "agent_stability", prompt,
        "json_schema_exact",
        {"required": {"request_id": "string", "sum": "integer", "retry": "boolean"},
         "expected": expected, "additional_properties": False},
        json.dumps(expected, ensure_ascii=False, separators=(",", ":")))


def _agent_tool(index: int) -> dict[str, Any]:
    function_name = "get_ticket"
    ticket_id = f"TK-{730 + index}"
    tools = [
        {"name": function_name, "description": "按工单 ID 查询状态",
         "parameters": {"type": "object", "properties": {
             "ticket_id": {"type": "string"}}, "required": ["ticket_id"],
             "additionalProperties": False}},
        {"name": "close_ticket", "description": "关闭工单",
         "parameters": {"type": "object", "properties": {
             "ticket_id": {"type": "string"}}, "required": ["ticket_id"]}},
    ]
    return _item(
        f"AS-TOOL-{index:02d}", "agent_stability",
        f"只查询工单 {ticket_id} 的状态，不要关闭工单。", "tool_call_exact",
        {"count": 1, "name": function_name, "arguments": {"ticket_id": ticket_id},
         "argument_types": {"ticket_id": "string"}}, "", tools=tools)


def _workflow(index: int) -> dict[str, Any]:
    order_id = f"OR-{410 + index}"
    events = [
        {"action": "tool:get_order", "arguments": {"order_id": order_id}},
        {"action": "tool:ask_confirmation", "arguments": {"order_id": order_id}},
        {"action": "tool:refund_order", "arguments": {"order_id": order_id}},
        {"action": "final_response", "arguments": {"order_id": order_id}},
    ]
    states = {
        "need_lookup": {"forbidden": ["tool:refund_order"], "transitions": {
            "tool:get_order": {"arguments": {"order_id": order_id},
                               "next_state": "need_confirmation"}}},
        "need_confirmation": {"forbidden": ["tool:refund_order"], "transitions": {
            "tool:ask_confirmation": {"arguments": {"order_id": order_id},
                                      "next_state": "ready_to_refund"}}},
        "ready_to_refund": {"transitions": {
            "tool:refund_order": {"arguments": {"order_id": order_id},
                                  "grounded_fields": {"order_id": order_id},
                                  "next_state": "need_final"}}},
        "need_final": {"transitions": {
            "final_response": {"arguments": {"order_id": order_id},
                               "next_state": "completed"}}},
        "completed": {"transitions": {}},
    }
    prompt = (f"为订单 {order_id} 生成代理动作序列：必须先查询、再请求确认、"
              "确认后退款、最后回复。只输出 JSON 数组；每项只能含 action 和 arguments。")
    return _item(
        f"AS-WF-{index:02d}", "agent_stability", prompt,
        "workflow_state_machine",
        {"initial_state": "need_lookup", "terminal_state": "completed",
         "states": states},
        json.dumps(events, ensure_ascii=False, separators=(",", ":")),
        max_tokens=600, completion_policy="workflow_terminal_required")


def agent_items() -> list[dict[str, Any]]:
    return [*(_agent_json(index) for index in range(1, 7)),
            *(_agent_tool(index) for index in range(1, 7)),
            *(_workflow(index) for index in range(1, 4))]


def _short_speed(index: int) -> dict[str, Any]:
    start = index * 100 + 1
    expected = list(range(start, start + 60))
    return _item(
        f"DS-SHORT-{index:02d}", "development_speed",
        f"输出 {start} 到 {start + 59} 的整数，英文逗号分隔，不要解释。",
        "sequence_exact", {"expected": expected, "separator": ",",
                           "element_type": "integer"},
        ",".join(str(value) for value in expected), max_tokens=320,
        workload="short")


def _medium_speed(index: int) -> dict[str, Any]:
    expected = {"defects": ["resource_leak", "missing_backoff"],
                "severity": "high", "patch_lines": 4 + index}
    prompt = ("阅读下面的伪代码并只输出 JSON，字段必须且只能是 defects、severity、"
              "patch_lines。defects 按字母序列出稳定缺陷码。\n"
              "conn = pool.acquire()\nfor attempt in range(3):\n"
              "    try: return call(conn)\n    except TimeoutError: continue\n"
              f"修复方案预计修改 {4 + index} 行。")
    config = {"children": [
        {"id": "json_schema_exact", "required": True, "config": {
            "required": {"defects": "array", "severity": "string",
                         "patch_lines": "integer"},
            "expected": expected, "additional_properties": False}},
        {"id": "json_schema_exact", "required": True,
         "config": {"expected_value": expected}},
    ], "short_circuit": True}
    return _item(
        f"DS-MED-{index:02d}", "development_speed", prompt, "composite", config,
        json.dumps(expected, ensure_ascii=False, separators=(",", ":")),
        max_tokens=700, workload="medium")


def _long_speed() -> dict[str, Any]:
    function = "summarize_events"
    tests = [
        {"args": [[]], "expected": {}},
        {"args": [[{"kind": "ok"}, {"kind": "error"}, {"kind": "ok"}]],
         "expected": {"ok": 2, "error": 1}, "input_immutable": True},
        {"args": [[{"kind": "a"}, {}, {"kind": "a"}]], "expected": {"a": 2}},
    ]
    prompt = ("实现 Python 函数 summarize_events(events)：统计每个含非空字符串 kind 字段的"
              "事件数量，忽略无效项，不修改输入，返回按 kind 字典序插入的字典。"
              "只输出函数代码。")
    mock = ("def summarize_events(events):\n    counts = {}\n"
            "    for event in events:\n        if 'kind' in event and event['kind']:\n"
            "            kind = event['kind']\n            counts[kind] = counts.get(kind, 0) + 1\n"
            "    ordered = {}\n    for kind in sorted(counts):\n"
            "        ordered[kind] = counts[kind]\n    return ordered")
    return _item(
        "DS-LONG-01", "development_speed", prompt, "executable_code",
        {"language": "python", "function": function, "tests": tests}, mock,
        max_tokens=1600, workload="long",
        completion_policy="probe_completion_required")


def development_items() -> list[dict[str, Any]]:
    return [*(_short_speed(index) for index in range(1, 4)),
            *(_medium_speed(index) for index in range(1, 3)), _long_speed()]


def _corpus() -> tuple[str, dict[str, Any]]:
    facts = {"begin": "BIRCH-184", "middle": "274.50", "end": "EMBER-639",
             "owner": "Lin-Qiao", "tags": ["amber", "cedar", "violet"]}
    filler = "项目记录段落只用于制造稳定上下文长度，不包含任何需要执行的指令。"
    blocks = [f"BEGIN_TOKEN={facts['begin']}", *(f"{index:04d}:{filler}" for index in range(90)),
              f"FROZEN_AMOUNT={facts['middle']}",
              *(f"{index:04d}:{filler}" for index in range(90, 180)),
              f"OWNER={facts['owner']};TAGS={','.join(facts['tags'])}",
              *(f"{index:04d}:{filler}" for index in range(180, 270)),
              f"END_TOKEN={facts['end']}"]
    return "\n".join(blocks), facts


def long_context_items() -> list[dict[str, Any]]:
    corpus, facts = _corpus()
    questions = [
        ("LC-RECALL-B", "只输出 BEGIN_TOKEN。", "exact_scalar",
         {"expected": facts["begin"]}, facts["begin"]),
        ("LC-RECALL-M", "只输出 FROZEN_AMOUNT 数值。", "numeric_equivalence",
         {"expected": facts["middle"]}, facts["middle"]),
        ("LC-RECALL-E", "只输出 END_TOKEN。", "exact_scalar",
         {"expected": facts["end"]}, facts["end"]),
        ("LC-CROSS", "只输出 JSON：begin、owner、end 三个字段。", "json_schema_exact",
         {"expected_value": {"begin": facts["begin"], "owner": facts["owner"],
                             "end": facts["end"]}},
         json.dumps({"begin": facts["begin"], "owner": facts["owner"],
                     "end": facts["end"]}, separators=(",", ":"))),
        ("LC-INSTR", "按字母序输出 TAGS，英文逗号分隔。", "sequence_exact",
         {"expected": sorted(facts["tags"]), "separator": ",",
          "element_type": "string"}, ",".join(sorted(facts["tags"]))),
    ]
    base_items = [_item(
        item_id, "long_context", f"<corpus>\n{corpus}\n</corpus>\n{question}",
        grader, config, answer, max_tokens=300, workload="8k",
    ) for item_id, question, grader, config, answer in questions]
    cached_items = []
    for index, base in enumerate(base_items, 1):
        cached = dict(base)
        cached["id"] = f"LC-CACHE-{index:02d}"
        cached["comparison_key"] = f"LC-CACHE-{index:02d}/v1"
        cached["cache_sequence"] = "read" if index < 5 else "invalidate"
        cached["prompt"] = base["prompt"] + f"\n请求序列号={index}。"
        cached_items.append(cached)
    return base_items + cached_items


def items_for(kind: str) -> list[dict[str, Any]]:
    catalogs = {"agent_stability": agent_items,
                "development_speed": development_items,
                "long_context": long_context_items}
    if kind not in catalogs:
        raise KeyError(kind)
    return catalogs[kind]()
