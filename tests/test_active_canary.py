"""Active Canary uses the complete frozen corpus and explicit trusted references."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.registry import Registry, RegistryError, Conflict
from features.integrity.unified import UnifiedService, CANARY_VERSION
from features.integrity.strategies import get_strategy
from features.stability.app import storage, layered


class ActiveCanaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="active-canary-synthetic-")
        self.old_db = storage.DB_PATH
        storage.close()
        storage.DB_PATH = Path(self.temp.name) / "stability.db"
        self.registry = Registry(Path(self.temp.name))
        self.channel = self.registry.save({"name":"Synthetic Canary", "base_url":"https://synthetic.example/v1",
            "api_key":"synthetic-canary-credential", "multiplier":1, "status":"recorded"})
        self.svc = UnifiedService(self.registry)
        self.env = patch.dict(os.environ, {"EVAL_INTEGRITY_EXECUTOR":"live"})
        self.env.start()
        self.sent = []
        self.answers = {p.probe_id:str(p.expected) for p in get_strategy("canary").probes}

    async def asyncTearDown(self):
        self.env.stop()
        storage.close()
        storage.DB_PATH = self.old_db
        self.temp.cleanup()

    def submit(self, key="synthetic-active-canary", **changes):
        return self.svc.submit(registry_channel_id=self.channel["id"], idempotency_key=key,
            confirm_live=True, kind=CANARY_VERSION, **changes)

    async def send(self, channel, probe, *, before_send):
        await before_send()
        self.sent.append(probe["id"])
        return {"status":"completed", "text":self.answers.get(probe["id"], "OK"),
            "input_tokens_reported":4390, "output_tokens_reported":512}

    async def test_full_corpus_explicit_baseline_and_same_condition_comparison(self):
        task = self.submit()
        self.assertEqual(task["limits"]["max_requests"],193)
        self.assertEqual(self.submit()["job_id"],task["job_id"])
        self.assertEqual(layered.list_baselines(),[])
        with self.assertRaises(ValueError):
            self.svc.lock_canary_baseline(task["job_id"],"premature")
        await self.svc.run_pending(self.send)
        final = self.svc.get(task["job_id"])
        report = final["reports"][0]
        self.assertEqual((final["status"],len(self.sent),report["attempted"],report["score"]["correct"]),("completed",193,192,192))
        self.assertEqual(report["score"]["status"],"current_only")
        self.assertEqual(layered.list_baselines(),[])
        baseline = self.svc.lock_canary_baseline(task["job_id"],"Explicit synthetic reference")
        compared = self.submit("synthetic-canary-comparison",baseline_id=baseline)
        await self.svc.run_pending(self.send)
        result = self.svc.get(compared["job_id"])
        self.assertEqual(result["reports"][0]["score"]["status"],"no_detected_degradation")
        self.assertEqual(len(self.sent),386)
        self.assertEqual(await self.svc.run_pending(self.send),0)
        with self.assertRaises(Conflict):
            self.submit(baseline_id=baseline)

    async def test_old_or_changed_reference_rejected_before_enqueue(self):
        task = self.submit()
        await self.svc.run_pending(self.send)
        baseline = self.svc.lock_canary_baseline(task["job_id"],"Synthetic reference")
        for drift in ("window", "manifest", "corpus", "connection"):
            with self.subTest(drift=drift):
                original = layered.get_baseline(baseline)
                changed = {**original}
                if drift == "window":
                    changed["conditions"]={**original["conditions"],"budget":{**original["conditions"]["budget"],"window":"overnight"}}
                elif drift == "corpus":
                    changed["outcomes"]={"synthetic-absent":True}
                elif drift == "connection":
                    changed["conditions"]={**original["conditions"],"provider":"registry:1:old"}
                else:
                    changed["manifest_hash"]="old"
                with patch.object(layered,"get_baseline",return_value=changed):
                    with self.assertRaises(RegistryError):
                        self.submit("synthetic-drift-"+drift,baseline_id=baseline)
                self.assertEqual(len(self.svc.list()),1)
        with self.assertRaises(RegistryError):
            self.submit("synthetic-sol-canary",model="gpt-6.1-sol")
        with self.assertRaises(RegistryError):
            self.submit("synthetic-monitor-canary",principal="monitor")

    async def test_cancel_resume_preserves_unknown_and_original_window(self):
        task = self.submit()
        started = asyncio.Event()
        async def delayed(channel,probe,*,before_send):
            if probe["id"] in self.answers and not started.is_set():
                await before_send();self.sent.append(probe["id"]);started.set()
                await asyncio.Event().wait()
            return await self.send(channel,probe,before_send=before_send)
        consumer=asyncio.create_task(self.svc.run_pending(delayed))
        await asyncio.wait_for(started.wait(),5)
        self.svc.cancel(task["job_id"],principal="workbench")
        await asyncio.wait_for(consumer,5)
        cancelled=self.svc.get(task["job_id"])
        self.assertEqual(cancelled["consumed"]["unknown_requests"],1)
        self.svc.resume(task["job_id"],principal="workbench")
        await self.svc.run_pending(self.send)
        final=self.svc.get(task["job_id"])
        self.assertEqual((len(self.sent),final["reports"][0]["unknown"],final["reports"][0]["valid"]),(193,1,191))
        self.assertEqual(final["deadline"],task["deadline"])
        with self.assertRaises(ValueError):
            self.svc.lock_canary_baseline(task["job_id"],"incomplete")

    async def test_health_failure_retains_all_192_not_run_without_retries(self):
        task=self.submit()
        async def failed(channel,probe,*,before_send):
            await before_send();self.sent.append(probe["id"])
            return {"status":"network_error","text":""}
        await self.svc.run_pending(failed)
        final=self.svc.get(task["job_id"])
        self.assertEqual((len(self.sent),final["reports"][0]["planned"],final["reports"][0]["not_run"]),(1,192,192))
        with self.assertRaises(ValueError):
            self.svc.lock_canary_baseline(task["job_id"],"failed")

    async def test_probe_connection_errors_do_not_retry_or_allow_baseline(self):
        task = self.submit()
        async def disconnected(channel, probe, *, before_send):
            if probe['id'] not in self.answers:
                return await self.send(channel, probe, before_send=before_send)
            await before_send()
            self.sent.append(probe['id'])
            return {'status': 'network_error', 'text': ''}
        await self.svc.run_pending(disconnected)
        final = self.svc.get(task['job_id'])
        self.assertEqual(len(self.sent), 193)
        self.assertEqual(len(set(self.sent)), 193)
        self.assertEqual((final['reports'][0]['attempted'], final['reports'][0]['request_errors']), (192, 192))
        with self.assertRaises(ValueError):
            self.svc.lock_canary_baseline(task['job_id'], 'failed probes')

    async def test_target_drift_before_permission_rejects_without_send(self):
        task = self.submit()
        current = self.registry.get(self.channel['id'])
        self.registry.save({**current, 'base_url': 'https://changed.synthetic.example/v1', 'api_key': ''},
                           self.channel['id'], current['version'])
        await self.svc.run_pending(self.send)
        final = self.svc.get(task['job_id'])
        self.assertEqual(final['status'], 'rejected')
        self.assertEqual(final['reports'][0]['not_run'], 192)
        self.assertEqual(self.sent, [])
        with self.assertRaises(ValueError):
            self.svc.lock_canary_baseline(task['job_id'], 'drifted')

    async def test_canary_api_requires_workbench_login_and_same_origin(self):
        import httpx
        from fastapi import FastAPI
        from shared.access import install_access
        from features.integrity.api import router
        with patch.dict(os.environ, {'PLATFORM_USERNAME': 'synthetic-user',
                                    'PLATFORM_PASSWORD': 'synthetic-password'}):
            api = FastAPI()
            install_access(api)
            api.include_router(router)
        body = {'registry_channel_id': self.channel['id'], 'idempotency_key': 'synthetic-api-canary',
                'confirm_live': True}
        with patch('features.integrity.unified.get_service', return_value=self.svc):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://testserver') as client:
                self.assertEqual((await client.post('/api/integrity/canary', json=body)).status_code, 401)
                self.assertEqual((await client.get('/api/integrity/canary/baselines')).status_code, 401)
                client.auth = ('synthetic-user', 'synthetic-password')
                self.assertEqual((await client.post('/api/integrity/canary', json=body,
                    headers={'Origin': 'https://other.synthetic.example'})).status_code, 403)
                self.assertEqual((await client.post('/api/integrity/canary', json={**body, 'confirm_live': False})).status_code, 422)
                self.assertEqual((await client.post('/api/integrity/canary', json={**body, 'baseline_id': True})).status_code, 422)
                self.assertEqual(self.svc.list(), [])
                accepted = await client.post('/api/integrity/canary', json=body)
                self.assertEqual(accepted.status_code, 202)
                job_id = accepted.json()['job_id']
                self.assertEqual((await client.get('/api/integrity/tests/' + job_id)).status_code, 200)
                self.assertEqual((await client.get('/api/integrity/tests/' + job_id + '/export')).status_code, 200)
        self.assertEqual(self.sent, [])

    async def test_active_queue_is_not_hidden_by_scheduled_or_finished_jobs(self):
        import copy
        task = self.submit()
        requests = self.svc._layout('gpt-6-astra', CANARY_VERSION)[2]
        # Create genuine durable jobs with distinct identities and later creation times.
        for index in range(101):
            for kind in ('modeltrace', CANARY_VERSION):
                strategy = copy.deepcopy(task['strategy'])
                strategy['strategy_id'] = kind
                with patch('time.time', return_value=task['created_at'] + index + 1):
                    job = self.svc.store.enqueue(idempotency_key=f'synthetic-mixed-{kind}-{index}',
                        target_snapshot=task['target_snapshot'], strategy=strategy, requests=requests,
                        limits=task['limits'], deadline=task['deadline'], principal='workbench')
                if kind == CANARY_VERSION:
                    with patch('time.time', return_value=task['created_at'] + index + 1):
                        claimed = self.svc.store.claim(job_id=job['job_id'], owner='synthetic-completed')
                        self.assertIsNotNone(claimed)
                        self.assertTrue(self.svc.store.session(job['job_id'], claimed['owner']).finish('completed', '', []))
        visible = self.svc.list()
        self.assertEqual(len(visible), 100)
        self.assertTrue(all(j['task_version'] == CANARY_VERSION for j in visible))
        self.assertEqual(await self.svc.run_pending(self.send), 1)
        self.assertEqual(self.svc.get(task['job_id'])['status'], 'completed')
        self.assertEqual(len(self.sent), 193)
