import asyncio
import hashlib
import json
import time
import uuid

import httpx

from .model import Result
from .security import target_url


class FaultTransport(httpx.AsyncBaseTransport):
    """Deterministic connect-timeout injection before any socket request is sent."""
    def __init__(self, limits, enabled):
        self.inner = httpx.AsyncHTTPTransport(limits=limits, retries=0, trust_env=False)
        self.enabled = enabled

    async def handle_async_request(self, request):
        if self.enabled and request.headers.get('x-relay-fault') == 'connect_timeout':
            await asyncio.sleep(float(request.headers['x-relay-timeout']) * 0.8)
            raise httpx.ConnectTimeout('injected_connect_timeout', request=request)
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


class OpenAIAdapter:
    def __init__(self, base_url, *, timeout=0.5, connection_limit=1500, confirm_live=False, api_key=None, model='relay-lab-model'):
        self.base_url = target_url(base_url, confirm_live)
        self.confirm_live = confirm_live
        self.model = model
        self.timeout = timeout
        self.connection_limit = connection_limit
        self.gate = asyncio.Semaphore(connection_limit)
        self.active = 0
        self.peak_active = 0
        self.receiving = 0
        self.occupancy = None
        self.is_mock = False
        limits = httpx.Limits(max_connections=connection_limit, max_keepalive_connections=connection_limit)
        self.transport = FaultTransport(limits, False)
        self.client = httpx.AsyncClient(transport=self.transport, timeout=timeout, trust_env=False,
                                        follow_redirects=False, headers={'Authorization': 'Bearer ' + api_key} if api_key else {})

    async def __aenter__(self):
        try:
            # With no live permission target_url has already pinned a numeric loopback.
            if not self.confirm_live:
                response = await self.client.get(self.base_url + '/__relay_lab__/identity')
                value = response.json()
                if response.status_code != 200 or value.get('service') != 'relay-lab-mock' or value.get('protocol') != 1:
                    raise ValueError('Target is not an identified local Mock')
                self.is_mock = True
                self.transport.enabled = True
        except Exception:
            await self.client.aclose()
            raise ValueError('Local Mock identity check failed') from None
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    async def request(self, stage, concurrency, *, phase='load', account=None, fault='normal',
                      stream=True, chunks=None, chunk_delay=None, step=None, attempt=1, key=None,
                      workload='short', output_tokens=1024, limit_field='max_tokens'):
        if workload not in ('short', 'long') or limit_field not in ('max_tokens', 'max_completion_tokens'):
            raise ValueError('Invalid workload')
        result = Result(uuid.uuid4().hex, stage, concurrency, phase=phase, step=step, attempt=attempt)
        result.started_at = time.time()
        start = time.perf_counter()
        first = None
        acquired = False
        digest = hashlib.sha256()
        headers = {}
        if workload == 'long' and self.is_mock and chunks is None:
            chunks, chunk_delay = output_tokens, .01
        if self.is_mock:
            headers = {'X-Relay-Fault': fault, 'X-Relay-Timeout': str(self.timeout)}
            if account is not None:
                headers['X-Relay-Account'] = str(account)
            if chunks is not None:
                headers['X-Relay-Chunks'] = str(chunks)
            if chunk_delay is not None:
                headers['X-Relay-Chunk-Delay'] = str(chunk_delay)
        elif fault != 'normal' or account is not None:
            raise ValueError('Mock controls unavailable for live adapter')
        if key:
            headers['Idempotency-Key'] = key

        def content(value):
            nonlocal first
            if not isinstance(value, str):
                raise ValueError('Invalid content')
            if value:
                if first is None:
                    first = time.perf_counter()
                    result.first_content_at = time.time()
                    self.receiving += 1
                    if self.occupancy:
                        self.occupancy.observe(self.active, self.receiving)
                    result.ttft_ms = (first - start) * 1000
                result.output_units += len(value)
                digest.update(value.encode())

        try:
            async with asyncio.timeout(self.timeout):
                async with self.gate:
                    acquired = True
                    result.inflight_started_at = time.time()
                    result.queue_ms = (time.perf_counter() - start) * 1000
                    self.active += 1
                    self.peak_active = max(self.peak_active, self.active)
                    if self.occupancy:
                        self.occupancy.observe(self.active, self.receiving)
                    try:
                        # Fixed synthetic workload; payloads and model responses never enter artifacts.
                        instruction = ('Generate a long numbered list of distinct imaginary objects with detailed descriptions. '
                                       'Continue producing entries until the output limit; omit introductions and conclusions.'
                                       if workload == 'long' else 'Return a short synthetic test response.')
                        payload = {'model': 'mock-model' if self.is_mock else self.model,
                                   'messages': [{'role': 'user', 'content': instruction}], 'stream': stream}
                        if workload == 'long':
                            payload[limit_field] = output_tokens
                        async with self.client.stream('POST', self.base_url + '/chat/completions', headers=headers,
                                                      json=payload) as response:
                            result.status = response.status_code
                            if self.is_mock:
                                number = response.headers.get('x-relay-account', '')
                                result.account = int(number) if number.isdigit() else None
                                result.collapse = response.headers.get('x-relay-collapse') == '1'
                            if result.status != 200:
                                result.error = 'http_error'
                                return result
                            if not stream:
                                value = json.loads(await response.aread())
                                content(value['choices'][0]['message']['content'])
                                reason = value['choices'][0].get('finish_reason')
                                result.finish_reason = reason if reason in ('stop', 'length', 'content_filter', 'tool_calls') else 'other'
                                result.complete = (reason == 'stop' or workload == 'long' and reason == 'length') and result.output_units > 0
                            else:
                                if 'text/event-stream' not in response.headers.get('content-type', ''):
                                    raise ValueError('Unexpected content type')
                                finish = done = False
                                pending = []
                                async for line in response.aiter_lines():
                                    if len(line) > 1048576:
                                        raise ValueError('Oversize frame')
                                    if line.startswith(':'):
                                        continue
                                    if line.startswith('data:'):
                                        pending.append(line[5:].lstrip(' '))
                                    elif not line and pending:
                                        data = '\n'.join(pending)
                                        pending = []
                                        if data == '[DONE]':
                                            done = True
                                            break
                                        value = json.loads(data)
                                        for choice in value.get('choices', []):
                                            content(choice.get('delta', {}).get('content') or '')
                                            reason = choice.get('finish_reason')
                                            if reason is not None:
                                                result.finish_reason = reason if reason in ('stop', 'length', 'content_filter', 'tool_calls') else 'other'
                                                finish = reason == 'stop' or workload == 'long' and reason == 'length'
                                    elif line and not line.startswith(('event:', 'id:', 'retry:')):
                                        raise ValueError('Invalid SSE field')
                                result.complete = finish and done and result.output_units > 0
                            result.success = result.complete
                            if not result.complete:
                                result.error = 'incomplete_stream'
                    finally:
                        if first is not None:
                            self.receiving -= 1
                        self.active -= 1
                        if self.occupancy:
                            self.occupancy.observe(self.active, self.receiving)
        except asyncio.CancelledError:
            result.error = 'cancelled'
        except httpx.ConnectTimeout:
            result.error = 'connect_timeout'
        except httpx.PoolTimeout:
            result.error, result.collapse = 'connection_pool_exhausted', True
        except httpx.ReadTimeout:
            result.error = 'read_timeout'
        except TimeoutError:
            if not acquired:
                result.error, result.collapse = 'connection_pool_exhausted', True
            else:
                result.error = 'read_timeout' if result.status else 'request_timeout'
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError):
            result.error = 'connection_error' if not result.status else 'stream_disconnect'
        except (ValueError, KeyError, TypeError, IndexError, UnicodeError):
            result.error = 'invalid_response'
        except httpx.HTTPError:
            result.error = 'transport_error'
        finally:
            end = time.perf_counter()
            result.ended_at = time.time()
            result.latency_ms = (end - start) * 1000
            if not acquired:
                result.queue_ms = result.latency_ms
            result.output_sha256 = digest.hexdigest()
            if first is not None:
                result.output_units_per_second = max(0, result.output_units - 1) / max(end - first, 1e-9)
        return result
