from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


FORBIDDEN_KEYS = {
    "api_key", "apikey", "authorization", "cookie", "password", "secret",
    "token", "prompt", "request_body", "response_body", "raw_response",
    "reasoning", "content",
}
LIKELY_SECRET = re.compile(
    r"(?:\bBearer\s+[A-Za-z0-9._~+/-]{12,}|\bsk-[A-Za-z0-9._-]{12,})",
    re.IGNORECASE,
)
MAX_SUMMARY_BYTES = 128 * 1024


def normalize_channel_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("渠道地址必须是完整的 HTTP 或 HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("渠道地址不能包含用户名或密码")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def validate_safe_summary(value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_SUMMARY_BYTES:
        raise ValueError("测试摘要不能超过 128 KiB")

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
                if normalized in FORBIDDEN_KEYS:
                    raise ValueError(f"测试摘要不能包含敏感字段：{key}")
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            if LIKELY_SECRET.search(item):
                raise ValueError("测试摘要疑似包含访问凭据")
            if len(item) > 2_000:
                raise ValueError("测试摘要中的单个文本值不能超过 2000 字符")

    walk(value)
