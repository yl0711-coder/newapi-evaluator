"""可复用判分器与证据资格自测，不访问网络。"""
import sys

from app import evidence, grading, test_catalog


failures: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        failures.append(name)


def item(grader_id: str, config: dict, **extra: object) -> dict:
    return {
        "id": "selftest", "version": 1,
        "completion_policy": "whole_response_required",
        "grader": {"id": grader_id, "version": "1.0.0", "config": config},
        **extra,
    }


def observation(text: str = "", **extra: object) -> dict:
    return {
        "completion_status": "completed", "text": text,
        "tool_calls": [], "finish_reason": "stop", **extra,
    }


strict = grading.grade(item("exact_scalar", {"expected": "READY-7"}),
                       observation("READY-7"))
strict_extra = grading.grade(item("exact_scalar", {"expected": "READY-7"}),
                             observation("答案：READY-7"))
check("严格标量正确", strict["status"] == "passed", strict)
check("严格标量拒绝额外文字",
      strict_extra["status"] == "failed"
      and "extra_text_not_allowed" in strict_extra["failure_codes"], strict_extra)

fraction = grading.grade(item("numeric_equivalence", {"expected": "16/3"}),
                         observation(r"\frac{16}{3}"))
check("LaTeX 分数等价", fraction["status"] == "passed", fraction)

json_ok = grading.grade(item("json_schema_exact", {
    "required": {"r": "string", "g": "integer"},
    "expected": {"r": "abc", "g": 6}, "additional_properties": False,
}), observation('{"g":6,"r":"abc"}'))
json_extra = grading.grade(item("json_schema_exact", {
    "required": {"r": "string", "g": "integer"},
    "expected": {"r": "abc", "g": 6}, "additional_properties": False,
}), observation('{"g":6,"r":"abc","note":"x"}'))
check("JSON 忽略键顺序", json_ok["status"] == "passed", json_ok)
check("JSON 拒绝额外字段",
      json_extra["status"] == "failed"
      and "extra_field" in json_extra["failure_codes"], json_extra)

tool_ok = grading.grade(item("tool_call_exact", {
    "count": 1, "name": "get_order", "arguments": {"order_id": "NX-2048"},
    "argument_types": {"order_id": "string"},
}), observation(tool_calls=[{"name": "get_order", "args": {"order_id": "NX-2048"}}]))
tool_extra = grading.grade(item("tool_call_exact", {
    "count": 1, "name": "get_order", "arguments": {"order_id": "NX-2048"},
}), observation(tool_calls=[
    {"name": "get_order", "args": {"order_id": "NX-2048"}},
    {"name": "send_email", "args": {}},
]))
check("工具调用名称与参数正确", tool_ok["status"] == "passed", tool_ok)
check("工具调用拒绝额外副作用",
      tool_extra["status"] == "failed"
      and "tool_count_mismatch" in tool_extra["failure_codes"], tool_extra)

constraints = grading.grade(item("constraint_set", {
    "validator": "numbered_line_records",
    "line_pattern": r"K(?P<index>\d+)=(?P<value>\d+);tag=(?P<tag>[a-z]+)",
    "line_count": 4,
    "index_sequence": [1, 2, 3, 4],
    "strictly_increasing": "value",
    "sum": {"field": "value", "expected": 30},
    "relation": {"left_index": 3, "right_index": 0, "field": "value", "multiplier": 2},
    "set": {"field": "tag", "expected": ["amber", "lime", "navy", "rose"]},
    "fixed": [{"index": 1, "field": "tag", "expected": "lime"},
              {"index": 3, "field": "tag", "expected": "rose"}],
}), observation("K1=5;tag=amber\nK2=6;tag=lime\nK3=9;tag=navy\nK4=10;tag=rose"))
check("多条件指令机械验证", constraints["status"] == "passed", constraints)

graph = grading.grade(item("graph_solution", {
    "edges": [["S", "A", 4], ["S", "B", 2], ["B", "A", 1],
              ["A", "C", 3], ["B", "C", 7], ["C", "T", 2],
              ["A", "T", 8], ["B", "T", 12]],
    "start": "S", "target": "T", "expected_cost": 8,
}), observation('{"cost":8,"path":["S","B","A","C","T"]}'))
check("图路径验证边与最优成本", graph["status"] == "passed", graph)

code_item = item("executable_code", {
    "language": "python", "function": "normalize_windows",
    "tests": [
        {"args": [[]], "expected": []},
        {"args": [[[5, 8], [1, 3], [3, 4], [7, 10]]],
         "expected": [[1, 4], [5, 10]], "input_immutable": True},
        {"args": [[[1, 10], [2, 3], [10, 12]]], "expected": [[1, 12]]},
    ],
})
code = """def normalize_windows(windows):
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged"""
code_grade = grading.grade(code_item, observation(code))
check("受限代码执行通过隐藏测试", code_grade["status"] == "passed", code_grade)

lambda_code = """def normalize_windows(windows):
    merged = []
    for start, end in sorted(windows, key=lambda window: window[0]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged"""
lambda_grade = grading.grade(code_item, observation(lambda_code))
check("受限代码允许安全的排序键 lambda",
      lambda_grade["status"] == "passed", lambda_grade)

generator_code = """def normalize_windows(windows):
    merged = []
    for start, end in sorted((start, end) for start, end in windows):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged"""
generator_grade = grading.grade(code_item, observation(generator_code))
check("受限代码允许安全的生成器表达式",
      generator_grade["status"] == "passed", generator_grade)

