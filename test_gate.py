#!/usr/bin/env python3
"""Offline, local mock tests. Never connects to the live OpenAI API."""

import json
import tempfile
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import unittest

from gate import GatewayServer, Meter, Rejected, validate


class FakeOpenAI(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.server.model_requests += 1
        self.server.discovery_auth = self.headers.get("Authorization")
        payload = json.dumps({"object": "list", "data": [
            {"id": "codex-mini-latest"},
            {"id": "gpt-6.1-sol"},
            {"id": "gpt-5.6-terra"},
            {"id": "gpt-6.1-terra"},
            {"id": "gpt-4.1-2025-04-14"},
        ]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        self.server.requests.append((self.path, body))
        # Reproduce what the real API returns for new-generation models.
        if body.get("model", "").startswith(("gpt-5", "gpt-6", "o3")) and "max_tokens" in body:
            payload = json.dumps({
                "error": {
                    "message": "Unsupported parameter: 'max_tokens' is not supported with "
                               "this model. Use 'max_completion_tokens' instead."
                }
            }).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if body.get("stream"):
            if self.path == "/v1/responses":
                events = [
                    'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"Hi"}\n\n',
                    'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":10,"output_tokens":11,"total_tokens":21}}}\n\n',
                ]
            else:
                events = [
                    'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n',
                    'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":11,"total_tokens":20}}\n\n',
                    'data: [DONE]\n\n',
                ]
            payload = "".join(events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
        else:
            payload = json.dumps(
                {"id": "mock", "choices": [{"message": {"content": "Hello"}}],
                 "usage": {"total_tokens": 33}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
        self.upstream.requests = []
        self.upstream.model_requests = 0
        self.upstream.discovery_auth = None
        self.addCleanup(self.upstream.server_close)
        self.mock_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.mock_thread.start()
        self.addCleanup(self.upstream.shutdown)
        self.dbfile = Path(self.temp.name) / "test.db"
        self.meter = Meter(self.dbfile, {"big": 240000, "small": 2400000})
        self.proxy = GatewayServer(
            ("127.0.0.1", 0), self.meter,
            f"http://127.0.0.1:{self.upstream.server_port}",
        )
        self.addCleanup(self.proxy.server_close)
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()
        self.addCleanup(self.proxy.shutdown)
        self.base = f"http://127.0.0.1:{self.proxy.server_port}"

    def post(self, path, body, key="Bearer fake-test-key"):
        request = Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Authorization": key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=3) as resp:
                return resp.status, resp.read()
        except HTTPError as exc:
            with exc:
                return exc.code, exc.read()

    def test_chat_and_responses(self):
        status, content = self.post(
            "/v1/chat/completions",
            {"model": "gpt-5.6-terra", "messages": [{"role": "user", "content": "Hi"}],
             "max_completion_tokens": 80},
        )
        self.assertEqual(status, 200)
        self.assertIn(b"mock", content)
        status, _ = self.post(
            "/v1/responses", {"model": "gpt-6.1-sol", "input": "Hi", "max_output_tokens": 80}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.meter.status()["groups"]["big"]["local_used"], 33)
        self.assertEqual(self.meter.status()["groups"]["small"]["local_used"], 33)
        self.assertEqual(len(self.upstream.requests), 2)

    def test_streaming_usage_is_reclaimed(self):
        status, content = self.post(
            "/v1/chat/completions",
            {"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "Hi"}],
             "max_tokens": 150, "stream": True},
        )
        self.assertEqual(status, 200)
        self.assertIn(b"[DONE]", content)
        self.assertEqual(self.meter.status()["groups"]["small"]["local_used"], 20)
        self.assertTrue(self.upstream.requests[0][1]["stream_options"]["include_usage"])
        self.assertEqual(self.upstream.requests[0][1]["max_completion_tokens"], 150)
        self.assertNotIn("max_tokens", self.upstream.requests[0][1])
        status, content = self.post(
            "/v1/responses",
            {"model": "gpt-6-astra", "input": "Hi", "stream": True,
             "max_output_tokens": 150},
        )
        self.assertEqual(status, 200)
        self.assertIn(b"response.completed", content)
        self.assertEqual(self.meter.status()["groups"]["big"]["local_used"], 21)

    def test_unlisted_and_chargeable_requests_blocked(self):
        attempts = [
            ("/v1/responses", {"model": "gpt-6.1-terra", "input": "Hi"}),
            ("/v1/chat/completions", {"model": "codex-mini-latest", "messages": [
                {"role": "user", "content": "Hi"}
            ]}),
            ("/v1/responses", {"model": "gpt-6.1-sol", "input": "Hi",
                                "tools": [{"type": "web_search"}]}),
            ("/v1/responses", {"model": "gpt-6.1-sol",
                                "input": [{"role": "user", "content": [
                                    {"type": "input_image", "image_url": "http://example.com"}
                                ]}]}),
            ("/v1/responses", {"model": "gpt-6.1-sol", "input": "Hi",
                                "service_tier": "priority"}),
            ("/v1/images/generations", {"model": "gpt-image-1", "prompt": "Hi"}),
        ]
        for path, body in attempts:
            with self.subTest(body=body):
                status, _ = self.post(path, body)
                self.assertIn(status, (400, 404))
        self.assertEqual(self.upstream.requests, [])

    def test_quota_blocks_next_request_before_network(self):
        body = {"model": "gpt-6.1-sol", "input": "Hi", "max_output_tokens": 80}
        group, prediction = validate("/v1/responses", dict(body))
        self.meter.limits[group] = prediction + 25
        first, _ = self.post("/v1/responses", body)
        second, data = self.post("/v1/responses", body)
        self.assertEqual(first, 200)
        self.assertEqual(second, 429)
        self.assertIn(b"No upstream request", data)
        self.assertEqual(len(self.upstream.requests), 1)

    def test_meter_utc_days_and_concurrent_reservation(self):
        amount = 200
        day = "2026-10-29"
        self.meter.limits["big"] = 250
        successes = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            try:
                self.meter.reserve(day, "big", amount)
                successes.append(True)
            except Rejected:
                successes.append(False)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertCountEqual(successes, [True, False])
        self.meter.settle(day, "big", amount, 42)
        self.assertEqual(self.meter.status(day)["groups"]["big"]["local_used"], 42)
        self.assertEqual(self.meter.status("2026-10-30")["groups"]["big"]["local_used"], 0)
        self.assertEqual(self.meter.status(day)["groups"]["big"]["in_flight_reserved"], 0)

    def test_auth_required_without_upstream_call(self):
        status, _ = self.post(
            "/v1/responses", {"model": "gpt-6.1-sol", "input": "Hi"}, key="not-a-key"
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.upstream.requests, [])

    def test_models_endpoint_is_real_accessible_allowlist_preferred_first(self):
        with urlopen(Request(
            self.base + "/v1/models",
            headers={"Authorization": "Bearer fake-test-key"}
        ), timeout=3) as resp:
            data = json.load(resp)
        rows = [model["id"] for model in data["data"]]
        names = set(rows)
        self.assertEqual(rows[0], "gpt-5.6-terra")
        self.assertIn("gpt-6.1-sol", names)
        self.assertIn("gpt-5.6-terra", names)
        self.assertNotIn("gpt-6.1-terra", names)
        self.assertNotIn("codex-mini-latest", names)
        self.assertNotIn("gpt-5.4-2026-03-05", names)
        self.assertEqual(self.upstream.discovery_auth, "Bearer fake-test-key")
        self.assertEqual(self.upstream.model_requests, 1)
        self.assertEqual(self.upstream.requests, [])

    def test_models_require_key(self):
        try:
            with urlopen(self.base + "/v1/models", timeout=3):
                self.fail("Model discovery without auth must fail")
        except HTTPError as exc:
            self.assertEqual(exc.code, 401)
            exc.close()
        self.assertEqual(self.upstream.model_requests, 0)

    def test_mixdog_auto_discovery_connection_test_chooses_live_model(self):
        with urlopen(Request(
            self.base + "/v1/models",
            headers={"Authorization": "Bearer fake-test-key"}
        ), timeout=3) as resp:
            first_model = json.load(resp)["data"][0]["id"]
        status, _ = self.post("/v1/chat/completions", {
            "model": first_model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 16,
        })
        self.assertEqual(status, 200)
        self.assertEqual(first_model, "gpt-5.6-terra")
        self.assertEqual(self.upstream.requests[0][1]["model"], first_model)
        self.assertEqual(self.upstream.requests[0][1]["max_completion_tokens"], 16)
        self.assertNotIn("max_tokens", self.upstream.requests[0][1])

    def test_legacy_chat_model_keeps_legacy_limit(self):
        status, _ = self.post("/v1/chat/completions", {
            "model": "gpt-4o-2024-11-20",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 80,
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.upstream.requests[0][1]["max_tokens"], 80)

    def test_conflicting_chat_limits_rejected_without_upstream_request(self):
        status, _ = self.post("/v1/chat/completions", {
            "model": "gpt-5.6-terra",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 100,
            "max_completion_tokens": 100,
        })
        self.assertEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])


if __name__ == "__main__":
    unittest.main()
