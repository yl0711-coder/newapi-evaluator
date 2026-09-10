import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from relay_lab.config import DATA_ROOT
from relay_lab.report import rebuild
from relay_lab.model import Result

ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='cli-', dir=DATA_ROOT))
        self.env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}

    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'relay_lab', *args], cwd=ROOT, env=self.env,
                              capture_output=True, text=True, timeout=15)

    def test_external_target_rejected_before_request(self):
        result = self.run_cli('account-test', '--base-url', 'https://example.invalid/v1', '--output', str(self.root / 'external'))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('example.invalid', result.stdout + result.stderr)
        value = json.loads((self.root / 'external/summary.json').read_text())
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['result_count'], 0)
        self.assertEqual(value['network_policy']['socket_audit']['loopback_connections'], 0)

    def test_ctrl_c_saves_partial_report(self):
        output = self.root / 'interrupt'
        cfg = self.root / 'config.yaml'
        cfg.write_text('samples: 10000\nstages: [1, 2]\ntimeout: 0.2\n')
        process = subprocess.Popen([sys.executable, '-m', 'relay_lab', 'account-test', '--config', str(cfg), '--output', str(output)],
                                   cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            raw = output / 'results.jsonl'
            while time.monotonic() < deadline:
                if raw.exists() and raw.stat().st_size > 0:
                    break
                if process.poll() is not None:
                    self.fail('CLI exited before interruption')
                time.sleep(.02)
            else:
                self.fail('CLI did not produce a request result')
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, stderr)
            value = json.loads((output / 'summary.json').read_text())
            self.assertEqual(value['status'], 'interrupted')
            self.assertGreater(value['result_count'], 0)
            self.assertLess(value['result_count'], 10000)
            self.assertTrue((output / 'report.md').is_file())
            rebuilt = self.run_cli('report', '--output', str(output))
            self.assertEqual(rebuilt.returncode, 0)
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)

    def test_config_inspection_safe(self):
        result = self.run_cli('inspect-config', '--config', 'config.example.yaml')
        self.assertEqual(result.returncode, 0)
        value = json.loads(result.stdout)
        self.assertEqual(value['protocol'], 'openai-chat-completions-sse')
        self.assertEqual(len(value['config_fingerprint']), 64)
        self.assertNotIn('base_url', value)

    def test_rebuild_after_abrupt_stop(self):
        output = self.root / 'torn'
        output.mkdir()
        row = Result('1', 'account-c1', 1, success=True, complete=True, latency_ms=10)
        recovery = Result('2', 'account-recovery-c1', 1, phase='recovery', success=True, complete=True)
        (output / 'results.jsonl').write_text(json.dumps(row.public()) + '\n' + json.dumps(recovery.public()) + '\n{"torn":')
        (output / 'run.json').write_text(json.dumps({'mode': 'account-test', 'environment': 'mock',
                                                   'revision': {'commit_sha': 'a' * 40, 'dirty': False}}))
        value = rebuild(output)
        self.assertEqual(value['status'], 'partial_reconstructed')
        self.assertEqual(value['result_count'], 2)
        self.assertEqual(value['stages'][0]['samples'], 1)
        self.assertTrue(value['stages'][0]['throughput_unavailable'])
        self.assertEqual(value['revision']['commit_sha'], 'a' * 40)

    def test_bad_yaml_never_echoes_sensitive_input(self):
        secret = 'sk-' + 'synthetic_invalid_configuration_secret'
        config = self.root / 'bad.yaml'
        config.write_text('model: [' + secret + '\n')
        result = self.run_cli('account-test', '--config', str(config), '--output', str(self.root / 'invalid'))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(secret, result.stderr + result.stdout)

    def test_mock_server_command(self):
        process = subprocess.Popen([sys.executable, '-m', 'relay_lab', 'mock-server', '--port', '0'],
                                   cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            from selectors import DefaultSelector, EVENT_READ
            with DefaultSelector() as selector:
                selector.register(process.stdout, EVENT_READ)
                self.assertTrue(selector.select(timeout=10))
                info = json.loads(process.stdout.readline())
            self.assertEqual(info['host'], '127.0.0.1')
            self.assertGreater(info['port'], 0)
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)
