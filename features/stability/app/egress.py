from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from .config import EGRESS_ALLOWLIST


class EgressDenied(ValueError):
    pass


def _allowlisted(host: str, address: ipaddress._BaseAddress | None = None) -> bool:
    normalized = host.casefold().rstrip(".")
    for item in EGRESS_ALLOWLIST:
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            if item.casefold().rstrip(".") == normalized:
                return True
        else:
            if address is not None and address in network:
                return True
    return False


def validate_url_sync(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise EgressDenied("地址格式或端口无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EgressDenied("地址必须是完整的 http:// 或 https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise EgressDenied("URL 不能携带用户名或密码")
    host = parsed.hostname.casefold().rstrip(".")
    try:
        literal = ipaddress.ip_address(host)
        addresses = (literal,)
    except ValueError:
        try:
            resolved = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise EgressDenied("无法解析上游地址") from exc
        addresses = tuple({ipaddress.ip_address(item[4][0]) for item in resolved})
    blocked = [str(address) for address in addresses if not address.is_global and not _allowlisted(host, address)]
    if blocked and not _allowlisted(host):
        raise EgressDenied("目标为内网、回环或保留地址；需要由部署者加入 STABILITY_EGRESS_ALLOWLIST")
    return url.rstrip("/")


async def validate_url(url: str) -> str:
    return await asyncio.to_thread(validate_url_sync, url)

