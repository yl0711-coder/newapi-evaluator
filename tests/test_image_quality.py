"""Independently authored synthetic fixtures; never contact an external upstream."""
import asyncio
import base64
import io
import json
import os
import struct
from pathlib import Path
import tempfile
import unittest
import zlib
from unittest.mock import patch

import httpx
from PIL import Image, PngImagePlugin
from pydantic import ValidationError

from features.image_quality import api, engine
from workbench import create_app


def synthetic_png(size=(16, 24), text="synthetic metadata"):
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", text)
    Image.new("RGB", size, (21, 93, 80)).save(output, "PNG", pnginfo=info)
    return output.getvalue()


def payload(**overrides):
    return {"base_url": "https://synthetic.example/v1", "api_key": "synthetic-ephemeral-key",
            "prompt": "Synthetic fixture: three geometric shapes.", "confirm_live": True,
            "timeout_seconds": 2, **overrides}


def upstream_image(**overrides):
    return {"data": [{"b64_json": base64.b64encode(synthetic_png()).decode()}], **overrides}


class SettingsTests(unittest.TestCase):
    def test_endpoint_normalization(self):
        cases = {
            "https://synthetic.example": "https://synthetic.example/v1/images/generations",
            "https://synthetic.example/v1/": "https://synthetic.example/v1/images/generations",
            "https://synthetic.example/prefix/v1": "https://synthetic.example/prefix/v1/images/generations",
            "https://synthetic.example/v1/images/generations/": "https://synthetic.example/v1/images/generations",
        }
        for supplied, expected in cases.items():
            with self.subTest(kind=supplied.split("/")[-1]):
                self.assertEqual(engine.endpoint_url(supplied), expected)

    def test_rejects_invalid_or_sensitive_input(self):
        for override in [
            {"model": "another-model"}, {"prompt": " \n"}, {"api_key": "bad\nkey"},
            {"base_url": "https://synthetic.example/?token=synthetic"},
            {"base_url": "file:///tmp/synthetic"}, {"base_url": "https://synthetic.example/#secret"},
            {"base_url": "https://" + "synthetic-user:synthetic-password@" + "example.test"},
            {"base_url": "https://synthetic.example:0"}, {"timeout_seconds": float("nan")},
            {"base_url": "http://[v1.synthetic]"},
            {"size": "unbounded"}, {"quality": "auto"}, {"n": 9}, {"confirm_live": "yes"},
        ]:
            with self.subTest(field=next(iter(override))):
                with self.assertRaises(ValidationError):
                    engine.GenerationInput(**payload(**override))

    def test_readonly_inspection_excludes_url_and_secrets(self):
        result = engine.inspect_settings(engine.Settings(base_url="https://synthetic.example/v1"))
        self.assertEqual(result["model"], "gpt-image-2")
        self.assertEqual(result["protocol"], "openai-images")
        self.assertEqual(len(result["endpoint_fingerprint"]), 64)
        self.assertNotIn("https://", json.dumps(result))

    def test_png_validation_preserves_pixels_and_strips_metadata(self):
        raw = synthetic_png(text="synthetic-ephemeral-key")
        clean, width, height, upstream_hash = engine.normalize_png(base64.b64encode(raw).decode())
        self.assertEqual((width, height), (16, 24))
        self.assertNotIn(b"synthetic-ephemeral-key", clean)
        with Image.open(io.BytesIO(clean)) as image:
            self.assertEqual(image.getpixel((0, 0)), (21, 93, 80))
        self.assertEqual(len(upstream_hash), 64)

    def test_png_preserves_16_bit_greyscale_without_clamping(self):
        levels = [0, 257, 16384, 32768, 65535]
        original = Image.frombytes("I;16", (5, 1), struct.pack("<5H", *levels))
        output = io.BytesIO()
        original.save(output, "PNG")
        raw = output.getvalue()
        clean, *_ = engine.normalize_png(base64.b64encode(raw).decode())
        self.assertEqual(clean, raw)
        with Image.open(io.BytesIO(clean)) as image:
            self.assertEqual([image.getpixel((x, 0)) for x in range(5)], levels)

    def test_png_preserves_palette_transparency_and_rendering_chunks(self):
        original = Image.new("P", (2, 1))
        original.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
        original.putdata([0, 1])
        info = PngImagePlugin.PngInfo()
        info.add(b"gAMA", struct.pack(">I", 45455))
        info.add(b"sRGB", b"\0")
        info.add_text("Comment", "synthetic-private-metadata")
        output = io.BytesIO()
        original.save(output, "PNG", transparency=bytes([0, 128]), pnginfo=info)
        raw = output.getvalue()
        clean, *_ = engine.normalize_png(base64.b64encode(raw).decode())
        self.assertNotIn(b"synthetic-private-metadata", clean)
        with Image.open(io.BytesIO(clean)) as image:
            self.assertEqual(image.info["gamma"], 0.45455)
            self.assertEqual(image.info["srgb"], 0)
            rgba = image.convert("RGBA")
            self.assertEqual([rgba.getpixel((x, 0)) for x in range(2)], [(255, 0, 0, 0), (0, 255, 0, 128)])

    def test_unpreservable_colour_profiles_and_animation_are_explicitly_rejected(self):
        for chunk in (b"iCCP", b"cICP", b"mDCV", b"cLLI", b"acTL"):
            raw = synthetic_png()
            data = b"synthetic-private-metadata"
            # Insert an actual wire-format chunk; Pillow's writer omits some chunk names.
            inserted = struct.pack(">I", len(data)) + chunk + data + struct.pack(">I", zlib.crc32(chunk + data))
            raw = raw[:33] + inserted + raw[33:]
            with self.assertRaisesRegex(engine.InvalidImage, "unsupported_png_features"):
                engine.normalize_png(base64.b64encode(raw).decode())

    def test_invalid_image_and_format_rejected(self):
        jpeg = io.BytesIO()
        Image.new("RGB", (2, 2)).save(jpeg, "JPEG")
        for encoded in ["not-base64%%", base64.b64encode(synthetic_png()[:-20]).decode(),
                        base64.b64encode(jpeg.getvalue()).decode()]:
            with self.assertRaises(engine.InvalidImage):
                engine.normalize_png(encoded)

    def test_usage_missing_values_and_boolean_not_reported_as_tokens(self):
        self.assertIsNone(engine.safe_usage(None))
        self.assertEqual(engine.safe_usage({"input_tokens": 0, "output_tokens": True, "total_tokens": -1,
                                          "unknown": "synthetic"}), {"input_tokens": 0})


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_request_and_safe_response(self):
        calls = []
        body = engine.GenerationInput(**payload())
        def handler(request):
            calls.append(request)
            sent = json.loads(request.content)
            self.assertEqual(sent, {"model": "gpt-image-2", "prompt": body.prompt, "n": 1,
                                   "size": "1024x1536", "quality": "high", "output_format": "png"})
            self.assertEqual(request.headers["authorization"], "Bearer synthetic-ephemeral-key")
            return httpx.Response(200, json=upstream_image(model="gpt-image-2", usage={"output_tokens": 12}),
                                  headers={"x-request-id": "synthetic-request"})
        report = await engine.generate(body, httpx.MockTransport(handler))
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["returned_model"], "gpt-image-2")
        self.assertFalse(report["size_matches"])
        self.assertGreaterEqual(report["total_seconds"], report["upstream_seconds"])
        encoded = json.dumps(report)
        for prohibited in [body.prompt, body.api_key.get_secret_value(), body.base_url]:
            self.assertNotIn(prohibited, encoded)

    async def test_model_absence_stays_null_and_foreign_model_is_not_certified(self):
        for model in [None, "different-model", "synthetic-ephemeral-key"]:
            report = await engine.generate(engine.GenerationInput(**payload()), httpx.MockTransport(
                lambda _: httpx.Response(200, json=upstream_image(model=model))))
            self.assertEqual(report["returned_model"], None if model in (None, "synthetic-ephemeral-key") else model)
            self.assertNotIn("authentic", report)

    async def test_http_errors_do_not_retry_or_leak_response_body(self):
        for status, expected in [(401, "authentication_failed"), (403, "authentication_failed"),
                                 (429, "rate_limited"), (500, "upstream_http_error"),
                                 (302, "redirect_rejected")]:
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, text="synthetic private error body",
                                      headers={"location": "https://unvisited.example", "x-request-id": "synthetic-ephemeral-key"})
            report = await engine.generate(engine.GenerationInput(**payload()), httpx.MockTransport(handler))
            self.assertEqual(report["error_code"], expected)
            self.assertEqual(len(calls), 1)
            self.assertIsNone(report["request_id"])
            self.assertNotIn("synthetic private", json.dumps(report))

    async def test_invalid_responses_are_classified(self):
        cases = [(b"not json", "invalid_json"), ([], "invalid_response"),
                 ({"data": []}, "missing_image"),
                 ({"data": [{"url": "https://unvisited.example/image"}]}, "image_url_only"),
                 ({"error": {"message": "synthetic failure"}}, "upstream_error"),
                 ({"data": [{"b64_json": "%%%"}]}, "invalid_image")]
        for value, expected in cases:
            response = httpx.Response(200, content=value) if isinstance(value, bytes) else httpx.Response(200, json=value)
            report = await engine.generate(engine.GenerationInput(**payload()), httpx.MockTransport(lambda _: response))
            self.assertEqual(report["error_code"], expected)
            self.assertNotIn("image", report)

    async def test_deadline_cancels_upstream_and_reports_unknown_result(self):
        cancelled = asyncio.Event()
        async def handler(_request):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        report = await engine.generate(engine.GenerationInput(**payload(timeout_seconds=1)), httpx.MockTransport(handler))
        self.assertTrue(cancelled.is_set())
        self.assertEqual(report["error_code"], "timeout_result_unknown")

    async def test_response_limit_and_connection_failure(self):
        with patch.object(engine, "MAX_RESPONSE_BYTES", 8):
            report = await engine.generate(engine.GenerationInput(**payload()), httpx.MockTransport(
                lambda _: httpx.Response(200, content=b"x" * 9)))
        self.assertEqual(report["error_code"], "response_too_large")
        def unavailable(request):
            raise httpx.ConnectError("synthetic-ephemeral-key", request=request)
        report = await engine.generate(engine.GenerationInput(**payload()), httpx.MockTransport(unavailable))
        self.assertEqual(report["error_code"], "connection_failed")
        self.assertNotIn("synthetic-ephemeral-key", json.dumps(report))

    async def test_no_confirmation_means_no_transport(self):
        with self.assertRaises(ValueError):
            await engine.generate(engine.GenerationInput(**payload(confirm_live=False)))


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app("image-quality")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        api.app.state.upstream_transport = httpx.MockTransport(lambda _: httpx.Response(200, json=upstream_image()))

    async def asyncTearDown(self):
        api.app.state.upstream_transport = None
        await self.client.aclose()

    async def test_mount_static_resources_and_platform_registration(self):
        info = (await self.client.get("/api/platform")).json()
        self.assertEqual(info["features"], [{"id": "image-quality", "name": "生图模型质量测试", "url": "/image-quality/"}])
        for url in ["/image-quality/", "/image-quality/assets/app.js", "/image-quality/assets/app.css"]:
            self.assertEqual((await self.client.get(url)).status_code, 200)

    async def test_validation_and_confirmation_never_echo_key(self):
        for body, status in [(payload(confirm_live=False), 400), (payload(model="invalid"), 422)]:
            response = await self.client.post("/image-quality/api/generate", json=body)
            self.assertEqual(response.status_code, status)
            self.assertNotIn(body["api_key"], response.text)

    async def test_actual_api_response_and_no_persistence(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}):
            response = await self.client.post("/image-quality/api/generate", json=payload())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "success")
            self.assertEqual(list(Path(tmp).rglob("*")), [])
            self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_authentication_and_cross_site_guard(self):
        response = await self.client.post("/image-quality/api/generate", json=payload(),
                                          headers={"origin": "https://untrusted.example"})
        self.assertEqual(response.status_code, 403)
        with patch.dict(os.environ, {"PLATFORM_USERNAME": "synthetic", "PLATFORM_PASSWORD": "synthetic-test-password"}):
            secured = create_app("image-quality")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=secured), base_url="http://testserver") as client:
            self.assertEqual((await client.get("/image-quality/")).status_code, 401)

    async def test_disconnect_cancels_operation(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def blocked(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        class Disconnected:
            async def is_disconnected(self):
                await started.wait()
                return True
        with patch.object(api, "generate", blocked):
            with self.assertRaises(api.HTTPException) as error:
                await api.create_image(engine.GenerationInput(**payload()), Disconnected())
        self.assertEqual(error.exception.status_code, 499)
        self.assertTrue(cancelled.is_set())
