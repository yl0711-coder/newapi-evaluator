"""Registered admission/workbench checks with isolated local Mock data."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_diagnosis import classify, snapshot
from scripts.verify_image_quality import (aggregate_status, cancellation_signals,
                                          classify as classify_image, run_process)
from scripts.test_manifest import admission, integrity


def collect_unittests():
    loader = unittest.TestLoader()
    suite = loader.discover("tests")
    modules = {}

    def visit(group):
        for case in group:
            if isinstance(case, unittest.TestSuite):
                visit(case)
            else:
                module = type(case).__module__
                if module.startswith("test_"):
                    module = "tests." + module
                modules[module] = modules.get(module, 0) + 1

    visit(suite)
    return {"count": suite.countTestCases(), "modules": modules, "errors": len(loader.errors)}


def validated_collection_count(value, required_modules):
    if not isinstance(value, dict) or type(value.get("errors")) is not int or value["errors"] != 0:
        return 0
    count, modules = value.get("count"), value.get("modules")
    if type(count) is not int or count <= 0 or not isinstance(modules, dict):
        return 0
    if any(type(number) is not int or number <= 0 for number in modules.values()):
        return 0
    if sum(modules.values()) != count or any(module not in modules for module in required_modules):
        return 0
    return count


def main(argv=None, *, default_plan="admission"):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sha", help="Require exact clean detached commit for independent acceptance")
    parser.add_argument("--plan", choices=("admission", "integrity"), default=default_plan)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents:
        parser.error("output must be external")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if args.sha and (args.sha != sha or len(args.sha) != 40 or
                    subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip() or
                    subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT).strip()):
        parser.error("acceptance requires exact clean detached SHA")
    output.mkdir(parents=True, exist_ok=False)
    (output / "tmp").mkdir()
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR",
               "PLAYWRIGHT_MODULE", "PLAYWRIGHT_CHANNEL", "PLAYWRIGHT_BROWSERS_PATH", "UI_TEST_PORT",
               "EVAL_TEST_ARTIFACT_ROOT", "DOCKER_HOST", "DOCKER_CONTEXT"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT), TMPDIR=str(output / "tmp"),
               PLATFORM_DATA_DIR=str(output / "platform"), RELAY_LAB_DATA_DIR=str(output),
               PYTHON_EXECUTABLE=sys.executable)
    plan = integrity if args.plan == "integrity" else admission
    suites = plan(sys.executable, output, sha=sha if args.sha else None)
    required_modules = sorted({module for suite in suites for module in suite.expected_modules})
    commands = [(suite.suite_id, list(suite.command), suite.timeout_seconds, suite.kind) for suite in suites]
    budget = (4200 if args.sha else 3000) if args.plan == "integrity" else (3000 if args.sha else 1800)
    before = snapshot()
    rows = [{"suite_id": name, "command": command, "timeout_seconds": timeout, "status": "not_run"}
            for name, command, timeout, _ in commands]
    result = {"source_sha": sha, "source_files": before, "independent": bool(args.sha), "rules_version": "1.0",
              "python": sys.version, "expected_unittest_cases": 0, "results": rows, "status": "incomplete",
              "plan": args.plan, "real_upstream_tested": False, "execution_budget_seconds": budget, "cancelled": False,
              "required_modules": required_modules,
              "not_applicable": {} if args.plan == "integrity" else {"diagnosis-container": "No build, dependency, startup or container changes",
                                 "image-container": "No build, dependency, startup or container changes"}}
    def save():
        (output / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    def record(row, code, text, kind, started, reason=None):
        log = output / (row["suite_id"] + ".log")
        log.write_text(text)
        effective_code = None if reason or code == 127 else code
        if kind in {"image-inspect", "container"}:
            counts = classify_image(effective_code, text, "inspect" if kind == "image-inspect" else "container")
            counts.update(executed=counts.pop("case_count"), failed=counts.pop("failure_count"))
        else:
            counts = classify(effective_code, text, kind, result["expected_unittest_cases"])
        row.update(counts, exit_code=code, elapsed_seconds=time.monotonic() - started, log=str(log))
        if reason:
            row["reason"] = reason
        if kind == "legacy" and row["status"] == "passed":
            try:
                accepted = json.loads((output / "legacy/acceptance.json").read_text()).get("passed") is True
            except (OSError, ValueError):
                accepted = False
            if not accepted:
                row["status"] = "incomplete"
        save()
    save()
    start = time.monotonic()
    active = None
    with cancellation_signals():
        try:
            code, text, timed_out = run_process([sys.executable, "-c", "import json; from scripts.verify_admission import collect_unittests; print(json.dumps(collect_unittests()))"], env, 60)
            (output / "collection.log").write_text(text)
            try:
                collection = json.loads(text) if code == 0 and not timed_out else {}
            except ValueError:
                collection = {}
            result["collection"] = collection
            result["expected_unittest_cases"] = validated_collection_count(collection, required_modules)
            save()
            for row, (_, command, timeout, kind) in zip(rows, commands):
                remaining = budget - (time.monotonic() - start)
                if not result["expected_unittest_cases"] or remaining <= 0:
                    break
                print("Verify: " + row["suite_id"], flush=True)
                active = (row, kind, time.monotonic())
                code, text, timed_out = run_process(command, env, min(timeout, remaining))
                record(row, code, text, kind, active[2], "timeout" if timed_out else None)
                active = None
        except KeyboardInterrupt as exc:
            result["cancelled"] = True
            if active:
                row, kind, started = active
                record(row, getattr(exc, "returncode", None), getattr(exc, "output", ""), kind, started, "cancelled")
            else:
                (output / "collection.log").write_text(getattr(exc, "output", ""))
    result["source_unchanged"] = before == snapshot()
    result["status"] = aggregate_status(rows, not result["source_unchanged"])
    if result["cancelled"] and result["status"] == "passed":
        result["status"] = "incomplete"
    save()
    print(json.dumps({"status": result["status"], "evidence": str(output / "verification.json")}))
    return 130 if result["cancelled"] else 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
