"""Independent synthetic contract fixtures; no supplier credentials or business data."""
import asyncio
from contextlib import closing
from datetime import date
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from features.protocol_admission.api import create_app
from features.protocol_admission.catalog import probes, request_body
from features.protocol_admission.engine import analyze_json, endpoint, execute, StreamState
from features.protocol_admission.models import PlanInput, StartInput
from features.protocol_admission.report import recommendation
from features.protocol_admission.service import Manager
from features.protocol_admission.storage import Store
from shared.channel_protocol import ProtocolProfile
from shared.registry import Registry


def plan(**changes):
    value = {"base_url": "https://synthetic.invalid/v1", "models": [{"model": "synthetic-model"}],
             "groups": [{"name": "search", "template": "codex_search"}],
             "profile": {"upstream_type": "newapi", "proposed_type": "newapi", "confirmation_source": "supplier", "confirmed_on": "2026-01-01"}}
    value.update(changes)
    return PlanInput.model_validate(value)


def probe(check):
    p = plan(groups=[{"name": name, "template": name} for name in ["codex_search", "openai_common", "claude"]])
    return next(v for v in probes(p) if v.check == check)


def response_body():
    return {"object": "response", "id": "fixture-response", "status": "completed", "model": "synthetic-model",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Synthetic ready"}]}],
            "usage": {"input_tokens": 11, "output_tokens": 3}}


def wire(events):
    return "".join("data: " + (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)) + "\n\n" for v in events).encode()


