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
    def __init__(self, base_url, *, timeout=0.5, connection_limit=1500, confirm_live=False, api_key=None, model='relay-lab-model', burst_timeouts=None):
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
        self.burst_timeouts = burst_timeouts
        self.on_progress = None
        limits = httpx.Limits(max_connections=connection_limit, max_keepalive_connections=connection_limit)
        self.transport = FaultTransport(limits, False)
        transport_timeout = (httpx.Timeout(connect=burst_timeouts['connect_timeout'], read=None,
                             write=burst_timeouts['connect_timeout'], pool=burst_timeouts['connect_timeout'])
                             if burst_timeouts else timeout)
        self.client = httpx.AsyncClient(transport=self.transport, timeout=transport_timeout, trust_env=False,
                                        follow_redirects=False, headers={'Authorization': 'Bearer ' + api_key} if api_key else {})

    async def __aenter__(self):
        try:
            # With no live permission target_url has already pinned a numeric loopback.
            if not self.confirm_live:
                response = await asyncio.wait_for(self.client.get(self.base_url + '/__relay_lab__/identity'),
                                                 self.burst_timeouts['connect_timeout'] if self.burst_timeouts else self.timeout)
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
                      workload='short', output_tokens=1024, limit_field='max_tokens', burst_label=''):
        if workload not in ('short', 'medium', 'long') or limit_field not in ('max_tokens', 'max_completion_tokens'):
            raise ValueError('Invalid workload')
        result = Result(uuid.uuid4().hex, stage, concurrency, phase=phase, step=step, attempt=attempt)
        result.started_at = time.time()
        result.burst_label = burst_label
        result.workload_profile = workload
        result.output_limit = output_tokens if workload == 'long' or burst_label else None
        start = time.perf_counter()
        first = None
        last = None
        total_timer = activity_timer = None
        acquired = False
        digest = hashlib.sha256()
        headers = {}
        if (workload == 'long' or burst_label) and self.is_mock and chunks is None:
            chunks, chunk_delay = output_tokens, .01
            if burst_label:
                chunks = {'short': 8, 'medium': 32, 'long': 96}[workload]
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

        def progress():
            if self.on_progress:
                self.on_progress(result)

        def content(value):
            nonlocal first, last
            if not isinstance(value, str):
                raise ValueError('Invalid content')
            if value:
                now = time.perf_counter()
                if last is not None:
                    result.max_output_gap_ms = max(result.max_output_gap_ms, (now - last) * 1000)
                last = now
                result.last_output_at = time.time()
                result.piece_count += 1
                if self.burst_timeouts and activity_timer is not None:
                    activity_timer.reschedule(asyncio.get_running_loop().time() + self.burst_timeouts['idle_timeout'])
                if first is None:
                    first = time.perf_counter()
                    result.first_content_at = time.time()
                    self.receiving += 1
                    if self.occupancy:
                        self.occupancy.observe(self.active, self.receiving)
                    result.ttft_ms = (first - start) * 1000
                result.output_units += len(value)
                digest.update(value.encode())
                progress()

        progress()
        try:
            async with asyncio.timeout(self.burst_timeouts['total_timeout'] if self.burst_timeouts else self.timeout) as total_timer, asyncio.timeout(self.burst_timeouts['first_output_timeout'] if self.burst_timeouts else None) as activity_timer:
                async with self.gate:
                    acquired = True
                    result.inflight_started_at = time.time()
                    progress()
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
                        if burst_label:
                            instruction = {'short': 'Return one short synthetic sentence, then stop.',
                                           'medium': 'Return a synthetic explanation of about 300 words, then stop.',
                                           'long': 'Produce a long synthetic numbered list until the output limit. Keep producing entries.'}[workload]
                            payload['messages'][0]['content'] = instruction
                        if workload == 'long' or burst_label:
                            payload[limit_field] = output_tokens
                        async with self.client.stream('POST', self.base_url + '/chat/completions', headers=headers,
                                                      json=payload) as response:
                            result.status = response.status_code
                            result.headers_at = time.time()
                            progress()
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
                                result.complete = (reason == 'stop' or (workload == 'long' or burst_label) and reason == 'length') and result.output_units > 0
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
                                        if not isinstance(value, dict):
                                            raise ValueError('Invalid stream event')
                                        if isinstance(value, dict) and 'error' in value:
                                            error = value['error']
                                            code = error.get('code', error.get('type', '')) if isinstance(error, dict) else ''
                                            result.upstream_error_kind = ('rate_limit' if code in ('rate_limit_exceeded', 'rate_limit_error', 'too_many_requests') else 'other')
                                            result.error = 'upstream_error'
                                            break
                                        choices = value.get('choices', [])
                                        if not isinstance(choices, list):
                                            raise ValueError('Invalid stream choices')
                                        for choice in choices:
                                            if not isinstance(choice, dict) or not isinstance(choice.get('delta', {}), dict):
                                                raise ValueError('Invalid stream delta')
                                            content(choice.get('delta', {}).get('content') or '')
                                            reason = choice.get('finish_reason')
                                            if reason is not None:
                                                result.finish_reason = reason if reason in ('stop', 'length', 'content_filter', 'tool_calls') else 'other'
                                                finish = reason == 'stop' or (workload == 'long' or burst_label) and reason == 'length'
                                    elif line and not line.startswith(('event:', 'id:', 'retry:')):
                                        raise ValueError('Invalid SSE field')
                                result.complete = finish and done and result.output_units > 0
                            result.success = result.complete
                            if not result.complete and not result.error:
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
            if self.burst_timeouts:
                result.error = ('total_timeout' if total_timer is not None and total_timer.expired()
                                else 'first_output_timeout' if first is None else 'stream_idle_timeout')
            elif not acquired:
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
            progress()
        return result
