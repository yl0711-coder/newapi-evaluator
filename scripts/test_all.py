#!/usr/bin/env python3
"""隔离执行完整离线清单；不读取业务配置、密钥或现有数据库。"""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SUITES = {"syntax": 30, "security": 30, "domain": 60, "http": 90, "browser": 90}


def checked_status(exit_code, result):
    if not result or not result.get("tests"):
        return "incomplete"
    if exit_code != 0 or result.get("failures") or result.get("errors"):
        return "failed"
    if result.get("skipped"):
        return "incomplete"
    return result.get("status", "incomplete")


def run_child(command, environment, timeout):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=environment, start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr), timed_out


def execute_suite(name, directory):
    result = {"suite": name, "status": "passed", "tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if name in ("domain", "http"):
        discovered = {p.name for p in (ROOT / "tests").glob("test_*.py")}
        if discovered != {"test_domain.py", "test_http.py"}:
            raise RuntimeError("测试文件与注册清单不一致")
        sys.path.insert(0, str(ROOT / "tests"))
        os.environ["DIAGNOSTIC_TEST_ROOT"] = str(directory)
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=f"test_{name}.py")
        tested = unittest.TextTestRunner(verbosity=2).run(suite)
        result.update(tests=tested.testsRun, failures=len(tested.failures), errors=len(tested.errors), skipped=len(tested.skipped))
        if not tested.wasSuccessful() or not tested.testsRun or tested.skipped:
            result["status"] = "failed"
    elif name == "syntax":
        for path in sorted(ROOT.rglob("*.py")):
            if ".git" in path.parts:
                continue
            ast.parse(path.read_text(), filename=str(path.relative_to(ROOT)))
            result["tests"] += 1
        subprocess.run(["node", "--check", str(ROOT / "tests/browser_check.cjs")], check=True, timeout=10)
        result["tests"] += 1
    elif name == "security":
        patterns = [re.compile(r"sk-[A-Za-z0-9_-]{20,}"), re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
                    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")]
        findings = []
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            result["tests"] += 1
            if path.name == "config.json" or path.name.startswith(".env") or ".sqlite3" in path.name:
                findings.append(str(path.relative_to(ROOT)))
                continue
            text = path.read_text()
            if any(pattern.search(text) for pattern in patterns):
                findings.append(str(path.relative_to(ROOT)))
        if findings:
            result.update(status="failed", failures=len(findings), files=findings)
    elif name == "browser":
        spec = importlib.util.spec_from_file_location("diagnostic", ROOT / "hourly_channel_diagnostic.py")
        diagnostic = importlib.util.module_from_spec(spec); spec.loader.exec_module(diagnostic)
        config = diagnostic.load_config(ROOT / "config.example.json")
        diagnostic.run_once(config, directory / "diagnostic.sqlite3", True)
        diagnostic.build_report(directory / "diagnostic.sqlite3", directory / "report.html", config["timezone"])
        subprocess.run(["node", str(ROOT / "tests/browser_check.cjs"), str(directory)], check=True)
        browser = json.loads((directory / "browser.json").read_text())
        result.update(tests=browser["checks"], status=browser["status"])
    (directory / "suite.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--suite", choices=SUITES)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        parser.error("测试输出必须在源码目录外")
    if args.suite:
        return execute_suite(args.suite, output)
    output.mkdir(parents=True, exist_ok=False)
    results = []
    environment = {k: v for k, v in os.environ.items() if "API_KEY" not in k}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    temporary = output / "tmp"; temporary.mkdir()
    environment["TMPDIR"] = str(temporary)
    started = time.monotonic()
    for name, limit in SUITES.items():
        directory = output / name; directory.mkdir()
        command = [sys.executable, "-B", str(Path(__file__).resolve()), "--suite", name, "--output", str(directory)]
        try:
            completed, timed_out = run_child(command, environment, limit)
            (directory / "stdout.log").write_text(completed.stdout)
            (directory / "stderr.log").write_text(completed.stderr)
            result_path = directory / "suite.json"
            result = json.loads(result_path.read_text()) if result_path.exists() else {"suite": name, "status": "incomplete"}
            result.update(command=command, exit_code=completed.returncode, timeout_seconds=limit)
            result["status"] = "incomplete" if timed_out else checked_status(completed.returncode, result)
            result["timed_out"] = timed_out
        except (OSError, ValueError) as exc:
            result = {"suite": name, "status": "incomplete", "exit_code": None, "error_class": type(exc).__name__}
        results.append(result)
        print(f"{name}: {result['status']}", flush=True)
    fingerprints = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts}
    status = "failed" if any(r["status"] == "failed" for r in results) else (
        "passed" if all(r["status"] == "passed" for r in results) else "incomplete")
    summary = {"status": status, "suites": results, "elapsed_seconds": round(time.monotonic()-started, 2),
               "python": sys.version, "source_fingerprints": fingerprints, "live_requests": False}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"overall: {status}", flush=True)
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