class ProtocolContractTests(unittest.TestCase):
    def test_template_union_mapping_and_request_counts(self):
        p = plan(models=[{"model": "public", "upstream_model": "upstream"}, {"model": "second"}],
                 groups=[{"name": "standard", "template": "codex_standard"}, {"name": "search", "template": "codex_search"}])
        values = probes(p)
        self.assertEqual(len(values), 8)
        self.assertEqual({v.upstream_model for v in values}, {"upstream", "second"})
        search = request_body(next(v for v in values if v.check == "alpha_search"), "session-fixture")
        self.assertEqual(search["id"], "session-fixture")
        self.assertEqual(search["commands"]["response_length"], "short")
        self.assertNotIn("stream", search)
        self.assertNotIn("messages", search)
        self.assertEqual(endpoint("https://example.invalid/v1/", "/alpha/search"), "https://example.invalid/v1/alpha/search")

    def test_endpoint_and_input_validation(self):
        for base in ["file:///tmp/x", "https://synthetic-user@example.invalid", "https://example.invalid?token=x", "https://example.invalid/v1/responses"]:
            with self.assertRaises(ValueError):
                endpoint(base, "/responses")
        with self.assertRaises(ValueError):
            plan(models=[{"model": "x"}, {"model": "x"}])
        with self.assertRaises(ValueError):
            ProtocolProfile(description="Authorization: Bearer synthetic-secret")

    def test_alpha_optional_fields_and_semantics(self):
        p = probe("alpha_search")
        output = "RFC 9110: https://www.rfc-editor.org/rfc/rfc9110.html"
        for body in [{"output": output}, {"output": output, "results": []}, {"output": output, "results": None, "encrypted_output": None},
                     {"output": output, "results": [17, {"future": True}], "extra": "accepted"}]:
            self.assertEqual(analyze_json(body, p)["result_status"], "passed")
        for body in [{}, {"output": []}, {"output": "x", "results": {}}, {"output": "x", "encrypted_output": 2}]:
            self.assertEqual(analyze_json(body, p)["schema_status"], "failed")
        for output in ["", "This is just a nonempty string", "No results"]:
            r = analyze_json({"output": output}, p)
            self.assertEqual((r["schema_status"], r["result_status"]), ("passed", "unconfirmed"))
        self.assertEqual(analyze_json({"output": output, "error": {"message": "synthetic error"}}, p)["result_status"], "failed")

    def test_usage_truncation_and_tool_contract(self):
        body = response_body()
        self.assertEqual(analyze_json(body, probe("responses_json"))["result_status"], "passed")
        body["status"] = "incomplete"
        r = analyze_json(body, probe("responses_json"))
        self.assertTrue(r["protocol_completed"])
        self.assertEqual(r["error_class"], "generation_incomplete")
        body["status"] = "completed"
        body["usage"]["output_tokens"] = True
        self.assertEqual(analyze_json(body, probe("responses_json"))["error_class"], "usage_missing")
        body = response_body()
        body["output"] = [{"type": "function_call", "call_id": "call-fixture", "name": "protocol_probe", "arguments": '{"marker":"ready"}'}]
        self.assertEqual(analyze_json(body, probe("responses_tool"))["result_status"], "passed")
        body["output"][0]["call_id"] = ""
        self.assertEqual(analyze_json(body, probe("responses_tool"))["error_class"], "invalid_tool_call")

    def test_responses_stream_fragmentation_and_terminal_integrity(self):
        events = [{"type": "response.output_text.delta", "delta": "合成"}, {"type": "response.completed", "response": response_body()}]
        state = StreamState(probe("responses_stream"))
        for i, byte in enumerate(wire(events)):
            state.feed(bytes([byte]), i)
        self.assertEqual(state.analyze()["result_status"], "passed")
        self.assertIsNotNone(state.ttft)
        for bad in [events[:-1], events + ["[DONE]"], events + [events[-1]], [{"type": "response.completed", "response": {**response_body(), "status": "failed"}}]]:
            state = StreamState(probe("responses_stream")); state.feed(wire(bad), 1)
            self.assertEqual(state.analyze()["result_status"], "failed")

    def test_chat_requires_finish_done_and_usage(self):
        chunks = [{"choices": [{"index": 0, "delta": {"content": "READY"}, "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}}, "[DONE]"]
        state = StreamState(probe("chat_stream")); state.feed(wire(chunks), 3)
        self.assertEqual(state.analyze()["result_status"], "passed")
        for events in [chunks[:-1], chunks[1:], [chunks[0], "[DONE]"], chunks + [chunks[0]]]:
            state = StreamState(probe("chat_stream")); state.feed(wire(events), 3)
            self.assertNotEqual(state.analyze()["result_status"], "passed")

    def test_claude_end_signal_and_tool(self):
        body = {"type": "message", "model": "synthetic-model", "content": [{"type": "tool_use", "id": "tool-fixture", "name": "protocol_probe", "input": {"marker": "ready"}}],
                "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}}
        self.assertEqual(analyze_json(body, probe("messages_tool"))["result_status"], "passed")
        events = [{"type": "message_start", "message": {**body, "content": [], "stop_reason": None}},
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "READY"}},
                  {"type": "content_block_stop", "index": 0}, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}}, {"type": "message_stop"}]
        state = StreamState(probe("messages_stream")); state.feed(wire(events), 3)
        self.assertEqual(state.analyze()["result_status"], "passed")
        state = StreamState(probe("messages_stream")); state.feed(wire(events[:3] + events[4:]), 3)
        self.assertEqual(state.analyze()["result_status"], "failed")

    def test_program_identity_not_inferred(self):
        profile = ProtocolProfile(upstream_type="codex", proposed_type="codex", confirmation_source="supplier", confirmed_on=date(2026, 1, 1)).model_dump(mode="json")
        self.assertEqual(recommendation(profile)[0], "unknown")
        profile["credential_mode"] = "codex_oauth"
        self.assertEqual(recommendation(profile)[0], "codex")
        profile.update(upstream_type="newapi", confirmation_source="observation")
        self.assertEqual(recommendation(profile)[0], "unknown")


class ProtocolExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="protocol-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.registry = Registry(Path(self.temp.name) / "registry")
        self.store = Store(Path(self.temp.name) / "reports")

    async def test_http_error_single_attempt_and_no_provider_body(self):
        for status, expected in [(401, "authentication_failed"), (404, "endpoint_unconfirmed"), (429, "rate_limited"), (524, "upstream_timeout")]:
            calls = []
            def respond(request):
                calls.append(request)
                return httpx.Response(status, json={"error": {"message": "synthetic-private-credential"}})
            r = await execute(probe("alpha_search"), "https://synthetic.invalid", "synthetic-private-credential", plan(), "session", httpx.MockTransport(respond))
            self.assertEqual(len(calls), 1)
            self.assertEqual(r["error_class"], expected)
            self.assertNotIn("synthetic-private-credential", json.dumps(r))
            self.assertIsNone(r["gateway_retries"])

    async def test_explicit_unsupported_and_bad_success_body(self):
        cases = [(404, {"error": {"code": "endpoint_unsupported"}}, "upstream_protocol_unsupported"),
                 (200, {"choices": []}, "invalid_schema"),
                 (500, {"error": {"message": "channel does not support /v1/alpha/search"}}, "local_protocol_unsupported")]
        for status, body, expected in cases:
            with self.subTest(status=status, expected=expected):
                result = await execute(probe("alpha_search"), "https://synthetic.invalid", "synthetic-key", plan(), "session",
                                       httpx.MockTransport(lambda request: httpx.Response(status, json=body)))
                self.assertEqual(result["error_class"], expected)
                self.assertNotEqual(result["status"], "passed")

    async def test_channel_changes_mark_saved_report_stale(self):
        channel = self.registry.save({"base_url": "https://synthetic.invalid", "api_key": "synthetic-saved-key", "multiplier": 1})
        p = plan(channel_id=channel["id"])
        manager = Manager(self.store, self.registry)
        async with manager.lifespan():
            run = await manager.start(StartInput(**p.model_dump(), preview_fingerprint=manager.preview(p)["fingerprint"]))
            await manager.task
        self.assertEqual(manager.report(run["id"])["freshness"], "current")
        self.registry.save({"base_url": "https://changed.invalid", "api_key": "", "multiplier": 1}, channel["id"], channel["version"])
        stale = manager.report(run["id"])
        self.assertEqual(stale["freshness"], "changed")
        self.assertTrue(any("重新" in value for value in stale["conclusion"]["warnings"]))

    async def test_timeout_and_body_limit(self):
        class Delayed(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.sleep(.4)
                yield b'{}'
        p = plan(first_byte_timeout=.1, idle_timeout=.1)
        result = await execute(probe("alpha_search"), p.base_url, "synthetic-key", p, "session", httpx.MockTransport(lambda _: httpx.Response(200, stream=Delayed())))
        self.assertEqual(result["error_class"], "first_byte_timeout")
        result = await execute(probe("alpha_search"), p.base_url, "synthetic-key", p, "session", httpx.MockTransport(lambda _: httpx.Response(200, content=b'x' * 1_048_577)))
        self.assertEqual(result["error_class"], "body_too_large")

    async def test_real_socket_transport_and_redirect_refusal(self):
        seen = []
        async def server(reader, writer):
            try:
                header = await reader.readuntil(b'\r\n\r\n')
                length = next(int(line.split(b':')[1]) for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:'))
                body = json.loads(await reader.readexactly(length))
                seen.append(body)
                data = json.dumps({"output": "RFC 9110 https://www.rfc-editor.org/rfc/rfc9110.html"}).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(data)).encode() + b'\r\n\r\n' + data)
                await writer.drain()
            finally:
                writer.close(); await writer.wait_closed()
        server_instance = await asyncio.start_server(server, '127.0.0.1', 0)
        self.addAsyncCleanup(server_instance.wait_closed)
        self.addCleanup(server_instance.close)
        base = 'http://127.0.0.1:' + str(server_instance.sockets[0].getsockname()[1])
        with patch.dict(os.environ, {"PLATFORM_EGRESS_ALLOWLIST": "127.0.0.1"}):
            r = await execute(probe("alpha_search"), base, "synthetic-key", plan(), "session", None)
        self.assertEqual(r["status"], "passed")
        self.assertEqual(len(seen), 1)
        seen.clear()
        with patch.dict(os.environ, {"PLATFORM_EGRESS_ALLOWLIST": ""}):
            r = await execute(probe("alpha_search"), base, "synthetic-key", plan(), "session", None)
        self.assertEqual(r["error_class"], "connection_error")
        self.assertEqual(seen, [])
        calls = []
        def redirect(request):
            calls.append(request)
            return httpx.Response(302, headers={"location": "https://never-contact.invalid"})
        await execute(probe("alpha_search"), base, "synthetic-key", plan(), "session", httpx.MockTransport(redirect))
        self.assertEqual(len(calls), 1)

    async def test_all_templates_mock_reports_and_no_production_release(self):
        manager = Manager(self.store, self.registry)
        p = plan(groups=[{"name": name, "template": name} for name in ["codex_standard", "codex_search", "openai_common", "claude"]])
        preview = manager.preview(p)
        self.assertEqual(preview["request_count"], 10)
        async with manager.lifespan():
            run = await manager.start(StartInput(**p.model_dump(), preview_fingerprint=preview["fingerprint"]))
            await manager.task
        report = self.store.get(run["id"])
        self.assertEqual(report["state"], "completed")
        self.assertTrue(all(row["status"] == "passed" for row in report["probes"]))
        self.assertFalse(report["conclusion"]["production_ready"])
        self.assertEqual({g["state"] for g in report["conclusion"]["groups"]}, {"internal_only"})
        self.assertNotIn("api_key", report["config"])

    async def test_openai_configuration_blocks_search_despite_http_success(self):
        p = plan(profile={"upstream_type": "openai", "proposed_type": "openai", "confirmation_source": "supplier", "confirmed_on": "2026-01-01"})
        manager = Manager(self.store, self.registry)
        async with manager.lifespan():
            run = await manager.start(StartInput(**p.model_dump(), preview_fingerprint=manager.preview(p)["fingerprint"]))
            await manager.task
        self.assertEqual(self.store.get(run["id"])["conclusion"]["groups"][0]["state"], "blocked")

    async def test_preview_change_and_live_confirmation(self):
        p = plan()
        manager = Manager(self.store, self.registry)
        f = manager.preview(p)["fingerprint"]
        with self.assertRaises(ValueError):
            await manager.start(StartInput(**p.model_dump(), mode="live", api_key="synthetic-key", preview_fingerprint=f))
        altered = p.model_dump(); altered["models"] = [{"model": "changed"}]
        with self.assertRaises(ValueError):
            await manager.start(StartInput(**altered, preview_fingerprint=f))
        self.assertEqual(self.store.list(), [])

    async def test_cancel_and_recovery(self):
        entered = asyncio.Event()
        async def delayed(request):
            entered.set()
            await asyncio.Event().wait()
        manager = Manager(self.store, self.registry, lambda: httpx.MockTransport(delayed))
        p = plan()
        async with manager.lifespan():
            run = await manager.start(StartInput(**p.model_dump(), mode="live", confirm_live=True, api_key="synthetic-temporary-key", preview_fingerprint=manager.preview(p)["fingerprint"]))
            await asyncio.wait_for(entered.wait(), 1)
            with self.assertRaises(ValueError):
                await manager.start(StartInput(**p.model_dump(), preview_fingerprint=manager.preview(p)["fingerprint"]))
            await manager.stop(run["id"])
        report = self.store.get(run["id"])
        self.assertEqual(report["state"], "cancelled")
        self.assertEqual(sum(r["attempts"] for r in report["probes"]), 1)
        self.assertNotIn("synthetic-temporary-key", self.store.path.read_bytes().decode(errors="ignore"))
        report["state"] = "running"; self.store.save(report)
        async with Manager(self.store, self.registry).lifespan():
            self.assertEqual(self.store.get(run["id"])["state"], "interrupted")


class ProtocolAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="protocol-api-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.registry = Registry(self.directory / "registry")

    def test_channel_profile_roundtrip_and_legacy_update_preservation(self):
        body = {"base_url": "https://synthetic.invalid", "api_key": "synthetic-channel-key", "multiplier": 1,
                "protocol_profile": {"upstream_type": "newapi", "supplier": "Synthetic supplier"}}
        saved = self.registry.save(body)
        self.assertEqual(saved["protocol_profile"]["upstream_type"], "newapi")
        self.assertNotIn(body["api_key"], json.dumps(saved))
        updated = self.registry.save({"base_url": body["base_url"], "api_key": "", "multiplier": 1}, saved["id"], saved["version"])
        self.assertEqual(updated["protocol_profile"], saved["protocol_profile"])
        body["protocol_profile"]["note"] = body["api_key"]
        with self.assertRaises(ValueError):
            self.registry.save(body, updated["id"], updated["version"])

    def test_existing_database_migration(self):
        saved = self.registry.save({"base_url": "https://legacy.invalid", "api_key": "synthetic-legacy-key", "multiplier": .7})
        with closing(sqlite3.connect(self.registry.path)) as conn, conn:
            conn.execute("ALTER TABLE channels DROP COLUMN protocol_profile")
        migrated = Registry(self.registry.directory)
        result = migrated.get(saved["id"], secret=True)
        self.assertEqual(result["api_key"], "synthetic-legacy-key")
        self.assertEqual(result["protocol_profile"]["upstream_type"], "unknown")

    def test_readonly_inspector_does_not_decrypt_or_mutate(self):
        from scripts.inspect_protocol_admission import inspect
        import hashlib
        self.registry.save({"base_url": "https://synthetic.invalid", "api_key": "synthetic-inspector-key", "multiplier": 1})
        before = hashlib.sha256(self.registry.path.read_bytes()).hexdigest()
        with patch.object(self.registry, "encrypt", side_effect=AssertionError("must not encrypt")):
            value = inspect(self.registry.directory)
        self.assertEqual(before, hashlib.sha256(self.registry.path.read_bytes()).hexdigest())
        self.assertEqual(value["requests_sent"], 0)
        self.assertFalse(value["secrets_read"])
        self.assertNotIn("synthetic-inspector-key", json.dumps(value))
        self.assertNotIn("synthetic.invalid", json.dumps(value))

    def test_api_error_redaction_and_html_json_equivalence(self):
        app = create_app(self.directory / "reports", self.registry)
        with TestClient(app) as client:
            invalid = client.post('/api/runs', json={"api_key": "synthetic-hidden-key", "models": "wrong"})
            self.assertEqual(invalid.status_code, 422)
            self.assertNotIn("synthetic-hidden-key", invalid.text)
            self.assertEqual(client.post('/api/preview', content=b'x' * 65_537).status_code, 413)
            p = plan().model_dump(mode="json")
            preview = client.post('/api/preview', json=p).json()
            started = client.post('/api/runs', json={**p, "preview_fingerprint": preview["fingerprint"]}).json()
            import time
            for _ in range(100):
                report = client.get('/api/runs/' + started["id"]).json()
                if report["state"] != "running": break
                time.sleep(.01)
            self.assertEqual(report["state"], "completed")
            exported = client.get('/api/runs/' + report["id"] + '/export/json').json()
            html = client.get('/api/runs/' + report["id"] + '/export/html').text
            self.assertEqual(report, exported)
            self.assertIn('仅允许内部测试', html)
            self.assertIn('production_ready', html)
            self.assertEqual(client.get('/api/runs/' + report["id"] + '/export/xml').status_code, 404)


if __name__ == '__main__':
    unittest.main()
