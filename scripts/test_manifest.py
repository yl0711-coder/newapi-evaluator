"""Authoritative read-only manifests for the repository verification plans.

The manifest contains suite identity, executable command, timeout and result
classification.  It does not execute commands or read runtime data.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class SuiteSpec:
    suite_id: str
    command: tuple[str, ...]
    timeout_seconds: int
    kind: str

    def as_dict(self) -> dict:
        return {
            "suite_id": self.suite_id,
            "command": list(self.command),
            "timeout_seconds": self.timeout_seconds,
            "kind": self.kind,
        }


def _python(python: str, *parts: str) -> tuple[str, ...]:
    return (python, *parts)


def _output_arg(output: Path, name: str) -> str:
    return str(output / name)


def _validate(suites: Sequence[SuiteSpec]) -> list[SuiteSpec]:
    ids = [suite.suite_id for suite in suites]
    if len(ids) != len(set(ids)):
        raise ValueError("verification suite IDs must be unique")
    for suite in suites:
        if not suite.suite_id or suite.timeout_seconds <= 0 or not suite.command:
            raise ValueError(f"invalid verification suite: {suite!r}")
    return list(suites)


def admission(python: str, output: Path, *, sha: str | None = None) -> list[SuiteSpec]:
    """Return the registered admission/workbench verification suites."""
    suites = [
        SuiteSpec("protocol-inspect", _python(python, "scripts/inspect_protocol_admission.py"), 60, "inspect"),
        SuiteSpec("protocol-browser", ("node", "scripts/protocol_admission_ui.cjs"), 300, "browser"),
        SuiteSpec("admission-inspect", _python(python, "scripts/inspect_admission.py", "--protocol", "openai"), 60, "inspect"),
        SuiteSpec("workbench-python", _python(python, "scripts/test_all.py", "--output", _output_arg(output, "workbench-python")), 900, "python"),
        SuiteSpec("workbench-web", ("node", "scripts/test_web.js"), 120, "web"),
        SuiteSpec("workbench-security", _python(python, "scripts/repo_security_scan.py", "."), 120, "security"),
        SuiteSpec("syntax", _python(python, "scripts/diagnosis_syntax.py"), 300, "json"),
        SuiteSpec("workbench-e2e", _python(python, "scripts/e2e.py", "--output", _output_arg(output, "e2e")), 600, "e2e"),
        SuiteSpec("workbench-browser", ("node", "scripts/ui_smoke.cjs"), 600, "browser"),
        SuiteSpec("diagnosis-browser", ("node", "scripts/diagnosis_ui.cjs"), 300, "json"),
        SuiteSpec("diagnosis-inspect", _python(python, "-m", "features.diagnosis.inspect", "--data-dir", _output_arg(output, "platform")), 60, "inspect"),
        SuiteSpec("image-inspect", _python(python, "-m", "features.image_quality", "inspect-config", "--config", "tests/fixtures/image_quality/config.json"), 60, "image-inspect"),
        SuiteSpec("model-coverage-inspect", _python(python, "scripts/inspect_model_coverage.py"), 60, "inspect"),
        SuiteSpec("model-coverage-browser", ("node", "scripts/model_coverage_ui.cjs"), 300, "browser"),
    ]
    if sha is not None:
        suites.append(SuiteSpec(
            "legacy-acceptance",
            _python(python, "scripts/acceptance.py", "--sha", sha, "--output", _output_arg(output, "legacy")),
            1200,
            "legacy",
        ))
    return _validate(suites)


def diagnosis(python: str, output: Path, *, sha: str | None = None) -> list[SuiteSpec]:
    """Return the registered request-diagnosis verification suites."""
    suites = [
        SuiteSpec("workbench-python", _python(python, "scripts/test_all.py", "--output", _output_arg(output, "workbench-python")), 900, "python"),
        SuiteSpec("workbench-web", ("node", "scripts/test_web.js"), 120, "web"),
        SuiteSpec("workbench-security", _python(python, "scripts/repo_security_scan.py", "."), 120, "security"),
        SuiteSpec("syntax", _python(python, "scripts/diagnosis_syntax.py"), 300, "json"),
        SuiteSpec("workbench-e2e", _python(python, "scripts/e2e.py", "--output", _output_arg(output, "e2e")), 600, "e2e"),
        SuiteSpec("workbench-browser", ("node", "scripts/ui_smoke.cjs"), 600, "browser"),
        SuiteSpec("diagnosis-browser", ("node", "scripts/diagnosis_ui.cjs"), 300, "json"),
        SuiteSpec("diagnosis-inspect", _python(python, "-m", "features.diagnosis.inspect", "--data-dir", _output_arg(output, "platform")), 60, "inspect"),
        SuiteSpec("build-container", _python(python, "scripts/diagnosis_container.py", "--output", _output_arg(output, "container")), 900, "json"),
    ]
    if sha is not None:
        suites.append(SuiteSpec(
            "legacy-acceptance",
            _python(python, "scripts/acceptance.py", "--sha", sha, "--output", _output_arg(output, "legacy")),
            1200,
            "legacy",
        ))
    return _validate(suites)


def image_quality(python: str, output: Path) -> list[SuiteSpec]:
    """Return the registered image-quality integration suites."""
    suites = [
        SuiteSpec("syntax", _python(python, "scripts/verify_image_quality.py", "--syntax-only"), 300, "syntax"),
        SuiteSpec("image-inspect", _python(python, "-m", "features.image_quality", "inspect-config", "--config", "tests/fixtures/image_quality/config.json"), 60, "inspect"),
        SuiteSpec("workbench-python", _python(python, "scripts/test_all.py", "--output", _output_arg(output, "workbench-python")), 900, "unittest"),
        SuiteSpec("workbench-web", ("node", "scripts/test_web.js"), 120, "web"),
        SuiteSpec("workbench-security", _python(python, "scripts/repo_security_scan.py", "."), 120, "security"),
        SuiteSpec("workbench-e2e", _python(python, "scripts/e2e.py", "--output", _output_arg(output, "e2e")), 600, "e2e"),
        SuiteSpec("workbench-browser", ("node", "scripts/ui_smoke.cjs"), 600, "browser"),
        SuiteSpec("build-container", _python(python, "scripts/image_quality_container.py", "--output", _output_arg(output, "container")), 900, "container"),
    ]
    return _validate(suites)


def manifest_dict(suites: Iterable[SuiteSpec], *, plan: str, rules_version: str = "1.0") -> dict:
    """Create the stable, JSON-serialisable inspection representation."""
    rows = [suite.as_dict() for suite in suites]
    return {"plan": plan, "rules_version": rules_version, "suite_count": len(rows), "suites": rows}
