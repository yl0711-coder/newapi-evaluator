"""Synthetic transport evidence: usage provenance, safe metadata and egress gates."""
import json
import socket
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from features.stability.app import egress, responses, transport
from shared import network


PROTOCOLS = ("openai", "anthropic", "responses")
MODES = (False, True)
FIXTURE_KEY = "fixture-" + "credential-material"
MODEL = "fixture-model-v1"


def usage_for(protocol, input_count, output_count):
    names = ("prompt_tokens", "completion_tokens") if protocol == "openai" else ("input_tokens", "output_tokens")
    return {name: value for name, value in zip(names, (input_count, output_count)) if value is not None}


def upstream_response(protocol, streaming, model=MODEL, usage=None):
    usage = usage or {}
    if protocol == "openai":
        data = {"model": model, "usage": usage,
                "choices": [{"message": {"content": "fixture answer"}, "finish_reason": "stop"}]}
        events = [{"model": model, "choices": [{"delta": {"content": "fixture "}}]},
                  {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}], "usage": usage}]
    elif protocol == "anthropic":
        data = {"model": model, "usage": usage, "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "fixture answer"}]}
        events = [{"type": "message_start", "message": {"model": model,
                   "usage": {key: value for key, value in usage.items() if key == "input_tokens"}}},
                  {"type": "content_block_delta", "delta": {"text": "fixture "}},
                  {"type": "content_block_delta", "delta": {"text": "answer"}},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                   "usage": {key: value for key, value in usage.items() if key == "output_tokens"}},
                  {"type": "message_stop"}]
    else:
        data = {"status": "completed", "model": model, "usage": usage,
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "fixture answer"}]}]}
        events = [{"type": "response.output_text.delta", "delta": "fixture "},
                  {"type": "response.output_text.delta", "delta": "answer"},
                  {"type": "response.completed", "response": data}]
    if not streaming:
        return httpx.Response(200, json=data)
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return httpx.Response(200, text=body + ("data: [DONE]\n\n" if protocol == "openai" else ""))


def channel_for(protocol):
    return {"protocol": protocol, "base_url": "https://fixture.example", "model": MODEL, "api_key": FIXTURE_KEY}


def probe_for(streaming):
    return {"id": "fixture", "name": "Fixture", "stream": streaming, "prompt": "Independent fixture", "max_tokens": 64}


class PermitDenied(RuntimeError):
    pass


class TransportEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def run_fixture(self, protocol, streaming, *, model=MODEL, usage=None):
        adapter = responses if protocol == "responses" else transport
        with patch.object(adapter, "validate_url", new=AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda _: upstream_response(protocol, streaming, model, usage))) as client:
                return await transport.run_probe(client, channel_for(protocol), probe_for(streaming))

    async def test_metadata_rejects_current_key_url_controls_and_credentials(self):
        metadata = (FIXTURE_KEY, "prefix-" + FIXTURE_KEY, "https://fixture.example/private?credential=value",
                    "fixture\nmodel", "sk-" + "x" * 24, "Bearer fixture-access-token", "api_key=fixture")
        for protocol in PROTOCOLS:
            for streaming in MODES:
                for index, model in enumerate(metadata):
                    with self.subTest(protocol=protocol, streaming=streaming, metadata_case=index):
                        result = await self.run_fixture(protocol, streaming, model=model)
                        self.assertTrue(result["actual_model"] == "unrecognized", "unsafe model metadata survived")
                        self.assertTrue(FIXTURE_KEY not in json.dumps(result), "credential survived transport metadata")
                        self.assertFalse(result["model_mismatch"])

    async def test_safe_models_keep_normal_mismatch_semantics(self):
        for protocol in PROTOCOLS:
            for streaming in MODES:
                for model, mismatch in ((MODEL, False), ("different-fixture-model", True)):
                    with self.subTest(protocol=protocol, streaming=streaming, mismatch=mismatch):
                        result = await self.run_fixture(protocol, streaming, model=model)
                        self.assertEqual(result["actual_model"], model)
                        self.assertEqual(result["model_mismatch"], mismatch)

    async def test_usage_missing_partial_zero_and_normal_remain_separate_from_estimates(self):
        for protocol in PROTOCOLS:
            for streaming in MODES:
                for input_count, output_count in ((None, None), (7, None), (None, 9), (0, 0), (7, 9)):
                    with self.subTest(protocol=protocol, streaming=streaming, input_count=input_count, output_count=output_count):
                        result = await self.run_fixture(protocol, streaming, usage=usage_for(protocol, input_count, output_count))
                        self.assertIn("input_tokens_reported", result)
                        self.assertIn("output_tokens_reported", result)
                        self.assertIn("output_tokens_estimated", result)
                        self.assertEqual(result["input_tokens_reported"], input_count)
                        self.assertEqual(result["output_tokens_reported"], output_count)
                        self.assertEqual(result["usage_complete"], input_count is not None and output_count is not None)
                        estimated = streaming and protocol != "responses" and output_count is None
                        self.assertEqual(result["output_tokens_estimated"] is not None, estimated)
                        if output_count is not None:
                            self.assertEqual(result["output_tokens"], output_count)
                            self.assertIsNone(result["output_tokens_estimated"])

    async def test_bool_negative_and_noninteger_usage_are_unknown(self):
        for protocol in PROTOCOLS:
            for streaming in MODES:
                for invalid in (True, False, -1, "7", 7.5):
                    with self.subTest(protocol=protocol, streaming=streaming, invalid_type=type(invalid).__name__):
                        result = await self.run_fixture(protocol, streaming, usage=usage_for(protocol, invalid, invalid))
                        self.assertIn("input_tokens_reported", result)
                        self.assertIn("output_tokens_reported", result)
                        self.assertIsNone(result["input_tokens_reported"])
                        self.assertIsNone(result["output_tokens_reported"])
                        self.assertFalse(result["usage_complete"])

    async def test_final_permit_runs_after_preflight_once_and_denial_never_sends(self):
        for protocol in PROTOCOLS:
            for streaming in MODES:
                for denied in (False, True):
                    with self.subTest(protocol=protocol, streaming=streaming, denied=denied):
                        order = []

                        async def preflight(_):
                            order.append("preflight")

                        async def before_send():
                            order.append("permit")
                            if denied:
                                raise PermitDenied("fixture permit rejected")

                        def handler(_):
                            order.append("send")
                            return upstream_response(protocol, streaming)

                        adapter = responses if protocol == "responses" else transport
                        with patch.object(adapter, "validate_url", new=preflight):
                            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                                if denied:
                                    with self.assertRaises(PermitDenied):
                                        await transport.run_probe(client, channel_for(protocol), probe_for(streaming), before_send=before_send)
                                else:
                                    result = await transport.run_probe(client, channel_for(protocol), probe_for(streaming), before_send=before_send)
                                    self.assertTrue(result["ok"])
                        self.assertEqual(order, ["preflight", "permit"] + ([] if denied else ["send"]))

    async def test_dns_rebinding_guard_rejects_with_no_real_socket(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        for protocol in PROTOCOLS:
            for streaming in MODES:
                with self.subTest(protocol=protocol, streaming=streaming):
                    permit = AsyncMock()
                    with patch.dict("os.environ", {"PLATFORM_EGRESS_ALLOWLIST": ""}), \
                            patch.object(egress, "EGRESS_ALLOWLIST", ()), \
                            patch.object(socket, "getaddrinfo", side_effect=[public, private]) as dns, \
                            patch.object(socket, "socket", side_effect=AssertionError("unexpected real socket")) as real_socket:
                        async with httpx.AsyncClient(transport=network.guarded_transport(), trust_env=False, follow_redirects=False) as client:
                            result = await transport.run_probe(client, channel_for(protocol), probe_for(streaming), before_send=permit)
                    self.assertEqual(result["status"], "egress_denied")
                    self.assertEqual(dns.call_count, 2)
                    self.assertEqual(real_socket.call_count, 0)
                    self.assertFalse(result["stream_break"])
                    permit.assert_awaited_once_with()

    async def test_policy_denial_and_dns_failure_before_permit_never_send(self):
        for protocol in PROTOCOLS:
            for streaming in MODES:
                for dns_failure in (False, True):
                    with self.subTest(protocol=protocol, streaming=streaming, dns_failure=dns_failure):
                        lookup = socket.gaierror("fixture DNS unavailable") if dns_failure else [
                            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
                        permit = AsyncMock()
                        handler = unittest.mock.Mock(side_effect=AssertionError("unexpected request"))
                        with patch.object(egress, "EGRESS_ALLOWLIST", ()), patch.object(socket, "getaddrinfo", side_effect=lookup if dns_failure else None, return_value=lookup):
                            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                                result = await transport.run_probe(client, channel_for(protocol), probe_for(streaming), before_send=permit)
                        self.assertEqual(result["status"], "network_error" if dns_failure else "egress_denied")
                        permit.assert_not_awaited()
                        handler.assert_not_called()

    async def test_httpx_preserves_typed_guard_cause_without_text_matching(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch.dict("os.environ", {"PLATFORM_EGRESS_ALLOWLIST": ""}), \
                patch.object(socket, "getaddrinfo", return_value=private), \
                patch.object(socket, "socket", side_effect=AssertionError("unexpected real socket")) as real_socket:
            async with httpx.AsyncClient(transport=network.guarded_transport()) as client:
                with self.assertRaises(httpx.ConnectError) as caught:
                    await client.get("https://fixture.example")
        self.assertIsInstance(caught.exception.__cause__, network.SocketEgressDenied)
        self.assertTrue(network.socket_egress_denied(caught.exception))
        self.assertEqual(real_socket.call_count, 0)
        self.assertFalse(network.socket_egress_denied(httpx.ConnectError(str(caught.exception))))
        context_wrapper = RuntimeError("fixture unrelated text")
        context_wrapper.__context__ = network.SocketEgressDenied("fixture different text")
        self.assertTrue(network.socket_egress_denied(context_wrapper))

    async def test_socket_dns_failure_and_connection_timeout_keep_network_classification(self):
        for protocol in PROTOCOLS:
            for streaming in MODES:
                for fault, expected in ((socket.gaierror("fixture DNS unavailable"), "network_error"),
                                        (TimeoutError(), "timeout")):
                    with self.subTest(protocol=protocol, streaming=streaming, status=expected):
                        adapter = responses if protocol == "responses" else transport
                        with patch.object(adapter, "validate_url", new=AsyncMock()), \
                                patch.object(socket, "getaddrinfo", side_effect=fault), \
                                patch.object(socket, "socket", side_effect=AssertionError("unexpected real socket")) as real_socket:
                            async with httpx.AsyncClient(transport=network.guarded_transport()) as client:
                                result = await transport.run_probe(client, channel_for(protocol), probe_for(streaming))
                        self.assertEqual(result["status"], expected)
                        self.assertEqual(real_socket.call_count, 0)

    async def test_real_connection_error_and_upstream_failure_are_not_policy_denials(self):
        for protocol in PROTOCOLS:
            for streaming in MODES:
                for status_code, expected in ((429, "rate_limited"), (500, "upstream_5xx")):
                    with self.subTest(protocol=protocol, streaming=streaming, http_status=status_code):
                        adapter = responses if protocol == "responses" else transport
                        with patch.object(adapter, "validate_url", new=AsyncMock()):
                            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status_code))) as client:
                                result = await transport.run_probe(client, channel_for(protocol), probe_for(streaming))
                        self.assertEqual(result["status"], expected)
                public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
                adapter = responses if protocol == "responses" else transport
                with patch.object(adapter, "validate_url", new=AsyncMock()), \
                        patch.object(socket, "getaddrinfo", return_value=public), \
                        patch.object(network.AutoBackend, "connect_tcp", new=AsyncMock(side_effect=network.httpcore.ConnectError("fixture connection failure"))):
                    async with httpx.AsyncClient(transport=network.guarded_transport()) as client:
                        result = await transport.run_probe(client, channel_for(protocol), probe_for(streaming))
                self.assertEqual(result["status"], "network_error")


if __name__ == "__main__":
    unittest.main()
