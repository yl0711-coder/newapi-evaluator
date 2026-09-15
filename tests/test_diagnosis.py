import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from features.diagnosis.api import create_app
from features.diagnosis.inspect import inspect
from features.diagnosis.mock import LocalMock
from features.diagnosis.models import CaseInput, PlanInput, StartInput, Target, make_plan
from features.diagnosis.report import report, markdown
from features.diagnosis.service import Manager
from features.diagnosis.storage import Store
from features.diagnosis.synthetic import text_for_tokens
from features.diagnosis.transport import StreamEvidence, measure
from shared.registry import Registry


class SchemaTests(unittest.TestCase):
    def test_unknown_split_remains_unknown_and_plan_is_explicit(self):
        case = {**CaseInput(total_tokens=1200, stream=True).model_dump(), "id": "a" * 32}
        body = PlanInput(case_id=case['id'], input_tokens=1000, output_tokens=200, repetitions=2)
        plan = make_plan(body, case, {"mode":"mock"})
        self.assertIsNone(plan['case']['input_tokens'])
        self.assertEqual(plan['request_count'], 8)
        self.assertEqual(plan['estimated_tokens'], 8400)
        self.assertEqual([r['variant'] for r in plan['attempts']][:4], ['baseline','smaller_input','smaller_output','toggle_stream'])
        self.assertEqual(len(text_for_tokens(1000, 17)),4000)
        self.assertEqual(text_for_tokens(500,17),text_for_tokens(1000,17)[:2000])

    def test_rejects_unknown_fields_and_ambiguous_types(self):
        for data in [{"total_tokens":True},{"total_tokens":"40"},{"total_tokens":-1},
                     {"total_tokens":10,"input_tokens":11},{"total_tokens":12,"input_tokens":10,"output_tokens":1},
                     {"total_tokens":10,"prompt":"secret"},{"latency_ms":float('nan'),"total_tokens":1}]:
            with self.subTest(data=list(data)):
                with self.assertRaises(ValidationError): CaseInput(stream=True,**data)
        with self.assertRaises(ValidationError): CaseInput(stream=True)
        with self.assertRaises(ValidationError): Target(mode='live')
        with self.assertRaises(ValueError): make_plan(PlanInput(case_id='a'*32,max_estimated_tokens=1),CaseInput(total_tokens=1,stream=True).model_dump(),{})

    def test_protocol_evidence_terminators_usage_refusal(self):
        e=StreamEvidence('openai'); e.event(json.dumps({'choices':[{'delta':{'content':'hello'},'finish_reason':'stop'}]}))
        self.assertEqual(e.outcome(),'incomplete_response')
        e.event('[DONE]'); self.assertEqual(e.outcome(),'completed'); self.assertIsNone(e.output_tokens)
        e=StreamEvidence('anthropic');e.usage({'input_tokens':12,'output_tokens':2});e.usage({'output_tokens':3})
        self.assertEqual((e.input_tokens,e.output_tokens),(12,3))
        e.nonstream({'content':[{'type':'text','text':'answer'}],'stop_reason':'max_tokens'})
        self.assertEqual(e.outcome(),'output_limit')
        e=StreamEvidence('responses');e.nonstream({'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'no'}]}]})
        self.assertEqual(e.outcome(),'refused')


class DiagnosisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.registry=Registry(self.root/'registry');self.store=Store(self.root/'diagnosis')
        self.case=self.store.import_cases([CaseInput(total_tokens=1400,stream=True,latency_ms=1000,latency_kind='unknown')])[0]

    async def asyncTearDown(self):
        self.temp.cleanup()

    def plan(self,**values):
        return PlanInput(case_id=self.case['id'],**values)

    async def test_actual_http_all_protocols_streaming_and_nonstream(self):
        for protocol in ('openai','responses','anthropic'):
            for stream in (True,False):
                with self.subTest(protocol=protocol,stream=stream):
                    async with LocalMock() as mock, httpx.AsyncClient(timeout=None,trust_env=False) as client:
                        metrics=await measure(client,mock.url,'',Target(protocol=protocol).model_dump(),
                                              {'input_tokens':1024,'output_tokens':128,'stream':stream,'seed':1},self.plan().model_dump())
                    self.assertEqual(metrics['outcome'],'completed')
                    self.assertTrue(metrics['transport_complete']);self.assertEqual(metrics['http_status'],200)
                    self.assertEqual(metrics['usage_input_tokens'],1024);self.assertEqual(metrics['usage_source'],'mock')
                    self.assertGreater(metrics['ttft_ms'],0);self.assertGreater(metrics['output_chars'],0)
                    self.assertNotIn('Synthetic',json.dumps(metrics))

    async def test_faults_missing_usage_and_single_factor_report(self):
        manager=Manager(self.store,self.registry)
        async with manager.lifespan():
            preview=manager.preview(self.plan(target=Target(mock_scenario='large_input_error'),repetitions=1))
            run=await manager.start(StartInput(preview_id=preview['preview_id']));await manager.task
            result=report(self.store.run(run['id']))
            self.assertEqual([r['outcome'] for r in result['run']['results']],['rate_limited','completed','rate_limited','rate_limited'])
            self.assertIsNone(result['groups'][1]['historical_latency_ratio'])
            self.assertIn('不能',markdown(result))
            self.assertEqual((await manager.start(StartInput(preview_id=preview['preview_id'])))['id'],run['id'])
        for protocol in ('openai','responses','anthropic'):
            for scenario,expected in [('stream_break','incomplete_response'),('missing_usage','completed'),('slow_first','first_content_timeout')]:
                with self.subTest(protocol=protocol,scenario=scenario):
                    async with LocalMock(scenario) as mock, httpx.AsyncClient(timeout=None,trust_env=False) as client:
                        result=await measure(client,mock.url,'',Target(protocol=protocol).model_dump(),{'input_tokens':100,'output_tokens':128,'stream':True,'seed':1},self.plan(first_content_timeout_seconds=.1).model_dump())
                    self.assertEqual(result['outcome'],expected)
                    if scenario=='missing_usage':self.assertIsNone(result['usage_input_tokens']);self.assertIsNone(result['usage_output_tokens'])

    async def test_stop_and_crash_never_replay_unknown_requests(self):
        manager=Manager(self.store,self.registry)
        async with manager.lifespan():
            p=manager.preview(self.plan(target=Target(mock_scenario='slow_first')))
            r=await manager.start(StartInput(preview_id=p['preview_id']))
            await asyncio.wait_for(manager.stop(r['id']),2)
            stopped=self.store.run(r['id']);self.assertEqual(stopped['state'],'stopped')
            self.assertTrue(all(x['outcome'] in ('cancelled','not_sent') for x in stopped['results']))
            p=manager.preview(self.plan());crash=self.store.create_run('b'*32,p['plan'])
            crash['results'][0]['outcome']='running';self.store.update(crash)
        manager=Manager(Store(self.root/'diagnosis'),self.registry)
        async with manager.lifespan():
            crash=self.store.run(crash['id']);self.assertEqual(crash['state'],'interrupted')
            self.assertEqual([x['outcome'] for x in crash['results']],['unknown']+['not_sent']*7)
            self.assertIsNone(manager.task)

    async def test_preview_expiry_concurrency_budget_and_configuration_binding(self):
        manager=Manager(self.store,self.registry,live_enabled=True)
        channel=self.registry.save({'name':'fixture','base_url':'https://fixture.invalid','api_key':'fixture-credential'})
        target=Target(mode='live',channel_id=channel['id'],model='fixture')
        p=manager.preview(self.plan(target=target))
        with self.assertRaises(ValueError):await manager.start(StartInput(preview_id=p['preview_id']))
        self.registry.save({**channel,'enabled':False},channel['id'],channel['version'])
        with self.assertRaises(ValueError):await manager.start(StartInput(preview_id=p['preview_id'],confirm_live=True))
        p=manager.preview(self.plan());manager.previews[p['preview_id']]=(0,*manager.previews[p['preview_id']][1:])
        with self.assertRaises(ValueError):await manager.start(StartInput(preview_id=p['preview_id']))
        async with manager.lifespan():
            p=manager.preview(self.plan(target=Target(mock_scenario='slow_first'),max_duration_seconds=1,repetitions=5))
            r=await manager.start(StartInput(preview_id=p['preview_id']))
            p2=manager.preview(self.plan())
            with self.assertRaises(ValueError):await manager.start(StartInput(preview_id=p2['preview_id']))
            await asyncio.wait_for(manager.task,3)
            result=self.store.run(r['id']);self.assertEqual(result['stop_reason'],'duration_budget')
            self.assertIn('not_sent',[x['outcome'] for x in result['results']])

    async def test_api_import_export_validation_and_metric_only_storage(self):
        app=create_app(self.root/'api',self.registry)
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            bad=await client.post('/api/cases',json={'cases':[{'total_tokens':3,'stream':True,'raw_key':'never-persist-this'}]})
            self.assertEqual(bad.status_code,422);self.assertNotIn('never-persist',bad.text)
            self.assertEqual((await client.post('/api/cases',content=b'x'*262145)).status_code,413)
            case=(await client.post('/api/cases',json={'cases':[{'total_tokens':1500,'stream':True}]})).json()[0]
            self.assertIsNone(case['input_tokens'])
            denied=await client.post('/api/preview',json={'case_id':case['id'],'target':{'mode':'live','channel_id':1}})
            self.assertEqual(denied.status_code,400)
            p=(await client.post('/api/preview',json={'case_id':case['id'],'repetitions':1})).json()
            r=(await client.post('/api/runs',json={'preview_id':p['preview_id']})).json()
            self.assertEqual((await client.delete('/api/runs/'+r['id'])).status_code,409)
            await app.state.manager.task
            for fmt in ('md','json'):
                response=await client.get(f"/api/runs/{r['id']}/export/{fmt}")
                self.assertEqual(response.status_code,200);self.assertNotIn('Synthetic observation',response.text)
            await client.delete('/api/cases/'+case['id'])
            self.assertEqual((await client.get('/api/runs/'+r['id'])).json()['run']['plan']['case']['id'],case['id'])
            await client.delete('/api/runs/'+r['id']);self.assertEqual((await client.get('/api/runs/'+r['id'])).status_code,404)
        self.assertNotIn(b'never-persist-this',(self.root/'api'/'diagnosis.db').read_bytes())

    async def test_read_only_inspect_no_secrets_and_no_database_creation(self):
        missing=self.root/'absent';value=inspect(missing);self.assertEqual(value['requests_sent'],0);self.assertFalse(missing.exists())
        channel=self.registry.save({'name':'sensitive alias','base_url':'https://private-target.invalid/v1','api_key':'fixture-credential'})
        before=self.registry.path.read_bytes();value=inspect(self.registry.directory,channel['id'])
        text=json.dumps(value);self.assertNotIn('private-target',text);self.assertNotIn('credential',text);self.assertNotIn('sensitive alias',text)
        self.assertEqual(self.registry.path.read_bytes(),before)

    async def test_live_uses_guarded_transport_and_denies_loopback(self):
        manager=Manager(self.store,self.registry,live_enabled=True)
        channel=self.registry.save({'name':'loopback fixture','base_url':'http://127.0.0.1:9','api_key':'fixture-credential'})
        with patch.dict('os.environ',{'PLATFORM_EGRESS_ALLOWLIST':''}):
            async with manager.lifespan():
                p=manager.preview(self.plan(target=Target(mode='live',channel_id=channel['id']),variants=[],repetitions=1))
                r=await manager.start(StartInput(preview_id=p['preview_id'],confirm_live=True));await manager.task
                result=self.store.run(r['id']);self.assertFalse(result['results'][0].get('request_ok'))
                self.assertNotIn('fixture-credential',json.dumps(result))


if __name__=='__main__':unittest.main()
