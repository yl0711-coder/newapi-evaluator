"""Synthetic fixtures verify one-time permits, query recovery and credential boundaries."""
import asyncio
import base64
import io
import json
import logging
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
import httpcore
from PIL import Image

from features.volcengine_admission import catalog
from features.volcengine_admission.api import create_app
from features.volcengine_admission.models import AuthInput, CleanupInput, PreviewInput, ResumeInput, RunInput
from features.volcengine_admission.media import MediaError, sanitize_mp4
from features.volcengine_admission.privacy import PrivacyScanLimit, media_contains_caller_text
from features.volcengine_admission.service import AdmissionService
from features.volcengine_admission.storage import Conflict, Store
from features.volcengine_admission.transport import (ArkTransport, BoundaryError, MediaNetwork,
                                                    MOCK_VIDEO, media_url, safe_usage, valid_mp4)
from shared.network import SocketEgressDenied

VIDEO = "doubao-seedance-2-5-260628"
IMAGE = "doubao-seedream-5-0-pro-260628"
SYNTHETIC_KEY = "synthetic-only-key-for-regression"
SYNTHETIC_PROMPT = "Synthetic admission test: a toy circle on a plain background."
SIGNED_URL = "https://ark-content-generation-cn-beijing.tos-cn-beijing.volces.com/synthetic.mp4?signature=synthetic-private-marker"


def png64():
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, "PNG")
    return base64.b64encode(output.getvalue()).decode()


def hevc_structure(metadata_kind=39, sample_text=b""):
    """Synthetic box/sample contract, without claiming decodable picture content."""
    def box(kind, payload):
        return struct.pack(">I4s", len(payload) + 8, kind) + payload
    def table(kind, values):
        return box(kind, bytes(4) + struct.pack(">I", len(values)) + b"".join(values))
    picture = b"\x26\x01" + sample_text + b"\x80"
    sample = struct.pack(">I", len(picture)) + picture
    arrays = []
    for kind in (32, 33, 34, metadata_kind):
        payload = bytes([kind << 1, 1]) + (b"synthetic-hevc-metadata" if kind == metadata_kind else b"\x80")
        arrays.append(bytes([kind]) + struct.pack(">HH", 1, len(payload)) + payload)
    config = bytearray(23)
    config[0], config[21], config[22] = 1, 3, len(arrays)
    entry = bytearray(78)
    entry[6:8] = b"\x00\x01"
    struct.pack_into(">HH", entry, 24, 2880, 2880)
    stsd = table(b"stsd", [box(b"hvc1", entry + box(b"hvcC", bytes(config) + b"".join(arrays)))])
    header = bytearray(24)
    struct.pack_into(">II", header, 12, 1, 1)
    reference = table(b"dref", [box(b"url ", b"\x00\x00\x00\x01")])
    def movie(offset):
        samples = (stsd + table(b"stts", [struct.pack(">II", 1, 1)]) + table(b"stsc", [struct.pack(">III", 1, 1, 1)])
                   + box(b"stsz", bytes(4) + struct.pack(">II", len(sample), 1)) + table(b"stco", [struct.pack(">I", offset)]))
        media = (box(b"mdhd", header) + box(b"hdlr", bytes(8) + b"vide" + bytes(12))
                 + box(b"minf", box(b"vmhd", bytes(12)) + box(b"dinf", reference) + box(b"stbl", samples)))
        return box(b"moov", box(b"mvhd", bytes(100)) + box(b"trak", box(b"tkhd", bytes(84)) + box(b"mdia", media)))
    ftyp = box(b"ftyp", b"isom" + bytes(4) + b"isom")
    return ftyp + movie(len(ftyp) + len(movie(0)) + 8) + box(b"mdat", sample)


class Provider:
    def __init__(self):
        self.requests = []
        self.generated = 0
        self.get_entered = asyncio.Event()
        self.post_entered = asyncio.Event()
        self.block_post = None
        self.block_get = None
        self.status = "succeeded"
        self.invalid_submit = False
        self.media = MOCK_VIDEO
        self.media_url = SIGNED_URL
        self.task_id = "synthetic-task-1"

    async def __call__(self, request):
        self.requests.append(request)
        if request.url.host != "ark.cn-beijing.volces.com":
            return httpx.Response(200, content=self.media, headers={"content-type": "video/mp4"})
        if request.method == "POST":
            self.generated += 1
            self.post_entered.set()
            if self.block_post is not None:
                await self.block_post.wait()
            if self.invalid_submit:
                return httpx.Response(200, json={"error": SYNTHETIC_KEY})
            if request.url.path.endswith("images/generations"):
                return httpx.Response(200, json={"data": [{"b64_json": png64()}], "usage": {"generated_images": 1, "untrusted": SYNTHETIC_KEY}})
            return httpx.Response(200, json={"id": self.task_id})
        if request.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"items": [{"prompt": SYNTHETIC_PROMPT, "video_url": SIGNED_URL, "key": SYNTHETIC_KEY}]})
        self.get_entered.set()
        if self.block_get is not None:
            await self.block_get.wait()
        return httpx.Response(200, json={"status": self.status, "content": {"video_url": self.media_url},
                                       "usage": {"completion_tokens": 38400, "total_tokens": 38400, "untrusted": SYNTHETIC_PROMPT},
                                       "error": {"message": SYNTHETIC_KEY}})


