import json
import socket
import tempfile
import unittest
from pathlib import Path

from relay_lab.config import DATA_ROOT, data_path, load
from relay_lab.metrics import limits, percentile, stage_summary
from relay_lab.model import Result
from relay_lab.network import mock_network_guard
from relay_lab.security import redact, target_url


class MetricSecurityTests(unittest.TestCase):
    def test_percentiles_interpolate(self):
        self.assertEqual(percentile([0, 10, 20, 30], 50), 15)
        self.assertAlmostEqual(percentile([0, 10, 20, 30], 95), 28.5)
        self.assertIsNone(percentile([], 95))
        self.assertEqual(percentile([12], 99), 12)

    def test_limits_from_request_results(self):
        stages = []
        for c, latency, failures, limited, collapse in [(1, 10, 0, 0, False), (2, 15, 0, 0, False),
                                                       (3, 25, 0, 0, False), (5, 30, 3, 5, False),
                                                       (8, 0, 100, 0, True)]:
            rows = [Result(str(i), str(c), c, latency_ms=latency, success=i >= failures + limited,
                           complete=i >= failures + limited, status=429 if i < limited else (503 if collapse else 200),
                           collapse=collapse) for i in range(100)]
            stages.append(stage_summary(str(c), c, rows, 1, 100))
        value = limits(stages, .25)
        self.assertEqual(value['slow_point'], 3)
        self.assertEqual(value['unstable_point'], 5)
        self.assertEqual(value['rate_limit_point'], 5)
        self.assertEqual(value['collapse_point'], 8)
        self.assertEqual(value['max_stable_concurrency'], 2)
        self.assertEqual(value['recovery_time'], .25)
        self.assertFalse(value['low_confidence'])

    def test_insufficient_samples_and_censoring(self):
        stage = stage_summary('tiny', 1, [Result('a', 'tiny', 1, success=True, complete=True, latency_ms=10)], .01, 100)
        value = limits([stage])
        self.assertTrue(value['low_confidence'])
        self.assertTrue(value['range_censored'])
        self.assertIsNone(value['collapse_point'])
        self.assertEqual(limits([])['max_stable_concurrency'], 0)

    def test_one_failure_is_not_collapse(self):
        rows = [Result('a', 's', 1, status=503), Result('b', 's', 1, success=True, complete=True)]
        self.assertFalse(stage_summary('s', 1, rows, 1, 100)['collapsed'])

    def test_url_policy(self):
        for url in ['http://example.com/v1', 'http://192.0.2.1/v1', 'http://127.0.0.1.evil/v1',
                    'http://localhost@evil.test/v1', 'http://127.0.0.1/v1?key=fake',
                    'file:///tmp/x', 'http://2130706433/v1', 'http://[::ffff:192.0.2.1]/v1']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                target_url(url)
        self.assertEqual(target_url('http://localhost:8877/v1'), 'http://127.0.0.1:8877/v1')
        self.assertEqual(target_url('https://example.com/v1', True), 'https://example.com/v1')

    def test_socket_guard_blocks_before_connect(self):
        evidence = {'loopback_connections': 0, 'blocked_external_attempts': 0}
        with mock_network_guard(evidence), socket.socket() as s:
            with self.assertRaises(ValueError):
                s.connect(('192.0.2.1', 443))
        self.assertEqual(evidence['blocked_external_attempts'], 1)
        self.assertEqual(evidence['loopback_connections'], 0)

    def test_redaction_nested_values(self):
        public = {'mode': 'long-task-test', 'status': 'completed', 'phase': 'long-steps'}
        self.assertEqual(redact(public), public)
        secret = 'sk-' + 'synthetic_sensitive_marker_123456'
        payload = {'api_key': secret, 'nested': [{'prompt': 'private user text',
                   'error': 'Bearer ' + secret + ' https://example.com/private?token=hidden'}]}
        output = json.dumps(redact(payload))
        for text in (secret, 'private user text', 'example.com', 'hidden'):
            self.assertNotIn(text, output)

    def test_config_and_output_validation(self):
        for config in [{'stages': [2, 1]}, {'samples': 0}, {'timeout': float('nan')},
                       {'mock': {'accounts': [{'capacity': 0}]}}, {'faults': ['invented']}]:
            with self.assertRaises(ValueError):
                load(overrides=config)
        with self.assertRaises(ValueError):
            data_path('/Users/lmurder/Desktop/api中转站/模型测试工作台/results')
        self.assertTrue(data_path(DATA_ROOT / 'tests').is_relative_to(DATA_ROOT))
