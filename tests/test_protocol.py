import asyncio
import os
import unittest
from unittest.mock import patch

from relay_lab.adapter import OpenAIAdapter
from relay_lab.config import load
from relay_lab.mock import MockServer


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = load(overrides={'mock': {'chunks': 5, 'chunk_delay': .003,
                                         'accounts': [{'capacity': 50, 'latency': .006}]}})
        self.server = await MockServer(self.cfg['mock']).start()
        self.adapter = await OpenAIAdapter(self.server.base_url, timeout=.3).__aenter__()

    async def asyncTearDown(self):
        await self.adapter.__aexit__()
        await self.server.close()

    async def test_normal_response(self):
        result = await self.adapter.request('normal', 1, stream=False)
        self.assertTrue(result.success and result.complete)
        self.assertEqual(result.output_units, 5)
        self.assertEqual(result.status, 200)
        self.assertNotIn('content', result.public())

    async def test_normal_and_slow_sse(self):
        normal = await self.adapter.request('sse', 1)
        slow = await self.adapter.request('sse', 1, fault='slow_sse')
        self.assertTrue(normal.complete and slow.complete)
        self.assertEqual(normal.output_units, 5)
        self.assertGreater(slow.latency_ms, normal.latency_ms * 2)
        self.assertLess(normal.ttft_ms, normal.latency_ms)
        self.assertLess(slow.output_units_per_second, normal.output_units_per_second)

    async def test_status_errors(self):
        for code in (401, 429, 500):
            with self.subTest(code=code):
                result = await self.adapter.request('error', 1, fault=f'http_{code}')
                self.assertEqual(result.status, code)
                self.assertFalse(result.success)

    async def test_timeout_faults(self):
        for fault, error in [('connect_timeout', 'connect_timeout'), ('read_timeout', 'request_timeout')]:
            with self.subTest(fault=fault):
                result = await self.adapter.request('timeout', 1, fault=fault)
                self.assertFalse(result.success)
                self.assertEqual(result.error, error)
                self.assertGreater(result.latency_ms, 150)

    async def test_stream_faults(self):
        for fault, error in [('disconnect', 'stream_disconnect'), ('missing_done', 'incomplete_stream'),
                             ('malformed_sse', 'invalid_response'), ('network_drop', 'connection_error')]:
            with self.subTest(fault=fault):
                result = await self.adapter.request('broken', 1, fault=fault)
                self.assertFalse(result.complete)
                self.assertEqual(result.error, error)

    async def test_per_account_capacity_and_recovery(self):
        self.server.accounts[0].capacity = 2
        self.server.accounts[0].cooldown = .03
        results = await asyncio.gather(*(self.adapter.request('limit', 5, account=0) for _ in range(5)))
        self.assertEqual(sum(r.success for r in results), 2)
        self.assertEqual(sum(r.status == 429 for r in results), 3)
        await asyncio.sleep(.04)
        self.assertTrue((await self.adapter.request('recover', 1, account=0)).success)
        self.assertEqual(self.server.accounts[0].active, 0)

    async def test_independent_account_quality(self):
        from relay_lab.mock import Account
        self.server.accounts = [Account(capacity=2, latency=.005), Account(capacity=2, latency=.04),
                                Account(capacity=2, failure_rate=1)]
        a, b, c = await asyncio.gather(*(self.adapter.request('quality', 3, account=i) for i in range(3)))
        self.assertTrue(a.success and b.success)
        self.assertGreater(b.ttft_ms, a.ttft_ms + 20)
        self.assertEqual(c.status, 500)
        self.assertEqual([x.requests for x in self.server.accounts], [1, 1, 1])

    async def test_temporary_account_and_upstream_outage(self):
        self.server.disable([0], .04)
        self.assertEqual((await self.adapter.request('disabled', 1, account=0)).status, 401)
        await asyncio.sleep(.05)
        self.assertTrue((await self.adapter.request('restored', 1)).success)
        self.server.outage_until = __import__('time').monotonic() + .04
        result = await self.adapter.request('outage', 1)
        self.assertEqual(result.status, 503)
        self.assertTrue(result.collapse)
        await asyncio.sleep(.05)
        self.assertTrue((await self.adapter.request('restored', 1)).success)

    async def test_pool_queue_is_measured(self):
        async with OpenAIAdapter(self.server.base_url, timeout=1, connection_limit=1) as adapter:
            rows = await asyncio.gather(*(adapter.request('queue', 4) for _ in range(4)))
            self.assertTrue(all(r.success for r in rows))
            self.assertGreater(max(r.queue_ms for r in rows), 20)
            self.assertEqual(adapter.peak_active, 1)

    async def test_environment_proxy_disabled(self):
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://192.0.2.1:9', 'ALL_PROXY': 'http://192.0.2.1:9', 'NO_PROXY': ''}):
            async with OpenAIAdapter(self.server.base_url) as adapter:
                self.assertTrue((await adapter.request('proxy', 1)).success)

    async def test_redirect_not_followed(self):
        async def redirect(writer, headers, payload):
            await self.server._json(writer, 302, {}, {'Location': 'http://192.0.2.1:9/private'})
        self.server._completion = redirect
        result = await self.adapter.request('redirect', 1)
        self.assertEqual(result.status, 302)
        self.assertFalse(result.success)

    async def test_local_nonmock_is_rejected(self):
        async def bad_health(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}')
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(bad_health, '127.0.0.1', 0)
        try:
            with self.assertRaises(ValueError):
                async with OpenAIAdapter(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1'):
                    self.fail('Must not accept an unidentified local service')
        finally:
            server.close()
            await server.wait_closed()
