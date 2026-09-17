"""Synthetic coverage, discovery and scheduling contracts; no external services."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
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
        self.assertEqual(len(self.catalog.models()),12)
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
        self.assertEqual(len(Catalog(self.registry).models()),13)
        self.assertEqual(len(service.coverage()['channels'][0]['models']),13)
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
                         httpx.Response(200,json={'data':[],'has_more':True}),
                         httpx.Response(302,headers={'location':'https://different.example/v1/models'}),
                         httpx.Response(200,text='malformed')]:
            with self.subTest(response=response.status_code), patch.object(discovery,'guarded_transport',return_value=httpx.MockTransport(lambda _:response)):
                result=await discovery.fetch_models(self.registry,self.channel['id'],'openai')
                self.assertFalse(result['ok'])
                self.assertEqual(self.catalog.discoveries()[self.channel['id']]['models'],[])

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
        self.assertEqual(len(result['models']),12)
        encoded=json.dumps(result)
        self.assertNotIn('synthetic-coverage-credential',encoded)
        self.assertNotIn('synthetic.example',encoded)

    async def test_daily_batches_can_accumulate_and_observation_storage_is_bounded(self):
        target=self.target()
        for days in [2,1,0]: self.observation(target,tested_at=time.time()-days*86400)
        self.assertEqual(self.row()['measurement']['status'],'stable')
        for _ in range(32): self.observation(target)
        self.assertEqual(len(storage.model_observations()),30)

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


if __name__=='__main__': unittest.main()
