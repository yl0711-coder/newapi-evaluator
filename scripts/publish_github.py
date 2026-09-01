"""将权威源码安全同步到 github-publish，并创建 GitHub 功能分支。

发布边界只包含功能源码和明确列出的题库；运行数据、渠道配置与凭据不会同步。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

from repo_security_scan import publication_candidates


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT / "github-publish"
TARGET_SLUG = "yl0711-coder/newapi-evaluator"


def run(*command: str, cwd: Path = ROOT, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip() if capture else ""


def verify_target_repo() -> str:
    remote = run("git", "remote", "get-url", "origin", cwd=REPO, capture=True)
    if "github.com" not in remote:
        raise RuntimeError("origin 不是 GitHub 仓库")
    slug = remote.removesuffix(".git").replace("https://github.com/", "").split("github.com:")[-1]
    if slug.casefold() != TARGET_SLUG.casefold():
        raise RuntimeError(f"安全拒绝：origin 必须是 {TARGET_SLUG}，当前是 {slug}")
    return slug


def quality_gate() -> None:
    run(sys.executable, "selftest_repository_security.py")
    run(
        sys.executable,
        "-m",
        "compileall",
        "-q",
        "app",
        "run.py",
        "local_runner.py",
        "manage_accounts.py",
    )
    node = shutil.which("node")
    if not node:
        raise RuntimeError("找不到 node，不能校验前端 JavaScript")
    for script in sorted((ROOT / "web").glob("*.js")):
        run(node, "--check", str(script))


def verified_publish_repo() -> Path:
    root = ROOT.resolve()
    repository = REPO.resolve()
    if repository.parent != root or repository.name != "github-publish":
        raise RuntimeError(f"发布工作副本路径异常：{repository}")
    if not (repository / ".git").exists():
        raise RuntimeError("github-publish 不是 Git 仓库")
    return repository


def sync_source() -> None:
    repository = verified_publish_repo()
    source_files, failures = publication_candidates(ROOT)
    if failures:
        details = "\n  - ".join(failures)
        raise RuntimeError(f"发布源文件安全扫描失败：\n  - {details}")

    for child in repository.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    for relative, source in source_files.items():
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    run(sys.executable, "scripts/repo_security_scan.py", str(repository))


def main() -> None:
    parser = argparse.ArgumentParser(description="安全同步源码或创建 GitHub 功能分支")
    parser.add_argument("--summary", help="Commit 摘要")
    parser.add_argument("--sync-only", action="store_true", help="只刷新 github-publish 并扫描，不访问 GitHub")
    args = parser.parse_args()

    verified_publish_repo()
    if args.sync_only:
        sync_source()
        print("github-publish 已按功能源码与题库白名单同步，未访问 GitHub")
        return
    if not args.summary:
        parser.error("创建功能分支时必须提供 --summary")

    slug = verify_target_repo()
    quality_gate()
    sync_source()
    if not run("git", "status", "--porcelain", cwd=REPO, capture=True):
        print("源码无变化，不创建空 PR")
        return
    branch = f"codex/automation-{time.strftime('%Y%m%d-%H%M%S')}"
    run("git", "checkout", "-b", branch, cwd=REPO)
    run("git", "add", "--all", cwd=REPO)
    staged = run("git", "diff", "--cached", "--name-only", cwd=REPO, capture=True)
    forbidden = [
        name
        for name in staged.splitlines()
        if any(part.casefold() in {"data", "runtime-data", "backups", "secrets", "exports"}
               for part in Path(name).parts)
        or Path(name).suffix.casefold() in {".db", ".sqlite", ".sqlite3", ".key", ".log"}
    ]
    if forbidden:
        raise RuntimeError(f"暂存区出现禁止文件：{forbidden}")
    run("git", "commit", "-m", args.summary, cwd=REPO)
    run("git", "push", "--set-upstream", "origin", branch, cwd=REPO)
    print(f"功能分支已推送，请创建 PR：https://github.com/{slug}/compare/main...{branch}?expand=1")


if __name__ == "__main__":
    main()
