import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from relay_lab.checkpoint import Checkpoint
from relay_lab.config import DATA_ROOT, load
from relay_lab.model import Result
from relay_lab.runner import Lab
from relay_lab.mock import MockServer


class ModeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='unit-', dir=DATA_ROOT))
        self.cfg = load(overrides={'samples': 8, 'timeout': .5, 'stages': [1, 2, 4],
                                  'recovery_timeout': .5, 'recovery_interval': .005, 'recovery_successes': 2,
                                  'mock': {'chunk_delay': .001, 'accounts': [{'capacity': 2, 'latency': .008, 'cooldown': .015}]},
                                  'long_task': {'steps': 5, 'fail_step': 3, 'stream_chunks': 12, 'stream_chunk_delay': .002}})

    async def test_account_limits_and_raw_artifacts(self):
        value = await Lab(self.cfg, self.root / 'account').run('account-test')
        self.assertEqual(value['analysis']['max_stable_concurrency'], 2)
        self.assertEqual(value['analysis']['rate_limit_point'], 4)
        self.assertTrue(value['analysis']['admission_gate_passed'])
        self.assertTrue(value['analysis']['low_confidence'])
        self.assertGreater(value['network_policy']['socket_audit']['loopback_connections'], 0)
        self.assertEqual(value['network_policy']['socket_audit']['blocked_external_attempts'], 0)
        rows = [json.loads(x) for x in (self.root / 'account/results.jsonl').read_text().splitlines()]
        self.assertEqual(value['result_count'], len(rows))
        self.assertTrue((self.root / 'account/report.md').is_file())
        self.assertTrue(all(r['request_id'] for r in rows))

    async def test_pool_scaling_and_capacity_drop(self):
        self.cfg.update(pool_sizes=[1, 2, 4], pool_stages=[1, 2, 4, 6])
        value = await Lab(self.cfg, self.root / 'pool').run('pool-test')
        cases = {(s['account_count'], s['scenario']): s for s in value['analysis']['pool_scenarios']}
        self.assertEqual(cases[1, 'healthy']['observed_capacity'], 2)
        self.assertEqual(cases[2, 'healthy']['observed_capacity'], 4)
        self.assertEqual(cases[2, 'half_failed']['observed_capacity'], 2)
        self.assertEqual(cases[4, 'half_failed']['healthy_account_count'], 2)
        self.assertEqual(cases[4, 'half_failed']['failed_account_count'], 2)
        self.assertEqual(cases[2, 'healthy']['capacity_utilization'], 1)
        self.assertEqual(cases[2, 'healthy']['scaling_efficiency'], 1)
        self.assertEqual(cases[1, 'one_failed']['observed_capacity'], 0)
        self.assertIsNotNone(cases[1, 'one_failed']['recovery_time'])

    async def test_gateway_slow_collapse_and_recovery(self):
        self.cfg.update(gateway_stages=[1, 2, 3, 5, 8, 13], timeout=1)
        self.cfg['mock'].update(latency=.02, slow_at=3, slow_factor=8, crash_at=8, crash_duration=.12)
        value = await Lab(self.cfg, self.root / 'gateway').run('gateway-test')
        analysis = value['analysis']
        self.assertEqual(analysis['slow_point'], 3)
        self.assertEqual(analysis['collapse_point'], 8)
        self.assertEqual(analysis['max_stable_concurrency'], 2)
        self.assertEqual(analysis['max_stable_inflight'], 2)
        self.assertTrue(all(r['recovered'] for r in value['recoveries']))
        resources = value['stages'][-1]['resources']
        self.assertGreater(resources['peak_rss_bytes'], 0)
        self.assertGreater(resources['peak_file_descriptors'], 0)
        self.assertEqual(resources['scope'], 'client_and_embedded_mock_process')

    async def test_long_task_auto_recovery_and_completeness(self):
        value = await Lab(self.cfg, self.root / 'long').run('long-task-test')
        analysis = value['analysis']
        self.assertEqual(analysis['first_failure_step'], 3)
        self.assertEqual(analysis['recovery_count'], 1)
        self.assertGreater(analysis['total_recovery_time'], 0)
        self.assertTrue(analysis['mock_result_verified'])
        self.assertTrue(analysis['mock_single_stream_verified'])
        self.assertEqual(analysis['single_stream_output_units'], 12)
        self.assertEqual([s['attempts'] for s in analysis['steps']], [1, 1, 2, 1, 1])
        self.assertEqual(analysis['confirmed_steps_reexecuted'], 0)

    async def test_resume_in_new_executor_skips_confirmed_steps(self):
        self.cfg['long_task']['auto_resume'] = False
        checkpoint = self.root / 'persist.sqlite3'
        first = await Lab(self.cfg, self.root / 'first', checkpoint=checkpoint, task_id='resume').run('long-task-test')
        self.assertEqual(first['status'], 'incomplete')
        self.assertEqual(first['analysis']['confirmed_steps'], 2)
        second = await Lab(self.cfg, self.root / 'second', checkpoint=checkpoint, task_id='resume').run('long-task-test')
        self.assertTrue(second['analysis']['final_complete'])
        rows = [json.loads(x) for x in (self.root / 'second/results.jsonl').read_text().splitlines()]
        self.assertEqual([r['step'] for r in rows if r['step'] is not None], [3, 4, 5])
        self.assertEqual([s['attempts'] for s in second['analysis']['steps']], [1, 1, 2, 1, 1])
        third = await Lab(self.cfg, self.root / 'third', checkpoint=checkpoint, task_id='resume').run('long-task-test')
        self.assertEqual(third['analysis']['final_digest'], second['analysis']['final_digest'])
        self.assertEqual(third['analysis']['steps'], second['analysis']['steps'])

    async def test_long_task_failure_types_at_selected_step(self):
        self.cfg['timeout'] = .1
        for fault in ('http_429', 'http_500', 'network_drop', 'read_timeout', 'connect_timeout', 'disconnect'):
            with self.subTest(fault=fault):
                self.cfg['long_task']['fault'] = fault
                value = await Lab(self.cfg, self.root / fault).run('long-task-test')
                self.assertEqual(value['analysis']['first_failure_step'], 3)
                self.assertTrue(value['analysis']['final_complete'])
                self.assertEqual(value['analysis']['steps'][2]['attempts'], 2)

    async def test_checkpoint_exclusion_and_mismatch(self):
        path = self.root / 'atomic.sqlite3'
        with Checkpoint(path, 'task', 'fp1', 2) as store:
            with self.assertRaises(ValueError):
                Checkpoint(path, 'task', 'fp1', 2)
            with self.assertRaises(ValueError):
                store.confirm(1, Result('x', 's', 1))
            store.begin(1)
            store.confirm(1, Result('x', 's', 1, success=True, complete=True,
                                    output_sha256=hashlib.sha256(b'x').hexdigest(), output_units=1))
            with self.assertRaises(ValueError):
                store.begin(1)
        with self.assertRaises(ValueError):
            Checkpoint(path, 'task', 'fp2', 2)
        with Checkpoint(path, 'task', 'fp1', 2) as store:
            self.assertEqual(store.summary()['confirmed_steps'], 1)

    async def test_chaos_injection_and_automatic_restoration(self):
        self.cfg.update(samples=5, faults=['http_401', 'http_429', 'http_500', 'upstream_outage', 'disable_account', 'partial_accounts'])
        self.cfg['mock']['fault_duration'] = .06
        value = await Lab(self.cfg, self.root / 'chaos').run('chaos-test')
        cases = value['analysis']['fault_scenarios']
        self.assertEqual(len(cases), 6)
        self.assertTrue(all(c['recovered'] for c in cases))
        self.assertTrue(all(c['recovery_time'] is not None for c in cases))
        stages = {s['stage']: s for s in value['stages']}
        self.assertEqual(stages['chaos-http_401']['statuses'], {'401': 5})
        self.assertEqual(stages['chaos-http_429']['rate_429'], 1)
        self.assertEqual(stages['chaos-upstream_outage']['success_rate'], 0)

    async def test_sensitive_response_and_config_never_written(self):
        secret = 'sk-' + 'synthetic_sensitive_payload_123456'
        server = await MockServer(self.cfg['mock']).start()
        async def sensitive(writer, headers, payload):
            await server._json(writer, 500, {'error': secret, 'prompt': 'private prompt marker'}, {'X-Secret': secret})
        server._completion = sensitive
        self.cfg['base_url'] = server.base_url
        self.cfg['stages'] = [1]
        self.cfg['model'] = secret
        self.cfg['recovery_timeout'] = .05
        try:
            await Lab(self.cfg, self.root / 'safe').run('account-test')
            for path in (self.root / 'safe').iterdir():
                text = path.read_text()
                for value in (secret, 'private prompt marker', server.base_url):
                    self.assertNotIn(value, text)
        finally:
            await server.close()

    async def test_existing_output_is_not_overwritten(self):
        Lab(self.cfg, self.root / 'existing').artifacts.finish({
            'mode': 'account-test', 'environment': 'mock', 'status': 'empty', 'revision': {'commit_sha': None, 'dirty': True},
            'result_count': 0, 'stages': [], 'analysis': {}})
        with self.assertRaises(FileExistsError):
            Lab(self.cfg, self.root / 'existing')
