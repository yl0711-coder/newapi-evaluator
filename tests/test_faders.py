import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from relay_lab.config import DATA_ROOT, load
from relay_lab.console import Console, run_config
from relay_lab.runner import Lab


class FaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='faders-', dir=DATA_ROOT))
        self.running = []

    async def asyncTearDown(self):
        for lab, task in self.running:
            lab.stop.set()
            await asyncio.wait_for(task, 3)

    def launch(self, overrides=None):
        cfg = load(overrides={'faders': {'enabled': True, 'duration': 5, 'max_requests': 100,
                                        'max_inflight': 6, 'refill_interval': .1,
                                        'mock_durations': [.08, .12, .6], **(overrides or {})},
                              'mixed_burst': {'first_output_timeout': 2, 'total_timeout': 3, 'idle_timeout': 1},
                              'mock': {'admission_policy': 'queue', 'queue_timeout': 2,
                                       'accounts': [{'capacity': 1, 'latency': .001, 'cooldown': 0}]}})
        lab = Lab(cfg, self.root / str(len(self.running)))
        task = asyncio.create_task(lab.run('account-test'))
        self.running.append((lab, task))
        return lab, task

    async def until(self, predicate, timeout=3):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(.01)

    async def test_raise_under_load_waits_then_receives_and_lower_does_not_cancel(self):
        lab, task = self.launch()
        await self.until(lambda: lab.current_phase == 'faders')
        await asyncio.sleep(.08)
        self.assertEqual(lab.faders.snapshot()['issued_requests'], 0)
        lab.faders.adjust({'targets': [0, 0, 1]})
        await self.until(lambda: lab.faders.snapshot()['channels'][2]['receiving'] == 1)
        lab.faders.adjust({'targets': [1, 1, 0]})
        await self.until(lambda: lab.faders.snapshot()['inflight'] == 3)
        s = lab.faders.snapshot()
        self.assertEqual([c['waiting'] for c in s['channels']], [1, 1, 0])
        lab.faders.adjust({'paused': True})
        await self.until(lambda: lab.faders.snapshot()['inflight'] == 0)
        lab.stop.set()
        summary = await task
        rows = [json.loads(x) for x in (lab.artifacts.output / 'results.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r['success'] for r in rows))
        long = next(r for r in rows if r['burst_label'] == 'L1')
        for r in rows:
            if r['burst_label'] != 'L1':
                self.assertLess(r['started_at'], long['ended_at'])
                self.assertGreater(r['first_content_at'], long['last_output_at'])
                self.assertLess(r['queue_ms'], 20)
        self.assertEqual(summary['recoveries'], [])
        self.assertFalse(summary['analysis']['capacity_limit_determined'])
        self.assertIsNone(summary['stages'][0]['occupancy']['target_occupancy_ratio'])
        self.assertIn('三路实时推子', (lab.artifacts.output / 'report.md').read_text())
        events = json.loads((lab.artifacts.output / 'fader-events.json').read_text())['events']
        self.assertIn([1, 1, 0], [e['targets'] for e in events])
        self.assertTrue(any(e['paused'] for e in events))

    async def test_reject_under_load_is_observed_and_refill_is_bounded(self):
        lab, task = self.launch({'refill_interval': .2})
        lab.cfg['mock']['admission_policy'] = 'reject'
        lab.faders.adjust({'targets': [0, 0, 1]})
        await self.until(lambda: lab.faders.snapshot()['channels'][2]['receiving'] == 1)
        lab.faders.adjust({'targets': [1, 0, 1]})
        await self.until(lambda: lab.faders.snapshot()['channels'][0]['errors'].get('http_error', 0) > 0)
        await asyncio.sleep(.22)
        lab.faders.adjust({'paused': True})
        rows = list(lab.faders.rows.values())
        rejected = [r for r in rows if r['status'] == 429]
        self.assertGreaterEqual(len(rejected), 1)
        self.assertLessEqual(len(rejected), 3)
        self.assertTrue(all(r['first_content_at'] is None and r['latency_ms'] < 500 for r in rejected))
        before = len(rows)
        await asyncio.sleep(.25)
        self.assertEqual(lab.faders.snapshot()['issued_requests'], before)

    async def test_request_cap_drains_and_no_recovery_probes(self):
        lab, task = self.launch({'targets': [1, 0, 0], 'max_requests': 2})
        summary = await asyncio.wait_for(task, 2)
        self.assertEqual(summary['result_count'], 2)
        self.assertEqual(summary['analysis']['faders']['stop_reason'], 'request_cap')
        self.assertTrue(all(r['success'] for r in lab.faders.rows.values()))
        self.assertEqual(summary['recoveries'], [])
        with self.assertRaises(ValueError):
            lab.faders.adjust({'targets': [1, 0, 0]})

    async def test_deadline_closes_zero_target_run(self):
        lab, task = self.launch({'duration': .15})
        summary = await asyncio.wait_for(task, 1)
        self.assertEqual(summary['result_count'], 0)
        self.assertEqual(summary['analysis']['faders']['stop_reason'], 'duration')

    async def test_partial_rebuild_keeps_adjustments_without_claiming_capacity(self):
        from relay_lab.report import rebuild
        lab, task = self.launch({'targets': [1, 0, 0], 'max_requests': 1})
        await task
        events = (lab.artifacts.output / 'fader-events.json').read_text()
        (lab.artifacts.output / 'summary.json').rename(lab.artifacts.output / 'original-summary.json')
        summary = rebuild(lab.artifacts.output)
        self.assertEqual(summary['status'], 'partial_reconstructed')
        self.assertIsNone(summary['analysis']['max_stable_concurrency'])
        self.assertEqual(summary['analysis']['faders']['issued_requests'], 1)
        self.assertEqual((lab.artifacts.output / 'fader-events.json').read_text(), events)

    async def test_stop_cancels_waiting_and_receiving_and_saves_partial_output(self):
        lab, task = self.launch({'targets': [0, 0, 2]})
        await self.until(lambda: lab.faders.snapshot()['channels'][2]['receiving'] == 1)
        lab.stop.set()
        summary = await asyncio.wait_for(task, 1)
        self.assertEqual(summary['status'], 'interrupted')
        self.assertEqual(summary['stages'][0]['errors'], {'cancelled': 2})
        self.assertEqual(summary['stages'][0]['occupancy']['current_inflight'], 0)
        self.assertTrue(any(r['output_units'] > 0 for r in lab.faders.rows.values()))

    async def test_total_limit_holds_while_old_channel_drains(self):
        lab, task = self.launch({'max_inflight': 2, 'targets': [0, 0, 2]})
        await self.until(lambda: lab.faders.snapshot()['inflight'] == 2)
        lab.faders.adjust({'targets': [2, 0, 0]})
        await asyncio.sleep(.15)
        self.assertEqual(lab.faders.snapshot()['channels'][0]['issued'], 0)
        self.assertEqual(lab.faders.snapshot()['inflight'], 2)
        self.assertLessEqual(lab.live_occupancy.snapshot()['peak_inflight'], 2)


class FaderValidationTests(unittest.TestCase):
    def test_console_configuration_and_invalid_controls(self):
        body = {'environment': 'mock', 'load_mode': 'faders', 'faders': {'targets': [0, 0, 0]}}
        _, live, cfg = run_config(body)
        self.assertFalse(live)
        self.assertTrue(cfg['faders']['enabled'])
        self.assertEqual(cfg['connection_limit'], cfg['faders']['max_inflight'])
        self.assertFalse(cfg['mixed_burst']['enabled'])
        self.assertTrue(run_config({**body, 'mixed_burst': {'counts': [0, 0, 0], 'output_limits': [0, 0, 0]}})[2]['faders']['enabled'])
        for bad in [{'targets': [True, 0, 0]}, {'targets': [121, 0, 0]}, {'targets': [-1, 0, 0]},
                    {'targets': [1, 2]}, {'duration': float('nan')}, {'refill_interval': 0},
                    {'max_inflight': 1201}, {'output_limits': [1, 512, 4096]}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                run_config({**body, 'faders': bad})
        with self.assertRaises(ValueError):
            run_config({**body, 'mode': 'pool-test'})
        with self.assertRaises(ValueError):
            run_config({**body, 'environment': 'live'})

    def test_invalid_adjustment_is_atomic(self):
        from relay_lab.faders import Faders
        root = Path(tempfile.mkdtemp(prefix='faders-controls-', dir=DATA_ROOT))
        control = Faders(load()['faders'], root)
        control.adjust({'targets': [1, 2, 3]})
        old = control.snapshot()
        for update in [{'targets': [0, 0, 0], 'paused': 'yes'}, {'targets': [120, 120, 0]}, {'api_key': 'synthetic'}]:
            with self.assertRaises(ValueError):
                control.adjust(update)
            self.assertEqual(control.snapshot()['targets'], old['targets'])
            self.assertEqual(control.snapshot()['version'], old['version'])