memory_exhaustion_code = """def normalize_windows(windows):
    return [0] * (10_000 * 10_000)"""
memory_exhaustion_grade = grading.grade(
    code_item, observation(memory_exhaustion_code))
check("受限代码超过 256 MiB 时由判分进程终止",
      memory_exhaustion_grade["status"] == "failed"
      and "resource_limit_exceeded" in memory_exhaustion_grade["failure_codes"],
      memory_exhaustion_grade)

original_subprocess_run = grading.subprocess.run
execution_attempts = [0]


def timeout_once(*args: object, **kwargs: object):
    execution_attempts[0] += 1
    if execution_attempts[0] == 1:
        raise grading.subprocess.TimeoutExpired(cmd="grader", timeout=5)
    return original_subprocess_run(*args, **kwargs)


grading.subprocess.run = timeout_once
try:
    retry_grade = grading.grade(code_item, observation(code))
finally:
    grading.subprocess.run = original_subprocess_run
check("受限代码执行超时后复核一次",
      retry_grade["status"] == "passed" and execution_attempts[0] == 2,
      {"attempts": execution_attempts[0], "grade": retry_grade})

unsafe_lambda_code = """def normalize_windows(windows):
    return sorted(windows, key=lambda window: open('forbidden.txt', 'w'))"""
unsafe_lambda_grade = grading.grade(code_item, observation(unsafe_lambda_code))
check("lambda 仍受函数调用白名单限制",
      unsafe_lambda_grade["status"] == "failed"
      and "static_policy_violation" in unsafe_lambda_grade["failure_codes"],
      unsafe_lambda_grade)

code_trace_item = next(candidate for candidate in test_catalog.admission_items()
                       if candidate["id"] == "QA-CODE-01")
check("代码追踪题强制使用 JSON 模式",
      code_trace_item.get("json_mode") is True, code_trace_item)

oracle = grading.grade(item("program_oracle", {
    "oracle_id": "sum_oracle", "oracle_version": "1.0.0",
    "expected": 45, "comparator": "exact_scalar",
}), observation("45"))
check("程序 Oracle 与比较器分离", oracle["status"] == "passed", oracle)

workflow_config = {
    "initial_state": "lookup", "terminal_state": "completed", "states": {
        "lookup": {"forbidden": ["tool:refund"], "transitions": {
            "tool:get": {"arguments": {"id": "O-1"}, "next_state": "confirm"}}},
        "confirm": {"forbidden": ["tool:refund"], "transitions": {
            "tool:ask": {"arguments": {"id": "O-1"}, "next_state": "refund"}}},
        "refund": {"transitions": {"tool:refund": {
            "arguments": {"id": "O-1"}, "grounded_fields": {"id": "O-1"},
            "next_state": "completed"}}},
        "completed": {"transitions": {}},
    },
}
workflow_events = ('[{"action":"tool:get","arguments":{"id":"O-1"}},'
                   '{"action":"tool:ask","arguments":{"id":"O-1"}},'
                   '{"action":"tool:refund","arguments":{"id":"O-1"}}]')
workflow = grading.grade(item("workflow_state_machine", workflow_config,
                              completion_policy="workflow_terminal_required"),
                         observation(workflow_events))
check("工作流逐事件到达终态", workflow["status"] == "passed", workflow)

composite = grading.grade(item("composite", {"children": [
    {"id": "json_schema_exact", "required": True,
     "config": {"required": {"status": "string"},
                "expected": {"status": "ok"}, "additional_properties": False}},
    {"id": "json_schema_exact", "required": True,
     "config": {"expected_value": {"status": "ok"}}},
]}), observation('{"status":"ok"}'))
check("组合判分器按声明组合", composite["status"] == "passed", composite)

truncated = grading.grade(item("exact_scalar", {"expected": 45}),
                          observation("45", completion_status="completed_with_truncation",
                                      finish_reason="length"))
check("完整输出策略下截断不形成判分",
      truncated["status"] == "not_graded" and truncated["score"] is None, truncated)

broken = grading.grade(item("missing_grader", {}), observation("x"))
check("未知判分器属于系统错误",
      broken["status"] == "grader_error" and broken["score"] is None, broken)

wrong_eligibility = evidence.assess(
    completion_status="completed", attribution="valid", grade_status="failed")
network_eligibility = evidence.assess(
    completion_status="client_interrupted", attribution="client_network_suspect",
    grade_status="not_graded")
upstream_eligibility = evidence.assess(
    completion_status="upstream_error", attribution="upstream_suspect",
    grade_status="not_graded")
check("完整错误回答仍是能力证据",
      wrong_eligibility["ability"] == "eligible"
      and wrong_eligibility["stability"] == "eligible", wrong_eligibility)
check("客户端网络不污染能力和稳定性",
      network_eligibility["ability"] == "ineligible"
      and network_eligibility["stability"] == "ineligible", network_eligibility)
check("上游错误只进入渠道稳定性",
      upstream_eligibility["ability"] == "ineligible"
      and upstream_eligibility["stability"] == "eligible", upstream_eligibility)

deterministic = [grading.grade(item("exact_scalar", {"expected": "x"}),
                               observation("x")) for _ in range(100)]
check("判分器确定性", all(value == deterministic[0] for value in deterministic),
      deterministic[-1])

print(f"通过 {len(failures) == 0}，失败项：{failures if failures else '无'}")
sys.exit(1 if failures else 0)
