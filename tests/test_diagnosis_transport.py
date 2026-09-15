import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from features.diagnosis.models import CaseInput, PlanInput, StartInput, Target
from features.diagnosis.report import report
from features.diagnosis.service import Manager
from features.diagnosis.storage import Store
from features.diagnosis.transport import measure
from shared.registry import Registry


@asynccontextmanager
async def wire(status=200, chunks=(), delays=(), content_type='text/event-stream', on_request=None):
    tasks=set(); received=[]
    async def handle(reader,writer):
        task=asyncio.current_task();tasks.add(task)
        try:
            head=await reader.readuntil(b'\r\n\r\n');received.append(head)
            if on_request:on_request()
            writer.write(f'HTTP/1.1 {status} Fixture\r\nContent-Type: {content_type}\r\nConnection: close\r\n\r\n'.encode());await writer.drain()
            for index,chunk in enumerate(chunks):
                if index<len(delays):await asyncio.sleep(delays[index])
                writer.write(chunk);await writer.drain()
        except (ConnectionError,asyncio.IncompleteReadError):pass
        finally:
            writer.close()
            try:await writer.wait_closed()
            except ConnectionError:pass
            tasks.discard(task)
    server=await asyncio.start_server(handle,'127.0.0.1',0)
    try:yield f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}',received
    finally:
        server.close();await server.wait_closed()
        owned=list(tasks)
        for t in owned:t.cancel()
        await asyncio.gather(*owned,return_exceptions=True)


