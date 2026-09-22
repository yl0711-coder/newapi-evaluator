"""导入带渠道标题的 TOML/URL+Key 文档；不执行其中任何设置或指令。"""
from __future__ import annotations

import hashlib
import re
import tomllib
from typing import Any

from hourly_channel_diagnostic import validate_endpoint

HEADER = re.compile(r"(.+?)[-_ ]?(\d+(?:\.\d+)?)\s*[xX×]")


def parse_document(text: str, default_model: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sections = []
    for line in text.splitlines():
        label = line.strip()
        match = HEADER.fullmatch(label)
        if match and not any(c in label for c in '=:/"'):
            sections.append({"label": label, "provider": match[1].rstrip("-_ "),
                             "multiplier": float(match[2]), "lines": []})
        elif label:
            if not sections:
                raise ValueError("文档须以渠道名称和倍率标题开头")
            sections[-1]["lines"].append(line)
    if not sections:
        raise ValueError("未识别到渠道标题")
    channels, credentials = [], {}
    for number, section in enumerate(sections, 1):
        block = "\n".join(section["lines"])
        try:
            headers = {}
            if re.search(r"^\s*model_provider\s*=", block, re.M):
                parsed = tomllib.loads(block)
                provider = parsed["model_providers"][parsed["model_provider"]]
                base_url = provider["base_url"].strip()
                key = provider["experimental_bearer_token"].strip()
                headers = provider.get("http_headers", {})
                model = parsed.get("model")
                if set(headers) - {"x-openai-actor-authorization"}:
                    raise ValueError
            else:
                urls = re.findall(r'https?://[^\s"\x27<>]+', block)
                keys = re.findall(r"\bsk-[A-Za-z0-9_-]+", block)
                if len(set(urls)) != 1 or len(set(keys)) != 1:
                    raise ValueError
                base_url, key, model = urls[0], keys[0], None
            validate_endpoint(base_url)
            if not isinstance(key, str) or not key or any(c in key for c in "\r\n"):
                raise ValueError
            if not isinstance(headers, dict) or any(not isinstance(v, str) or any(c in v for c in "\r\n") for v in headers.values()):
                raise ValueError
            if model is not None and (not isinstance(model, str) or not model.strip()):
                raise ValueError
            model_confirmed = bool(model or default_model)
            model = model or default_model or "gpt-5.6-sol"
            identity = hashlib.sha256((section["label"].casefold() + "\n" + base_url.rstrip("/")).encode()).hexdigest()[:16]
            channel_id = "ch_" + identity
            variable = "CHANNEL_" + identity.upper() + "_API_KEY"
            if variable in credentials:
                raise ValueError
            channels.append({"id": channel_id, "name": section["label"], "provider": section["provider"],
                             "multiplier": section["multiplier"], "base_url": base_url, "model": model,
                             "model_confirmed": model_confirmed, "api_key_env": variable,
                             "protocol": "openai", "enabled": False})
            credentials[variable] = {"api_key": key, "headers": headers}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"第 {number} 个渠道配置无效或重复；请检查该段字段") from None
    return channels, credentials
