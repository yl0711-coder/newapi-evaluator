from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.registry import get_registry


TAG_PATTERN = re.compile(r"\b(codex|claude)-([0-9]+(?:\.[0-9]+)?)x\b", re.I)


def tag_near(lines: list[str], start: int, count: int = 6) -> tuple[str, float] | None:
    for line in lines[start:min(len(lines), start + count)]:
        match = TAG_PATTERN.search(line)
        if match:
            return match.group(1).casefold(), float(match.group(2))
    return None


def entry(url: str, key: str, tag: tuple[str, float] | None, source_kind: str) -> dict[str, Any] | None:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not key.strip() or not tag:
        return None
    return {
        "name": parsed.hostname.casefold(),
        "base_url": url.strip().rstrip("/"),
        "scope": tag[0],
        "multiplier": tag[1],
        "api_key": key.strip(),
        "source_kind": source_kind,
    }


def extract(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("channels"), list):
        data = data["channels"]
    elif isinstance(data, dict):
        data = [data]
    if isinstance(data, list) and data:
        records = []
        for item in data:
            if not isinstance(item, dict):
                break
            url = item.get("base_url") or item.get("baseURL") or item.get("url")
            key = item.get("api_key") or item.get("apiKey") or item.get("key")
            rate = item.get("multiplier", item.get("upstream_multiplier"))
            if not url or not key or rate is None:
                break
            records.append({"name": item.get("name", ""), "base_url": url, "api_key": key,
                            "multiplier": rate, "scope": item.get("scope", ""),
                            "source_kind": "json-import", "note": item.get("note", "")})
        else:
            return records
    lines = raw.splitlines()
    found: list[dict[str, Any]] = []

    for index, line in enumerate(lines):
        base_match = re.search(r'"baseURL"\s*:\s*"([^"]+)"', line)
        if base_match:
            for offset in range(index + 1, min(index + 5, len(lines))):
                key_match = re.search(r'"apiKey"\s*:\s*"([^"]+)"', lines[offset])
                if key_match:
                    value = entry(base_match.group(1), key_match.group(1), tag_near(lines, offset), "provider-json")
                    if value:
                        found.append(value)
                    break

        anthropic_match = re.search(r'export\s+ANTHROPIC_BASE_URL="([^"]+)"', line)
        if anthropic_match:
            for offset in range(index + 1, min(index + 6, len(lines))):
                key_match = re.search(r'export\s+ANTHROPIC_AUTH_TOKEN="([^"]+)"', lines[offset])
                if key_match:
                    value = entry(anthropic_match.group(1), key_match.group(1), tag_near(lines, offset), "anthropic-env")
                    if value:
                        found.append(value)
                    break

        newapi_match = re.search(
            r'\{"_type":"newapi_channel_conn","key":"([^"]+)","url":"([^"]+)"\}\s*(.*)',
            line,
        )
        if newapi_match:
            tag_match = TAG_PATTERN.search(newapi_match.group(3))
            tag = (tag_match.group(1).casefold(), float(tag_match.group(2))) if tag_match else None
            value = entry(newapi_match.group(2), newapi_match.group(1), tag, "newapi-link")
            if value:
                found.append(value)

        toml_base = re.match(r'\s*base_url\s*=\s*"([^"]+)"', line)
        if toml_base:
            for offset in range(index + 1, min(index + 15, len(lines))):
                key_match = re.match(r'\s*experimental_bearer_token\s*=\s*"([^"]+)"', lines[offset])
                if key_match:
                    value = entry(toml_base.group(1), key_match.group(1), tag_near(lines, offset), "codex-toml")
                    if value:
                        found.append(value)
                    break

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, float]] = set()
    for item in found:
        identity = (item["base_url"], item["api_key"], item["scope"], item["multiplier"])
        if identity not in seen:
            seen.add(identity)
            unique.append(item)
    return unique


def main() -> None:
    parser = argparse.ArgumentParser(description="从混合配置文本导入渠道资料")
    parser.add_argument("source")
    args = parser.parse_args()
    source = Path(args.source).expanduser().resolve()
    records = extract(source.read_text(encoding="utf-8"))
    if not records:
        raise SystemExit("没有识别到同时包含 Base URL、密钥和倍率的渠道资料")

    result = get_registry().import_records(records)
    print(f"识别 {len(records)} 条，新增 {result['added']} 条，跳过重复 {result['skipped']} 条；未创建任何定时计划。")


if __name__ == "__main__":
    main()
