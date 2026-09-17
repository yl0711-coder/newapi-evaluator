"""Exercise the admission registry's real control flow with synthetic subprocesses."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import verify_admission as verifier


class AdmissionVerificationTests(unittest.TestCase):
    def test_failed_group_survives_later_passed_groups(self):
        def fake_run(command, env, timeout):
            if '-c' in command:
                return 0, '1\n', False
            if 'scripts/test_web.js' in command:
                return 3, 'UI contract tests passed:\n', False
            if 'scripts/test_all.py' in command:
                return 0, ''.join(f'Ran {n} tests in 0.1s\n\nOK\n' for n in [34, 41, 1]) + '  OK check\n' * 22 + 'All engine and integration checks passed.', False
            if 'scripts/e2e.py' in command:
                return 0, 'Running x\nx passed: 1\n' * 5 + 'All five Mock CLI modes passed', False
            return 0, json.dumps({'status': 'passed', 'checks': 1, 'skipped': 0, 'requests_sent': 0,
                                 'fingerprint': 'synthetic', 'mockRequests': 1, 'files_checked': 1,
                                 'findings': [], 'passed': True}) + '\n' + json.dumps(
                                     {'status': 'valid', 'network_requested': False, 'model': 'gpt-image-2'}), False
        with tempfile.TemporaryDirectory() as folder, patch.object(sys, 'argv', ['verify', '--output', str(Path(folder) / 'report')]), patch.object(verifier, 'run_process', fake_run), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verifier.main(), 1)
            data = json.loads((Path(folder) / 'report/verification.json').read_text())
            self.assertEqual(data['status'], 'failed')
            self.assertEqual(len(data['results']), 12)
            self.assertEqual(data['results'][2]['status'], 'failed')
            self.assertTrue(all(row['status'] == 'passed' for i, row in enumerate(data['results']) if i != 2))

    def test_cancel_during_collection_or_suite_cleans_child_and_records_incomplete(self):
        for stage, number in [('collection', signal.SIGINT), ('suite', signal.SIGTERM)]:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(prefix='admission-cancel-') as folder:
                root = Path(folder)
                ledger = root / 'child.json'
                report = root / 'report'
                child = f'''import json, os, time
from pathlib import Path
p = Path({str(ledger)!r})
t = p.with_suffix('.tmp')
t.write_text(json.dumps({{'pid': os.getpid(), 'pgid': os.getpgrp()}}))
t.replace(p)
time.sleep(30)
'''
                wrapper = f'''import sys
from scripts import verify_admission as v
real_run = v.run_process
def run(command, env, timeout):
    if {stage!r} == 'suite' and '-c' in command:
        return 0, '1\\n', False
    return real_run([sys.executable, '-c', {child!r}], env, timeout)
v.run_process = run
sys.argv = ['verify', '--output', {str(report)!r}]
sys.exit(v.main())
'''
                process = subprocess.Popen([sys.executable, '-B', '-c', wrapper], cwd=verifier.ROOT,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
                owned = None
                try:
                    deadline = time.monotonic() + 5
                    while not ledger.exists():
                        if process.poll() is not None or time.monotonic() >= deadline:
                            self.fail('synthetic child did not start')
                        time.sleep(0.02)
                    owned = json.loads(ledger.read_text())
                    process.send_signal(number)
                    process.communicate(timeout=8)
                    self.assertEqual(process.returncode, 130)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(owned['pid'], 0)
                    data = json.loads((report / 'verification.json').read_text())
                    self.assertTrue(data['cancelled'])
                    self.assertEqual(data['status'], 'incomplete')
                    self.assertEqual(len(data['results']), 12)
                    if stage == 'suite':
                        self.assertEqual(data['results'][0]['reason'], 'cancelled')
                        self.assertEqual(data['results'][0]['status'], 'incomplete')
                    self.assertTrue(all(row['status'] == 'not_run' for row in data['results'][1:]))
                finally:
                    for group in ([owned['pgid']] if owned else []) + [process.pid]:
                        try:
                            os.killpg(group, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    process.communicate(timeout=5)
