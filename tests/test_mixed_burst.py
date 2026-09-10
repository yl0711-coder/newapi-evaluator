import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from relay_lab.adapter import OpenAIAdapter
from relay_lab.config import DATA_ROOT, load
from relay_lab.console import Job, run_config
from relay_lab.report import rebuild
from relay_lab.runner import Lab


class MixedBurstTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='mixed-burst-', dir=DATA_ROOT))
        self.cfg = load('configs/mixed-burst.yaml', overrides={
            'mixed_burst': {'first_output_timeout': 4, 'idle_timeout': .5, 'total_timeout': 5}})

    async def test_queue_preserves_original_cohort_and_observes_release(self):
        lab = Lab(self.cfg, self.root / 'queue')
        task = asyncio.create_task(lab.run('account-test'))
        await asyncio.sleep(.06)
        live = lab.burst_snapshot()
        self.assertEqual(live['issued_requests'], 10)
        self.assertTrue(any(r['state'] == 'waiting_first_output' for r in live['timeline']))
        self.assertTrue(any(r['state'] == 'receiving' for r in live['timeline']))
        job = Job('account-test', 'mock', self.root / 'queue')
        job.lab = lab
        self.assertEqual(job.public()['burst']['planned_requests'], 10)
        summary = await task
        b = summary['analysis']['burst']
        self.assertEqual(summary['result_count'], 10)
        self.assertEqual(summary['recoveries'], [])
        self.assertEqual(b['finished_requests'], 10)
        self.assertEqual(b['received_output_requests'], 10)
        self.assertEqual(b['peak_inflight'], 10)
        self.assertEqual(b['peak_receiving'], 5)
        self.assertEqual(summary['stages'][0]['max_observed_inflight'], 5)
        self.assertEqual(summary['stages'][0]['success_rate'], 1)
        self.assertIsNone(summary['analysis']['max_stable_concurrency'])
        self.assertFalse(b['server_queue_verified'])
        self.assertLess(max(r['start_seconds'] for r in b['timeline']), min(r['end_seconds'] for r in b['timeline']))
        self.assertEqual(len(b['release_observations']), 4)
        self.assertTrue(any(e['waiting_labels'] and e['later_output'] for e in b['release_observations']))
        self.assertTrue(any(r['after_release'] for r in b['timeline']))
        rows = [json.loads(l) for l in (self.root / 'queue/results.jsonl').read_text().splitlines()]
        self.assertEqual(len({r['request_id'] for r in rows}), 10)
        self.assertEqual([sum(r['workload_profile'] == p for r in rows) for p in ('short', 'medium', 'long')], [2, 2, 6])
        self.assertEqual(b['total_output_chars'], sum(r['output_units'] for r in rows))
        self.assertIn('固定混合批次：等待与释放观察', (self.root / 'queue/report.md').read_text())
        (self.root / 'queue/summary.json').unlink()
        restored = rebuild(self.root / 'queue')
        self.assertEqual(restored['analysis']['burst']['issued_requests'], 10)
        self.assertIsNone(restored['analysis']['max_stable_concurrency'])

    async def test_reject_returns_five_429_without_refill(self):
        self.cfg['mock']['admission_policy'] = 'reject'
        summary = await Lab(self.cfg, self.root / 'reject').run('account-test')
        b = summary['analysis']['burst']
        self.assertEqual(summary['result_count'], 10)
        self.assertEqual(b['early_http_errors'], 5)
        self.assertEqual(b['received_output_requests'], 5)
        self.assertEqual(b['peak_receiving'], 5)
        self.assertEqual(sum(r['http_status'] == 429 for r in b['timeline']), 5)
        self.assertFalse(any(e['later_output'] for e in b['release_observations']))

    async def test_queue_deadline_has_timed_errors_and_no_extra_requests(self):
        self.cfg['mock']['queue_timeout'] = .04
        summary = await Lab(self.cfg, self.root / 'queue-timeout').run('account-test')
        b = summary['analysis']['burst']
        failed = [r for r in b['timeline'] if r['http_status'] == 429]
        self.assertEqual(len(failed), 5)
        self.assertTrue(all(r['wait_seconds'] >= .04 for r in failed))
        self.assertEqual(b['issued_requests'], 10)

    async def test_stop_preserves_partial_metrics_without_refill(self):
        stop = asyncio.Event()
        lab = Lab(self.cfg, self.root / 'stop', stop=stop)
        task = asyncio.create_task(lab.run('account-test'))
        await asyncio.sleep(.13)
        stop.set()
        summary = await asyncio.wait_for(task, 1)
        self.assertEqual(summary['status'], 'interrupted')
        self.assertEqual(summary['result_count'], 10)
        self.assertTrue(all(r['end_seconds'] is not None for r in summary['analysis']['burst']['timeline']))
        self.assertGreater(summary['stages'][0]['total_output_chars'], 0)
        self.assertGreater(summary['stages'][0]['mean_output_chars'], 0)

    def test_console_uses_sum_not_stale_ladder_and_validates_input(self):
        body = {'environment': 'mock', 'load_mode': 'mixed_burst', 'stages': [4]}
        mode, live, cfg = run_config(body)
        self.assertEqual(cfg['connection_limit'], 10)
        self.assertEqual(cfg['stages'], [10])
        self.assertEqual(cfg['stage_duration'], 0)
        self.assertEqual(cfg['mock']['accounts'][0]['capacity'], 5)
        self.assertEqual(run_config({**body, 'stages': [], 'timeout': None, 'samples': None})[2]['connection_limit'], 10)
        job = Job('account-test', 'mock', self.root / 'pending')
        job.burst_config = cfg['mixed_burst']
        self.assertEqual(job.public()['burst']['planned_requests'], 10)
        self.assertEqual(job.public()['burst']['issued_requests'], 0)
        for change in ({'counts': [0, 0, 0]}, {'counts': [1, 2]}, {'counts': [True, 2, 6]},
                       {'output_limits': [64, 64, 4096]}, {'total_timeout': float('nan')}):
            with self.assertRaises(ValueError):
                run_config({**body, 'mixed_burst': change})
        with self.assertRaises(ValueError):
            load(overrides={'mixed_burst': {'enabled': True}, 'connection_limit': 4})


