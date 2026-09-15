"""Run registered workbench checks with bounded processes and explicit result states."""
import argparse
import ast
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def classify(returncode, output, kind):
    summaries = re.findall(r"(?m)^Ran (\d+) tests?[^\n]*\n\s*\n?(OK[^\n]*|FAILED[^\n]*)", output)
    counts = [int(value) for value in re.findall(r"(?m)^Ran (\d+) tests?", output)]
    manual_checks = len(re.findall(r"(?m)^  OK\s", output)) if kind == "unittest" else 0
    count = sum(counts) + manual_checks if kind == "unittest" else 0
    skipped = sum(int(value) for value in re.findall(r"skipped=(\d+)", output))
    failures = sum(int(value) for value in re.findall(r"(?:failures|errors|unexpected successes)=(\d+)", output))
    failures += len(re.findall(r"(?m)^  FAIL\s", output))
    if re.search(r"(?m)^FAILED\b", output):
        failures = max(1, failures)
    dependency_missing = bool(re.search(
        r"ModuleNotFoundError|ImportError|Required executable unavailable|Cannot find module|"
        r"Cannot connect to the Docker daemon|Executable doesn't exist", output))
    exit_failed = returncode not in (None, 0, 127) and returncode > 0 and output.strip() and not dependency_missing
    child_incomplete = False
    complete = returncode == 0 and skipped == 0 and not dependency_missing
    if kind == "unittest":
        complete = (complete and len(counts) == len(summaries) == 3
                    and all(value > 0 for value in counts) and manual_checks >= 22
                    and all(status == "OK" for _, status in summaries)
                    and "test_image_quality" in output
                    and "All engine and integration checks passed." in output)
    if kind in {"security", "syntax", "inspect", "container", "browser"}:
        try:
            data = json.loads(output.strip().splitlines()[-1])
            if kind == "security":
                count = data["files_checked"]
                failures = max(failures, len(data.get("findings", [])), int(data["passed"] is False))
                complete = complete and data["passed"] is True
            elif kind == "syntax":
                count = data["syntax_units"]
                failures = max(failures, int(data["status"] == "failed"))
                complete = complete and data["status"] == "passed"
            elif kind == "inspect":
                count = 1
                complete = complete and data["status"] == "valid" and data["network_requested"] is False and data["model"] == "gpt-image-2"
            elif kind == "container":
                count = len(data["checks"])
                failures = max(failures, int(data["status"] == "failed"))
                complete = (complete and data["status"] == "passed"
                            and {row["mode"] for row in data["checks"]} == {"all", "image-quality"}
                            and all(row["status"] == "passed" for row in data["checks"]))
            elif kind == "browser":
                count = 1
                failures = max(failures, int(data["status"] == "failed"))
                complete = complete and data["status"] == "passed" and data["mockRequests"] > 0 and "image-quality" in data["checks"]
            if data.get("status") == "incomplete":
                complete = False
                # The child distinguishes environmental gaps from known check failures.
                child_incomplete = True
                failures = max(failures, data.get("failure_count", 0))
        except (ValueError, KeyError, TypeError, IndexError):
            complete = False
    elif kind == "web":
        count = 1 if "UI contract tests passed:" in output else 0
    elif kind == "e2e":
        modes = set(re.findall(r"(?m)^(account-test|pool-test|gateway-test|long-task-test|chaos-test) passed: [1-9]\d* results$", output))
        count = len(modes)
        complete = complete and count == 5 and "All five Mock CLI modes passed" in output
    complete = complete and type(count) is int and count > 0
    if exit_failed and not child_incomplete:
        failures = max(1, failures)
    status = "failed" if failures else "passed" if complete else "incomplete"
    return {"status": status, "case_count": count, "failure_count": failures, "skipped": skipped,
            "framework_cases": sum(counts), "manual_checks": manual_checks,
            "framework_suites": [{"case_count": int(n), "result": result} for n, result in summaries]}


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.chunks = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            attributes = dict(attrs)
            self.active = "src" not in attributes and attributes.get("type", "") not in {"application/json", "application/ld+json"}
            self.chunks = []

    def handle_data(self, text):
        if self.active:
            self.chunks.append(text)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            self.scripts.append("".join(self.chunks))
            self.active = False