def event(value):return ('data: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode()


def text_event(protocol):
    if protocol=='openai':return event({'choices':[{'delta':{'content':'合成文本'}}]})
    if protocol=='responses':return event({'type':'response.output_text.delta','delta':'合成文本'})
    return event({'type':'content_block_delta','delta':{'type':'text_delta','text':'合成文本'}})


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def run_wire(self,protocol='openai',stream=True,settings=None,**fixture):
        async with wire(**fixture) as (url,received),httpx.AsyncClient(timeout=None,trust_env=False) as client:
            result=await measure(client,url,'',Target(protocol=protocol).model_dump(),
                {'input_tokens':100,'output_tokens':100,'seed':1,'stream':stream},
                {**PlanInput(case_id='a'*32).model_dump(),**(settings or {})})
            self.assertEqual(len(received),1,'diagnostics must not silently retry')
            return result

    async def test_http_error_and_malformed_paths_for_every_protocol_mode(self):
        for protocol in ('openai','responses','anthropic'):
            for stream in (True,False):
                for status,outcome in [(401,'authentication_error'),(403,'permission_error'),(429,'rate_limited'),(503,'upstream_http_error'),(302,'request_rejected')]:
                    with self.subTest(protocol=protocol,stream=stream,status=status):
                        r=await self.run_wire(protocol,stream,status=status,chunks=[b'private body'])
                        self.assertEqual(r['outcome'],outcome);self.assertFalse(r['request_ok']);self.assertNotIn('private body',json.dumps(r))
                r=await self.run_wire(protocol,stream,chunks=[b'data: {invalid}\n\n' if stream else b'not json'])
                self.assertEqual(r['outcome'],'invalid_response')
                r=await self.run_wire(protocol,stream,chunks=[b'{}'],content_type='application/json')
                self.assertFalse(r['request_ok'])

    async def test_stream_idle_first_and_total_deadlines_are_distinct(self):
        for protocol in ('openai','responses','anthropic'):
            with self.subTest(protocol=protocol):
                r=await self.run_wire(protocol,chunks=[text_event(protocol),b': ping\n\n'],delays=[0,.15],settings={'idle_timeout_seconds':.05})
                self.assertEqual(r['outcome'],'idle_timeout');self.assertGreater(r['output_chars'],0);self.assertIsNotNone(r['ttft_ms'])
                r=await self.run_wire(protocol,chunks=[b': ping\n\n',text_event(protocol)],delays=[0,.15],settings={'first_content_timeout_seconds':.05})
                self.assertEqual(r['outcome'],'first_content_timeout');self.assertIsNone(r['ttft_ms'])
                for stream in (True,False):
                    r=await self.run_wire(protocol,stream,chunks=[text_event(protocol)],delays=[.15],settings={'timeout_seconds':.05})
                    self.assertEqual(r['outcome'],'total_timeout')

    async def test_sse_chunk_boundaries_utf8_output_limit_and_terminal_usage(self):
        payload=text_event('openai')+event({'choices':[{'delta':{},'finish_reason':'length'}]})+event({'choices':[],'usage':{'prompt_tokens':15,'completion_tokens':10}})+b'data: [DONE]\n\n'
        r=await self.run_wire(chunks=[payload[i:i+1] for i in range(len(payload))])
        self.assertEqual(r['outcome'],'output_limit');self.assertTrue(r['request_ok'])
        self.assertEqual((r['usage_input_tokens'],r['usage_output_tokens']),(15,10));self.assertEqual(r['output_chars'],4)
        r=await self.run_wire(chunks=[text_event('openai'),event({'error':{'message':'private provider detail'}})])
        self.assertEqual(r['outcome'],'upstream_error');self.assertNotIn('private provider',json.dumps(r))

    async def test_cancel_preserves_partial_metrics(self):
        header_seen=asyncio.Event()
        async def progress(value):
            if value['ttft_ms'] is not None:header_seen.set()
        async with wire(chunks=[text_event('openai'),b''],delays=[0,10]) as (url,_),httpx.AsyncClient(timeout=None,trust_env=False) as client:
            task=asyncio.create_task(measure(client,url,'',Target().model_dump(),{'input_tokens':100,'output_tokens':100,'seed':1,'stream':True},PlanInput(case_id='a'*32).model_dump(),progress))
            await asyncio.wait_for(header_seen.wait(),2);task.cancel();r=await asyncio.wait_for(task,2)
        self.assertEqual(r['outcome'],'cancelled');self.assertEqual(r['http_status'],200);self.assertEqual(r['output_chars'],4);self.assertFalse(r['transport_complete'])

    async def test_reasoning_output_limit_without_visible_text(self):
        payload={'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'},'output':[],
                 'usage':{'input_tokens':20,'output_tokens':100,'output_tokens_details':{'reasoning_tokens':100}}}
        for stream in (True,False):
            content=event({'type':'response.incomplete','response':payload}) if stream else json.dumps(payload).encode()
            row=await self.run_wire('responses',stream,chunks=[content])
            self.assertEqual(row['outcome'],'output_limit');self.assertTrue(row['request_ok'])
            self.assertEqual(row['output_chars'],0);self.assertIsNone(row['ttft_ms']);self.assertEqual(row['usage_reasoning_tokens'],100)
            value=report({'id':'a'*32,'state':'completed','plan':{'case':{'latency_kind':'unknown','latency_ms':None}},'results':[{**row,'variant':'baseline'}]})
            self.assertEqual(value['groups'][0]['outcomes'],{'output_limit':1});self.assertEqual(value['groups'][0]['completed'],0)

    async def test_live_configuration_auth_headers_and_mid_run_change(self):
        terminals={'openai':event({'choices':[{'delta':{},'finish_reason':'stop'}]})+b'data: [DONE]\n\n',
                   'responses':event({'type':'response.completed','response':{'status':'completed'}}),
                   'anthropic':event({'type':'message_delta','delta':{'stop_reason':'end_turn'}})+event({'type':'message_stop'})}
        for protocol,disabled in [('openai',False),('responses',False),('anthropic',False),('openai',True)]:
            with self.subTest(protocol=protocol,disabled=disabled),tempfile.TemporaryDirectory() as directory:
                registry=Registry(Path(directory)/'registry');store=Store(Path(directory)/'diagnosis')
                case=store.import_cases([CaseInput(total_tokens=100,stream=True)])[0]
                def change():registry.save({**channel,'name':'updated fixture','enabled':not disabled},channel['id'],channel['version'])
                async with wire(chunks=[text_event(protocol)+terminals[protocol]],on_request=change) as (url,received):
                    channel=registry.save({'name':'fixture','base_url':url,'api_key':'synthetic-diagnosis-credential'})
                    manager=Manager(store,registry)
                    with patch.dict('os.environ',{'PLATFORM_EGRESS_ALLOWLIST':'127.0.0.1'}):
                        async with manager.lifespan():
                            p=manager.preview(PlanInput(case_id=case['id'],target=Target(mode='live',channel_id=channel['id'],protocol=protocol),repetitions=1))
                            run=await manager.start(StartInput(preview_id=p['preview_id'],confirm_live=True));await manager.task
                    value=store.run(run['id']);self.assertEqual(value['results'][0]['outcome'],'completed')
                    self.assertEqual(value['stop_reason'],'target_changed');self.assertEqual(len(received),1)
                    self.assertEqual([r['outcome'] for r in value['results'][1:]],['not_sent']*3)
                    expected=b'x-api-key: synthetic-diagnosis-credential' if protocol=='anthropic' else b'authorization: Bearer synthetic-diagnosis-credential'
                    self.assertIn(expected,received[0]);self.assertNotIn('synthetic-diagnosis-credential',json.dumps(value))
                    if protocol=='anthropic':self.assertIn(b'anthropic-version: 2023-06-01',received[0])


if __name__=='__main__':unittest.main()
