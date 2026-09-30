"""Bounded local verification for the Eval workflow control plane."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    commands = {
        "workflow-python": [sys.executable, "-m", "unittest", "tests.test_model_coverage", "tests.test_monitor_internal", "-q"],
        "workflow-syntax": [sys.executable, "scripts/diagnosis_syntax.py"],
        "workflow-security": [sys.executable, "scripts/repo_security_scan.py", "."],
        "workflow-web": ["node", "scripts/test_web.js"],
        "workflow-browser": ["node", "scripts/model_coverage_ui.cjs"],
    }
    summary = {"started_at": time.time(), "suites": {}, "status": "passed"}
    for name, command in commands.items():
        started = time.time()
        proc = subprocess.run(command, text=True, capture_output=True, timeout=900)
        (args.output / f"{name}.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        status = "passed" if proc.returncode == 0 else "failed"
        if name == "workflow-browser" and "Cannot find module 'playwright'" in (proc.stdout + proc.stderr):
            status = "incomplete"
        summary["suites"][name] = {"status": status, "exit_code": proc.returncode, "duration_seconds": time.time() - started}
        if status == "failed":
            summary["status"] = "failed"
        elif status == "incomplete" and summary["status"] == "passed":
            summary["status"] = "incomplete"
    summary["finished_at"] = time.time()
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