class TimedStream(httpx.AsyncByteStream):
    def __init__(self, mode):
        self.mode = mode

    async def __aiter__(self):
        if self.mode == 'error':
            yield b'data: {"error":{"code":"rate_limit_exceeded","message":"private-diagnostic-marker"}}\n\n'
            return
        if self.mode == 'normal_end':
            yield b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"length"}]}\n\n'
            yield b'data: [DONE]\n\n'
            return
        for i in range(50):
            await asyncio.sleep(.01)
            if self.mode == 'first_wait' or self.mode == 'idle' and i > 0:
                yield b': heartbeat\n\n'
            else:
                yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'


class BurstTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, mode, **timeouts):
        cfg = {**load()['mixed_burst'], 'first_output_timeout': .08, 'idle_timeout': .06, 'total_timeout': .15, **timeouts}
        adapter = OpenAIAdapter('http://127.0.0.1:9/v1', confirm_live=True, burst_timeouts=cfg)
        await adapter.client.aclose()
        captured = []
        def handle(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=TimedStream(mode))
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        async with adapter:
            result = await adapter.request('mixed', 10, workload='medium', output_tokens=512,
                                           limit_field='max_completion_tokens', burst_label='M1')
        self.assertEqual(captured[0]['max_completion_tokens'], 512)
        self.assertNotIn('max_tokens', captured[0])
        return result

    async def test_healthy_stream_hits_total_not_idle_and_keeps_output(self):
        r = await self.request('continuous')
        self.assertEqual(r.error, 'total_timeout')
        self.assertGreater(r.output_units, 5)
        self.assertGreater(r.piece_count, 5)
        self.assertIsNotNone(r.last_output_at)

    async def test_heartbeats_do_not_hide_first_output_or_idle_timeout(self):
        self.assertEqual((await self.request('first_wait')).error, 'first_output_timeout')
        r = await self.request('idle')
        self.assertEqual(r.error, 'stream_idle_timeout')
        self.assertEqual(r.output_units, 1)

    async def test_sse_error_is_classified_without_raw_message(self):
        r = await self.request('error')
        self.assertEqual(r.error, 'upstream_error')
        self.assertEqual(r.upstream_error_kind, 'rate_limit')
        self.assertNotIn('private-diagnostic-marker', json.dumps(r.public()))
        self.assertFalse(r.complete)

    async def test_bounded_medium_length_is_complete(self):
        r = await self.request('normal_end')
        self.assertTrue(r.success)
        self.assertEqual(r.finish_reason, 'length')
