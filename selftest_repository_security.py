"""验证 GitHub 发布前扫描器既放行源码，也拒绝运行数据与伪造凭据。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCANNER = ROOT / "scripts" / "repo_security_scan.py"
checks: list[tuple[str, bool, str]] = []


def run_scan(files: dict[str, str | bytes]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="repo-security-selftest-") as directory:
        workspace = Path(directory)
        (workspace / "repo-allowlist.txt").write_text(
            "repo-allowlist.txt\napp/\n", encoding="utf-8",
        )
        for relative, content in files.items():
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCANNER), str(workspace)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )


def record(name: str, passed: bool, detail: str = "") -> None:
    checks.append((name, passed, detail))
    print(f"  {'OK  ' if passed else 'FAIL'} {name}{f'：{detail}' if detail else ''}")


docker_copy_lines = {
    line.strip()
    for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    if line.strip().upper().startswith("COPY ")
}
expected_copy_lines = {
    "COPY requirements.txt ./",
    "COPY app ./app",
    "COPY web ./web",
    "COPY run.py local_runner.py manage_accounts.py ./",
}
record(
    "Docker 镜像仅复制运行功能与题库",
    docker_copy_lines == expected_copy_lines,
    ", ".join(sorted(docker_copy_lines)),
)

docker_ignores = {
    line.strip()
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
}
required_docker_ignores = {
    ".env",
    ".env.*",
    "**/data",
    "**/runtime-data",
    "**/backups",
    "**/*.db",
    "**/*.key",
    "**/*channel*.json",
}
record(
    "Docker 构建上下文排除运行数据与渠道配置",
    required_docker_ignores <= docker_ignores,
    ", ".join(sorted(required_docker_ignores - docker_ignores)),
)


clean = run_scan({"app/main.py": "print('safe source')\n"})
record("正常源码通过", clean.returncode == 0, clean.stdout.strip())

question_bank = run_scan({"app/hard_items.json": "[]\n"})
record("仅放行明确题库 JSON", question_bank.returncode == 0, question_bank.stdout.strip())

unknown_json = run_scan({"app/selected_items.json": "[]\n"})
record(
    "拒绝未登记 JSON 数据",
    unknown_json.returncode != 0 and "文件类型或数据文件" in unknown_json.stdout,
    unknown_json.stdout.strip(),
)

channel_data = run_scan({"app/data/channels.json": "[]\n"})
record(
    "拒绝渠道运行数据目录",
    channel_data.returncode != 0 and "敏感或运行文件" in channel_data.stdout,
    channel_data.stdout.strip(),
)

environment = run_scan({"app/main.py": "pass\n", "app/.env": "TOKEN=private\n"})
record("拒绝 .env", environment.returncode != 0 and "敏感或运行文件" in environment.stdout,
       environment.stdout.strip())

database = run_scan({"app/main.py": "pass\n", "app/history.db": b"SQLite format 3\x00"})
record("拒绝数据库", database.returncode != 0 and "敏感或运行文件" in database.stdout,
       database.stdout.strip())

synthetic_key = "s" + "k-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + "1234567890"
fake_key = run_scan({"app/main.py": f"KEY = {synthetic_key!r}\n"})
record("伪造密钥触发扫描", fake_key.returncode != 0 and "疑似 api_key" in fake_key.stdout,
       fake_key.stdout.strip())

failed = [name for name, passed, _ in checks if not passed]
print("\n失败项：" + ("、".join(failed) if failed else "无"))
raise SystemExit(1 if failed else 0)
