"""统一出站地址策略：默认仅公网，内网目标必须精确加入白名单。"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from .config import EGRESS_ALLOWLIST, EGRESS_MAX_REDIRECTS, EGRESS_MAX_RESPONSE_BYTES


class EgressDenied(ValueError):
    pass


def _normalized_host(host: str) -> str:
    return host.strip().rstrip(".").casefold()


def _allowlisted(host: str, address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None) -> bool:
    normalized = _normalized_host(host)
    for entry in EGRESS_ALLOWLIST:
        candidate = entry.strip()
        try:
            network = ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            if _normalized_host(candidate) == normalized:
                return True
        else:
            if address is not None and address in network:
                return True
    return False


def validate_host(host: str, port: int | None = None) -> tuple[str, ...]:
    normalized = _normalized_host(host)
    if not normalized:
        raise EgressDenied("地址缺少主机名")
    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = (literal,)
    else:
        try:
            resolved = socket.getaddrinfo(
                normalized, port, type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise EgressDenied(f"无法解析主机名：{host}") from exc
        addresses = tuple({ipaddress.ip_address(item[4][0]) for item in resolved})
    if not addresses:
        raise EgressDenied(f"主机名没有可用地址：{host}")
    blocked = [str(address) for address in addresses
               if not address.is_global and not _allowlisted(normalized, address)]
    if blocked and not _allowlisted(normalized):
        raise EgressDenied(
            "目标解析到内网、回环或保留地址；如确需访问，请由部署者加入"
            f" TEST_EGRESS_ALLOWLIST（已拦截：{', '.join(sorted(blocked))}）"
        )
    return tuple(sorted(str(address) for address in addresses))


def validate_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise EgressDenied("地址格式或端口无效") from exc
    if parsed.scheme not in {"http", "https"}:
        raise EgressDenied("只允许 http 或 https 上游地址")
    if not parsed.hostname:
        raise EgressDenied("地址缺少主机名")
    if parsed.username is not None or parsed.password is not None:
        raise EgressDenied("上游地址不能在 URL 中携带用户名或密码")
    validate_host(parsed.hostname, port or (443 if parsed.scheme == "https" else 80))
    return url


async def guard_httpx_request(request: httpx.Request) -> None:
    try:
        await asyncio.to_thread(validate_url, str(request.url))
    except EgressDenied as exc:
        from . import store
        store.record_system_alert(
            f"egress-denied:{request.url.host}", "egress_denied",
            "出站地址被安全策略拦截", str(exc), "warning",
        )
        raise


async def guard_httpx_response(response: httpx.Response) -> None:
    """重定向的每个请求均复验，并提前拒绝已知超大正文。"""
    await asyncio.to_thread(validate_url, str(response.url))
    value = response.headers.get("content-length")
    if value:
        try:
            size = int(value)
        except ValueError:
            size = 0
        if size > EGRESS_MAX_RESPONSE_BYTES:
            raise EgressDenied(
                f"响应声明大小 {size} 字节，超过上限 {EGRESS_MAX_RESPONSE_BYTES} 字节"
            )


def ensure_response_size(response: httpx.Response) -> None:
    """对非流式响应执行实际字节上限，覆盖无 Content-Length 的情况。"""
    try:
        size = len(response.content)
    except (httpx.ResponseNotRead, AttributeError):
        return
    if size > EGRESS_MAX_RESPONSE_BYTES:
        raise EgressDenied(
            f"响应实际大小 {size} 字节，超过上限 {EGRESS_MAX_RESPONSE_BYTES} 字节"
        )


def event_hooks() -> dict[str, list]:
    return {"request": [guard_httpx_request], "response": [guard_httpx_response]}
