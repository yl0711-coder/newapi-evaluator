"""Bounded text delivery shared by stability and daily reports."""
import json
import re
from urllib.parse import urlsplit

import httpx
from .network import guarded_transport


def validate_webhook(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != "open.feishu.cn" or parsed.query or parsed.fragment
            or not re.fullmatch(r"/open-apis/bot/v2/hook/[A-Za-z0-9-]{1,200}", parsed.path)):
        raise ValueError("请输入飞书群机器人 HTTPS Webhook")
    return value


async def send_text(webhook, text, *, transport=None):
    payload = {"msg_type": "text", "content": {"text": text}}
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 20_000:
        raise ValueError("feishu_message_too_large")
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False,
                                 transport=transport or guarded_transport()) as client:
        async with client.stream("POST", webhook, json=payload) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 32768:
                    raise ValueError("feishu_response_too_large")
    data = json.loads(raw)
    if (not isinstance(data, dict) or not ({"code", "StatusCode"} & set(data))
            or any(type(data[field]) is not int or data[field] != 0 for field in ("code", "StatusCode") if field in data)):
        raise ValueError("feishu_delivery_rejected")