class CatalogTests(unittest.TestCase):
    def test_high_resolution_hevc_configuration_sei_is_removed_without_moving_samples(self):
        raw = hevc_structure()
        clean = sanitize_mp4(raw, secrets=("synthetic-hevc-metadata",))
        self.assertEqual(len(clean), len(raw))
        self.assertNotIn(b"synthetic-hevc-metadata", clean)
        self.assertTrue(valid_mp4(clean))
        with self.assertRaisesRegex(MediaError, "media_codec_unsupported"):
            sanitize_mp4(hevc_structure(metadata_kind=35))

    def test_default_and_all_estimates_are_official_low_cost_specs(self):
        rows = [catalog.normalize_item(model["model_id"], {}) for model in catalog.MODELS]
        self.assertEqual(len(rows), 11)
        self.assertEqual(sum(model["default_selected"] for model in catalog.MODELS), 6)
        self.assertAlmostEqual(sum(row["estimated_cny"] for row in rows[:6]), 7.1784)
        self.assertAlmostEqual(sum(row["estimated_cny"] for row in rows), 8.07112)
        self.assertEqual(rows[4]["spec"], {"size": "1K"})
        self.assertEqual(rows[6]["spec"], {"resolution": "480p", "ratio": "1:1", "frames": 29})

    def test_resolution_specific_rates_and_image_pixel_tier(self):
        expected = [(VIDEO, {"resolution": "720p"}, 6.048), (VIDEO, {"resolution": "1080p"}, 14.9688),
                    ("doubao-seedance-2-0-260128", {"resolution": "1080p"}, 9.9144),
                    ("doubao-seedance-2-0-260128", {"resolution": "4k"}, 20.2176),
                    (IMAGE, {"size": "1.5K"}, .30), (IMAGE, {"size": "2K"}, .60)]
        for model, spec, estimate in expected:
            with self.subTest(model=model, spec=spec):
                self.assertAlmostEqual(catalog.normalize_item(model, spec)["estimated_cny"], estimate)

    def test_body_contract_single_image_and_no_frames_on_2x(self):
        for model in catalog.MODELS:
            item = catalog.normalize_item(model["model_id"], {})
            body = catalog.request_body(item, SYNTHETIC_PROMPT)
            self.assertNotIn("n", body)
            if item["kind"] == "image":
                self.assertEqual(body["response_format"], "b64_json")
                self.assertEqual(body.get("sequential_image_generation"), "disabled" if model["legacy"] else None)
            elif model["legacy"]:
                self.assertNotIn("duration", body)
                self.assertNotIn("generate_audio", body)
                self.assertEqual(body["frames"], 29)
            else:
                self.assertEqual(body["duration"], 4)
                self.assertIs(body["generate_audio"], False)
        for spec in ({"frames": 29}, {"duration": 1}, {"size": "1K"}, {"resolution": "4k"}):
            with self.assertRaises(ValueError):
                catalog.normalize_item(VIDEO, spec)

    def test_media_and_usage_boundaries_are_conservative(self):
        userinfo = "user" + ":" + "secret"
        for url in ("http://example.org/x", "https://127.0.0.1/x", "https://169.254.169.254/x", "https://" + userinfo + "@example.org/x",
                    "https://ark.cn-beijing.volces.com/api/v3/x", "https://example.org/x#fragment", "https://example.org:444/x"):
            with self.subTest(url=url), self.assertRaises(BoundaryError):
                media_url(url)
        self.assertEqual(media_url(SIGNED_URL), SIGNED_URL)
        self.assertEqual(safe_usage({"completion_tokens": 12, "total_tokens": True, "generated_images": -1, "bad": "body"}), {"completion_tokens": 12})
        self.assertTrue(valid_mp4(MOCK_VIDEO))
        self.assertFalse(valid_mp4(MOCK_VIDEO[:-4]))


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = os.environ.get("EVAL_TEST_ARTIFACT_ROOT") or os.environ.get("TMPDIR")
        if not root:
            raise RuntimeError("external_test_artifact_root_required")
        self.temp = tempfile.TemporaryDirectory(prefix="volcengine-test-", dir=root)
        self.root = Path(self.temp.name)
        self.provider = Provider()
        self.service = AdmissionService(self.root / "runtime", transport=httpx.MockTransport(self.provider), poll_interval=.001)
        self.services = [self.service]

    async def asyncTearDown(self):
        for service in self.services:
            await service.close()
        self.temp.cleanup()

    def preview(self, ids=(VIDEO, IMAGE), *, mode="live"):
        return self.service.preview(PreviewInput(mode=mode, items=[{"model_id": model} for model in ids], prompt=SYNTHETIC_PROMPT))

    def request(self, preview, **extra):
        return RunInput(preview_id=preview["preview_id"], fingerprint=preview["fingerprint"], api_key=SYNTHETIC_KEY,
                        confirm_live=True, idempotency_key="synthetic-idempotency-0001", **extra)

    async def finish(self, run, service=None):
        service = service or self.service
        await asyncio.wait_for(service.worker, 3)
        return service.detail(run["id"])

    def assert_no_secret_files(self):
        for path in self.root.rglob("*"):
            if path.is_file():
                raw = path.read_bytes()
                for secret in (SYNTHETIC_KEY, SYNTHETIC_PROMPT, SIGNED_URL, "synthetic-private-marker"):
                    self.assertNotIn(secret.encode(), raw, str(path.relative_to(self.root)))

    async def test_full_serial_single_output_history_and_media(self):
        preview = self.preview()
        run = await self.service.create(self.request(preview))
        result = await self.finish(run)
        self.assertEqual((result["status"], result["counts"]), ("completed", {"passed": 2}))
        self.assertEqual(self.provider.generated, 2)
        self.assertEqual([request.method for request in self.provider.requests], ["POST", "GET", "GET", "POST"])
        for item in result["items"]:
            self.assertEqual(item["media_status"], "saved")
            self.assertGreater(item["media_bytes"], 0)
            self.assertGreaterEqual(item["elapsed_ms"], 0)
            path, content_type = self.service.store.media(run["id"], item["media_id"])
            self.assertTrue(path.exists())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result["billing_status"], "unverified")
        self.assertIsNone(result["actual_charged_cny"])
        self.assertEqual(result["items"][0]["usage_estimated_cny"], 2.688)
        media_request = self.provider.requests[2]
        self.assertNotIn("Authorization", media_request.headers)
        self.assertNotIn("Referer", media_request.headers)
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(result))
        self.assertNotIn(SIGNED_URL, json.dumps(result))
        self.assertEqual(self.service.credentials, {})
        self.assert_no_secret_files()

    async def test_confirmation_required_and_auth_does_not_retain_history(self):
        with self.assertRaises(ValueError):
            await self.service.auth(AuthInput(mode="live", api_key=SYNTHETIC_KEY))
        self.assertEqual(self.provider.requests, [])
        result = await self.service.auth(AuthInput(mode="live", api_key=SYNTHETIC_KEY, confirm_read=True))
        self.assertEqual((result["status"], result["scope"]), ("passed", "task_list_only"))
        self.assertEqual(self.provider.generated, 0)
        self.assertEqual(self.provider.requests[0].url.params["page_size"], "1")
        preview = self.preview()
        body = self.request(preview).model_copy(update={"confirm_live": False})
        with self.assertRaises(ValueError):
            await self.service.create(body)
        self.assertEqual(self.service.runs(), [])
        self.assert_no_secret_files()

    async def test_auth_does_not_consume_or_parse_historical_response_body(self):
        class HistoryBody(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                raise AssertionError("historical_body_must_not_be_read")
                yield b""
            async def aclose(self):
                self.closed = True
        history = HistoryBody()
        transport = ArkTransport(transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=history)))
        result = await transport.auth(SYNTHETIC_KEY)
        self.assertEqual(result.status, "passed")
        self.assertTrue(history.closed)

    async def test_concurrent_idempotency_and_consumed_preview(self):
        preview = self.preview((IMAGE,))
        first, second = await asyncio.gather(self.service.create(self.request(preview)), self.service.create(self.request(preview)))
        self.assertEqual(first["id"], second["id"])
        result = await self.finish(first)
        replay = await self.service.create(self.request(preview))
        self.assertEqual(replay["id"], result["id"])
        self.assertEqual(self.provider.generated, 1)
        with self.assertRaises(Conflict):
            await self.service.create(self.request(preview).model_copy(update={"idempotency_key": "synthetic-idempotency-0002"}))
        with self.assertRaises(Conflict):
            await self.service.create(self.request(preview).model_copy(update={"fingerprint": "a" * 64}))

    async def test_unknown_submission_is_not_repeated(self):
        self.provider.invalid_submit = True
        preview = self.preview((VIDEO,))
        run = await self.service.create(self.request(preview))
        result = await self.finish(run)
        self.assertEqual(result["items"][0]["status"], "unknown")
        self.assertIsNone(result["items"][0]["task_id"])
        self.assertFalse(result["can_resume"])
        with self.assertRaises(Conflict):
            await self.service.resume(run["id"], ResumeInput(api_key=SYNTHETIC_KEY, confirm_read=True))
        await self.service.create(self.request(preview))
        self.assertEqual(self.provider.generated, 1)
        self.assert_no_secret_files()

    async def test_stop_waiting_post_preserves_task_and_read_only_resume(self):
        self.provider.block_post = asyncio.Event()
        run = await self.service.create(self.request(self.preview()))
        await asyncio.wait_for(self.provider.post_entered.wait(), 1)
        stopped = await self.service.stop(run["id"])
        self.assertEqual(stopped["items"][1]["status"], "not_sent")
        self.provider.block_post.set()
        result = await self.finish(run)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["items"][0]["status"], "submitted")
        self.assertTrue(result["can_resume"])
        self.assertEqual(self.provider.generated, 1)
        with self.assertRaises(Conflict):
            await self.service.resume(run["id"], ResumeInput(api_key="synthetic-wrong-key", confirm_read=True))
        with self.assertRaises(ValueError):
            await self.service.resume(run["id"], ResumeInput(api_key=SYNTHETIC_KEY))
        resumed = await self.service.resume(run["id"], ResumeInput(api_key=SYNTHETIC_KEY, confirm_read=True))
        final = await self.finish(resumed)
        self.assertEqual([item["status"] for item in final["items"]], ["passed", "not_sent"])
        self.assertTrue(final["resume_only"])
        self.assertEqual(self.provider.generated, 1)

    async def test_restart_keeps_known_task_id_but_consumes_unsent_permits(self):
        self.provider.block_get = asyncio.Event()
        run = await self.service.create(self.request(self.preview()))
        await asyncio.wait_for(self.provider.get_entered.wait(), 1)
        await self.service.close()
        new = AdmissionService(self.root / "runtime", transport=httpx.MockTransport(self.provider), poll_interval=.001)
        self.services.append(new)
        after = new.detail(run["id"])
        self.assertEqual(after["items"][0]["task_id"], "synthetic-task-1")
        self.assertEqual([item["status"] for item in after["items"]], ["submitted", "not_sent"])
        self.provider.block_get = None
        resumed = await new.resume(run["id"], ResumeInput(api_key=SYNTHETIC_KEY, confirm_read=True))
        result = await self.finish(resumed, new)
        self.assertEqual([item["status"] for item in result["items"]], ["passed", "not_sent"])
        self.assertEqual(self.provider.generated, 1)
        self.assert_no_secret_files()

    async def test_immediate_resume_while_stopped_poll_is_unwinding_is_rejected(self):
        self.provider.status = "running"
        self.provider.block_get = asyncio.Event()
        run = await self.service.create(self.request(self.preview()))
        await asyncio.wait_for(self.provider.get_entered.wait(), 1)
        stopped = await self.service.stop(run["id"])
        self.assertTrue(stopped["execution_active"])
        self.assertFalse(stopped["can_resume"])
        with self.assertRaises(Conflict):
            await self.service.resume(run["id"], ResumeInput(api_key=SYNTHETIC_KEY, confirm_read=True))
        self.assertEqual(len(self.provider.requests), 2)
        self.provider.block_get.set()
        unwound = await self.finish(run)
        self.assertFalse(unwound["execution_active"])
        self.assertTrue(unwound["can_resume"])
        self.provider.status = "succeeded"
        resumed = await self.service.resume(run["id"], ResumeInput(api_key=SYNTHETIC_KEY, confirm_read=True))
        result = await self.finish(resumed)
        self.assertEqual([item["status"] for item in result["items"]], ["passed", "not_sent"])
        self.assertEqual(self.provider.generated, 1)
        self.assertEqual([request.method for request in self.provider.requests], ["POST", "GET", "GET", "GET"])

    async def test_restart_during_send_becomes_unknown_without_resend(self):
        self.provider.block_post = asyncio.Event()
        run = await self.service.create(self.request(self.preview()))
        await asyncio.wait_for(self.provider.post_entered.wait(), 1)
        await self.service.close()
        new = Store(self.root / "runtime")
        try:
            result = new.get(run["id"])
        finally:
            new.close()
        self.assertEqual([item["status"] for item in result["items"]], ["unknown", "not_sent"])
        self.assertEqual(self.provider.generated, 1)

    async def test_video_id_or_terminal_status_alone_does_not_pass(self):
        for terminal in ("failed", "cancelled", "expired"):
            self.provider.status = terminal
            result = await ArkTransport(transport=httpx.MockTransport(self.provider)).poll("synthetic-task-1", SYNTHETIC_KEY)
            self.assertEqual((result.status, result.error_category), ("failed", "generation_" + terminal))
            self.assertIsNone(result.media)
        self.provider.status, self.provider.media = "succeeded", b"truncated"
        result = await ArkTransport(transport=httpx.MockTransport(self.provider)).poll("synthetic-task-1", SYNTHETIC_KEY)
        self.assertEqual((result.status, result.upstream_status, result.media_status), ("inconclusive", "succeeded", "failed"))

    async def test_media_no_redirect_no_signed_url_logging_and_size_bound(self):
        records = []
        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        handler = Capture()
        logger = logging.getLogger("httpx")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            result = await ArkTransport(transport=httpx.MockTransport(self.provider)).poll("synthetic-task-1", SYNTHETIC_KEY)
            self.assertEqual(result.status, "passed")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        self.assertFalse(any(SIGNED_URL in value or "signature=" in value for value in records))
        calls = []
        def redirect(request):
            calls.append(request)
            return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
        status, _ = await ArkTransport(transport=httpx.MockTransport(redirect))._request("GET", SIGNED_URL, media_kind="video")
        self.assertEqual(status, 302)
        self.assertEqual(len(calls), 1)
        with patch("features.volcengine_admission.transport.MAX_VIDEO_BYTES", 10):
            result = await ArkTransport(transport=httpx.MockTransport(self.provider)).poll("synthetic-task-1", SYNTHETIC_KEY)
        self.assertEqual(result.error_category, "response_too_large")

    async def test_sensitive_http_context_suppresses_header_debug_but_not_other_logs(self):
        records = []
        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        logger = logging.getLogger("httpcore.http11")
        capture, old_level = Capture(), logger.level
        logger.addHandler(capture)
        logger.setLevel(logging.DEBUG)
        def response(_request):
            logger.debug("synthetic signed location: %s", SIGNED_URL)
            return httpx.Response(200, json={"items": []})
        try:
            result = await ArkTransport(transport=httpx.MockTransport(response)).auth(SYNTHETIC_KEY)
            logger.debug("synthetic-unrelated-request")
        finally:
            logger.removeHandler(capture)
            logger.setLevel(old_level)
        self.assertEqual(result.status, "passed")
        self.assertEqual(records, ["synthetic-unrelated-request"])

    async def test_key_echo_in_task_id_is_rejected_instead_of_persisted(self):
        transport = ArkTransport(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"id": SYNTHETIC_KEY})))
        result = await transport.submit(catalog.normalize_item(VIDEO, {}), SYNTHETIC_PROMPT, SYNTHETIC_KEY)
        self.assertEqual((result.status, result.task_id), ("unknown", None))
        self.assertNotIn(SYNTHETIC_KEY, str(result))

    async def test_preview_duplicate_expiration_and_contract_change(self):
        with self.assertRaises(ValueError):
            self.preview((VIDEO, VIDEO))
        preview = self.preview((VIDEO,))
        self.service.previews[preview["preview_id"]]["expires_at"] = 0
        with self.assertRaises(Conflict):
            await self.service.create(self.request(preview))
        self.assertEqual(self.provider.requests, [])
        self.provider.block_get = asyncio.Event()
        run = await self.service.create(self.request(self.preview((VIDEO,))))
        await asyncio.wait_for(self.provider.get_entered.wait(), 1)
        await self.service.close()
        new = AdmissionService(self.root / "runtime", transport=httpx.MockTransport(self.provider))
        self.services.append(new)
        new.store.update(run["id"], {"contract_version": "synthetic-older-contract"})
        with self.assertRaises(Conflict):
            await new.resume(run["id"], ResumeInput(api_key=SYNTHETIC_KEY, confirm_read=True))
        self.assertEqual(self.provider.generated, 1)

    async def test_same_version_request_builder_drift_blocks_query_recovery(self):
        self.provider.status = "running"
        self.provider.block_get = asyncio.Event()
        run = await self.service.create(self.request(self.preview((VIDEO,))))
        await asyncio.wait_for(self.provider.get_entered.wait(), 1)
        await self.service.stop(run["id"])
        self.provider.block_get.set()
        await self.finish(run)
        original = catalog.request_body
        def changed(item, prompt):
            return {**original(item, prompt), "synthetic_parameter_change": True}
        with patch.object(catalog, "request_body", changed):
            with self.assertRaises(Conflict):
                await self.service.resume(run["id"], ResumeInput(api_key=SYNTHETIC_KEY, confirm_read=True))
        self.assertEqual(self.provider.generated, 1)

    async def test_usage_estimate_uses_frozen_resolution_price_after_catalog_changes(self):
        self.provider.block_get = asyncio.Event()
        run = await self.service.create(self.request(self.preview((VIDEO,))))
        await asyncio.wait_for(self.provider.get_entered.wait(), 1)
        with patch.dict(catalog.BY_ID[VIDEO]["price"]["cny_per_million_tokens_by_resolution"], {"480p": 999}):
            self.provider.block_get.set()
            result = await self.finish(run)
        self.assertEqual(result["items"][0]["unit_price_cny_per_million_tokens"], 70)
        self.assertEqual(result["items"][0]["usage_estimated_cny"], 2.688)
        self.assertNotIn("_prompt_digest", result)

    async def test_guarded_media_binds_only_public_ips_even_with_deployment_allowlist(self):
        class Backend:
            def __init__(self):
                self.hosts = []
            async def connect_tcp(self, host, *_args):
                self.hosts.append(host)
                return "synthetic-socket"
        backend = Backend()
        network = MediaNetwork(backend)
        def rows(addresses):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443)) for address in addresses]
        with patch("socket.getaddrinfo", return_value=rows(["8.8.8.8"])), patch.dict(os.environ, {"PLATFORM_EGRESS_ALLOWLIST": "127.0.0.1"}):
            self.assertEqual(await network.connect_tcp("cdn.example.org", 443, 1), "synthetic-socket")
        self.assertEqual(backend.hosts, ["8.8.8.8"])
        with patch("socket.getaddrinfo", return_value=rows(["8.8.8.8", "127.0.0.1"])):
            with self.assertRaises(SocketEgressDenied):
                await network.connect_tcp("cdn.example.org", 443, 1)
        self.assertEqual(backend.hosts, ["8.8.8.8"])

    async def test_real_guarded_transport_maps_private_media_refusal_and_connection_error(self):
        rows = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=rows), patch.dict(os.environ, {"PLATFORM_EGRESS_ALLOWLIST": "127.0.0.1"}):
            with self.assertRaises(httpx.ConnectError) as caught:
                await ArkTransport()._request("GET", SIGNED_URL, media_kind="video")
        self.assertEqual(ArkTransport._exception_category(caught.exception), "egress_denied")
        class FailedBackend:
            async def connect_tcp(self, *_args):
                raise httpcore.ConnectError("synthetic private connection detail")
        rows = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]
        with patch("socket.getaddrinfo", return_value=rows):
            with self.assertRaises(httpcore.ConnectError):
                await MediaNetwork(FailedBackend()).connect_tcp("cdn.example.org", 443, 1)

    async def test_api_contract_media_ownership_and_validation_does_not_echo_key(self):
        app = create_app(data_dir=self.root / "api-runtime", transport=httpx.MockTransport(self.provider), poll_interval=.001)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            meta = await client.get("/api/meta")
            self.assertEqual(len(meta.json()["catalog"]), 11)
            self.assertIsNone(app.state.service)
            self.assertFalse((self.root / "api-runtime").exists())
            capacity = await client.get("/api/capacity")
            self.assertEqual(capacity.json()["used_bytes"], 0)
            self.services.append(app.state.service)
            invalid = await client.post("/api/auth", json={"mode": "live", "api_key": SYNTHETIC_KEY + "\n", "confirm_read": True})
            self.assertEqual(invalid.status_code, 422)
            self.assertNotIn(SYNTHETIC_KEY, invalid.text)
            wrongbase = await client.post("/api/auth", json={"base_url": "http://127.0.0.1", "api_key": SYNTHETIC_KEY})
            self.assertEqual(wrongbase.status_code, 422)
            preview = (await client.post("/api/preview", json={"items": [{"model_id": IMAGE}]})).json()
            response = await client.post("/api/runs", json={"preview_id": preview["preview_id"], "fingerprint": preview["fingerprint"],
                                                           "idempotency_key": "synthetic-api-idempotency"})
            self.assertEqual(response.status_code, 200)
            service = app.state.service
            result = await self.finish(response.json(), service)
            self.assertEqual(result["mode"], "mock")
            media_id = result["items"][0]["media_id"]
            local = await client.get(f"/api/runs/{result['id']}/media/{media_id}")
            self.assertEqual((local.status_code, local.headers["content-type"]), (200, "image/png"))
            wrong_run = await client.get(f"/api/runs/{'f'*32}/media/{media_id}")
            self.assertEqual(wrong_run.status_code, 404)
            report = await client.get(f"/api/runs/{result['id']}/report")
            self.assertEqual(report.json()["billing_status"], "unverified")
            self.assertNotIn(SYNTHETIC_KEY, report.text)

    async def test_media_capacity_failure_keeps_generation_success_separate(self):
        self.provider.block_get = asyncio.Event()
        run = await self.service.create(self.request(self.preview((VIDEO,))))
        await asyncio.wait_for(self.provider.get_entered.wait(), 1)
        with patch("features.volcengine_admission.storage.MAX_MEDIA_BYTES", 1):
            self.provider.block_get.set()
            result = await self.finish(run)
        item = result["items"][0]
        self.assertEqual((item["status"], item["upstream_status"], item["media_status"]), ("inconclusive", "succeeded", "failed"))
        self.assertEqual(self.provider.generated, 1)
        self.assertTrue(result["can_resume"])

    async def test_http_errors_use_fixed_categories_and_do_not_infer_fake_key(self):
        for status, expected in [(401, "failed"), (403, "inconclusive"), (500, "inconclusive")]:
            upstream = ArkTransport(transport=httpx.MockTransport(lambda _request: httpx.Response(status, json={"message": SYNTHETIC_KEY})))
            result = await upstream.auth(SYNTHETIC_KEY)
            self.assertEqual(result.status, expected)
            self.assertNotIn(SYNTHETIC_KEY, str(result))
        upstream = ArkTransport(transport=httpx.MockTransport(lambda _request: httpx.Response(503, json={"message": SYNTHETIC_PROMPT})))
        item = catalog.normalize_item(VIDEO, {})
        result = await upstream.submit(item, SYNTHETIC_PROMPT, SYNTHETIC_KEY)
        self.assertEqual((result.status, result.error_category), ("unknown", "upstream_unavailable"))
        self.assertGreaterEqual(result.elapsed_ms, 0)

    async def test_prompt_echo_task_identifier_never_reaches_durable_report(self):
        prompt = "SyntheticPrivatePromptEcho"
        self.provider.task_id = "prefix-" + prompt + "-suffix"
        preview = self.service.preview(PreviewInput(mode="live", items=[{"model_id": VIDEO}], prompt=prompt))
        result = await self.finish(await self.service.create(self.request(preview)))
        self.assertEqual(result["items"][0]["status"], "unknown")
        self.assertIsNone(result["items"][0]["task_id"])
        self.assertNotIn(prompt, json.dumps(result))
        for path in (self.root / "runtime").rglob("*"):
            if path.is_file():
                self.assertNotIn(prompt.encode(), path.read_bytes())

    async def test_secret_or_prompt_idempotency_is_only_persisted_as_digest(self):
        prompt = "SyntheticPrivateIdempotencyPrompt"
        for idem in (SYNTHETIC_KEY, prompt):
            preview = self.service.preview(PreviewInput(mode="live", items=[{"model_id": IMAGE}], prompt=prompt))
            body = self.request(preview).model_copy(update={"idempotency_key": idem})
            run = await self.service.create(body)
            await self.finish(run)
            replay = await self.service.create(body)
            self.assertEqual(replay["id"], run["id"])
        self.assertEqual(self.provider.generated, 2)
        for path in (self.root / "runtime").rglob("*"):
            if path.is_file():
                self.assertNotIn(prompt.encode(), path.read_bytes())
                self.assertNotIn(SYNTHETIC_KEY.encode(), path.read_bytes())

    async def test_media_url_rejects_secret_and_prompt_echo_before_third_origin(self):
        from urllib.parse import quote
        prompt = "SyntheticSensitivePrompt42"
        echoes = (SYNTHETIC_KEY, quote(SYNTHETIC_KEY, safe=""), quote(quote("prefix " + prompt + " suffix", safe=""), safe=""),
                  "".join("\\u%04x" % ord(c) for c in prompt))
        upstream = ArkTransport(transport=httpx.MockTransport(self.provider))
        for echo in echoes:
            self.provider.requests.clear()
            self.provider.media_url = "https://media.example.org/x?echo=" + echo
            result = await upstream.poll("synthetic-task-1", SYNTHETIC_KEY,
                                         prompt_digest=catalog.fingerprint(prompt), prompt_length=len(prompt))
            self.assertEqual(result.error_category, "sensitive_media_url_rejected")
            self.assertEqual(len(self.provider.requests), 1)
            self.assertEqual(self.provider.requests[0].url.host, "ark.cn-beijing.volces.com")

    async def test_empty_mp4_boxes_are_rejected_and_free_metadata_is_removed(self):
        empty = b"".join(struct.pack(">I4s", 8, kind) for kind in (b"ftyp", b"moov", b"mdat"))
        self.assertFalse(valid_mp4(empty))
        self.provider.media = empty
        result = await ArkTransport(transport=httpx.MockTransport(self.provider)).poll("synthetic-task-1", SYNTHETIC_KEY)
        self.assertEqual((result.status, result.media_status), ("inconclusive", "failed"))
        payload = SYNTHETIC_PROMPT.encode() + SYNTHETIC_KEY.encode()
        self.provider.media = MOCK_VIDEO + struct.pack(">I4s", len(payload) + 8, b"free") + payload
        run = await self.service.create(self.request(self.preview((VIDEO,))))
        final = await self.finish(run)
        self.assertEqual(final["items"][0]["status"], "passed")
        path, _ = self.service.store.media(run["id"], final["items"][0]["media_id"])
        cleaned = path.read_bytes()
        self.assertTrue(valid_mp4(cleaned))
        self.assertEqual(len(cleaned), len(self.provider.media))
        self.assertNotIn(SYNTHETIC_PROMPT.encode(), cleaned)
        self.assertNotIn(SYNTHETIC_KEY.encode(), cleaned)
        self.assert_no_secret_files()

    async def test_track_tables_must_reference_real_samples_and_codec(self):
        invalid = bytearray(MOCK_VIDEO)
        location = invalid.index(b"stco")
        invalid[location + 12:location + 16] = (0).to_bytes(4, "big")
        self.assertFalse(valid_mp4(bytes(invalid)))
        invalid = bytearray(MOCK_VIDEO)
        location = invalid.index(b"avcC")
        invalid[location + 4] = 0
        self.assertFalse(valid_mp4(bytes(invalid)))
        cleaned = sanitize_mp4(MOCK_VIDEO)
        self.assertTrue(valid_mp4(cleaned))
        self.assertNotIn(b"Lavf", cleaned)
        self.assertNotIn(b"x264", cleaned)

    async def test_cleaned_media_checks_retained_samples_for_prompt_and_key_echoes(self):
        from urllib.parse import quote
        prompt = "EchoZ"
        encoded = "".join("%%%02x" % ord(c) for c in prompt)
        echoes = [prompt, encoded, quote(encoded, safe=""),
                  "".join("\\u%04x" % ord(c) for c in prompt), "".join("&#%d;" % ord(c) for c in prompt),
                  SYNTHETIC_KEY]
        upstream = ArkTransport(transport=httpx.MockTransport(self.provider))
        for echo in echoes:
            contaminated = bytearray(MOCK_VIDEO)
            cursor = contaminated.index(b"mdat") + 4
            while contaminated[cursor + 4] & 31 != 5:
                cursor += 4 + int.from_bytes(contaminated[cursor:cursor + 4], "big")
            start = cursor + 5
            payload = echo.encode() + b"\x00"
            self.assertLessEqual(len(payload), int.from_bytes(contaminated[cursor:cursor + 4], "big") - 1)
            contaminated[start:start + len(payload)] = payload
            self.assertTrue(valid_mp4(bytes(contaminated)))
            self.provider.media = bytes(contaminated)
            result = await upstream.poll("synthetic-task-1", SYNTHETIC_KEY,
                                         prompt_digest=catalog.fingerprint(prompt), prompt_length=len(prompt))
            self.assertEqual((result.status, result.media_status, result.error_category),
                             ("inconclusive", "failed", "sensitive_media_metadata_rejected"))
            self.assertEqual((result.http_status, result.media_http_status), (200, 200))
            self.assertIsNone(result.media)
        self.assertEqual(self.provider.generated, 0)
        preview = self.service.preview(PreviewInput(mode="live", items=[{"model_id": VIDEO}], prompt=prompt))
        final = await self.finish(await self.service.create(self.request(preview)))
        self.assertEqual(final["items"][0]["error_category"], "sensitive_media_metadata_rejected")
        self.assertEqual(self.provider.generated, 1)
        self.assertFalse(list((self.root / "runtime" / "media").glob("*")))

    async def test_media_privacy_scan_handles_chunk_edges_unicode_and_bounded_work(self):
        from features.volcengine_admission import privacy
        prompt = "SyntheticPrivate跨块🚀"
        for echo in (prompt, json.dumps(prompt, ensure_ascii=True)[1:-1]):
            value = b"\x00" * (privacy.MEDIA_TEXT_CHUNK - 5) + echo.encode() + b"\xff"
            self.assertTrue(media_contains_caller_text(value, prompt_digest=catalog.fingerprint(prompt), prompt_length=len(prompt)))
        with patch.object(privacy, "MAX_MEDIA_TEXT_RUN", 32):
            with self.assertRaises(PrivacyScanLimit):
                media_contains_caller_text(b"A" * 33, secrets=(SYNTHETIC_KEY,))
        with patch.object(privacy.ScanBudget, "consume", side_effect=PrivacyScanLimit("media_privacy_check_inconclusive")):
            with self.assertRaisesRegex(MediaError, "media_privacy_check_inconclusive"):
                sanitize_mp4(MOCK_VIDEO, prompt_digest=catalog.fingerprint("xyz"), prompt_length=3)
        app = create_app(data_dir=self.root / "invalid-prompt")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            for control in ("\x00", "\x7f", "\ud800"):
                result = await client.post("/api/preview", content=json.dumps({"items": [{"model_id": IMAGE}], "prompt": "synthetic" + control}),
                                           headers={"content-type": "application/json"})
                self.assertEqual(result.status_code, 422)
                self.assertIsNone(app.state.service)

    async def test_signed_url_echo_is_not_saved_on_initial_or_restored_queries(self):
        from urllib.parse import quote
        url = "https://example.org/a?sig=toy"
        echoes = [url, quote(url, safe=""), quote(quote(url, safe=""), safe=""),
                  "".join("\\u%04x" % ord(c) for c in url), "".join("&#%d;" % ord(c) for c in url)]
        self.provider.media_url = url
        self.provider.media = hevc_structure(sample_text=echoes[0].encode())
        result = await self.finish(await self.service.create(self.request(self.preview((VIDEO,)))))
        self.assertEqual(result["items"][0]["error_category"], "sensitive_media_metadata_rejected")
        self.assertTrue(result["can_resume"])
        run_id = result["id"]
        await self.service.close()
        restored = AdmissionService(self.root / "runtime", transport=httpx.MockTransport(self.provider), poll_interval=.001)
        self.services.append(restored)
        for echo in echoes[1:]:
            self.provider.media = hevc_structure(sample_text=echo.encode())
            self.assertTrue(valid_mp4(self.provider.media))
            resumed = await restored.resume(run_id, ResumeInput(api_key=SYNTHETIC_KEY, confirm_read=True))
            result = await self.finish(resumed, restored)
            item = result["items"][0]
            self.assertEqual((item["status"], item["upstream_status"], item["media_status"], item["error_category"]),
                             ("inconclusive", "succeeded", "failed", "sensitive_media_metadata_rejected"))
            self.assertEqual((item["http_status"], item["media_http_status"]), (200, 200))
            self.assertIsNone(item["media_id"])
            self.assertNotIn(url, json.dumps(result))
            self.assertEqual(self.provider.generated, 1)
        self.assertFalse(list((self.root / "runtime" / "media").glob("*")))
        for path in (self.root / "runtime").rglob("*"):
            if path.is_file():
                for echo in echoes:
                    self.assertNotIn(echo.encode(), path.read_bytes())
        self.provider.media = MOCK_VIDEO
        result = await self.finish(await restored.resume(run_id, ResumeInput(api_key=SYNTHETIC_KEY, confirm_read=True)), restored)
        self.assertEqual(result["items"][0]["status"], "passed")
        self.assertEqual(self.provider.generated, 1)
        self.assertEqual(sum(request.method == "POST" for request in self.provider.requests), 1)
        self.assertFalse(any(request.method == "DELETE" for request in self.provider.requests))

    async def test_second_service_cannot_recover_or_mutate_a_live_owner(self):
        self.provider.block_post = asyncio.Event()
        run = await self.service.create(self.request(self.preview()))
        await asyncio.wait_for(self.provider.post_entered.wait(), 1)
        before = self.service.store.get(run["id"])
        with patch.object(Store, "recover", side_effect=AssertionError("recovery_must_not_start")):
            with self.assertRaises(Conflict):
                AdmissionService(self.root / "runtime")
        self.assertEqual(self.service.store.get(run["id"]), before)
        code = "from features.volcengine_admission.service import AdmissionService\nfrom features.volcengine_admission.storage import Conflict\nimport sys\ntry:\n s=AdmissionService(sys.argv[1])\nexcept Conflict:\n print('locked');sys.exit(0)\ns.store.close();sys.exit(1)"
        process = subprocess.run([sys.executable, "-B", "-c", code, str(self.root / "runtime")], capture_output=True, text=True, timeout=3)
        self.assertEqual((process.returncode, process.stdout.strip()), (0, "locked"))
        self.assertEqual(self.service.store.get(run["id"]), before)
        self.provider.block_post.set()
        final = await self.finish(run)
        self.assertEqual(final["counts"], {"passed": 2})

    async def test_lock_failure_api_is_safe_409_and_failed_initialization_releases(self):
        app = create_app(data_dir=self.root / "runtime")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            result = await client.get("/api/meta")
            self.assertEqual(result.status_code, 200)
            self.assertIsNone(app.state.service)
            result = await client.get("/api/capacity")
            self.assertEqual(result.status_code, 409)
            self.assertNotIn(str(self.root), result.text)
            self.assertIsNone(app.state.service)
        with patch.object(Store, "_initialize", side_effect=RuntimeError("synthetic initialization failure")):
            with self.assertRaises(RuntimeError):
                Store(self.root / "failed-init")
        store = Store(self.root / "failed-init")
        store.close()

    async def test_full_capacity_skips_post_and_confirmed_cleanup_releases_space(self):
        original = await self.finish(await self.service.create(self.request(self.preview((IMAGE,)))))
        before_requests = len(self.provider.requests)
        capacity = self.service.capacity()
        with patch("features.volcengine_admission.storage.MAX_MEDIA_BYTES", capacity["used_bytes"]):
            preview = self.preview((IMAGE,))
            self.assertTrue(preview["capacity_warnings"])
            body = self.request(preview).model_copy(update={"idempotency_key": "synthetic-capacity-second"})
            blocked = await self.finish(await self.service.create(body))
            self.assertEqual(blocked["items"][0]["status"], "not_sent")
            self.assertEqual(blocked["items"][0]["error_category"], "media_capacity_unavailable")
            self.assertEqual(len(self.provider.requests), before_requests)
        with self.assertRaises(ValueError):
            await self.service.cleanup(original["id"], CleanupInput())
        cleaned = await self.service.cleanup(original["id"], CleanupInput(confirm_local_delete=True))
        self.assertEqual(len(self.provider.requests), before_requests)
        self.assertEqual((cleaned["status"], cleaned["items"][0]["status"]), ("completed", "passed"))
        self.assertEqual(cleaned["items"][0]["usage"], original["items"][0]["usage"])
        self.assertEqual(cleaned["items"][0]["media_status"], "removed")
        self.assertIsNone(cleaned["items"][0]["media_id"])
        self.assertEqual(self.service.capacity()["used_bytes"], 0)
        body = self.request(self.preview((IMAGE,))).model_copy(update={"idempotency_key": "synthetic-capacity-third"})
        completed = await self.finish(await self.service.create(body))
        self.assertEqual(completed["items"][0]["status"], "passed")
        self.assertEqual(self.provider.generated, 2)

    async def test_reservation_precedes_post_and_is_released_on_stop(self):
        from features.volcengine_admission.transport import MAX_VIDEO_BYTES
        self.provider.block_post = asyncio.Event()
        run = await self.service.create(self.request(self.preview((VIDEO,))))
        await asyncio.wait_for(self.provider.post_entered.wait(), 1)
        state = self.service.capacity()
        self.assertEqual((state["reserved_bytes"], state["reserved_files"]), (MAX_VIDEO_BYTES, 1))
        with self.assertRaises(Conflict):
            await self.service.cleanup(run["id"], CleanupInput(confirm_local_delete=True))
        await self.service.stop(run["id"])
        self.provider.block_post.set()
        await self.finish(run)
        self.assertEqual(self.service.capacity()["reserved_bytes"], 0)

    async def test_http_body_rejection_preserves_known_200_status(self):
        upstream = ArkTransport(transport=httpx.MockTransport(lambda _request: httpx.Response(200, headers={"content-length": str(100 * 1024 * 1024)})))
        result = await upstream.submit(catalog.normalize_item(IMAGE, {}), SYNTHETIC_PROMPT, SYNTHETIC_KEY)
        self.assertEqual((result.http_status, result.error_category, result.status), (200, "response_too_large", "inconclusive"))


if __name__ == "__main__":
    unittest.main()
