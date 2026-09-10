import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from relay_lab.adapter import OpenAIAdapter
from relay_lab.config import DATA_ROOT, load
from relay_lab.console import Console, run_config
from relay_lab.model import Result
from relay_lab.occupancy import Occupancy
from relay_lab.report import rebuild
from relay_lab.runner import Lab


class OccupancyTests(unittest.TestCase):
    def test_exact_integrals_exclude_drain(self):
        now = [0.0]
        o = Occupancy('test', 5, 10, clock=lambda: now[0])
        o.observe(5, 0)
        now[0] = 2
        o.observe(5, 2)
        now[0] = 8
        o.observe(0, 0)
        now[0] = 12
        o.finish('duration')
        s = o.snapshot()
        self.assertEqual(s['mean_inflight'], 4)
        self.assertEqual(s['mean_receiving'], 1.2)
        self.assertEqual(s['target_occupancy_ratio'], .8)
        self.assertEqual(s['load_seconds'], 10)
        self.assertEqual(s['drain_seconds'], 2)
        self.assertIsNone(s['upstream_account_occupancy'])

    def test_timeline_is_bounded_and_keeps_origin_and_end(self):
        now = [0.0]
        o = Occupancy('test', 3, clock=lambda: now[0])
        o.observe(3, 1)
        for i in range(10000):
            now[0] += 1
            o.sample()
        o.observe(0, 0)
        o.finish('request_count')
        s = o.snapshot()
        self.assertLessEqual(len(s['series']), 1200)
        self.assertEqual(s['series'][0]['seconds'], 0)
        self.assertEqual(s['series'][-1]['inflight'], 0)
        self.assertEqual(s['mean_inflight'], 3)

    def test_console_validation_and_legacy_defaults(self):
        body = {'environment': 'mock', 'mode': 'account-test', 'stages': [3],
                'load_mode': 'duration', 'stage_duration': 1, 'max_stage_requests': 20,
                'workload_profile': 'long', 'output_tokens': 16}
        _, live, cfg = run_config(body)
        self.assertFalse(live)
        self.assertEqual(cfg['stage_duration'], 1)
        self.assertEqual(cfg['workload']['output_tokens'], 16)
        for bad in [{'max_stage_requests': 2}, {'stage_duration': float('nan')},
                    {'mode': 'pool-test'}, {'limit_field': 'unsupported'}, {'output_tokens': 0}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                run_config({**body, **bad})
        self.assertEqual(run_config({'environment': 'mock'})[2]['stage_duration'], 0)


class SustainedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='sustained-', dir=DATA_ROOT))
        self.cfg = load(overrides={'samples': 1, 'stages': [3], 'timeout': 1,
                                  'stage_duration': .4, 'max_stage_requests': 100,
                                  'recovery_timeout': .2, 'recovery_interval': .005,
                                  'workload': {'profile': 'long', 'output_tokens': 16},
                                  'mock': {'accounts': [{'capacity': 20, 'latency': .001}], 'chunk_delay': .001}})

    async def test_refill_overlap_duration_and_drain(self):
        out = self.root / 'refill'
        s = await Lab(self.cfg, out).run('account-test')
        stage = s['stages'][0]
        rows = [json.loads(line) for line in (out / 'results.jsonl').read_text().splitlines()]
        loads = sorted((r for r in rows if r['phase'] == 'load'), key=lambda r: r['started_at'])
        self.assertGreaterEqual(len(loads), 6)
        self.assertTrue(all(r['success'] for r in loads))
        self.assertLess(loads[2]['inflight_started_at'], min(r['ended_at'] for r in loads))
        self.assertLess(max(r['started_at'] for r in loads), loads[0]['started_at'] + .43)
        self.assertTrue(all(r['first_content_at'] <= r['ended_at'] for r in loads))
        o = stage['occupancy']
        self.assertEqual(o['stop_reason'], 'duration')
        self.assertAlmostEqual(o['load_seconds'], .4, delta=.015)
        self.assertGreater(o['drain_seconds'], 0)
        self.assertGreater(o['mean_inflight'], 2.7)
        self.assertGreater(o['mean_receiving'], 2)
        self.assertEqual(o['peak_inflight'], 3)
        self.assertEqual(o['current_inflight'], 0)
        self.assertEqual(stage['max_observed_inflight'], 3)  # Server-side Mock occupancy.
        self.assertTrue(any(p['receiving'] == 3 for p in o['series']))
        self.assertTrue(all(r['started_at'] >= max(l['ended_at'] for l in loads)
                            for r in rows if r['phase'] == 'recovery'))
        report = (out / 'report.md').read_text()
        self.assertIn('持续并发与占用', report)
        self.assertIn('时长到达', report)
        history = Console(self.root, history=False)
        # Completed job loading must retain the timeline without any original credentials.
        (self.root / 'runs').mkdir()
        out.rename(self.root / 'runs' / 'history')
        history._history()
        job = next(j for j in history.jobs.values() if j.output.name == 'history')
        self.assertEqual(job.public()['stages'][0]['occupancy']['peak_inflight'], 3)

    async def test_request_cap_does_not_claim_sustained_success(self):
        self.cfg.update(stage_duration=2, max_stage_requests=3)
        s = await Lab(self.cfg, self.root / 'cap').run('account-test')
        self.assertEqual(s['stages'][0]['samples'], 3)
        self.assertEqual(s['stages'][0]['occupancy']['stop_reason'], 'request_cap')
        self.assertEqual(s['analysis']['max_stable_concurrency'], 0)
        self.assertFalse(s['analysis']['range_censored'])

    async def test_stop_cancels_pending_streams_and_preserves_metrics(self):
        self.cfg.update(stage_duration=5)
        self.cfg['workload']['output_tokens'] = 256
        self.cfg['timeout'] = 10
        stop = asyncio.Event()
        lab = Lab(self.cfg, self.root / 'stopped', stop=stop)
        task = asyncio.create_task(lab.run('account-test'))
        await asyncio.sleep(.15)
        stop.set()
        s = await asyncio.wait_for(task, .7)
        self.assertEqual(s['status'], 'interrupted')
        self.assertEqual(s['stages'][0]['samples'], 3)
        self.assertEqual(s['stages'][0]['errors'], {'cancelled': 3})
        self.assertEqual(s['stages'][0]['occupancy']['current_receiving'], 0)
        self.assertEqual(s['stages'][0]['occupancy']['stop_reason'], 'stopped')
        self.assertTrue(all(r['probes'] == 0 for r in s['recoveries']))

    async def test_timeouts_release_slots_and_refill(self):
        self.cfg.update(timeout=.06, stage_duration=.18)
        s = await Lab(self.cfg, self.root / 'timeout').run('account-test')
        stage = s['stages'][0]
        self.assertGreater(stage['samples'], 3)
        self.assertEqual(stage['success_rate'], 0)
        self.assertEqual(stage['occupancy']['current_inflight'], 0)
        self.assertEqual(stage['occupancy']['current_receiving'], 0)

    async def test_long_output_limit_and_stream_ending(self):
        captured = []
        ending = ['length']
        done = [True]
        def handle(request):
            captured.append(json.loads(request.content))
            frames = [{'choices': [{'delta': {'content': 'synthetic'}}]},
                      {'choices': [{'delta': {}, 'finish_reason': ending[0]}]}]
            stream = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames)
            if done[0]:
                stream += 'data: [DONE]\n\n'
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=stream)
        a = OpenAIAdapter('http://127.0.0.1:9/v1', confirm_live=True)
        await a.client.aclose()
        a.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        async with a:
            long = await a.request('long', 1, workload='long', output_tokens=128, limit_field='max_completion_tokens')
            self.assertTrue(long.complete)
            self.assertEqual(long.finish_reason, 'length')
            self.assertEqual(captured[-1]['max_completion_tokens'], 128)
            self.assertNotIn('max_tokens', captured[-1])
            short = await a.request('short', 1)
            self.assertFalse(short.complete)
            self.assertNotIn('max_completion_tokens', captured[-1])
            done[0] = False
            self.assertFalse((await a.request('broken', 1, workload='long')).complete)
            done[0] = True
            ending[0] = 'private_untrusted_value'
            unknown = await a.request('unknown', 1, workload='long')
            self.assertFalse(unknown.complete)
            self.assertEqual(unknown.finish_reason, 'other')
            self.assertEqual(a.active, 0)
            self.assertEqual(a.receiving, 0)

    async def test_legacy_report_rebuild(self):
        out = self.root / 'legacy'
        out.mkdir()
        row = Result('legacy', 'account-c1', 1, success=True, complete=True, latency_ms=10).public()
        for key in ('started_at', 'inflight_started_at', 'first_content_at', 'ended_at', 'finish_reason'):
            row.pop(key)
        (out / 'results.jsonl').write_text(json.dumps(row) + '\n')
        value = rebuild(out)
        self.assertEqual(value['result_count'], 1)
        self.assertTrue(value['stages'][0]['throughput_unavailable'])