def syntax_check():
    names = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT).decode().split("\0")
    checked = 0
    for name in sorted(set(names) - {""}):
        path = ROOT / name
        if not path.is_file():
            continue
        if path.suffix == ".py":
            ast.parse(path.read_text(), filename=name)
            checked += 1
        if path.suffix in {".js", ".cjs", ".mjs"}:
            subprocess.run(["node", "--check", str(path)], check=True, capture_output=True, timeout=20)
            checked += 1
        if path.suffix == ".html":
            parser = Scripts()
            parser.feed(path.read_text())
            for script in parser.scripts:
                subprocess.run(["node", "--check"], input=script, text=True, check=True, capture_output=True, timeout=20)
                checked += 1
    print(json.dumps({"status": "passed", "syntax_units": checked}))


def run_process(command, env, timeout):
    try:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, start_new_session=True)
    except OSError:
        return 127, "Required executable unavailable\n", False
    try:
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output, False
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate(timeout=5)
        return process.returncode, output, True


def source_fingerprint():
    names = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT).decode().split("\0")
    digest = hashlib.sha256()
    for name in sorted(set(names) - {""}):
        path = ROOT / name
        if path.is_file():
            digest.update(name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--syntax-only", action="store_true")
    args = parser.parse_args()
    if args.syntax_only:
        syntax_check()
        return 0
    if not args.output:
        parser.error("--output 必须指定全新的仓库外目录")
    output = args.output.expanduser().resolve()
    if output == ROOT or ROOT in output.parents or output.exists():
        parser.error("--output 必须为不存在的仓库外目录")
    if sys.platform == "darwin":
        allowed = Path("/Users/lmurder/Desktop/api中转站/中转站极限测试数据").resolve()
        if allowed not in output.parents:
            parser.error("本机证据必须位于统一测试数据目录")
    output.mkdir(parents=True)
    (output / "tmp").mkdir()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(output / "tmp"),
           "PLATFORM_DATA_DIR": str(output / "platform"), "RELAY_LAB_DATA_DIR": str(output),
           "PLATFORM_USERNAME": "", "PLATFORM_PASSWORD": "", "PYTHON_EXECUTABLE": sys.executable}
    for key in list(env):
        if key.startswith("ADMISSION_FEISHU_"):
            del env[key]
    commands = [
        ("syntax", [sys.executable, __file__, "--syntax-only"], 300, "syntax"),
        ("image-inspect", [sys.executable, "-m", "features.image_quality", "inspect-config",
                           "--config", "tests/fixtures/image_quality/config.json"], 60, "inspect"),
        ("workbench-python", [sys.executable, "scripts/test_all.py"], 900, "unittest"),
        ("workbench-web", ["node", "scripts/test_web.js"], 120, "web"),
        ("workbench-security", [sys.executable, "scripts/repo_security_scan.py", "."], 120, "security"),
        ("workbench-e2e", [sys.executable, "scripts/e2e.py", "--output", str(output / "e2e")], 600, "e2e"),
        ("workbench-browser", ["node", "scripts/ui_smoke.cjs"], 600, "browser"),
        ("build-container", [sys.executable, "scripts/image_quality_container.py", "--output", str(output / "container")], 900, "container"),
    ]
    results = []
    began = time.monotonic()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    source_hash = source_fingerprint()
    for name, command, timeout, kind in commands:
        remaining = 1800 - (time.monotonic() - began)
        if remaining <= 0:
            results.append({"suite_id": name, "status": "not_run", "reason": "overall_budget"})
            continue
        started = time.monotonic()
        print("Running " + name, flush=True)
        code, text, timed_out = run_process(command, env, min(timeout, remaining))
        (output / (name + ".log")).write_text(text)
        result = {"suite_id": name, "command": command, "exit_code": code,
                  "seconds": round(time.monotonic() - started, 3),
                  **classify(code, text, kind)}
        if timed_out:
            if result["status"] != "failed":
                result["status"] = "incomplete"
            result["reason"] = "timeout"
        results.append(result)
        (output / "checks.json").write_text(json.dumps({"revision": revision, "complete": False, "checks": results}, indent=2))
        print(name + ": " + result["status"], flush=True)
    state = ("failed" if any(row["status"] == "failed" for row in results) else
             "passed" if all(row["status"] == "passed" for row in results) else "incomplete")
    if source_hash != source_fingerprint():
        state = "incomplete"
    versions = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
    (output / "dependencies.txt").write_text(versions)
    (output / "checks.json").write_text(json.dumps({
        "revision": revision, "source_sha256": source_hash, "complete": True, "status": state, "checks": results,
        "real_upstream_tested": False,
        "worktree_status": subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True),
    }, indent=2))
    print(json.dumps({"status": state, "evidence": str(output)}))
    return 0 if state == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
