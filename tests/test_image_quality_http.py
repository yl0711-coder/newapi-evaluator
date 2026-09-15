"""Real local HTTP framing fixtures; no user requests, images or credentials."""
import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from features.image_quality.engine import GenerationInput, JSONBoundary, generate

FIXTURE = (Path(__file__).parent / 'fixtures/image_quality/response.json').read_bytes()


class BoundaryTests(unittest.TestCase):
    def test_nested_unicode_strings_and_escape_boundaries(self):
        value = {'data': [{'text': '合成的 { bracket } [ ] " quote \\ slash \\\" end'}], 'ok': True}
        raw = json.dumps(value, ensure_ascii=False).encode()
        for prefix in (b'', b' \n', b'\xef\xbb\xbf'):
            encoded = prefix + raw
            for size in (1, 2, 3, 7, len(encoded)):
                boundary = JSONBoundary()
                for offset in range(0, len(encoded), size):
                    end = min(len(encoded), offset + size)
                    self.assertEqual(boundary.feed(encoded[offset:end]), end == len(encoded))
                    self.assertFalse(boundary.feed(b''))

    def test_incomplete_strings_never_finish_at_inner_braces(self):
        for raw in (b'{"data":[{"text":"}","more":', b'{"text":"unterminated }',
                    b'{"text":"escaped \\" }', b'{"data":['):
            boundary = JSONBoundary()
            self.assertFalse(any(boundary.feed(bytes([byte])) for byte in raw))


class HTTPReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handlers = set()
        self.release = asyncio.Event()
        self.body_sent = asyncio.Event()
        self.peer_closed = asyncio.Event()
        self.mode = 'chunked'
        self.payload = FIXTURE
        self.guard = patch.dict(os.environ, {'PLATFORM_EGRESS_ALLOWLIST':'127.0.0.1'})
        self.guard.start()
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.handlers):
            task.cancel()
        await asyncio.gather(*list(self.handlers), return_exceptions=True)
        self.guard.stop()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            request = await reader.readuntil(b'\r\n\r\n')
            headers = {key.lower():value.strip() for line in request.split(b'\r\n')[1:]
                       if b':' in line for key,value in [line.split(b':',1)]}
            await reader.readexactly(int(headers.get(b'content-length', b'0')))
            status = b'401 Unauthorized' if self.mode == 'http_error' else b'200 OK'
            content_type = b'text/event-stream' if self.mode == 'sse' else b'application/json'
            writer.write(b'HTTP/1.1 ' + status + b'\r\nContent-Type: ' + content_type +
                         b'\r\nTransfer-Encoding: chunked\r\n\r\n')
            await writer.drain()
            if self.mode == 'slow_headers_only':
                await self.release.wait()
            # Deliberately omit the final 0-length chunk. Completion is controlled by the client.
            for offset in range(0, len(self.payload), 37):
                data = self.payload[offset:offset+37]
                writer.write(format(len(data),'x').encode()+b'\r\n'+data+b'\r\n')
                await writer.drain()
                await asyncio.sleep(0)
            self.body_sent.set()
            await reader.read()
            self.peer_closed.set()
        except (ConnectionError, asyncio.IncompleteReadError):
            self.peer_closed.set()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.handlers.discard(task)

    def body(self, **overrides):
        return GenerationInput(base_url=f'http://127.0.0.1:{self.port}/v1',
                               api_key='synthetic-receipt-key', prompt='Synthetic local image receipt.',
                               confirm_live=True, timeout_seconds=1, **overrides)

    async def test_complete_image_json_returns_before_http_terminator(self):
        report = await generate(self.body())
        self.assertTrue(self.body_sent.is_set())
        self.assertEqual(report['status'], 'success')
        self.assertTrue(base64.b64decode(report['image']['b64_json']).startswith(b'\x89PNG'))
        self.assertEqual(report['diagnostics']['response_completion'], 'json_complete')
        self.assertEqual(report['diagnostics']['received_bytes'], len(FIXTURE))
        await asyncio.wait_for(self.peer_closed.wait(), 1)

    async def test_partial_json_waits_and_reports_received_bytes_at_deadline(self):
        self.payload = FIXTURE[:-16]
        with self.assertLogs('image_quality.progress', level=logging.INFO) as captured:
            report = await generate(self.body())
        self.assertEqual(report['error_code'], 'timeout_result_unknown')
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['diagnostics']['phase'], 'receiving_body')
        self.assertEqual(report['diagnostics']['received_bytes'], len(self.payload))
        self.assertIsNone(report['diagnostics']['response_completion'])
        self.assertFalse('image' in report)
        final = json.loads(captured.records[-1].getMessage())
        self.assertEqual(final['status'], 'failed')
        self.assertEqual(final['phase'], 'receiving_body')

    async def test_unrequested_sse_fails_without_waiting_for_stream_end(self):
        self.mode = 'sse'
        self.payload = b'data: ' + FIXTURE + b'\n\ndata: [DONE]\n\n'
        report = await generate(self.body())
        self.assertEqual(report['error_code'], 'unexpected_stream_response')
        self.assertEqual(report['diagnostics']['content_type'], 'event_stream')
        self.assertIsNone(report['diagnostics']['first_byte_seconds'])

    async def test_http_rejection_does_not_wait_for_error_body_end(self):
        self.mode = 'http_error'
        self.payload = b'{"error":"synthetic-private-error"}'
        report = await generate(self.body())
        self.assertEqual(report['error_code'], 'authentication_failed')
        self.assertEqual(report['diagnostics']['response_completion'], 'http_status')

    async def test_cancelled_request_keeps_safe_last_phase_in_diagnostics(self):
        self.payload = FIXTURE[:-16]
        body = self.body()
        with self.assertLogs('image_quality.progress', level=logging.INFO) as captured:
            task = asyncio.create_task(generate(body))
            await asyncio.wait_for(self.body_sent.wait(), 1)
            # Use the actual first-body event, not a timing guess, before cancellation.
            for _ in range(100):
                if any(json.loads(row.getMessage())['event'] == 'body_started' for row in captured.records):
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail('body receipt was not observed')
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        final = json.loads(captured.records[-1].getMessage())
        self.assertEqual(final['status'], 'cancelled')
        self.assertEqual(final['phase'], 'receiving_body')
        self.assertGreater(final['received_bytes'], 0)
        output = '\n'.join(row.getMessage() for row in captured.records)
        for private in (body.api_key.get_secret_value(), body.prompt, body.base_url,
                        'synthetic-private-error', 'b64_json'):
            self.assertNotIn(private, output)

    async def test_complete_but_invalid_json_is_not_accepted(self):
        self.payload = b'{"data":[],} trailing'
        report = await generate(self.body())
        self.assertEqual(report['error_code'], 'invalid_json')
        self.assertEqual(report['status'], 'failed')
