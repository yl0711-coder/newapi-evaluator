import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from relay_lab.config import DATA_ROOT
from relay_lab.console import Console


class FaderHTTPTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='fader-http-', dir=DATA_ROOT))
        self.console = Console(self.root, history=False)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self.console.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        for job in list(self.console.jobs.values()):
            self.console.cancel(job.id)
            if job.thread:
                job.thread.join(3)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, body=None, token=True, origin=None):
        headers = {}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            if token:
                headers['X-Relay-UI'] = self.console.token
        if origin:
            headers['Origin'] = origin
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            response = self.opener.open(req, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read().decode()
            return response.status, json.loads(raw) if response.headers['Content-Type'].startswith('application/json') else raw

    def wait_job(self, identifier, predicate):
        until = time.monotonic() + 4
        while time.monotonic() < until:
            status, job = self.request('/api/jobs/' + identifier)
            self.assertEqual(status, 200)
            if predicate(job):
                return job
            time.sleep(.02)
        self.fail('Expected job state was not reached')

    def test_real_console_adjust_pause_cancel_download_and_history(self):
        code, state = self.request('/api/state')
        self.assertEqual(state['ui_schema_version'], 4)
        for path, marker in [('/', '三路负载推子'), ('/faders.js', '/faders'), ('/app.css', '.fader-bank')]:
            code, source = self.request(path)
            self.assertEqual(code, 200)
            self.assertIn(marker, source)
        code, job = self.request('/api/jobs', {'environment': 'mock', 'load_mode': 'faders',
                                              'faders': {'targets': [0, 0, 1], 'max_inflight': 4},
                                              'mixed_burst': {'expected_capacity': 1}})
        self.assertEqual(code, 202)
        identifier = job['id']
        self.wait_job(identifier, lambda j: j.get('faders') and j['faders']['channels'][2]['receiving'] == 1)
        path = '/api/jobs/' + identifier + '/faders'
        for kwargs in [{'token': False}, {'origin': 'https://example.invalid'}]:
            self.assertEqual(self.request(path, {'targets': [1, 0, 1]}, **kwargs)[0], 403)
        self.assertEqual(self.request(path, {'targets': [4, 4, 4]})[0], 400)
        self.assertEqual(self.request(path, {'targets': [1, 0, 1]})[0], 200)
        job = self.wait_job(identifier, lambda j: j['faders']['channels'][0]['waiting'] == 1)
        self.assertEqual(job['faders']['targets'], [1, 0, 1])
        self.assertEqual(job['faders']['channels'][0]['receiving'], 0)
        code, job = self.request(path, {'paused': True})
        self.assertTrue(job['faders']['paused'])
        self.request('/api/jobs/' + identifier + '/stop', {})
        job = self.wait_job(identifier, lambda j: j['status'] == 'interrupted')
        self.assertEqual(job['metrics']['errors'], {'cancelled': 2})
        code, text = self.request('/api/jobs/' + identifier + '/fader-events.json')
        self.assertEqual(code, 200)
        self.assertTrue(any(e['targets'] == [1, 0, 1] for e in json.loads(text)['events']))
        history = Console(self.root, history=False)
        history._history()
        restored = next(iter(history.jobs.values())).public()
        self.assertEqual(restored['faders']['issued_requests'], 2)
        self.assertFalse(restored['faders']['server_queue_verified'])
