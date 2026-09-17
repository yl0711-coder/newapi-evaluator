"""Synthetic Responses contracts for scheduled tests, including actual SSE parsing."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from features.stability.app import transport, responses
from features.stability.app.main import ChannelInput


def completed(text='synthetic answer', **fields):
    return {'type':'response.completed','response':{'status':'completed','model':'gpt-6-astra',
            'output':[{'type':'message','content':[{'type':'output_text','text':text}]}],
            'usage':{'input_tokens':10,'output_tokens':20,'output_tokens_details':{'reasoning_tokens':0}},**fields}}


def sse(events):
    return ''.join('data: '+json.dumps(e)+'\n\n' for e in events)


class DelayedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.Event().wait()
        yield b''


class ResponsesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel={'base_url':'https://synthetic.example/prefix/v1/chat/completions','api_key':'synthetic-response-key',
                      'model':'gpt-6-astra','protocol':'responses'}
        self.probe={'id':'performance-1','name':'Synthetic streaming','prompt':'Synthetic fixture','max_tokens':320,'stream':True}

    async def run_response(self, response, probe=None):
        with patch.object(responses,'validate_url',new=AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:response)) as client:
                return await transport.run_probe(client,self.channel,probe or self.probe)

    async def test_payload_endpoint_headers_and_nonstream_judgment(self):
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(200,json=completed('391')['response'])
        with patch.object(responses,'validate_url',new=AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result=await transport.run_probe(client,self.channel,{**self.probe,'stream':False,'expect':'391'})
        self.assertTrue(result['ok'])
        request=calls[0]; body=json.loads(request.content)
        self.assertEqual(request.url.path,'/prefix/v1/responses')
        self.assertEqual(request.headers['authorization'],'Bearer synthetic-response-key')
        self.assertEqual(body,{'model':'gpt-6-astra','input':'Synthetic fixture','stream':False,'store':False,
                               'max_output_tokens':4096,'reasoning':{'effort':'low'}})
        self.assertEqual(result['output_tokens'],20)
        self.assertIsNone(result['tokens_per_second'])
        self.assertTrue(result['usage_complete'])

    async def test_completed_stream_and_multiline_frames(self):
        events=[{'type':'response.output_text.delta','delta':'synthetic '},
                {'type':'response.output_text.delta','delta':'answer'}, completed()]
        response=httpx.Response(200,text=sse(events))
        result=await self.run_response(response)
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'],'completed')
        self.assertIsNotNone(result['ttft_ms'])
        value=completed()
        # JSON line split is legal only between JSON tokens.
        frame='event: response.completed\ndata: {"type":"response.completed",\ndata: "response":'+json.dumps(value['response'])+'}\n\n'
        self.assertTrue((await self.run_response(httpx.Response(200,text=frame)))['ok'])

    async def test_missing_terminal_done_and_malformed_sse_never_pass(self):
        cases=[sse([{'type':'response.output_text.delta','delta':'answer'}]),
               sse([{'type':'response.output_text.delta','delta':'answer'}])+'data: [DONE]\n\n',
               'data: not-json\n\n'+sse([completed()]),
               'data: '+json.dumps(completed()),
               sse([{'type':'response.completed','response':{'status':'in_progress'}}])]
        for text in cases:
            with self.subTest(text=text[:20]):
                self.assertFalse((await self.run_response(httpx.Response(200,text=text)))['ok'])

    async def test_empty_reasoning_refusal_failed_and_incomplete_do_not_pass(self):
        cases=[completed(''),completed('',output=[{'type':'reasoning','summary':[]}]),
               completed('',output=[{'type':'message','content':[{'type':'refusal','refusal':'synthetic refusal'}]}]),
               {'type':'response.incomplete','response':{'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'}}},
               {'type':'response.failed','response':{'status':'failed','error':{'message':'synthetic secret'}}}]
        for event in cases:
            result=await self.run_response(httpx.Response(200,text=sse([event])))
            self.assertFalse(result['ok'])
            self.assertNotIn('synthetic secret',json.dumps(result))

    async def test_trailing_content_duplicate_terminal_and_mismatched_final_fail(self):
        for events in [[completed(),{'type':'response.output_text.delta','delta':'extra'}],
                       [completed(),completed()],
                       [{'type':'response.output_text.delta','delta':'different'},completed()]]:
            self.assertEqual((await self.run_response(httpx.Response(200,text=sse(events))))['status'],'invalid_response')

    async def test_upstream_failure_has_its_own_classification_without_body(self):
        failed = {'type': 'response.failed', 'response': {'status': 'failed',
                  'error': {'code': 'server_error', 'message': 'synthetic-response-key private-body'}}}
        for streaming in (True, False):
            with self.subTest(streaming=streaming):
                response = httpx.Response(200, text=sse([failed])) if streaming else httpx.Response(200, json=failed['response'])
                result = await self.run_response(response, {**self.probe, 'stream': streaming})
                self.assertEqual(result['status'], 'upstream_error')
                self.assertFalse(result['ok'])
                self.assertFalse(result['stream_break'])
                self.assertNotIn('synthetic-response-key', json.dumps(result))
                self.assertNotIn('private-body', json.dumps(result))
        error_event = {'type': 'error', 'code': 'server_error', 'message': 'private-body'}
        result = await self.run_response(httpx.Response(200, text=sse([error_event])))
        self.assertEqual(result['status'], 'upstream_error')
        self.assertNotIn('private-body', json.dumps(result))

    async def test_protocol_damage_takes_precedence_over_upstream_failure(self):
        failed = {'type': 'response.failed', 'response': {'status': 'failed'}}
        cases = [[failed, failed], [completed(), failed],
                 [{'type': 'response.failed', 'response': {'status': 'completed'}}],
                 [completed(), {'type': 'error', 'code': 'server_error'}]]
        for events in cases:
            with self.subTest(events=[event['type'] for event in events]):
                result = await self.run_response(httpx.Response(200, text=sse(events)))
                self.assertEqual(result['status'], 'invalid_response')

    async def test_completed_only_content_and_reasoning_usage_do_not_invent_speed(self):
        result=await self.run_response(httpx.Response(200,text=sse([completed()])))
        self.assertTrue(result['ok']); self.assertIsNone(result['tokens_per_second'])
        for usage in [{}, {'input_tokens':10,'output_tokens':20},
                      {'input_tokens':10,'output_tokens':20,'output_tokens_details':{'reasoning_tokens':10}}]:
            events=[{'type':'response.output_text.delta','delta':'synthetic '},
                    {'type':'response.output_text.delta','delta':'answer'},completed(usage=usage)]
            result=await self.run_response(httpx.Response(200,text=sse(events)))
            self.assertTrue(result['ok']); self.assertIsNone(result['tokens_per_second'])
            if not usage: self.assertIsNone(result['output_tokens'])

    async def test_content_done_recovery_and_mismatch(self):
        events=[{'type':'response.output_text.delta','delta':'synthetic '},
                {'type':'response.output_text.done','text':'synthetic answer'},completed()]
        self.assertTrue((await self.run_response(httpx.Response(200,text=sse(events))))['ok'])
        events[1]['text']='wrong text'
        self.assertFalse((await self.run_response(httpx.Response(200,text=sse(events))))['ok'])

    async def test_http_failures_are_classified_without_body_or_key(self):
        for code,status in [(401,'auth_error'),(403,'auth_error'),(429,'rate_limited'),(500,'upstream_5xx'),(504,'timeout')]:
            result=await self.run_response(httpx.Response(code,text='synthetic-response-key raw body'))
            self.assertEqual(result['status'],status)
            self.assertNotIn('synthetic-response-key',json.dumps(result))
            self.assertNotIn('raw body',json.dumps(result))

    async def test_timeout_cancellation_and_invalid_json(self):
        with patch.object(responses,'RESPONSES_TOTAL_TIMEOUT_SECONDS',0.02):
            result=await self.run_response(httpx.Response(200,stream=DelayedStream()))
        self.assertEqual(result['status'],'timeout')
        self.assertTrue(result['stream_break'])
        task=asyncio.create_task(self.run_response(httpx.Response(200,stream=DelayedStream())))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        result=await self.run_response(httpx.Response(200,json=[]),{**self.probe,'stream':False})
        self.assertEqual(result['status'],'invalid_response')

    async def test_reported_model_redacts_saved_key(self):
        result=await self.run_response(httpx.Response(200,text=sse([completed(model='synthetic-response-key')])) )
        self.assertNotIn('synthetic-response-key',json.dumps(result))

    async def test_direct_target_submission_uses_responses_for_gpt6(self):
        value=ChannelInput(name='Synthetic',registry_channel_id=1,model='gpt-6-astra',protocol='openai')
        self.assertEqual(value.protocol,'responses')


if __name__=='__main__': unittest.main()
