"""Loopback-only HTTP Mock. Each virtual account owns its own concurrency/health state."""
import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass

from .config import FAULTS


@dataclass
class Account:
    capacity: int = 3
    latency: float = 0.015
    jitter: float = 0
    failure_rate: float = 0
    cooldown: float = 0.04
    active: int = 0
    disabled_until: float = 0
    limited_until: float = 0
    requests: int = 0
    successes: int = 0


class MockServer:
    def __init__(self, config):
        self.config = config
        self.accounts = [Account(**{k: v for k, v in a.items() if k in Account.__dataclass_fields__ and k not in
                                  ('active', 'disabled_until', 'limited_until', 'requests', 'successes')})
                         for a in config['accounts']]
        self.random = random.Random(config['seed'])
        self.active = 0
        self.peak_active = 0
        self.crashed_until = 0
        self.outage_until = 0
        self.connections = set()
        self.tasks = set()
        self.server = None
        self.port = None
        self.cursor = 0
        self.executions = {}
        self.requests_received = 0
        self.slot_changed = asyncio.Condition()

    async def start(self, port=0):
        self.server = await asyncio.start_server(self._accept, '127.0.0.1', port, backlog=4096)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    @property
    def base_url(self):
        return f'http://127.0.0.1:{self.port}/v1'

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self.connections):
            writer.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.close()

    def disable(self, indexes, duration):
        for i in indexes:
            self.accounts[i].disabled_until = time.monotonic() + duration

    def health(self):
        now = time.monotonic()
        return {'service': 'relay-lab-mock', 'protocol': 1,
                'healthy': now >= max(self.crashed_until, self.outage_until),
                'accounts': len(self.accounts), 'active': self.active,
                'peak_active': self.peak_active,
                'healthy_account_count': sum(a.disabled_until <= now for a in self.accounts),
                'failed_account_count': sum(a.disabled_until > now for a in self.accounts)}

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.connections.add(writer)
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            lines = header.decode('latin-1').split('\r\n')
            method, path, _ = lines[0].split(' ', 2)
            headers = {k.lower(): v.strip() for line in lines[1:] if ':' in line for k, v in [line.split(':', 1)]}
            size = int(headers.get('content-length', '0'))
            if size < 0 or size > 65536:
                await self._json(writer, 413, {})
                return
            body = await asyncio.wait_for(reader.readexactly(size), 5)
            if method == 'GET' and path == '/v1/__relay_lab__/identity':
                await self._json(writer, 200, self.health())
            elif method == 'POST' and path == '/v1/chat/completions':
                await self._completion(writer, headers, json.loads(body))
            else:
                await self._json(writer, 404, {'error': 'not_found'})
        except (ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                ValueError, UnicodeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.connections.discard(writer)
            self.tasks.discard(task)

    async def _json(self, writer, status, value, extra=None):
        body = json.dumps(value).encode()
        headers = {'Content-Type': 'application/json', 'Content-Length': str(len(body)), 'Connection': 'close', **(extra or {})}
        writer.write(f'HTTP/1.1 {status} Mock\r\n'.encode() + ''.join(f'{k}: {v}\r\n' for k, v in headers.items()).encode() + b'\r\n' + body)
        await writer.drain()

    def _pick(self, selected):
        now = time.monotonic()
        if selected is not None:
            return int(selected) if 0 <= int(selected) < len(self.accounts) else None
        choices = [(self.cursor + i) % len(self.accounts) for i in range(len(self.accounts))]
        self.cursor = (self.cursor + 1) % len(self.accounts)
        return next((i for i in choices if self.accounts[i].disabled_until <= now
                     and self.accounts[i].limited_until <= now and self.accounts[i].active < self.accounts[i].capacity), None)

    async def _completion(self, writer, headers, payload):
        self.requests_received += 1
        now = time.monotonic()
        fault = headers.get('x-relay-fault', 'normal')
        if fault not in FAULTS:
            await self._json(writer, 400, {'error': 'unknown_fault'})
            return
        if now < self.crashed_until or self.active + 1 >= self.config['crash_at']:
            if now >= self.crashed_until:
                self.crashed_until = now + self.config['crash_duration']
            await self._json(writer, 503, {}, {'X-Relay-Collapse': '1'})
            return
        if now < self.outage_until:
            await self._json(writer, 503, {}, {'X-Relay-Collapse': '1'})
            return
        if self.config.get('admission_policy') == 'queue':
            try:
                async with asyncio.timeout(self.config['queue_timeout']):
                    async with self.slot_changed:
                        while True:
                            index = self._pick(headers.get('x-relay-account'))
                            if index is not None and self.accounts[index].active < self.accounts[index].capacity:
                                break
                            await self.slot_changed.wait()
            except TimeoutError:
                await self._json(writer, 429, {})
                return
        else:
            index = self._pick(headers.get('x-relay-account'))
        if index is None:
            await self._json(writer, 429, {}, {'Retry-After': '0.04'})
            return
        account = self.accounts[index]
        extra = {'X-Relay-Account': str(index)}
        if account.disabled_until > now:
            await self._json(writer, 401, {}, extra)
            return
        if account.active >= account.capacity or now < account.limited_until:
            account.limited_until = max(account.limited_until, now + account.cooldown)
            await self._json(writer, 429, {}, extra)
            return
        account.active += 1
        account.requests += 1
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            if fault.startswith('http_'):
                await self._json(writer, int(fault.removeprefix('http_')), {}, extra)
                return
            if fault == 'network_drop':
                writer.transport.abort()
                return
            # A real TCP connection timeout is injected by the transport in adapter.py.
            if fault in ('connect_timeout', 'read_timeout'):
                await asyncio.sleep(float(headers.get('x-relay-timeout', '0.5')) * 2 + 0.05)
            if self.random.random() < account.failure_rate:
                await self._json(writer, 500, {}, extra)
                return
            delay = account.latency + self.random.uniform(0, account.jitter + self.config['jitter'])
            if fault == 'jitter':
                delay += self.random.uniform(0.01, 0.05)
            if self.active >= self.config['slow_at']:
                delay *= self.config['slow_factor']
            await asyncio.sleep(delay)
            chunks = min(int(headers.get('x-relay-chunks', self.config['chunks'])), 1000000)
            chunk_delay = min(float(headers.get('x-relay-chunk-delay', self.config['chunk_delay'])), 86400)
            if fault == 'slow_sse':
                chunk_delay *= 6
            key = headers.get('idempotency-key', '')
            if not payload.get('stream', True):
                await self._json(writer, 200, {'choices': [{'message': {'content': 'x' * chunks}, 'finish_reason': 'stop'}],
                                                'usage': {'completion_tokens': chunks}}, extra)
                account.successes += 1
                if key:
                    self.executions[key] = self.executions.get(key, 0) + 1
                return
            writer.write(b'HTTP/1.1 200 Mock\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n'
                         + f'X-Relay-Account: {index}\r\n\r\n'.encode())
            await writer.drain()

            async def event(data):
                frame = ('data: ' + data + '\n\n').encode()
                writer.write(f'{len(frame):x}\r\n'.encode() + frame + b'\r\n')
                await writer.drain()

            if fault == 'malformed_sse':
                await event('{invalid-json')
            for i in range(chunks):
                await asyncio.sleep(chunk_delay)
                await event(json.dumps({'choices': [{'delta': {'content': 'x'}, 'finish_reason': None}]}))
                if fault == 'disconnect' and i >= chunks // 2:
                    writer.transport.abort()
                    return
            await event(json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]}))
            if fault != 'missing_done':
                await event('[DONE]')
            writer.write(b'0\r\n\r\n')
            await writer.drain()
            if fault not in ('missing_done', 'malformed_sse'):
                account.successes += 1
                if key:
                    self.executions[key] = self.executions.get(key, 0) + 1
        finally:
            self.active -= 1
            account.active -= 1
            async with self.slot_changed:
                self.slot_changed.notify_all()
