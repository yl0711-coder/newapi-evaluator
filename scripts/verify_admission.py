"""Registered admission/workbench checks with isolated local Mock data."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_diagnosis import classify, snapshot
from scripts.verify_image_quality import (aggregate_status, cancellation_signals,
                                          classify as classify_image, run_process)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sha", help="Require exact clean detached commit for independent acceptance")
    args = parser.parse_args()
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
               "PLAYWRIGHT_MODULE", "PLAYWRIGHT_CHANNEL", "PLAYWRIGHT_BROWSERS_PATH", "UI_TEST_PORT"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT), TMPDIR=str(output / "tmp"),
               PLATFORM_DATA_DIR=str(output / "platform"), RELAY_LAB_DATA_DIR=str(output),
               PYTHON_EXECUTABLE=sys.executable)
    commands = [
        ("protocol-inspect", [sys.executable, "scripts/inspect_protocol_admission.py"], 60, "inspect"),
        ("protocol-browser", ["node", "scripts/protocol_admission_ui.cjs"], 300, "browser"),
        ("admission-inspect", [sys.executable, "scripts/inspect_admission.py", "--protocol", "openai"], 60, "inspect"),
        ("workbench-python", [sys.executable, "scripts/test_all.py"], 900, "python"),
        ("workbench-web", ["node", "scripts/test_web.js"], 120, "web"),
        ("workbench-security", [sys.executable, "scripts/repo_security_scan.py", "."], 120, "security"),
        ("syntax", [sys.executable, "scripts/diagnosis_syntax.py"], 300, "json"),
        ("workbench-e2e", [sys.executable, "scripts/e2e.py", "--output", str(output / "e2e")], 600, "e2e"),
        ("workbench-browser", ["node", "scripts/ui_smoke.cjs"], 600, "browser"),
        ("diagnosis-browser", ["node", "scripts/diagnosis_ui.cjs"], 300, "json"),
        ("diagnosis-inspect", [sys.executable, "-m", "features.diagnosis.inspect", "--data-dir", str(output / "platform")], 60, "inspect"),
        ("image-inspect", [sys.executable, "-m", "features.image_quality", "inspect-config", "--config", "tests/fixtures/image_quality/config.json"], 60, "image-inspect"),
        ("model-coverage-inspect", [sys.executable, "scripts/inspect_model_coverage.py"], 60, "inspect"),
        ("model-coverage-browser", ["node", "scripts/model_coverage_ui.cjs"], 300, "browser"),
    ]
    if args.sha:
        commands.append(("legacy-acceptance", [sys.executable, "scripts/acceptance.py", "--sha", sha,
                                                "--output", str(output / "legacy")], 1200, "legacy"))
    budget = 3000 if args.sha else 1800
    before = snapshot()
    rows = [{"suite_id": name, "command": command, "timeout_seconds": timeout, "status": "not_run"}
            for name, command, timeout, _ in commands]
    result = {"source_sha": sha, "source_files": before, "independent": bool(args.sha), "rules_version": "1.0",
              "python": sys.version, "expected_unittest_cases": 0, "results": rows, "status": "incomplete",
              "real_upstream_tested": False, "execution_budget_seconds": budget, "cancelled": False,
              "not_applicable": {"diagnosis-container": "No build, dependency, startup or container changes",
                                 "image-container": "No build, dependency, startup or container changes"}}
    def save():
        (output / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    def record(row, code, text, kind, started, reason=None):
        log = output / (row["suite_id"] + ".log")
        log.write_text(text)
        effective_code = None if reason or code == 127 else code
        if kind == "image-inspect":
            counts = classify_image(effective_code, text, "inspect")
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
            code, text, timed_out = run_process([sys.executable, "-c", "import unittest; print(unittest.TestLoader().discover('tests').countTestCases())"], env, 60)
            (output / "collection.log").write_text(text)
            result["expected_unittest_cases"] = int(text.strip()) if code == 0 and not timed_out and text.strip().isdigit() else 0
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
