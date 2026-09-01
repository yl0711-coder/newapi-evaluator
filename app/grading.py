"""Deterministic, registered graders for evaluation item instances."""
from __future__ import annotations

import ast
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import heapq
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable

GRADER_VERSION = "1.0.0"
GRADE_STATUSES = {
    "passed", "failed", "partial", "not_graded", "not_applicable", "grader_error",
}


def _check(check_id: str, passed: bool, *, required: bool = True,
           expected: Any = None, actual: Any = None,
           failure_code: str = "value_mismatch") -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": check_id, "required": required,
        "status": "passed" if passed else "failed",
    }
    if expected is not None:
        value["expected"] = expected
    if actual is not None:
        value["actual"] = actual
    if not passed:
        value["failure_code"] = failure_code
    return value


def _grade_result(checks: list[dict[str, Any]], normalized_answer: Any = None,
                  *, partial_allowed: bool = False) -> dict[str, Any]:
    required_failed = any(c["required"] and c["status"] == "failed" for c in checks)
    passed_weight = sum(1 for c in checks if c["status"] == "passed")
    score = passed_weight / len(checks) if checks else 0.0
    if not required_failed:
        status, score = "passed", 1.0
    elif partial_allowed and score > 0:
        status = "partial"
    else:
        status = "failed"
    failure_codes = sorted({
        str(c["failure_code"]) for c in checks if c["status"] == "failed"
        and c.get("failure_code")
    })
    return {
        "status": status, "score": round(score, 6),
        "normalized_answer": normalized_answer,
        "checks": checks, "failure_codes": failure_codes,
    }


def _non_grade(status: str, failure_code: str, detail: str = "") -> dict[str, Any]:
    result = {
        "status": status, "score": None, "normalized_answer": None,
        "checks": [], "failure_codes": [failure_code],
    }
    if detail:
        result["detail"] = detail
    return result


def _whole_text(observation: dict[str, Any]) -> str:
    return str(observation.get("text") or "").strip()


def _json_value(text: str) -> Any:
    return json.loads(text)


def _type_matches(value: Any, expected: str) -> bool:
    rules: dict[str, Callable[[Any], bool]] = {
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "null": lambda v: v is None,
    }
    return expected in rules and rules[expected](value)


