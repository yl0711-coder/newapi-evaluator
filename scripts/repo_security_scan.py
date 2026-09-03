"""发布前仓库白名单、运行数据与凭据扫描。"""
from __future__ import annotations

import fnmatch
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path


DEFAULT_ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".idea",
    ".vscode",
    "node_modules",
    "github-clean",
    "github-publish",
    "dist",
    "dist-simple",
}
FORBIDDEN_PARTS = {
    "data",
    "runtime-data",
    "runtime_data",
    "backups",
    "backup",
    "logs",
    "log",
    "secrets",
    "exports",
    "uploads",
    "report-data",
    "artifacts",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".key",
    ".pem",
    ".pfx",
    ".p12",
    ".log",
    ".bak",
    ".backup",
    ".dump",
    ".enc",
}
SOURCE_SUFFIXES = {".py", ".js", ".css", ".html", ".mjs"}
ALLOWED_EXACT_FILES = {
    ".dockerignore",
    ".env.example",
    ".gitignore",
    ".github/CODEOWNERS",
    ".github/pull_request_template.md",
    ".github/workflows/quality.yml",
    ".github/workflows/release.yml",
    "Dockerfile",
    "README.md",
    "compose.yml",
    "repo-allowlist.txt",
    "requirements.txt",
    "tools/domestic-model-speed-bench/.gitignore",
    "tools/domestic-model-speed-bench/README.md",
    "tools/domestic-model-speed-bench/requirements.txt",
    "tools/domestic-model-speed-bench/start.ps1",
}
QUESTION_BANK_FILES = {
    "app/hard_items.json",
    "tools/domestic-model-speed-bench/questions.json",
}
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{30,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "api_key": re.compile(r"\bsk-[A-Za-z0-9_-]{28,}\b"),
    "bearer": re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{32,}", re.I),
}
TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{40,}(?![A-Za-z0-9])")
DUMMY_MARKERS = {
    "test",
    "wrong",
    "example",
    "abcdefgh",
    "workspace",
    "specialty",
    "legacy",
    "local",
    "lifecycle",
    "review",
    "launch",
    "secret-value",
}


def rules(root: Path) -> list[str]:
    allowlist = root / "repo-allowlist.txt"
    if not allowlist.exists():
        raise RuntimeError("缺少 repo-allowlist.txt")
    return [
        line.strip()
        for line in allowlist.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def allowed(relative: str, patterns: list[str]) -> bool:
    top = relative.split("/", 1)[0]
    for pattern in patterns:
        if pattern.endswith("/"):
            subtree = pattern[:-1]
            if (
                relative == subtree
                or relative.startswith(pattern)
                or subtree.startswith(f"{relative}/")
            ):
                return True
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(top, pattern):
            return True
    return False


def ignored(relative_path: Path) -> bool:
    return any(part in IGNORED_PARTS for part in relative_path.parts)


def entropy(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def dummy_allowed(path: str, token: str) -> bool:
    test_file = Path(path).name.startswith("selftest_") or Path(path).name == "mock_upstream.py"
    return test_file and any(marker in token.casefold() for marker in DUMMY_MARKERS)


def structural_problem(path: Path, relative_path: Path) -> str | None:
    relative = relative_path.as_posix()
    folded_parts = {part.casefold() for part in relative_path.parts}
    name = path.name.casefold()
    if path.is_symlink():
        return f"符号链接不允许进入仓库：{relative}"
    if folded_parts & FORBIDDEN_PARTS:
        kind = "运行数据目录" if path.is_dir() else "敏感或运行文件"
        return f"{kind}不允许进入仓库：{relative}{'/' if path.is_dir() else ''}"
    if path.is_dir():
        return None
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return f"敏感或运行文件不允许进入仓库：{relative}"
    if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
        return f"敏感或运行文件不允许进入仓库：{relative}"
    if (
        path.suffix.casefold() not in SOURCE_SUFFIXES
        and relative not in ALLOWED_EXACT_FILES
        and relative not in QUESTION_BANK_FILES
    ):
        return f"文件类型或数据文件不在允许清单：{relative}"
    if path.stat().st_size > 10 * 1024 * 1024:
        return f"源码文件异常大（>10MB）：{relative}"
    return None


def content_problems(path: Path, relative: str) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [f"仓库只允许 UTF-8 文本文件：{relative}"]
    failures = []
    for label, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(text):
            if not dummy_allowed(relative, match.group(0)):
                line = text.count("\n", 0, match.start()) + 1
                failures.append(f"疑似 {label}：{relative}:{line}")
    for match in TOKEN_PATTERN.finditer(text):
        token = match.group(0)
        if entropy(token) >= 4.6 and not dummy_allowed(relative, token):
            line = text.count("\n", 0, match.start()) + 1
            failures.append(f"疑似高熵字符串：{relative}:{line}")
    return failures


def publication_candidates(root: Path) -> tuple[dict[str, Path], list[str]]:
    """返回可同步源码；白名单外文件不发布，白名单内异常会阻止发布。"""
    root = root.resolve()
    patterns = rules(root)
    files: dict[str, Path] = {}
    failures: list[str] = []
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        publishable_directories = []
        for name in directory_names:
            path = current_path / name
            relative_path = path.relative_to(root)
            relative = relative_path.as_posix()
            if ignored(relative_path) or not allowed(relative, patterns):
                continue
            problem = structural_problem(path, relative_path)
            if problem:
                failures.append(problem)
                continue
            publishable_directories.append(name)
        directory_names[:] = publishable_directories

        for name in file_names:
            path = current_path / name
            relative_path = path.relative_to(root)
            relative = relative_path.as_posix()
            if ignored(relative_path) or not allowed(relative, patterns):
                continue
            problem = structural_problem(path, relative_path)
            if problem:
                failures.append(problem)
                continue
            failures.extend(content_problems(path, relative))
            files[relative] = path
    return files, failures


def scan(root: Path = DEFAULT_ROOT) -> list[str]:
    root = root.resolve()
    patterns = rules(root)
    failures: list[str] = []
    for path in root.rglob("*"):
        relative_path = path.relative_to(root)
        relative = relative_path.as_posix()
        if ignored(relative_path):
            continue
        problem = structural_problem(path, relative_path)
        if problem:
            failures.append(problem)
            continue
        if not allowed(relative, patterns):
            failures.append(f"不在仓库白名单：{relative}{'/' if path.is_dir() else ''}")
            continue
        if path.is_file():
            failures.extend(content_problems(path, relative))
    return failures


if __name__ == "__main__":
    scan_root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ROOT
    problems = scan(scan_root)
    if problems:
        print("仓库安全扫描失败：")
        for problem in problems:
            print(f"  - {problem}")
        raise SystemExit(1)
    print("仓库安全扫描通过：仅包含功能源码、明确题库，且无运行数据或凭据异常")
