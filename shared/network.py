"""Validate resolved IPs at connection time while retaining HTTP Host and TLS SNI."""
import asyncio
import ipaddress
import os
import socket
import time

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend


def permitted(host, address):
    ip = ipaddress.ip_address(address)
    if ip.is_global and not ip.is_multicast and not ip.is_reserved and not ip.is_unspecified:
        return True
    for entry in os.getenv("PLATFORM_EGRESS_ALLOWLIST", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry.casefold().rstrip(".") == host.casefold().rstrip("."):
            return True
        try:
            if ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            pass
    return False


class PublicNetwork(AutoBackend):
    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        started = time.monotonic()
        try:
            resolved = await asyncio.wait_for(asyncio.to_thread(
                socket.getaddrinfo, host, port, type=socket.SOCK_STREAM), timeout)
        except asyncio.TimeoutError as exc:
            raise httpcore.ConnectTimeout("上游域名解析超时") from exc
        except socket.gaierror as exc:
            raise httpcore.ConnectError("上游域名无法解析") from exc
        addresses = list(dict.fromkeys(item[4][0] for item in resolved))
        if not addresses or any(not permitted(host, value) for value in addresses):
            raise httpcore.ConnectError("内网或保留地址不在部署者设置的访问白名单中")
        last_error = None
        for address in addresses:
            remaining = None if timeout is None else max(0, timeout - (time.monotonic() - started))
            try:
                return await super().connect_tcp(address, port, remaining, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise last_error


def guarded_transport():
    transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    # httpx 0.28.1 / httpcore 1.0.9 are pinned; only the socket backend is replaced.
    transport._pool._network_backend = PublicNetwork()
    return transport