def _exact_scalar(config: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    expected = config.get("expected")
    raw = _whole_text(observation)
    if isinstance(expected, bool):
        matched = raw == ("true" if expected else "false")
        normalized: Any = raw == "true" if raw in {"true", "false"} else raw
    elif isinstance(expected, int):
        matched = bool(re.fullmatch(r"-?\d+", raw)) and int(raw) == expected
        normalized = int(raw) if re.fullmatch(r"-?\d+", raw) else raw
    else:
        candidate = raw.casefold() if config.get("casefold") else raw
        wanted = str(expected).casefold() if config.get("casefold") else str(expected)
        matched = candidate == wanted
        normalized = raw
    failure = "extra_text_not_allowed" \
        if str(expected) in raw and raw != str(expected) else "value_mismatch"
    return _grade_result([
        _check("scalar.exact", matched, expected=expected, actual=normalized,
               failure_code=failure)
    ], normalized)


_LATEX_FRACTION = re.compile(r"^\\frac\{(-?\d+)\}\{(-?\d+)\}$")


def _fraction(value: Any) -> Fraction:
    if isinstance(value, int):
        return Fraction(value)
    raw = str(value).strip()
    latex = _LATEX_FRACTION.fullmatch(raw)
    if latex:
        return Fraction(int(latex.group(1)), int(latex.group(2)))
    if raw.endswith("%"):
        return Fraction(Decimal(raw[:-1])) / 100
    if "/" in raw and re.fullmatch(r"-?\d+/-?\d+", raw):
        numerator, denominator = raw.split("/", 1)
        return Fraction(int(numerator), int(denominator))
    return Fraction(Decimal(raw))


def _numeric_equivalence(config: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    raw = _whole_text(observation)
    try:
        actual = _fraction(raw)
        expected = _fraction(config["expected"])
    except (InvalidOperation, ValueError, ZeroDivisionError):
        return _grade_result([
            _check("numeric.parse", False, actual=raw, failure_code="parse_error")
        ], raw)
    tolerance = _fraction(config.get("absolute_tolerance", 0))
    matched = abs(actual - expected) <= tolerance
    return _grade_result([
        _check("numeric.equivalent", matched, expected=str(expected), actual=str(actual))
    ], str(actual))


def _sequence_exact(config: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    raw = _whole_text(observation)
    separator = str(config.get("separator", ","))
    element_type = config.get("element_type", "integer")
    parts = [part.strip() for part in raw.split(separator)] if raw else []
    try:
        actual = [int(part) for part in parts] if element_type == "integer" else parts
        parsed = all(part != "" for part in parts)
    except ValueError:
        actual, parsed = parts, False
    expected = list(config.get("expected") or [])
    checks = [
        _check("sequence.parse", parsed, failure_code="parse_error"),
        _check("sequence.length", len(actual) == len(expected),
               expected=len(expected), actual=len(actual), failure_code="sequence_mismatch"),
        _check("sequence.values", actual == expected, expected=expected, actual=actual,
               failure_code="sequence_mismatch"),
    ]
    return _grade_result(checks, actual)


def _set_equivalence(config: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    raw = _whole_text(observation)
    separator = str(config.get("separator", ","))
    actual = [part.strip() for part in raw.split(separator) if part.strip()]
    expected = list(config.get("expected") or [])
    matched = set(actual) == set(expected) and (
        config.get("allow_duplicates", False) or len(actual) == len(set(actual)))
    return _grade_result([
        _check("set.equivalent", matched, expected=sorted(expected), actual=sorted(actual),
               failure_code="set_mismatch")
    ], actual)


def _json_schema_exact(config: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    raw = _whole_text(observation)
    try:
        value = _json_value(raw)
    except (json.JSONDecodeError, TypeError):
        return _grade_result([
            _check("json.parse", False, actual=raw, failure_code="parse_error")
        ], raw)
    checks = [_check("json.parse", True)]
    if "expected_value" in config:
        checks.append(_check("json.value", value == config["expected_value"],
                             expected=config["expected_value"], actual=value))
        return _grade_result(checks, value)
    root_ok = isinstance(value, dict)
    checks.append(_check("json.root", root_ok, expected="object",
                         actual=type(value).__name__, failure_code="schema_mismatch"))
    if not root_ok:
        return _grade_result(checks, value)
    required = dict(config.get("required") or {})
    for key, value_type in required.items():
        checks.append(_check(f"json.required.{key}", key in value,
                             failure_code="schema_mismatch"))
        if key in value:
            checks.append(_check(f"json.type.{key}", _type_matches(value[key], value_type),
                                 expected=value_type, actual=type(value[key]).__name__,
                                 failure_code="schema_mismatch"))
    expected_values = dict(config.get("expected") or {})
    for key, expected in expected_values.items():
        checks.append(_check(f"json.value.{key}", value.get(key) == expected,
                             expected=expected, actual=value.get(key)))
    allowed = set(required) | set(expected_values)
    if config.get("additional_properties") is False:
        extras = sorted(set(value) - allowed)
        checks.append(_check("json.extra_fields", not extras, expected=[], actual=extras,
                             failure_code="extra_field"))
    return _grade_result(checks, value)


def _tool_call_exact(config: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    calls = list(observation.get("tool_calls") or [])
    expected_count = int(config.get("count", 1))
    checks = [_check("tool.count", len(calls) == expected_count,
                     expected=expected_count, actual=len(calls),
                     failure_code="tool_count_mismatch")]
    if len(calls) != expected_count:
        return _grade_result(checks, calls)
    call = calls[0]
    name = call.get("name")
    args = call.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = None
    checks.append(_check("tool.name", name == config.get("name"),
                         expected=config.get("name"), actual=name,
                         failure_code="tool_name_mismatch"))
    expected_args = dict(config.get("arguments") or {})
    checks.append(_check("tool.arguments.object", isinstance(args, dict),
                         expected="object", actual=type(args).__name__,
                         failure_code="argument_mismatch"))
    if isinstance(args, dict):
        checks.append(_check("tool.arguments.keys", set(args) == set(expected_args),
                             expected=sorted(expected_args), actual=sorted(args),
                             failure_code="argument_mismatch"))
        for key, expected in expected_args.items():
            checks.append(_check(f"tool.arguments.value.{key}", args.get(key) == expected,
                                 expected=expected, actual=args.get(key),
                                 failure_code="argument_mismatch"))
        for key, expected_type in dict(config.get("argument_types") or {}).items():
            checks.append(_check(f"tool.arguments.type.{key}",
                                 key in args and _type_matches(args[key], expected_type),
                                 expected=expected_type,
                                 actual=type(args.get(key)).__name__,
                                 failure_code="argument_mismatch"))
    return _grade_result(checks, calls)


def _numbered_line_records(config: dict[str, Any], text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lines = text.splitlines()
    pattern = re.compile(str(config["line_pattern"]))
    rows: list[dict[str, Any]] = []
    parse_ok = True
    for line in lines:
        matched = pattern.fullmatch(line)
        if not matched:
            parse_ok = False
            continue
        row: dict[str, Any] = dict(matched.groupdict())
        for key, value in list(row.items()):
            if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
                row[key] = int(value)
        rows.append(row)
    checks = [
        _check("constraints.line_count", len(lines) == int(config["line_count"]),
               expected=int(config["line_count"]), actual=len(lines),
               failure_code="constraint_violation"),
        _check("constraints.line_format", parse_ok and len(rows) == len(lines),
               failure_code="constraint_violation"),
    ]
    if not parse_ok or len(rows) != len(lines):
        return rows, checks
    if "index_sequence" in config:
        actual = [row["index"] for row in rows]
        checks.append(_check("constraints.index_sequence", actual == config["index_sequence"],
                             expected=config["index_sequence"], actual=actual,
                             failure_code="constraint_violation"))
    field = config.get("strictly_increasing")
    if field:
        values = [row[field] for row in rows]
        checks.append(_check("constraints.strictly_increasing",
                             all(left < right for left, right in zip(values, values[1:])),
                             actual=values, failure_code="constraint_violation"))
    if config.get("sum"):
        rule = config["sum"]
        actual = sum(row[rule["field"]] for row in rows)
        checks.append(_check("constraints.sum", actual == rule["expected"],
                             expected=rule["expected"], actual=actual,
                             failure_code="constraint_violation"))
    if config.get("relation"):
        rule = config["relation"]
        left = rows[int(rule["left_index"])][rule["field"]]
        right = rows[int(rule["right_index"])][rule["field"]] * rule["multiplier"]
        checks.append(_check("constraints.relation", left == right,
                             expected=right, actual=left,
                             failure_code="constraint_violation"))
    if config.get("set"):
        rule = config["set"]
        actual = [row[rule["field"]] for row in rows]
        checks.append(_check("constraints.set", set(actual) == set(rule["expected"])
                             and len(actual) == len(set(actual)),
                             expected=sorted(rule["expected"]), actual=sorted(actual),
                             failure_code="constraint_violation"))
    for index, rule in enumerate(config.get("fixed") or []):
        actual = rows[int(rule["index"])][rule["field"]]
        checks.append(_check(f"constraints.fixed.{index}", actual == rule["expected"],
                             expected=rule["expected"], actual=actual,
                             failure_code="constraint_violation"))
    return rows, checks


def _constraint_set(config: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    validator = config.get("validator")
    if validator != "numbered_line_records":
        raise ValueError(f"unknown constraint validator: {validator}")
    rows, checks = _numbered_line_records(config, _whole_text(observation))
    return _grade_result(checks, rows, partial_allowed=bool(config.get("partial_allowed")))


def _graph_solution(config: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    raw = _whole_text(observation)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return _grade_result([
            _check("graph.parse", False, actual=raw, failure_code="parse_error")
        ], raw)
    path = value.get("path") if isinstance(value, dict) else None
    declared_cost = value.get("cost") if isinstance(value, dict) else None
    edges = {(str(a), str(b)): int(cost) for a, b, cost in config.get("edges") or []}
    path_valid = isinstance(path, list) and len(path) >= 2 \
        and path[0] == config["start"] and path[-1] == config["target"] \
        and all((str(a), str(b)) in edges for a, b in zip(path, path[1:]))
    actual_cost = sum(edges[(str(a), str(b))] for a, b in zip(path, path[1:])) \
        if path_valid else None
    adjacency: dict[str, list[tuple[int, str]]] = {}
    for (start, end), cost in edges.items():
        adjacency.setdefault(start, []).append((cost, end))
    queue: list[tuple[int, str]] = [(0, str(config["start"]))]
    best: dict[str, int] = {str(config["start"]): 0}
    while queue:
        cost, node = heapq.heappop(queue)
        if cost != best[node]:
            continue
        for edge_cost, target in adjacency.get(node, []):
            candidate = cost + edge_cost
            if candidate < best.get(target, 2 ** 63):
                best[target] = candidate
                heapq.heappush(queue, (candidate, target))
    optimal = best.get(str(config["target"]))
    checks = [
        _check("graph.path", path_valid, failure_code="sequence_mismatch"),
        _check("graph.declared_cost", declared_cost == actual_cost,
               expected=actual_cost, actual=declared_cost),
        _check("graph.optimal", actual_cost == optimal,
               expected=optimal, actual=actual_cost, failure_code="non_optimal_solution"),
    ]
    if "expected_cost" in config:
        checks.append(_check("graph.oracle_cost", optimal == config["expected_cost"],
                             expected=config["expected_cost"], actual=optimal,
                             failure_code="oracle_error"))
    return _grade_result(checks, value)


_ALLOWED_AST_NODES = {
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
    ast.AnnAssign, ast.AugAssign, ast.For, ast.If, ast.Compare, ast.BoolOp,
    ast.BinOp, ast.UnaryOp, ast.Name, ast.Load, ast.Store, ast.Constant,
    ast.List, ast.Tuple, ast.Dict, ast.Subscript, ast.Slice, ast.Call, ast.keyword,
    ast.Expr, ast.Break, ast.Continue, ast.IfExp, ast.ListComp, ast.GeneratorExp,
    ast.comprehension, ast.Lambda,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.Eq, ast.NotEq, ast.Lt,
    ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot, ast.Attribute,
}
_ALLOWED_CALLS = {"sorted", "max", "min", "len", "range", "enumerate", "zip", "list", "tuple"}
_ALLOWED_METHODS = {"append", "extend", "insert", "sort", "copy", "get"}
_PYTHON_EXECUTION_TIMEOUT_SECONDS = 5
_PYTHON_EXECUTION_ATTEMPTS = 2
_PYTHON_EXECUTION_MEMORY_BYTES = 256 * 1024 * 1024


def _safe_python(code: str, function_name: str) -> tuple[bool, str]:
    if len(code.encode("utf-8")) > 16_384:
        return False, "source_size"
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"syntax:{exc.msg}"
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if len(functions) != 1 or functions[0].name != function_name:
        return False, "function_definition"
    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_AST_NODES:
            return False, type(node).__name__
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return False, "dunder_name"
        if isinstance(node, ast.Constant) and isinstance(node.value, int) \
                and abs(node.value) > 100_000:
            return False, "integer_limit"
        if isinstance(node, ast.Attribute) and node.attr not in _ALLOWED_METHODS:
            return False, f"attribute:{node.attr}"
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id not in _ALLOWED_CALLS:
                return False, f"call:{node.func.id}"
            if isinstance(node.func, ast.Attribute) and node.func.attr not in _ALLOWED_METHODS:
                return False, f"method:{node.func.attr}"
    return True, ""


_PYTHON_TEST_RUNNER = r"""
import copy
import ctypes
import json
import os
import sys

memory_limit = int(os.environ["GRADER_MEMORY_LIMIT_BYTES"])
try:
    if sys.platform == "win32":
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("read_operations", ctypes.c_ulonglong),
                ("write_operations", ctypes.c_ulonglong),
                ("other_operations", ctypes.c_ulonglong),
                ("read_bytes", ctypes.c_ulonglong),
                ("write_bytes", ctypes.c_ulonglong),
                ("other_bytes", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("per_process_time", ctypes.c_longlong),
                ("per_job_time", ctypes.c_longlong),
                ("limit_flags", wintypes.DWORD),
                ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t),
                ("active_process_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority_class", wintypes.DWORD),
                ("scheduling_class", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimitInformation),
                ("io", IoCounters),
                ("process_memory_limit", ctypes.c_size_t),
                ("job_memory_limit", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE

        job_handle = kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimitInformation()
        limits.basic.limit_flags = 0x100 | 0x2000
        limits.process_memory_limit = memory_limit
        if not kernel32.SetInformationJobObject(
                job_handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.AssignProcessToJobObject(job_handle, kernel32.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
except Exception:
    sys.stderr.write("sandbox_setup_failed")
    raise SystemExit(86)

payload = json.loads(sys.stdin.read())
namespace = {}
exec(compile(payload["code"], "submission.py", "exec"), {"__builtins__": {
    "sorted": sorted, "max": max, "min": min, "len": len, "range": range,
    "enumerate": enumerate, "zip": zip, "list": list, "tuple": tuple,
}}, namespace)
function = namespace[payload["function"]]
results = []
for case in payload["tests"]:
    args = copy.deepcopy(case["args"])
    before = copy.deepcopy(args)
    try:
        actual = function(*args)
        passed = actual == case["expected"]
        if case.get("input_immutable"):
            passed = passed and args == before
        if passed:
            results.append({"passed": True})
        elif sys.getsizeof(actual) > 65536:
            results.append({"passed": False, "error": "OutputLimitExceeded"})
        else:
            results.append({"passed": False, "actual": actual})
    except Exception as exc:
        results.append({"passed": False, "error": type(exc).__name__})
sys.stdout.write(json.dumps(results, ensure_ascii=False, separators=(",", ":")))
"""


def _extract_python(text: str) -> tuple[str, bool]:
    blocks = re.findall(r"```(?:python)?\s*\n(.*?)```", text, flags=re.I | re.S)
    if blocks:
        return (blocks[0].strip(), len(blocks) == 1)
    return text.strip(), True


def _executable_code(config: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    if config.get("language") != "python":
        raise ValueError("only registered Python execution is available")
    code, single = _extract_python(_whole_text(observation))
    function_name = str(config["function"])
    safe, rejection = _safe_python(code, function_name)
    checks = [
        _check("code.single_submission", single, failure_code="code_extraction_failed"),
        _check("code.static_policy", safe, actual=rejection or "allowed",
               failure_code="static_policy_violation"),
    ]
    if not single or not safe:
        return _grade_result(checks, {"code": code})
    payload = json.dumps({
        "code": code, "function": function_name,
        "tests": list(config.get("tests") or []),
    }, ensure_ascii=False)
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["GRADER_MEMORY_LIMIT_BYTES"] = str(_PYTHON_EXECUTION_MEMORY_BYTES)
    for _ in range(_PYTHON_EXECUTION_ATTEMPTS):
        try:
            with tempfile.TemporaryDirectory(prefix="grader-") as directory:
                completed = subprocess.run(
                    [sys.executable, "-I", "-S", "-c", _PYTHON_TEST_RUNNER],
                    input=payload, text=True, capture_output=True,
                    timeout=_PYTHON_EXECUTION_TIMEOUT_SECONDS,
                    cwd=directory, env=environment, check=False,
                )
            break
        except subprocess.TimeoutExpired:
            continue
    else:
        checks.append(_check("code.resources", False,
                             failure_code="resource_limit_exceeded"))
        return _grade_result(checks, {"code": code})
    if completed.returncode == 86 and "sandbox_setup_failed" in completed.stderr:
        raise RuntimeError("grader sandbox unavailable")
    checks.append(_check("code.exit", completed.returncode == 0,
                         expected=0, actual=completed.returncode,
                         failure_code="runtime_error"))
    if completed.returncode != 0 or len(completed.stdout) > 65536:
        return _grade_result(checks, {"code": code})
    try:
        results = json.loads(completed.stdout)
    except json.JSONDecodeError:
        checks.append(_check("code.results", False, failure_code="runtime_error"))
        return _grade_result(checks, {"code": code})
    if any(result.get("error") in {"MemoryError", "OutputLimitExceeded"}
           for result in results):
        checks.append(_check("code.resources", False,
                             failure_code="resource_limit_exceeded"))
        return _grade_result(checks, {"code": code})
    for index, result in enumerate(results):
        checks.append(_check(f"code.test.{index}", bool(result.get("passed")),
                             actual=result.get("actual", result.get("error")),
                             failure_code="hidden_test_failed"))
    return _grade_result(checks, {"code": code, "tests": len(results)})


def _program_oracle(config: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    """Compare against an explicitly materialized deterministic oracle value."""
    comparator = str(config.get("comparator") or "")
    if comparator in {"", "program_oracle", "composite"} or comparator not in _GRADERS:
        raise ValueError("invalid oracle comparator")
    comparator_config = dict(config.get("comparator_config") or {})
    comparator_config["expected"] = config.get("expected")
    if comparator == "json_schema_exact":
        comparator_config.pop("expected", None)
        comparator_config["expected_value"] = config.get("expected")
    result = _GRADERS[comparator](comparator_config, observation)
    result.setdefault("diagnostics", {})["oracle"] = {
        "id": str(config.get("oracle_id") or "materialized"),
        "version": str(config.get("oracle_version") or "1.0.0"),
    }
    return result


def _workflow_state_machine(
    config: dict[str, Any], observation: dict[str, Any],
) -> dict[str, Any]:
    raw = _whole_text(observation)
    try:
        events = json.loads(raw)
    except json.JSONDecodeError:
        return _grade_result([
            _check("workflow.parse", False, actual=raw, failure_code="parse_error")
        ], raw)
    checks = [_check("workflow.parse", isinstance(events, list),
                     expected="array", actual=type(events).__name__,
                     failure_code="schema_mismatch")]
    if not isinstance(events, list):
        return _grade_result(checks, events)
    state = str(config["initial_state"])
    states = dict(config.get("states") or {})
    terminal = str(config["terminal_state"])
    terminal_seen = False
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            checks.append(_check(f"workflow.{index}.event", False,
                                 failure_code="schema_mismatch"))
            continue
        action = str(event.get("action") or "")
        state_spec = dict(states.get(state) or {})
        transitions = dict(state_spec.get("transitions") or {})
        transition = dict(transitions.get(action) or {})
        legal = bool(transition) and not terminal_seen
        checks.append(_check(f"workflow.{index}.legal_action", legal,
                             expected=sorted(transitions), actual=action,
                             failure_code="illegal_transition"))
        if not legal:
            continue
        arguments = event.get("arguments") or {}
        expected_arguments = dict(transition.get("arguments") or {})
        arguments_valid = isinstance(arguments, dict) and all(
            arguments.get(key) == value for key, value in expected_arguments.items())
        checks.append(_check(f"workflow.{index}.arguments_valid", arguments_valid,
                             expected=expected_arguments, actual=arguments,
                             failure_code="argument_mismatch"))
        grounded_fields = dict(transition.get("grounded_fields") or {})
        grounded = isinstance(arguments, dict) and all(
            arguments.get(key) == value for key, value in grounded_fields.items())
        checks.append(_check(f"workflow.{index}.grounded", grounded,
                             expected=grounded_fields, actual=arguments,
                             failure_code="ungrounded_claim"))
        safe = action not in set(state_spec.get("forbidden") or [])
        checks.append(_check(f"workflow.{index}.side_effect_safe", safe,
                             actual=action, failure_code="confirmation_bypassed"))
        state = str(transition["next_state"])
        terminal_seen = state == terminal
        checks.append(_check(f"workflow.{index}.transitioned", True,
                             actual=state))
    checks.append(_check("workflow.terminal", state == terminal,
                         expected=terminal, actual=state,
                         failure_code="workflow_incomplete"))
    return _grade_result(checks, {"events": events, "final_state": state})


def _composite(config: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    children = list(config.get("children") or [])
    if not children:
        raise ValueError("composite requires children")
    checks: list[dict[str, Any]] = []
    normalized: dict[str, Any] = {}
    required_failed = False
    for index, child in enumerate(children):
        grader_id = str(child.get("id") or "")
        if grader_id in {"", "composite"} or grader_id not in _GRADERS:
            raise ValueError("invalid composite child")
        child_result = _GRADERS[grader_id](dict(child.get("config") or {}), observation)
        child_required = bool(child.get("required", True))
        child_passed = child_result.get("status") == "passed"
        checks.append(_check(f"composite.{index}.{grader_id}", child_passed,
                             required=child_required,
                             actual=child_result.get("status"),
                             failure_code=(child_result.get("failure_codes") or
                                           ["value_mismatch"])[0]))
        normalized[str(index)] = child_result.get("normalized_answer")
        required_failed = required_failed or (child_required and not child_passed)
        if required_failed and bool(config.get("short_circuit")):
            break
    return _grade_result(checks, normalized,
                         partial_allowed=bool(config.get("partial_allowed")))


_GRADERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
    "exact_scalar": _exact_scalar,
    "numeric_equivalence": _numeric_equivalence,
    "sequence_exact": _sequence_exact,
    "set_equivalence": _set_equivalence,
    "json_schema_exact": _json_schema_exact,
    "tool_call_exact": _tool_call_exact,
    "constraint_set": _constraint_set,
    "graph_solution": _graph_solution,
    "executable_code": _executable_code,
    "program_oracle": _program_oracle,
    "workflow_state_machine": _workflow_state_machine,
    "composite": _composite,
}


def registered_graders() -> tuple[str, ...]:
    return tuple(sorted(_GRADERS))


def grade(item_instance: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    grader_spec = dict(item_instance.get("grader") or {})
    grader_id = str(grader_spec.get("id") or "")
    version = str(grader_spec.get("version") or GRADER_VERSION)
    completion_status = str(observation.get("completion_status") or "empty")
    completion_policy = str(item_instance.get("completion_policy") or "answer_sufficient")
    if completion_status == "completed_with_truncation" \
            and completion_policy in {"whole_response_required", "workflow_terminal_required"}:
        result = _non_grade("not_graded", "response_incomplete")
    elif completion_status not in {"completed", "completed_with_truncation"}:
        result = _non_grade("not_graded", "response_missing")
    elif grader_id not in _GRADERS:
        result = _non_grade("grader_error", "grader_config_error",
                            f"unknown grader: {grader_id}")
    else:
        try:
            result = _GRADERS[grader_id](dict(grader_spec.get("config") or {}), observation)
        except Exception as exc:
            result = _non_grade("grader_error", "grader_config_error", type(exc).__name__)
    result["grader"] = {"id": grader_id, "version": version}
    return result
