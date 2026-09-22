#!/usr/bin/env python3
"""从干净提交的显式文件清单生成不含本地配置或凭据的资源包。"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ("hourly_channel_diagnostic.py", "channel_catalog.py", "manage.py", "Dockerfile", "compose.yaml",
         "nginx.conf", ".dockerignore", ".gitignore", "README.md", "DEPLOYMENT.md", "TESTING.md", "config.example.json",
         "scripts/test_all.py", "scripts/build_release.py", "tests/test_domain.py", "tests/test_http.py",
         "tests/test_catalog.py", "tests/browser_check.cjs")


def build(output: Path) -> Path:
    if output.resolve().is_relative_to(ROOT):
        raise ValueError("资源包必须位于源码目录外")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=ROOT, text=True):
        raise ValueError("请先固定干净候选提交")
    contents = {}
    for name in FILES:
        data = subprocess.check_output(["git", "show", f"{head}:{name}"], cwd=ROOT)
        if re.search(rb"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}", data):
            raise ValueError("资源文件命中凭据模式")
        contents[name] = data
    manifest = {"source_commit": head, "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
                "contains_credentials": False, "initial_behavior": "report_only"}
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"hourly-channel-diagnostic-{head[:7]}.zip"
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in contents.items():
            bundle.writestr("hourly-channel-diagnostic/" + name, data)
        bundle.writestr("hourly-channel-diagnostic/MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    (output / (archive.name + ".sha256")).write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build(args.output))
