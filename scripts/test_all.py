"""Run the registered local workbench checks with bounded, auditable groups.

The default invocation remains compatible with the historical ``python
scripts/test_all.py`` command.  Verification callers should pass ``--output``
so that each group log and the aggregate summary are kept outside the
repository.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OVERALL_BUDGET_SECONDS = 840
TERMINATION_GRACE_SECONDS = 5


@dataclass(frozen=True)
class GroupSpec:
    group_id: str
    command: tuple[str, ...]
    timeout_seconds: int
    kind: str
    minimum_checks: int = 1


def registered_groups(python: str | None = None) -> tuple[GroupSpec, ...]:
    """Return the stable internal test groups in execution order."""
    interpreter = python or sys.executable
    return (
        GroupSpec(
            "admission-selftest",
            (interpreter, "features/admission/selftest.py"),
            300,
            "unittest",
        ),
        GroupSpec(
            "stability-selftest",
            (interpreter, "features/stability/selftest.py"),
            300,
            "manual",
            minimum_checks=22,
        ),
        GroupSpec(
            "reasoning-selftest",
            (interpreter, "features/reasoning/selftest.py"),
            300,
            "unittest",
        ),
        GroupSpec(
            "unittest-discover",
            (interpreter, "-m", "unittest", "discover", "-s", "tests", "-v"),
            900,
            "unittest",
        ),
    )


class VerificationCancelled(Exception):
    """Raised when the parent receives a cancellation signal."""


def request_cancel(_signum: int, _frame: object) -> None:
    raise VerificationCancelled()


@contextmanager
def cancellation_signals():
    previous = {
        number: signal.signal(number, request_cancel)
        for number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def stop_owned_process(process: subprocess.Popen[str]) -> str:
    """Terminate only the process group created for one test group."""
    with cancellation_signals_disabled():
        for number in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, number)
            except ProcessLookupError:
                pass
            try:
                output, _ = process.communicate(timeout=TERMINATION_GRACE_SECONDS)
                # A descendant may keep the pipe open after the group leader
                # exits.  A second group kill is still limited to this run.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return output
            except subprocess.TimeoutExpired:
                if number == signal.SIGKILL:
                    raise
    return ""


@contextmanager
def cancellation_signals_disabled():
    previous = {
        number: signal.signal(number, signal.SIG_IGN)
        for number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def run_group(command: tuple[str, ...], env: dict[str, str], timeout: float) -> tuple[int | None, str, bool]:
    """Run one command in its own process group.

    The boolean is true only for an ordinary per-group timeout.  A parent
    cancellation raises ``VerificationCancelled`` after cleaning up the
    owned process group, allowing the caller to mark the group incomplete and
    all later groups not_run.
    """
    try:
        process = subprocess.Popen(
            list(command),
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return 127, f"Required executable unavailable: {exc.__class__.__name__}\n", False
    try:
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output, False
    except subprocess.TimeoutExpired:
        output = stop_owned_process(process)
        return process.returncode, output, True
    except VerificationCancelled:
        output = stop_owned_process(process)
        raise VerificationCancelled(output) from None
    except BaseException:
        stop_owned_process(process)
        raise


def _unittest_counts(output: str) -> tuple[list[int], int, int]:
    counts = [int(value) for value in re.findall(r"(?m)^Ran (\d+) tests?", output)]
    summaries = list(re.finditer(r"(?m)^(?:OK|FAILED)\b[^\n]*", output))
    skipped = failures = previous = 0
    for match in summaries:
        line = match.group()
        skipped += sum(int(value) for value in re.findall(r"skipped=(\d+)", line))
        values = re.findall(r"(?:failures|errors|unexpected successes)=(\d+)", line)
        if values:
            failures += sum(map(int, values))
        elif line.startswith("FAILED"):
            failures += max(1, len(re.findall(r"(?m)^(?:ERROR|FAIL):\s", output[previous:match.start()])))
        previous = match.end()
    failures += len(re.findall(r"(?m)^(?:ERROR|FAIL):\s", output[previous:]))
    return counts, skipped, failures


def classify_group(spec: GroupSpec, returncode: int | None, output: str,
                   *, timed_out: bool = False, reason: str | None = None) -> dict[str, object]:
    """Convert process and framework evidence into explicit result semantics."""
    counts, skipped, failures = _unittest_counts(output)
    if spec.kind == "manual":
        executed = len(re.findall(r"(?m)^  OK\s", output)) + len(re.findall(r"(?m)^  FAIL\s", output))
        failures = max(failures, len(re.findall(r"(?m)^  FAIL\s", output)))
    else:
        executed = sum(counts)

    dependency_missing = bool(re.search(r"ModuleNotFoundError|ImportError|Required executable unavailable", output))
    failed_cases = set(re.findall(r"(?m)^(?:FAIL|ERROR):\s+(.+?\([^)]*\))", output))
    summaries = list(re.finditer(r"(?m)^(?:OK|FAILED)\b[^\n]*", output))
    count_exact = (not failures or bool(summaries) and all(
        not match.group().startswith("FAILED") or bool(re.search(
            r"(?:failures|errors|unexpected successes)=\d+", match.group())) for match in summaries)
        and not re.search(r"(?m)^(?:ERROR|FAIL):\s", output[summaries[-1].end():]))
    if returncode not in (None, 0) and not summaries:
        count_exact = False

    known_failure = failures > 0 and (not dependency_missing or bool(re.search(
        r"(?m)^FAIL:\s|^  FAIL\s|failures=[1-9]", output)))
    if known_failure:
        status = "failed"
    elif reason in {"cancelled", "overall_budget", "timeout"} or timed_out or returncode is None:
        status = "incomplete"
    elif dependency_missing or returncode == 127 or returncode < 0:
        status = "failed" if re.search(r"(?m)^FAIL:\s|failures=[1-9]", output) else "incomplete"
    elif failures:
        status = "failed"
    elif returncode != 0:
        status = "failed"
    elif spec.kind == "manual":
        status = "passed" if executed >= spec.minimum_checks and failures == 0 and "失败项：无" in output else "incomplete"
    else:
        status = "passed" if (
            len(counts) == 1
            and executed >= spec.minimum_checks
            and skipped == 0
            and failures == 0
            and bool(re.search(r"(?m)^OK(?:\s|$)", output))
        ) else "incomplete"
    result = {
        "status": status,
        "executed": executed,
        "failed": failures,
        "skipped": skipped,
        "framework_runs": counts,
        "failed_test_count": len(failed_cases) if failed_cases else None if failures or returncode not in (None, 0) else 0,
        "failure_count_kind": "framework_failure_and_error_records",
        "failure_count_exact": count_exact,
        "timed_out": timed_out or reason == "timeout",
    }
    if reason:
        result["reason"] = reason
    elif timed_out:
        result["reason"] = "timeout"
    elif returncode is None:
        result["reason"] = "exit_unknown"
    elif returncode < 0:
        result["reason"] = "child_signal"
    return result


def _safe_environment(output: Path, artifact_root: Path | None = None) -> dict[str, str]:
    allowed = {
        "PATH", "LANG", "LC_ALL", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "PLAYWRIGHT_MODULE", "PLAYWRIGHT_CHANNEL",
        "PLAYWRIGHT_BROWSERS_PATH", "UI_TEST_PORT",
        "EVAL_TEST_ARTIFACT_ROOT",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    for directory in ("tmp", "home", "platform", "relay-lab"):
        (output / directory).mkdir(parents=True, exist_ok=True)
    env.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=str(ROOT),
        HOME=str(output / "home"),
        TMPDIR=str(output / "tmp"),
        PLATFORM_DATA_DIR=str(output / "platform"),
        RELAY_LAB_DATA_DIR=str(output / "relay-lab"),
        PYTHON_EXECUTABLE=sys.executable,
    )
    if artifact_root is not None:
        env["EVAL_TEST_ARTIFACT_ROOT"] = str(artifact_root)
    return env


def _write_summary(path: Path, summary: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _new_output(path: Path | None, artifact_root: Path | None = None) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    from scripts.verify_image_quality import test_artifact_root, validate_new_output
    artifact_root = artifact_root or test_artifact_root(repository=ROOT)
    if path is not None:
        output = validate_new_output(path, artifact_root, repository=ROOT)
        output.mkdir(parents=True)
        return output, None
    holder = tempfile.TemporaryDirectory(prefix="workbench-all-", dir=artifact_root)
    output = Path(holder.name) / "evidence"
    output.mkdir()
    return output, holder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new external evidence directory")
    parser.add_argument("--artifact-root", type=Path, help="explicit external test-artifact root")
    args = parser.parse_args(argv)
    try:
        from scripts.verify_image_quality import test_artifact_root
        artifact_root = test_artifact_root(args.artifact_root, repository=ROOT)
        output, temporary = _new_output(args.output, artifact_root)
    except ValueError as exc:
        parser.error(str(exc))
    groups = registered_groups(sys.executable)
    rows: list[dict[str, object]] = [
        {
            "group_id": spec.group_id,
            "command": list(spec.command),
            "timeout_seconds": spec.timeout_seconds,
            "status": "not_run",
            "executed": 0,
            "failed": 0,
            "skipped": 0,
            "reason": "not_started",
        }
        for spec in groups
    ]
    summary: dict[str, object] = {
        "schema_version": "1.0",
        "status": "incomplete",
        "groups": rows,
        "group_count": len(rows),
        "overall_budget_seconds": OVERALL_BUDGET_SECONDS,
        "cancelled": False,
        "real_upstream_tested": False,
        "evidence_directory": str(output),
    }
    summary_path = output / "summary.json"
    _write_summary(summary_path, summary)
    started_all = time.monotonic()
    cancelled = False
    active_row: dict[str, object] | None = None
    try:
        with cancellation_signals():
            for index, (spec, row) in enumerate(zip(groups, rows)):
                remaining = OVERALL_BUDGET_SECONDS - (time.monotonic() - started_all)
                if remaining <= 0:
                    row["reason"] = "overall_budget"
                    break
                row.pop("reason", None)
                started = time.monotonic()
                active_row = row
                print(f"=== {spec.group_id} ===", flush=True)
                try:
                    env = _safe_environment(output / "runtime" / spec.group_id, artifact_root)
                    code, output_text, timed_out = run_group(spec.command, env, min(spec.timeout_seconds, remaining))
                    reason = "timeout" if timed_out else None
                except VerificationCancelled as exc:
                    cancelled = True
                    code, output_text, timed_out, reason = None, str(exc.args[0] if exc.args else ""), False, "cancelled"
                log_path = output / f"{spec.group_id}.log"
                log_path.write_text(output_text)
                row.update(classify_group(spec, code, output_text, timed_out=timed_out, reason=reason))
                row.update({
                    "exit_code": code,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "log": str(log_path),
                })
                if reason:
                    row["reason"] = reason
                print(output_text, end="" if output_text.endswith("\n") else "\n", flush=True)
                _write_summary(summary_path, summary)
                if cancelled:
                    for remaining_row in rows[index + 1:]:
                        remaining_row["reason"] = "cancelled"
                    break
                active_row = None
    except VerificationCancelled:
        # A signal between groups still leaves the active row and later rows
        # explicitly incomplete/not_run in the durable summary.
        cancelled = True
        if active_row is not None and active_row["status"] == "not_run":
            active_row.update(
                status="incomplete",
                executed=0,
                failed=0,
                skipped=0,
                exit_code=None,
                reason="cancelled",
            )
        for row in rows:
            if row["status"] == "not_run":
                row["reason"] = "cancelled"
    finally:
        if not cancelled:
            elapsed = time.monotonic() - started_all
            for row in rows:
                if row["status"] == "not_run" and row.get("reason") == "not_started":
                    row["reason"] = "overall_budget" if elapsed >= OVERALL_BUDGET_SECONDS else "not_run"
        summary["cancelled"] = cancelled
        passed = sum(row["status"] == "passed" for row in rows)
        failed = sum(row["status"] == "failed" for row in rows)
        incomplete = sum(row["status"] == "incomplete" for row in rows)
        not_run = sum(row["status"] == "not_run" for row in rows)
        summary["counts"] = {"passed": passed, "failed": failed, "incomplete": incomplete, "not_run": not_run}
        summary["elapsed_seconds"] = round(time.monotonic() - started_all, 3)
        summary["status"] = "failed" if failed else "passed" if passed == len(rows) else "incomplete"
        _write_summary(summary_path, summary)
    if summary["status"] == "passed":
        # Keep the historical success marker for the outer verifiers while
        # only emitting it after every registered group actually passed.
        print("All engine and integration checks passed.")
    print(json.dumps({"status": summary["status"], "evidence": str(summary_path)}))
    if temporary is not None:
        # Keep the temporary evidence available while the final line is
        # emitted.  The historical no-argument mode remains ephemeral.
        temporary.cleanup()
    return 130 if cancelled else 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
