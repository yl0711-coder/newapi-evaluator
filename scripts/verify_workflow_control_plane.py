"""Bounded local verification for the Eval workflow control plane."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.test_all import (GroupSpec, VerificationCancelled, _safe_environment,
                              _write_summary, cancellation_signals, classify_group,
                              run_group)
from scripts.test_manifest import SuiteSpec, workflow_control_plane
from scripts.verify_image_quality import (aggregate_status, test_artifact_root,
                                          validate_new_output)

OVERALL_BUDGET_SECONDS = 1800


def run_python_tests(modules: tuple[str, ...]) -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    collected = []
    for module in modules:
        before = len(loader.errors)
        cases = loader.loadTestsFromName(module)
        collected.append({"module": module, "case_count": cases.countTestCases(),
                          "collection_errors": len(loader.errors) - before})
        suite.addTests(cases)
    print("WORKFLOW_COLLECTION=" + json.dumps({
        "modules": collected, "registered_cases": suite.countTestCases(),
        "collection_errors": len(loader.errors),
    }), flush=True)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print("WORKFLOW_RESULT=" + json.dumps({
        "executed": result.testsRun,
        "failed": len(result.failures),
        "errors": max(0, len(result.errors) - len(loader.errors)),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "skipped": len(result.skipped),
    }), flush=True)
    return 0 if result.wasSuccessful() and not loader.errors else 1


def _marker(output: str, name: str) -> dict:
    rows = re.findall(r"(?m)^" + re.escape(name) + r"=(.*)$", output)
    if len(rows) != 1:
        raise ValueError("missing or duplicate framework evidence")
    value = json.loads(rows[0])
    if not isinstance(value, dict):
        raise ValueError("invalid framework evidence")
    return value


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid check count")
    return value


def classify_suite(spec: SuiteSpec, code: int | None, output: str,
                   *, reason: str | None = None) -> dict:
    fallback = classify_group(GroupSpec(spec.suite_id, spec.command, spec.timeout_seconds, "unittest"),
                              code, output, reason=reason)
    result = {"status": "incomplete", "executed": 0, "failed": 0, "skipped": 0,
              "registered_cases": None, "failure_count_exact": True}
    missing = bool(re.search(r"ModuleNotFoundError|ImportError|Required executable unavailable|Cannot find module|Executable doesn't exist", output))
    complete = code == 0 and not missing
    try:
        if spec.kind == "workflow-unittest":
            collection = _marker(output, "WORKFLOW_COLLECTION")
            framework = _marker(output, "WORKFLOW_RESULT")
            result.update(executed=_count(framework["executed"]),
                          failed=sum(_count(framework[key]) for key in ("failed", "errors", "unexpected_successes")),
                          skipped=_count(framework["skipped"]),
                          registered_cases=_count(collection["registered_cases"]),
                          registered_modules=collection["modules"],
                          collection_errors=_count(collection["collection_errors"]))
            rows = collection["modules"]
            complete = (complete and isinstance(rows, list)
                        and [row["module"] for row in rows] == list(spec.expected_modules)
                        and all(_count(row["case_count"]) > 0 and _count(row["collection_errors"]) == 0 for row in rows)
                        and sum(row["case_count"] for row in rows) == result["registered_cases"]
                        and result["collection_errors"] == 0
                        and result["executed"] == result["registered_cases"]
                        and fallback["framework_runs"] == [result["executed"]]
                        and result["skipped"] == 0 and fallback["status"] == "passed")
            if result["collection_errors"]:
                result["reason"] = "collection_error"
        elif spec.kind in {"syntax", "security", "browser"}:
            data = json.loads(output.strip().splitlines()[-1])
            if not isinstance(data, dict):
                raise ValueError("invalid check evidence")
            result["failed"] = _count(data.get("failure_count", 0))
            if data.get("status") == "failed" and data.get("error") == "AssertionError":
                if result["failed"] == 0:
                    result.update(failed=1, failure_count_exact=False)
            result["skipped"] = _count(data.get("skipped", 0))
            if spec.kind == "security":
                result["executed"] = _count(data["files_checked"])
                result["failed"] = max(result["failed"], len(data["findings"]), int(data["passed"] is False))
                complete = complete and data["passed"] is True
            else:
                checks = data["checks"]
                result["executed"] = _count(checks) if type(checks) is int else len(checks) if isinstance(checks, (list, str)) else 0
                result["failed"] = max(result["failed"], int(data["status"] == "failed"))
                complete = complete and data["status"] == "passed"
                if spec.kind == "browser":
                    complete = complete and _count(data["mockRequests"]) > 0
        elif spec.kind == "web":
            result["executed"] = int("UI contract tests passed:" in output)
        else:
            raise ValueError("unregistered result classifier")
    except (ValueError, KeyError, TypeError, IndexError):
        complete = False
        result["reason"] = "result_evidence_missing"
        if fallback["status"] == "failed" and (not missing or int(fallback["failed"]) > 0):
            result.update(failed=max(1, int(fallback["failed"])), failure_count_exact=False)
    if not missing or fallback["status"] == "failed" and int(fallback["failed"]) > 0:
        observed = max(0, int(fallback["failed"]) - result.get("collection_errors", 0))
        result["failed"] = max(result["failed"], observed)
    interrupted = bool(reason) or code is None or code < 0
    if interrupted:
        result["reason"] = reason or ("exit_unknown" if code is None else "child_signal")
    result["timed_out"] = reason == "timeout"
    if result["failed"]:
        result["status"] = "failed"
    elif interrupted:
        result["status"] = "incomplete"
    elif missing or code == 127:
        result.update(status="incomplete", reason="missing_dependency")
    elif code != 0 and result.get("reason") != "collection_error":
        result.update(status="failed", failed=1, failure_count_exact=False)
    elif complete and result["executed"] > 0 and result["skipped"] == 0:
        result["status"] = "passed"
    else:
        result.setdefault("reason", "required_skip" if result["skipped"] else
                          "zero_collection" if result["executed"] == 0 else "collection_or_completion_gap")
    result.update(case_count=result["executed"], failure_count=result["failed"],
                  failure_count_kind="framework_failure_and_error_records")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--artifact-root", type=Path, help="explicit prepared external test-artifact root")
    parser.add_argument("--overall-budget", type=float, default=OVERALL_BUDGET_SECONDS)
    parser.add_argument("--python-tests", action="store_true", help="run the manifest's registered Python cases")
    args = parser.parse_args(argv)
    if args.python_tests:
        spec = next(suite for suite in workflow_control_plane(sys.executable, Path("/EXTERNAL_TEST_EVIDENCE"))
                    if suite.suite_id == "workflow-python")
        return run_python_tests(spec.expected_modules)
    if args.output is None:
        parser.error("--output must specify a new external directory")
    if not 0 < args.overall_budget < float("inf"):
        parser.error("--overall-budget must be a positive finite number")
    try:
        artifact_root = test_artifact_root(args.artifact_root, repository=ROOT)
        output = validate_new_output(args.output, artifact_root, repository=ROOT)
    except ValueError as exc:
        parser.error(str(exc))
    output.mkdir(parents=True)
    specs = workflow_control_plane(sys.executable, output)
    suites = {spec.suite_id: {
        **spec.as_dict(), "status": "not_run", "exit_code": None,
        "duration_seconds": 0, "executed": 0, "failed": 0, "skipped": 0,
        "case_count": 0, "failure_count": 0, "reason": "not_started",
    } for spec in specs}
    summary = {"started_at": time.time(), "suites": suites, "status": "incomplete",
               "overall_budget_seconds": args.overall_budget, "suite_count": len(suites),
               "complete": False, "cancelled": False, "real_upstream_tested": False,
               "artifact_root": str(artifact_root)}
    path = output / "summary.json"
    _write_summary(path, summary)
    began = time.monotonic()
    active = None
    cancelled = False
    try:
        with cancellation_signals():
            for spec in specs:
                remaining = args.overall_budget - (time.monotonic() - began)
                if remaining <= 0:
                    break
                active = suites[spec.suite_id]
                active.update(status="incomplete", reason="running",
                              effective_timeout_seconds=min(spec.timeout_seconds, remaining))
                _write_summary(path, summary)
                started = time.monotonic()
                print("Running " + spec.suite_id, flush=True)
                env = _safe_environment(output / "runtime" / spec.suite_id, artifact_root)
                try:
                    code, text, timed_out = run_group(spec.command, env, min(spec.timeout_seconds, remaining))
                    reason = "timeout" if timed_out else None
                except VerificationCancelled as exc:
                    cancelled = True
                    code, text, reason = None, str(exc.args[0] if exc.args else ""), "cancelled"
                log = output / (spec.suite_id + ".log")
                log.write_text(text, encoding="utf-8")
                active.pop("reason", None)
                active.update(classify_suite(spec, code, text, reason=reason))
                active.update(exit_code=code, duration_seconds=round(time.monotonic() - started, 3), log=str(log))
                summary["status"] = aggregate_status(list(suites.values()))
                _write_summary(path, summary)
                print(spec.suite_id + ": " + active["status"], flush=True)
                active = None
                if cancelled:
                    break
    except VerificationCancelled:
        cancelled = True
        if active is not None:
            active.update(status="incomplete", reason="cancelled")
    finally:
        for row in suites.values():
            if row["status"] == "not_run":
                row["reason"] = "cancelled" if cancelled else "overall_budget"
        summary.update(status=aggregate_status(list(suites.values())), finished_at=time.time(),
                       elapsed_seconds=round(time.monotonic() - began, 3), complete=True, cancelled=cancelled,
                       counts={state: sum(row["status"] == state for row in suites.values())
                               for state in ("passed", "failed", "incomplete", "not_run")})
        _write_summary(path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 130 if cancelled else 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
