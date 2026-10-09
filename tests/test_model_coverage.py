"""Synthetic coverage, discovery and scheduling contracts; no external services."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from contextlib import contextmanager
from unittest.mock import patch

import httpx

from shared import registry as registry_module
from shared.registry import Registry, Conflict, RegistryError
from features.model_coverage import discovery, service
from features.model_coverage.catalog import Catalog
from features.stability.app import storage, scheduler
from features.stability.app.main import ScheduleInput
from workbench import create_app


class CoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_sol_catalog_upgrade_preserves_existing_ids_and_edits(self):
        original = self.catalog.models()[0]
        with self.registry.connect() as conn:
            conn.execute("DELETE FROM model_catalog WHERE model='gpt-6.1-sol'")
            conn.execute("DELETE FROM registry_meta WHERE key='model_catalog_sol61'")
            conn.execute("UPDATE model_catalog SET label='Synthetic edited label' WHERE id=?", (original['id'],))
        Catalog(self.registry)
        upgraded = self.catalog.models()
        sol = next(row for row in upgraded if row['model'] == 'gpt-6.1-sol')
        self.assertEqual(sol['protocol'], 'responses')
        self.assertEqual(next(row for row in upgraded if row['id'] == original['id'])['label'],
                         'Synthetic edited label')
        Catalog(self.registry)
        self.assertEqual(sum(row['model'] == 'gpt-6.1-sol' for row in self.catalog.models()), 1)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='coverage-synthetic-')
        self.directory = Path(self.temp.name)
        self.previous_registry, self.previous_db = registry_module._registry, storage.DB_PATH
        storage.close()
        self.registry = Registry(self.directory)
        registry_module._registry = self.registry
        storage.DB_PATH = self.directory / 'stability.db'
        self.catalog = Catalog(self.registry)
        self.channel = self.registry.save({'name':'Synthetic channel','base_url':'https://synthetic.example/v1',
                                           'api_key':'synthetic-coverage-credential','multiplier':1})
        self.model = self.catalog.models()[0]
        self.selection = [{'channel_id':self.channel['id'], 'model_id':self.model['id']}]
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url='http://testserver')

    async def asyncTearDown(self):
        await scheduler.stop()
        await self.client.aclose()
        storage.close()
        storage.DB_PATH = self.previous_db
        registry_module._registry = self.previous_registry
        self.temp.cleanup()

    def stamp(self):
        return self.registry.connection_fingerprint(self.registry.get(self.channel['id'], secret=True))

    def row(self, index=0):
        return service.coverage()['channels'][0]['models'][index]

    def target(self, **changes):
        return storage.upsert_channel({'name':'Synthetic target','registry_channel_id':self.channel['id'],
                                      'model':self.model['model'], 'protocol':'openai','enabled':True, **changes})

    def schedule(self, ids, **changes):
        return storage.upsert_schedule(ScheduleInput(name='Synthetic schedule', daily_times='23:58,23:59',
                                                     channel_ids=ids, **changes).model_dump())

    def observation(self, target_id, *, healthy=True, fingerprint=None, protocol='openai', tested_at=None, source='manual'):
        config = ScheduleInput(name='Synthetic measurement',daily_times='23:58',channel_ids=[target_id]).model_dump()
        run_id = storage.create_run(config, time.time(), source=source)
        summary = {'channels':[{'channel_id':target_id,'registry_channel_id':self.channel['id'],
                                'model':self.model['model'],'protocol':protocol,'connection_fingerprint':fingerprint or self.stamp(),
                                'total':6,'completed':6 if healthy else 4,'timeout_count':0 if healthy else 2,
                                'stream_break_count':0,'streaming_total':3,'pass_rate':1 if healthy else 4/6,
                                'timeout_rate':0 if healthy else 2/6,'stream_break_rate':0,'p95_latency_ms':10,
                                'p95_ttft_ms':2,'health_pass':healthy,'failures':{} if healthy else {'timeout':2},
                                'speed_assessment':{'status':'normal'}}]}
        storage.finish_run(run_id, 'completed', summary)
        if tested_at is not None:
            with storage.cursor() as cur:
                cur.execute('UPDATE model_observations SET tested_at=? WHERE run_id=?', (tested_at,run_id))
        return run_id

    async def test_seed_new_models_global_and_no_requests_on_reads(self):
        self.assertEqual(len(self.catalog.models()),13)
        self.assertEqual([m['model'] for m in self.catalog.models()[:5]],
                         ['gpt-5.5','gpt-5.6-luna','gpt-5.6-terra','gpt-5.6-sol','gpt-6-astra'])
        with patch.object(discovery,'guarded_transport',side_effect=AssertionError('unexpected request')):
            response = await self.client.get('/api/model-coverage')
            self.assertEqual(response.status_code,200)
            self.assertEqual(self.row()['availability'],'unknown')
            self.assertEqual(self.row()['enrollment'],'missing')
            self.assertEqual(self.row()['measurement']['status'],'untested')
        new = await self.client.post('/api/model-coverage/models',json={'model':'synthetic-new-model','label':'New model','family':'Synthetic','protocol':'openai'})
        self.assertEqual(new.status_code,200)
        self.assertEqual(len(Catalog(self.registry).models()),14)
        self.assertEqual(len(service.coverage()['channels'][0]['models']),14)
        self.assertEqual(storage.list_channels(),[])
        self.assertEqual(storage.list_schedules(),[])

    async def test_catalog_validation_and_duplicate(self):
        body={'model':self.model['model'],'label':'Duplicate','family':'GPT','protocol':'openai'}
        self.assertEqual((await self.client.post('/api/model-coverage/models',json=body)).status_code,409)
        body['model']='https://invalid.example/key'
        self.assertEqual((await self.client.post('/api/model-coverage/models',json=body)).status_code,400)
        body['model']='synthetic'; body['protocol']='unknown'
        self.assertEqual((await self.client.post('/api/model-coverage/models',json=body)).status_code,422)

    async def test_manual_fetch_explicit_confirmation_and_exact_matching(self):
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(200,json={'data':[{'id':'gpt-5.5'},{'id':'gpt-5.6-sol-extra'},{'id':'gpt-5.5'}]})
        url=f"/api/model-coverage/channels/{self.channel['id']}/fetch"
        with patch.object(discovery,'guarded_transport',return_value=httpx.MockTransport(handler)):
            self.assertEqual((await self.client.post(url,json={})).status_code,422)
            self.assertEqual((await self.client.post(url,json={'confirm_live':False})).status_code,422)
            self.assertEqual(len(calls),0)
            result=await self.client.post(url,json={'confirm_live':True})
        self.assertTrue(result.json()['ok'])
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0].method,'GET')
        self.assertEqual(calls[0].url.path,'/v1/models')
        self.assertEqual(calls[0].headers['authorization'],'Bearer synthetic-coverage-credential')
        self.assertEqual(self.row()['availability'],'listed')
        self.assertEqual(self.row(3)['availability'],'not_listed')
        self.assertEqual(service.coverage()['channels'][0]['new_models'],['gpt-5.6-sol-extra'])
        self.assertNotIn('synthetic-coverage-credential',(await self.client.get('/api/model-coverage')).text)

    async def test_failed_discovery_preserves_snapshot_without_time_expiry(self):
        with patch.object(discovery,'guarded_transport',return_value=httpx.MockTransport(lambda _:httpx.Response(200,json={'data':[{'id':'gpt-5.5'}]}))):
            await discovery.fetch_models(self.registry,self.channel['id'],'openai')
        with self.registry.connect() as conn:
            conn.execute('UPDATE channel_model_discovery SET succeeded_at=1')
        self.assertEqual(self.row()['availability'],'listed')
        with patch.object(discovery,'guarded_transport',return_value=httpx.MockTransport(lambda _:httpx.Response(403,text='synthetic-coverage-credential'))):
            result=await discovery.fetch_models(self.registry,self.channel['id'],'openai')
        self.assertEqual(result['error'],'http_403')
        row=self.row()
        self.assertEqual(row['availability'],'fetch_failed')
        self.assertTrue(row['previously_listed'])
        self.assertEqual(self.catalog.discoveries()[self.channel['id']]['models'],['gpt-5.5'])

    async def test_anthropic_pagination_and_headers(self):
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(200,json={'data':[{'id':'claude-opus-5' if len(calls)==1 else 'claude-fable-5'}],
                                          'has_more':len(calls)==1,'last_id':'claude-opus-5'})
        with patch.object(discovery,'guarded_transport',return_value=httpx.MockTransport(handler)):
            result=await discovery.fetch_models(self.registry,self.channel['id'],'anthropic')
        self.assertTrue(result['ok'])
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[1].url.params['after_id'],'claude-opus-5')
        self.assertEqual(calls[0].headers['x-api-key'],'synthetic-coverage-credential')
        self.assertNotIn('authorization',calls[0].headers)

    async def test_bad_lists_redirects_and_partial_pages_are_unknown(self):
        for response in [httpx.Response(200,json=[]),httpx.Response(200,json={'data':[{}]}),
                         httpx.Response(200,json={'data':[{'id':'synthetic-coverage-credential'}]}),
                         httpx.Response(200,json={'data':[{'id':'vendor/sk-synthetic-model-0123456789'}]}),
                         httpx.Response(200,json={'data':[],'has_more':True}),
                         httpx.Response(302,headers={'location':'https://different.example/v1/models'}),
                         httpx.Response(200,text='malformed')]:
            with self.subTest(response=response.status_code), patch.object(discovery,'guarded_transport',return_value=httpx.MockTransport(lambda _:response)):
                result=await discovery.fetch_models(self.registry,self.channel['id'],'openai')
                self.assertFalse(result['ok'])
                self.assertEqual(self.catalog.discoveries()[self.channel['id']]['models'],[])
                self.assertNotIn('vendor/sk-synthetic-model-0123456789', json.dumps(result))

    async def test_list_size_and_timeout_are_bounded(self):
        def timeout(request): raise httpx.ReadTimeout('synthetic secret',request=request)
        with patch.object(discovery,'guarded_transport',return_value=httpx.MockTransport(timeout)):
            result=await discovery.fetch_models(self.registry,self.channel['id'],'openai')
        self.assertEqual(result['error'],'timeout')
        with patch.object(discovery,'MAX_BYTES',20), patch.object(discovery,'guarded_transport',return_value=httpx.MockTransport(lambda _:httpx.Response(200,content=b' ' * 30))):
            result=await discovery.fetch_models(self.registry,self.channel['id'],'openai')
        self.assertEqual(result['error'],'list_too_large')

    async def test_late_fetch_cannot_overwrite_newer_fetch(self):
        started=asyncio.Event(); release=asyncio.Event(); number=0
        async def handler(_):
            nonlocal number
            number+=1
            current=number
            if current==1:
                started.set(); await release.wait()
            return httpx.Response(200,json={'data':[{'id':'old-model' if current==1 else 'new-model'}]})
        with patch.object(discovery,'guarded_transport',side_effect=lambda:httpx.MockTransport(handler)):
            old=asyncio.create_task(discovery.fetch_models(self.registry,self.channel['id'],'openai'))
            await started.wait()
            await discovery.fetch_models(self.registry,self.channel['id'],'openai')
            release.set(); await old
        self.assertEqual(self.catalog.discoveries()[self.channel['id']]['models'],['new-model'])

    async def test_rotation_during_discovery_rejects_old_result(self):
        def handler(_):
            self.registry.save({**self.channel,'api_key':'synthetic-rotated'},self.channel['id'],self.channel['version'])
            return httpx.Response(200,json={'data':[{'id':'gpt-5.5'}]})
        with patch.object(discovery,'guarded_transport',return_value=httpx.MockTransport(handler)):
            result=await discovery.fetch_models(self.registry,self.channel['id'],'openai')
        self.assertEqual(result['error'],'connection_changed')
        self.assertFalse(self.row()['previously_listed'])

    async def test_exact_mapping_and_responses_lock(self):
        self.catalog.bind(self.channel['id'],self.model['id'],'upstream-gpt','responses')
        row=self.row()
        self.assertEqual((row['upstream_model'],row['protocol']),('upstream-gpt','responses'))
        with self.assertRaises(RegistryError):
            self.catalog.bind(self.channel['id'],self.catalog.models()[4]['id'],'gpt6-alias','openai')
        with self.assertRaises(RegistryError):
            self.catalog.bind(self.channel['id'],self.model['id'],'synthetic-coverage-credential','openai')

    async def test_real_enrollment_states_and_paused_channel(self):
        target=self.target()
        self.assertEqual(self.row()['enrollment'],'unscheduled')
        plan=self.schedule([target])
        self.assertEqual(self.row()['enrollment'],'scheduled')
        with storage.cursor() as cur: cur.execute('UPDATE schedules SET enabled=0 WHERE id=?',(plan,))
        self.assertEqual(self.row()['enrollment'],'paused')
        with storage.cursor() as cur: cur.execute('UPDATE schedules SET enabled=1 WHERE id=?',(plan,))
        self.registry.save({**self.channel,'api_key':'','enabled':False},self.channel['id'],self.channel['version'])
        self.assertEqual(self.row()['enrollment'],'paused')

    async def test_mapped_gpt6_requires_responses_for_verification_and_enrollment(self):
        path = f"/api/model-coverage/channels/{self.channel['id']}/models/{self.model['id']}"
        response = await self.client.put(path, json={'upstream_model': 'gpt-6-astra', 'protocol': 'openai'})
        self.assertEqual(response.status_code, 400)
        response = await self.client.put(path, json={'upstream_model': 'gpt-6-astra', 'protocol': 'responses'})
        self.assertEqual(response.status_code, 200)
        with patch.object(scheduler, 'tick'):
            response = await self.client.post('/api/model-coverage/verify', json={'items': self.selection, 'confirm_live': True})
        self.assertEqual(response.status_code, 202)
        run = storage.get_run(response.json()['run_id'])
        target = storage.get_channel(run['snapshot']['channel_ids'][0])
        self.assertEqual((target['model'], target['protocol']), ('gpt-6-astra', 'responses'))
        other = self.target(model='synthetic-existing')
        plan = self.schedule([other])
        body = {'schedule_id': plan, 'items': self.selection}
        preview = await self.client.post('/api/model-coverage/enrollment/preview', json=body)
        self.assertEqual(preview.status_code, 200)
        response = await self.client.post('/api/model-coverage/enrollment', json={**body,
                                         'preview_token': preview.json()['preview_token'], 'confirm_live': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(storage.get_schedule(plan)['channel_ids']), {other, target['id']})

    async def test_saved_invalid_mapping_cannot_create_verification_or_enroll(self):
        self.catalog.bind(self.channel['id'], self.model['id'], 'gpt-6-astra', 'responses')
        with self.registry.connect() as conn:
            conn.execute("UPDATE channel_model_bindings SET protocol='openai'")
        other = self.target(model='synthetic-existing')
        plan = self.schedule([other])
        with patch.object(scheduler, 'tick') as tick:
            response = await self.client.post('/api/model-coverage/verify', json={'items': self.selection, 'confirm_live': True})
        self.assertEqual(response.status_code, 400)
        tick.assert_not_awaited()
        body = {'schedule_id': plan, 'items': self.selection}
        response = await self.client.post('/api/model-coverage/enrollment/preview', json=body)
        self.assertEqual(response.status_code, 400)
        response = await self.client.post('/api/model-coverage/enrollment', json={**body,
                                         'preview_token': '0' * 64, 'confirm_live': True})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(storage.list_runs(), [])
        self.assertEqual(len(storage.list_channels()), 1)
        self.assertEqual(storage.get_schedule(plan)['channel_ids'], [other])

    async def test_health_needs_multiple_batches_and_is_separate_from_speed(self):
        target=self.target()
        self.observation(target)
        self.assertEqual(self.row()['measurement']['status'],'observing')
        self.observation(target); self.observation(target)
        measured=self.row()['measurement']
        self.assertEqual((measured['status'],measured['batches'],measured['samples']),('stable',3,18))
        self.observation(target,healthy=False)
        self.assertEqual(self.row()['measurement']['status'],'unstable')
        self.assertEqual(self.row()['measurement']['failures'],{'timeout':2})

    async def test_duplicate_targets_in_one_run_do_not_inflate_batch_count(self):
        target=self.target(); run=self.observation(target)
        with storage.cursor() as cur:
            cur.execute('INSERT INTO model_observations SELECT run_id,target_id+1,registry_channel_id,model,protocol,connection_fingerprint,tested_at,summary_json FROM model_observations WHERE run_id=?',(run,))
        self.assertEqual(self.row()['measurement']['batches'],1)

    async def test_expiry_and_connection_change_invalidate_health(self):
        target=self.target(); self.observation(target,tested_at=time.time()-service.FRESH_SECONDS-1)
        self.assertEqual(self.row()['measurement']['status'],'stale')
        self.registry.save({**self.channel,'api_key':'synthetic-new'},self.channel['id'],self.channel['version'])
        self.assertEqual(self.row()['measurement']['status'],'connection_changed')

    async def test_name_edit_does_not_invalidate_connection(self):
        target=self.target(); self.observation(target)
        self.registry.save({**self.channel,'api_key':'','name':'Renamed'},self.channel['id'],self.channel['version'])
        self.assertEqual(self.row()['measurement']['status'],'observing')

    async def test_protocol_and_model_mapping_do_not_mix_health(self):
        target=self.target(); self.observation(target,protocol='anthropic')
        self.assertEqual(self.row()['measurement']['status'],'untested')
        self.catalog.bind(self.channel['id'],self.model['id'],'new-alias','anthropic')
        self.assertEqual(self.row()['measurement']['status'],'untested')

    async def test_speed_baseline_does_not_cross_protocol_or_connection(self):
        target = self.target()
        for _ in range(5):
            run_id = self.observation(target, source='schedule')
            storage.add_probe_result(run_id, {
                'channel_id': target, 'channel_name': 'Synthetic target', 'model': self.model['model'],
                'round_number': 1, 'probe_id': 'synthetic-speed', 'ok': True,
                'status': 'completed', 'latency_ms': 1000,
            })
        snapshot = ScheduleInput(name='Synthetic speed check', daily_times='23:58', channel_ids=[target],
                                 rounds=1, round_interval_seconds=0, speed_threshold_mode='adaptive').model_dump()

        async def probe_result(_client, _channel, probe):
            return {'probe_id': probe['id'], 'ok': True, 'status': 'completed', 'latency_ms': 500,
                    'ttft_ms': None, 'tokens_per_second': None, 'stream_break': False}

        for protocol, rotate_key, expected_runs in [('openai', False, 5), ('responses', False, 0), ('openai', True, 0)]:
            with self.subTest(protocol=protocol, rotate_key=rotate_key):
                if rotate_key:
                    self.registry.save({**self.channel, 'api_key': 'synthetic-rotated'},
                                       self.channel['id'], self.channel['version'])
                storage.upsert_channel({'id': target, 'name': 'Synthetic target', 'registry_channel_id': self.channel['id'],
                                        'model': self.model['model'], 'protocol': protocol, 'enabled': True})
                run_id = storage.create_run(snapshot, time.time(), source='coverage')
                channel = storage.list_channels(include_secrets=True, ids=[target])[0]
                with patch.object(scheduler.transport, 'run_probe', side_effect=probe_result):
                    measured = await scheduler._measure_channel(run_id, channel, snapshot)
                speed = measured['speed_assessment']
                self.assertEqual(speed['historical_runs'], expected_runs)
                self.assertEqual(speed['status'], 'normal' if expected_runs else 'collecting')
                self.assertEqual(speed['baseline_median_p95_ms'], 1000 if expected_runs else None)

    async def test_report_pruning_preserves_last_observation(self):
        target=self.target(); run=self.observation(target)
        storage.update_notification(run,'skipped')
        storage.prune_run_history(time.time()+7*86400,5)
        result=self.row()['measurement']
        self.assertIsNone(result['report_id'])
        self.assertEqual(result['status'],'observing')

    async def test_preview_enroll_deduplication_and_preserve_existing_plan(self):
        other=self.target(model='synthetic-other'); plan=self.schedule([other])
        before=storage.get_schedule(plan)
        preview=service.plan_preview(plan,self.selection*2)
        self.assertEqual((len(preview['items']),preview['added_requests_per_run'],preview['added_requests_per_day']),(1,18,36))
        result=service.enroll(plan,self.selection,preview['preview_token'])
        self.assertEqual(result['added'],1)
        after=storage.get_schedule(plan)
        self.assertIn(other,after['channel_ids'])
        self.assertEqual(after['daily_times'],before['daily_times'])
        self.assertEqual(storage.list_runs(),[])
        again=service.plan_preview(plan,self.selection)
        self.assertEqual(again['added_requests_per_day'],0)
        self.assertEqual(service.enroll(plan,self.selection,again['preview_token'])['skipped'],1)
        self.assertEqual(len(storage.list_channels()),2)
        self.assertEqual(self.row()['enrollment'],'scheduled')

    async def test_preview_detects_changed_plan_mapping_and_credentials(self):
        other=self.target(model='synthetic-other'); plan=self.schedule([other])
        preview=service.plan_preview(plan,self.selection)
        self.catalog.bind(self.channel['id'],self.model['id'],'new-alias','openai')
        with self.assertRaises(Conflict): service.enroll(plan,self.selection,preview['preview_token'])
        self.assertEqual(len(storage.list_channels()),1)
        preview=service.plan_preview(plan,self.selection)
        self.registry.save({**self.channel,'api_key':'synthetic-rotated'},self.channel['id'],self.channel['version'])
        with self.assertRaises(Conflict): service.enroll(plan,self.selection,preview['preview_token'])
        self.assertEqual(len(storage.list_channels()),1)

    async def test_capacity_failure_rolls_back_created_targets(self):
        ids=[self.target(name=f'Synthetic {i}',model=f'synthetic-{i}') for i in range(100)]
        plan=self.schedule(ids)
        preview=service.plan_preview(plan,self.selection)
        with self.assertRaises(RegistryError): service.enroll(plan,self.selection,preview['preview_token'])
        self.assertEqual(len(storage.list_channels()),100)
        self.assertEqual(len(storage.get_schedule(plan)['channel_ids']),100)

    async def test_single_verification_does_not_schedule_or_notify(self):
        with patch.object(scheduler,'tick') as tick:
            response=await self.client.post('/api/model-coverage/verify',json={'items':self.selection,'confirm_live':True})
        self.assertEqual(response.status_code,202)
        tick.assert_awaited_once()
        run=storage.get_run(response.json()['run_id'])
        self.assertEqual((run['source'],run['notify_status'],run['snapshot']['rounds']),('coverage','skipped',1))
        self.assertEqual(storage.list_schedules(),[])
        self.assertEqual(self.row()['enrollment'],'unscheduled')
        with self.assertRaises(Conflict): service.verify_once(self.selection)

    async def test_single_verification_cannot_resume_paused_target(self):
        target=self.target(enabled=False)
        self.schedule([target])
        with self.assertRaises(RegistryError): service.verify_once(self.selection)
        self.assertFalse(storage.get_channel(target)['enabled'])
        self.assertEqual(storage.list_runs(),[])

    async def test_auth_cross_site_modes_and_no_secret_validation_echo(self):
        with patch.dict('os.environ',{'PLATFORM_USERNAME':'synthetic','PLATFORM_PASSWORD':'synthetic-password'}):
            protected=create_app('channels')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=protected),base_url='http://testserver') as client:
            self.assertEqual((await client.get('/api/model-coverage')).status_code,401)
        response=await self.client.post('/api/model-coverage/models',headers={'origin':'https://other.example'},json={})
        self.assertEqual(response.status_code,403)
        isolated=create_app('channels')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated),base_url='http://testserver') as client:
            response=await client.post('/api/model-coverage/verify',json={'items':self.selection,'confirm_live':True})
            self.assertEqual(response.status_code,409)
        response=await self.client.post('/api/model-coverage/models',json={'api_key':'synthetic-hidden'})
        self.assertEqual(response.status_code,422)
        self.assertNotIn('synthetic-hidden',response.text)

    async def test_readonly_inspection_preserves_configuration_without_decryption(self):
        from scripts.inspect_model_coverage import inspect
        before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.directory.iterdir() if p.suffix in {'.db','.key'}}
        # WAL readers may create SQLite coordination sidecars, even with mode=ro.
        with patch.object(Path,'read_bytes',side_effect=AssertionError('inspection must not load the key file')):
            result=inspect(self.directory)
        after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.directory.iterdir() if p.suffix in {'.db','.key'}}
        self.assertEqual(before,after)
        self.assertEqual(result['requests_sent'],0)
        self.assertFalse(result['secrets_read'])
        self.assertEqual(len(result['models']),13)
        encoded=json.dumps(result)
        self.assertNotIn('synthetic-coverage-credential',encoded)
        self.assertNotIn('synthetic.example',encoded)

    async def test_daily_batches_can_accumulate_and_observation_storage_is_bounded(self):
        target=self.target()
        for days in [2,1,0]: self.observation(target,tested_at=time.time()-days*86400)
        self.assertEqual(self.row()['measurement']['status'],'stable')
        for _ in range(32): self.observation(target)
        self.assertEqual(len(storage.model_observations()),30)

    async def test_readonly_inspection_tracks_channel_model_mapping(self):
        from scripts.inspect_model_coverage import inspect
        before = inspect(self.directory)
        self.catalog.bind(self.channel['id'], self.model['id'], 'synthetic-model-alias', 'responses')
        mapped = inspect(self.directory)
        self.assertNotEqual(mapped['fingerprint'], before['fingerprint'])
        self.assertEqual(mapped['mappings'], [{'channel_alias': f"channel-{self.channel['id']}",
                         'model': self.model['model'], 'upstream_model': 'synthetic-model-alias', 'protocol': 'responses'}])
        self.catalog.bind(self.channel['id'], self.model['id'], 'synthetic-model-alias', 'openai')
        changed = inspect(self.directory)
        self.assertNotEqual(changed['fingerprint'], mapped['fingerprint'])
        self.assertEqual(changed['mappings'][0]['protocol'], 'openai')
        self.assertEqual(changed['requests_sent'], 0)
        self.assertFalse(changed['secrets_read'])

    async def test_readonly_inspection_supports_uninitialized_catalog(self):
        from scripts.inspect_model_coverage import inspect
        legacy = Registry(self.directory / 'legacy')
        default = inspect()
        result = inspect(legacy.directory)
        self.assertEqual(result['models'], default['models'])
        self.assertEqual(result['mappings'], [])
        self.assertEqual(default['mappings'], [])
        with legacy.connect() as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('model_catalog', names)
        self.assertNotIn('channel_model_bindings', names)

    async def test_enrollment_api_requires_confirmation_and_preserves_preview(self):
        target=self.target(model='other-synthetic'); plan=self.schedule([target])
        body={'items':self.selection,'schedule_id':plan}
        preview=(await self.client.post('/api/model-coverage/enrollment/preview',json=body)).json()
        self.assertEqual((await self.client.post('/api/model-coverage/enrollment',json={**body,'preview_token':preview['preview_token']})).status_code,422)
        self.assertEqual(len(storage.list_channels()),1)
        result=await self.client.post('/api/model-coverage/enrollment',json={**body,'preview_token':preview['preview_token'],'confirm_live':True})
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['added'],1)

    async def test_enrollment_does_not_resume_unselected_plans(self):
        target=self.target(enabled=False)
        self.schedule([target])
        other=self.target(name='Synthetic other',model='other-model')
        plan=storage.upsert_schedule(ScheduleInput(name='Second plan',daily_times='23:59',channel_ids=[other]).model_dump())
        with self.assertRaises(RegistryError): service.plan_preview(plan,self.selection)
        self.assertFalse(storage.get_channel(target)['enabled'])

    async def test_production_snapshot_import_is_idempotent_and_separates_coverage(self):
        payload = {'source':'synthetic-production','version':'v1','generated_at':time.time(),'cursor':'c1',
                   'items':[{'channel_identity':'channel-a','model':self.model['model'],'protocol':'openai',
                             'production_status':'online','eval_channel_id':self.channel['id']},
                            {'channel_identity':'channel-b','model':'missing-model','protocol':'openai',
                             'production_status':'online'}]}
        first = await self.client.post('/api/model-coverage/production/import', json=payload)
        self.assertEqual(first.status_code, 200)
        second = await self.client.post('/api/model-coverage/production/import', json=payload)
        self.assertEqual((second.status_code, second.json()['added']), (200, False))
        overview = (await self.client.get('/api/model-coverage')).json()['production_coverage']['sources'][0]
        self.assertEqual([item['coverage'] for item in overview['items']], ['covered','conflict'])
        changed = {**payload, 'items':[{'channel_identity':'channel-a','model':'changed-model','protocol':'openai','production_status':'online'}]}
        self.assertEqual((await self.client.post('/api/model-coverage/production/import', json=changed)).status_code, 409)

    async def test_production_snapshot_rejects_secret_and_marks_stale(self):
        bad = {'source':'synthetic-production','version':'secret','generated_at':time.time(),'items':[
            {'channel_identity':'https://secret.example','model':'safe-model','protocol':'openai','production_status':'online'}]}
        self.assertEqual((await self.client.post('/api/model-coverage/production/import', json=bad)).status_code, 400)
        old = {'source':'synthetic-old','version':'v1','generated_at':time.time()-49*3600,'items':[
            {'channel_identity':'channel-a','model':'safe-model','protocol':'openai','production_status':'online'}]}
        self.assertEqual((await self.client.post('/api/model-coverage/production/import', json=old)).status_code, 200)
        sources = (await self.client.get('/api/model-coverage')).json()['production_coverage']['sources']
        self.assertEqual(sources[0]['items'][0]['coverage'], 'stale')
        from features.model_coverage.production import ProductionCoverage
        nan = {**old, 'source':'synthetic-nan', 'generated_at':float('nan')}
        with self.assertRaises(RegistryError): ProductionCoverage(self.registry).import_snapshot(nan)

    async def test_workflow_queue_is_idempotent_and_cancellable(self):
        body={'task_type':'synthetic-reconcile','payload':{'source':'local'},'idempotency_key':'synthetic-key','budget_seconds':60}
        first=await self.client.post('/api/model-coverage/workflow/tasks',json=body)
        self.assertEqual(first.status_code,200)
        duplicate=await self.client.post('/api/model-coverage/workflow/tasks',json=body)
        self.assertEqual((duplicate.status_code,duplicate.json()['id']),(200,first.json()['id']))
        conflict={**body,'payload':{'source':'changed'}}
        self.assertEqual((await self.client.post('/api/model-coverage/workflow/tasks',json=conflict)).status_code,409)
        task_id=first.json()['id']
        cancelled=await self.client.post(f'/api/model-coverage/workflow/tasks/{task_id}/cancel')
        self.assertEqual(cancelled.json()['state'],'cancelled')
        self.assertEqual((await self.client.get('/api/model-coverage/workflow/tasks')).json()['tasks'][0]['cancel_requested'],True)

    async def test_monitor_event_maps_to_idempotent_local_task(self):
        event={'event_id':'monitor-1','event_type':'coverage.changed','payload':{'channel_identity':'synthetic'}}
        first=await self.client.post('/api/model-coverage/workflow/events',json=event)
        second=await self.client.post('/api/model-coverage/workflow/events',json=event)
        self.assertEqual((first.status_code,second.status_code),(202,202))
        self.assertEqual(first.json()['id'],second.json()['id'])
        self.assertEqual(first.json()['task_type'],'monitor:coverage.changed')

    async def test_workflow_payload_rejects_sensitive_fields(self):
        body={'task_type':'synthetic','payload':{'api_key':'sk-synthetic-secret'},'idempotency_key':'secret-key'}
        response=await self.client.post('/api/model-coverage/workflow/tasks',json=body)
        self.assertEqual(response.status_code,400)
        oversized={'task_type':'synthetic','payload':{'blob':'x'*(64*1024)},'idempotency_key':'oversized'}
        self.assertEqual((await self.client.post('/api/model-coverage/workflow/tasks',json=oversized)).status_code,400)

    async def test_workflow_finish_requires_claim_and_terminal_state_is_immutable(self):
        from features.model_coverage.workflow import WorkflowQueue
        queue=WorkflowQueue(self.registry)
        task=queue.enqueue('synthetic',{},idempotency_key='immutable')
        with self.assertRaises(Conflict): queue.finish(task['id'],'succeeded')
        claimed=queue.claim_next()
        self.assertEqual(claimed['state'],'running')
        done=queue.finish(task['id'],'succeeded',{'ok':True})
        self.assertEqual(done['state'],'succeeded')
        with self.assertRaises(Conflict): queue.finish(task['id'],'failed')

    async def test_workflow_outbox_is_idempotent_and_guarded(self):
        from features.model_coverage.workflow import WorkflowQueue
        queue=WorkflowQueue(self.registry)
        task=queue.enqueue('synthetic-outbox',{},idempotency_key='outbox-task')
        first=queue.enqueue_outbox(task['id'],'notify',{'n':1})
        again=queue.enqueue_outbox(task['id'],'notify',{'n':2})
        self.assertEqual((first['id'],again['payload']),(again['id'],{'n':1}),'same task and kind keep the first row')
        with self.assertRaises(KeyError): queue.enqueue_outbox(9999,'notify',{})
        with self.assertRaises(RegistryError): queue.enqueue_outbox(task['id'],'secret',{'api_key':'sk-synthetic'})
        with self.assertRaises(RegistryError): queue.enqueue_outbox(task['id'],'large',{'blob':'x'*(64*1024)})

    async def test_workflow_claim_is_single_winner(self):
        from features.model_coverage.workflow import WorkflowQueue
        queue=WorkflowQueue(self.registry)
        queue.enqueue('synthetic',{},idempotency_key='single-winner')
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed=list(pool.map(lambda _: WorkflowQueue(self.registry).claim_next(), range(2)))
        self.assertEqual(sum(item is not None for item in claimed),1)

    async def test_workflow_runner_records_success_and_failure_without_hidden_retries(self):
        from features.model_coverage.runner import run_one
        from features.model_coverage.workflow import WorkflowQueue
        queue=WorkflowQueue(self.registry)
        queue.enqueue('synthetic-success',{'value':1},idempotency_key='runner-success')
        result=run_one(queue,lambda payload,cancel:{'value':payload['value']+1})
        self.assertEqual((result['state'],result['result']['value']),("succeeded",2))
        queue.enqueue('synthetic-failure',{'value':1},idempotency_key='runner-failure')
        failed=run_one(queue,lambda payload,cancel: (_ for _ in ()).throw(RuntimeError('synthetic failure')))
        self.assertEqual((failed['state'],failed['error']),('failed','synthetic failure'))


    async def test_production_latest_uses_generation_time_and_keeps_history(self):
        from features.model_coverage.production import ProductionCoverage
        production = ProductionCoverage(self.registry)
        now = time.time()
        def snapshot(source, version, generated_at):
            return {'source': source, 'version': version, 'generated_at': generated_at,
                    'items': [{'channel_identity': 'synthetic-channel', 'model': 'synthetic-model',
                               'protocol': 'openai', 'production_status': 'online'}]}
        newer = snapshot('synthetic-z', 'new', now)
        older = snapshot('synthetic-z', 'old', now - 49 * 3600)
        for payload in [newer, snapshot('synthetic-a', 'a-new', now - 10), older,
                        snapshot('synthetic-a', 'a-old', now - 20)]:
            self.assertTrue(production.import_snapshot(payload)['added'])
        sources = production.overview([])['sources']
        self.assertEqual([(x['source'], x['version']) for x in sources],
                         [('synthetic-a', 'a-new'), ('synthetic-z', 'new')])
        self.assertNotEqual(sources[1]['items'][0]['coverage'], 'stale')
        with self.registry.connect() as conn:
            before = [tuple(row) for row in conn.execute('SELECT * FROM production_coverage_snapshots ORDER BY id')]
        self.assertFalse(production.import_snapshot(older)['added'])
        for changed in [{**newer, 'generated_at': now - 1},
                        {**newer, 'items': [{**newer['items'][0], 'production_status': 'offline'}]}]:
            with self.assertRaises(Conflict): production.import_snapshot(changed)
        with self.registry.connect() as conn:
            self.assertEqual(before, [tuple(row) for row in conn.execute('SELECT * FROM production_coverage_snapshots ORDER BY id')])
        production.import_snapshot(snapshot('synthetic-z', 'same-time', now))
        self.assertEqual(production.overview([])['sources'][1]['version'], 'same-time')
        self.assertFalse(production.import_snapshot(newer)['added'])
        self.assertEqual(production.overview([])['sources'][1]['version'], 'same-time')

    def workflow_rows(self):
        with self.registry.connect() as conn:
            return [tuple(row) for row in conn.execute('SELECT * FROM eval_workflow_tasks ORDER BY id')]

    async def test_workflow_finish_rejects_sensitive_values_without_any_write(self):
        from features.model_coverage.workflow import WorkflowQueue
        queue = WorkflowQueue(self.registry)
        unsafe = [{'nested': [{field: 'synthetic-value'}]} for field in
                  ['api_key', 'api key', 'token', 'cookie', 'password', 'secret', 'auth', 'authorization']]
        unsafe += [{'message': text} for text in ['Bearer synthetic-value', 'Basic synthetic-value', 'sk-synthetic-value',
                   'https://synthetic.invalid/path?value=synthetic', 'api_key = synthetic-value',
                   'API key: synthetic-value', 'password: synthetic-value', 'secret=synthetic-value',
                   'auth: synthetic-value', 'token = synthetic-value', 'cookie : synthetic-value']]
        unsafe += [{'nested': ({'token': 'synthetic-value'},)},
                   {'https://synthetic.invalid': 'safe'}, {'sk-synthetic-value': 'safe'},
                   {'safe': ('Bearer synthetic-value',)}]
        for index, result in enumerate(unsafe):
            with self.subTest(case=index):
                task = queue.enqueue('synthetic', {}, idempotency_key=f'result-{index}')
                queue.claim_next()
                before = self.workflow_rows()
                with self.assertRaises(RegistryError) as raised: queue.finish(task['id'], 'succeeded', result)
                self.assertNotIn('synthetic-value', str(raised.exception))
                self.assertEqual(before, self.workflow_rows())
                self.assertEqual(queue.finish(task['id'], 'succeeded', {'ok': True})['state'], 'succeeded')
        for index, error in enumerate(['Bearer synthetic-value', 'api_key=synthetic-value',
                                       'x' * 301 + ' password = synthetic-value',
                                       'API key: synthetic-value', 'token = synthetic-value',
                                       'secret: synthetic-value', 'Authorization: synthetic-value',
                                       'Authorization: Basic synthetic-value', 'Basic synthetic-value',
                                       'x' * 5000 + ' https://synthetic.invalid/path']):
            with self.subTest(error_case=index):
                task = queue.enqueue('synthetic', {}, idempotency_key=f'error-{index}')
                queue.claim_next()
                before = self.workflow_rows()
                with self.assertRaises(RegistryError): queue.finish(task['id'], 'failed', {'ok': False}, error)
                self.assertEqual(before, self.workflow_rows())
                done = queue.finish(task['id'], 'failed', {'ok': False}, 'synthetic failure')
                self.assertEqual((done['state'], done['error']), ('failed', 'synthetic failure'))

    async def test_workflow_finish_preserves_safe_json_and_limits(self):
        from features.model_coverage.workflow import WorkflowQueue
        queue = WorkflowQueue(self.registry)
        task = queue.enqueue('synthetic', {}, idempotency_key='safe-json')
        queue.claim_next()
        result = {'metrics': [None, True, 1, 1.5, 'safe'], 'nested': {'status': 'ok'},
                  'tuple': (1, 'safe'), 12: 'number-key'}
        done = queue.finish(task['id'], 'succeeded', result, 'x' * 5000)
        self.assertEqual(done['result'], json.loads(json.dumps(result)))
        self.assertEqual(done['error'], 'x' * 300)
        for index, result in enumerate([{'blob': 'x' * 4097}, {'nodes': [0] * 1001},
                                       {'value': float('nan')}, {'value': object()},
                                       {'size': ['x' * 4096] * 17}]):
            task = queue.enqueue('synthetic', {}, idempotency_key=f'invalid-{index}')
            queue.claim_next()
            before = self.workflow_rows()
            with self.assertRaises(RegistryError): queue.finish(task['id'], 'succeeded', result)
            self.assertEqual(before, self.workflow_rows())
            queue.finish(task['id'], 'failed', {}, 'invalid result')
        task = queue.enqueue('synthetic', {}, idempotency_key='too-deep')
        queue.claim_next()
        result = {}
        for _ in range(10): result = {'nested': result}
        with self.assertRaises(RegistryError): queue.finish(task['id'], 'succeeded', result)

    async def test_workflow_cancel_discards_unsafe_finish_and_terminals_stay_immutable(self):
        from features.model_coverage.workflow import WorkflowQueue
        queue = WorkflowQueue(self.registry)
        task = queue.enqueue('synthetic', {}, idempotency_key='cancel-unsafe')
        before = self.workflow_rows()
        with self.assertRaises(Conflict): queue.finish(task['id'], 'succeeded', {'token': 'synthetic'})
        self.assertEqual(before, self.workflow_rows())
        queue.claim_next()
        queue.cancel(task['id'])
        done = queue.finish(task['id'], 'failed', {'token': 'synthetic'}, 'Bearer synthetic')
        self.assertEqual((done['state'], done['result'], done['error']),
                         ('cancelled', {'reason': 'cancel_requested'}, ''))
        before = self.workflow_rows()
        self.assertEqual(queue.cancel(task['id']), done)
        with self.assertRaises(Conflict): queue.finish(task['id'], 'succeeded', {'ok': True})
        self.assertEqual(before, self.workflow_rows())

    async def test_workflow_finish_competition_has_one_terminal_winner(self):
        from features.model_coverage.workflow import WorkflowQueue
        queue = WorkflowQueue(self.registry)
        task = queue.enqueue('synthetic', {}, idempotency_key='finish-race')
        queue.claim_next()
        barrier = Barrier(2)
        def finish(state):
            barrier.wait(timeout=5)
            try: return queue.finish(task['id'], state, {'winner': state})
            except Conflict: return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(finish, ['succeeded', 'failed']))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(queue.list()[0], winners[0])
        self.assertEqual(queue.cancel(task['id']), winners[0])

    async def test_workflow_finish_waits_for_committing_cancel_request(self):
        from features.model_coverage.workflow import WorkflowQueue
        queue = WorkflowQueue(self.registry)
        task = queue.enqueue('synthetic', {}, idempotency_key='cancel-commit-race')
        queue.claim_next()
        connected, committed = Event(), Event()
        original_connect = self.registry.connect
        @contextmanager
        def observed_connect():
            with original_connect() as conn:
                class ObservedConnection:
                    def execute(self, sql, *args):
                        if sql == 'BEGIN IMMEDIATE':
                            connected.set()
                        cursor = conn.execute(sql, *args)
                        if sql.startswith('SELECT * FROM eval_workflow_tasks WHERE id='):
                            # Consume the old row before allowing the cancel commit on the baseline.
                            rows = cursor.fetchall()
                            connected.set()
                            if not committed.wait(timeout=5):
                                raise AssertionError('cancel transaction did not commit')
                            class Rows:
                                def fetchone(self): return rows[0] if rows else None
                            return Rows()
                        return cursor
                yield ObservedConnection()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with original_connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('UPDATE eval_workflow_tasks SET cancel_requested=1 WHERE id=?', (task['id'],))
                with patch.object(self.registry, 'connect', observed_connect):
                    future = pool.submit(queue.finish, task['id'], 'succeeded', {'ok': True})
                    self.assertTrue(connected.wait(timeout=5))
            committed.set()
            result = future.result(timeout=5)
        self.assertEqual((result['state'], result['result']), ('cancelled', {'reason': 'cancel_requested'}))

    async def test_workflow_runner_sensitive_failure_is_rejected_without_persistence(self):
        from features.model_coverage.runner import run_one
        from features.model_coverage.workflow import WorkflowQueue
        queue = WorkflowQueue(self.registry)
        task = queue.enqueue('synthetic', {}, idempotency_key='runner-sensitive')
        before_finish = []
        def unsafe_handler(payload, cancel):
            before_finish.extend(self.workflow_rows())
            raise RuntimeError('Bearer synthetic-value')
        with self.assertRaises(RegistryError):
            run_one(queue, unsafe_handler)
        self.assertEqual(before_finish, self.workflow_rows())
        row = queue.list()[0]
        self.assertEqual((row['state'], row['result'], row['error']), ('running', {}, ''))
        self.assertEqual(queue.finish(task['id'], 'failed', {}, 'synthetic failure')['state'], 'failed')
        task2 = queue.enqueue('synthetic', {}, idempotency_key='runner-unsafe-result')
        done = run_one(queue, lambda payload, cancel: {'api_key': 'synthetic-value'})
        self.assertEqual((done['id'], done['state'], done['result']), (task2['id'], 'failed', {}))
        self.assertNotIn('synthetic-value', json.dumps(queue.list()))

    async def test_workflow_finish_rejects_sensitive_json_key_collisions(self):
        from features.model_coverage.workflow import WorkflowQueue
        queue = WorkflowQueue(self.registry)
        collisions = [
            {1: 'Bearer synthetic-value', '1': 'safe'},
            {1: {'api_key': 'synthetic-value'}, '1': 'safe'},
            {'nested': {1: 'Bearer synthetic-value', '1': 'safe'}},
            {'nested': {1: {'token': 'synthetic-value'}, '1': {}}},
            {True: 'password=synthetic-value', 'true': 'safe'},
            {None: 'Basic synthetic-value', 'null': 'safe'},
        ]
        for index, result in enumerate(collisions):
            with self.subTest(case=index):
                task = queue.enqueue('synthetic', {}, idempotency_key=f'key-collision-{index}')
                queue.claim_next()
                before = self.workflow_rows()
                with self.assertRaises(RegistryError): queue.finish(task['id'], 'succeeded', result)
                self.assertEqual(before, self.workflow_rows())
                self.assertEqual(queue.finish(task['id'], 'succeeded', {'ok': True})['state'], 'succeeded')

if __name__=='__main__': unittest.main()
