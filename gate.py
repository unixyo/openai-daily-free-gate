#!/usr/bin/env python3
"""Local, fail-closed text API gateway for OpenAI's daily data-sharing token grant.

Supports text-only Chat Completions and Responses requests, including streaming.
This gateway measures ONLY requests routed through itself. It cannot guarantee
that other API clients in the same organization have not used the shared grant.
"""

import argparse
from contextlib import contextmanager
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# Only model IDs explicitly included in the official complimentary-token list
# AND not already shut down. The help article still lists retired snapshots,
# so a second check against the caller's actual /models is essential.
BIG_MODELS = frozenset("""
gpt-6.1-sol gpt-6-astra gpt-6-sol gpt-6-luna gpt-5.6-sol
gpt-5.5-2026-04-23 gpt-5.4-2026-03-05 gpt-5.2-2025-12-11
gpt-5.1-2025-11-13 gpt-5-2025-08-07 gpt-4.1-2025-04-14
gpt-4o-2024-05-13 gpt-4o-2024-08-06 gpt-4o-2024-11-20
o3-2025-04-16
""".split())
SMALL_MODELS = frozenset("""
gpt-5.6-terra gpt-5.6-luna gpt-5.4-mini-2026-03-17
gpt-5.4-nano-2026-03-17
gpt-5-mini-2025-08-07 gpt-5-nano-2025-08-07
gpt-4.1-mini-2025-04-14 gpt-4o-mini-2024-07-18
""".split())

# Mixdog's connection test sends a real text request to the first discovered
# model. Prefer an efficient, currently documented model to avoid testing a
# retired snapshot simply because it sorts first alphabetically.
PREFERRED_MODELS = (
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-6.1-sol",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-5.4-mini-2026-03-17",
    "gpt-6-astra",
)

DEFAULT_CAPS = {"big": 240_000, "small": 2_400_000}
MAX_OUTPUT = 8192
DEFAULT_OUTPUT = 2048
MAX_REQUEST_BYTES = 2_000_000
INPUT_HEADROOM = 2048


def utc_day():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


class Rejected(Exception):
    def __init__(self, reason, status=400):
        super().__init__(reason)
        self.status = status


class Meter:
    def __init__(self, filename, limits=None):
        self.filename = str(filename)
        self.limits = dict(DEFAULT_CAPS if limits is None else limits)
        Path(self.filename).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS usage ("
                "day TEXT NOT NULL, grp TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0, "
                "reserved INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY(day, grp))"
            )

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.filename, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def reserve(self, day, group, amount):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO usage(day,grp) VALUES(?,?)", (day, group))
            used, reserved = conn.execute(
                "SELECT used,reserved FROM usage WHERE day=? AND grp=?",
                (day, group),
            ).fetchone()
            if used + reserved + amount > self.limits[group]:
                raise Rejected(
                    f"{group} daily budget exhausted: used={used}, in_flight={reserved}, "
                    f"requested_reservation={amount}, cap={self.limits[group]}. "
                    "No upstream request was sent.",
                    429,
                )
            conn.execute(
                "UPDATE usage SET reserved=reserved+? WHERE day=? AND grp=?",
                (amount, day, group),
            )

    def settle(self, day, group, reserved, actual):
        # On an interrupted/unknown response, charge the full reservation.
        if actual is None:
            actual = reserved
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE usage SET reserved=reserved-?,used=used+? "
                "WHERE day=? AND grp=? AND reserved>=?",
                (reserved, actual, day, group, reserved),
            )
            if conn.total_changes != 1:
                raise RuntimeError("reservation bookkeeping is inconsistent")

    def status(self, day=None):
        day = day or utc_day()
        with self.connection() as conn:
            rows = dict(
                (group, (used, reserved))
                for group, used, reserved in conn.execute(
                    "SELECT grp,used,reserved FROM usage WHERE day=?", (day,)
                )
            )
        return {
            "utc_day": day,
            "reset": "00:00 UTC / 09:00 Asia/Seoul",
            "groups": {
                group: {
                    "local_used": rows.get(group, (0, 0))[0],
                    "in_flight_reserved": rows.get(group, (0, 0))[1],
                    "local_cap": cap,
                    "available_for_reservation": max(
                        0, cap - sum(rows.get(group, (0, 0)))
                    ),
                }
                for group, cap in self.limits.items()
            },
            "note": "Local gateway traffic only; OpenAI organization usage may be higher.",
        }


def text_parts(value, allow_array=False):
    if isinstance(value, str):
        return
    if allow_array and isinstance(value, list):
        for part in value:
            if not isinstance(part, dict):
                raise Rejected("Only text content is supported")
            if part.get("type") not in ("text", "input_text", "output_text"):
                raise Rejected("Images, audio, files and non-text content are blocked")
            if not isinstance(part.get("text"), str):
                raise Rejected("A text part must contain a text string")
        return
    raise Rejected("Only text content is supported")


def validate(path, body):
    if not isinstance(body, dict):
        raise Rejected("Expected a JSON object")
    model = body.get("model")
    if model in BIG_MODELS:
        group = "big"
    elif model in SMALL_MODELS:
        group = "small"
    else:
        raise Rejected(f"Model {model!r} is not on the eligible allowlist")

    # Tool and other endpoints can generate billable charges outside the grant.
    blocked_keys = (
        "tools", "tool_choice", "functions", "function_call",
        "web_search_options", "modalities", "audio", "prediction",
    )
    for key in blocked_keys:
        if body.get(key):
            raise Rejected(f"{key} is not supported by the free-only gateway")
    if body.get("service_tier") not in (None, "auto", "default"):
        raise Rejected("Non-default paid service tiers are blocked")
    if body.get("background") or body.get("conversation") or body.get("previous_response_id"):
        raise Rejected("Background or stored conversations cannot be cost-checked")
    if body.get("n", 1) != 1:
        raise Rejected("Multiple completions per request are blocked")
    if body.get("best_of", 1) != 1:
        raise Rejected("Multiple completion candidates are blocked")

    if path == "/v1/responses":
        inp = body.get("input")
        if isinstance(inp, str):
            pass
        elif isinstance(inp, list) and inp:
            for msg in inp:
                if not isinstance(msg, dict):
                    raise Rejected("Input items must be text messages")
                if msg.get("role") not in ("user", "assistant", "system", "developer"):
                    raise Rejected("Only ordinary text message roles are allowed")
                if msg.get("type") not in (None, "message"):
                    raise Rejected("Only text messages are allowed")
                text_parts(msg.get("content"), allow_array=True)
        else:
            raise Rejected("A text input is required")
        if "instructions" in body and not isinstance(body["instructions"], str):
            raise Rejected("Instructions must be plain text")
        cap_name = "max_output_tokens"
        if "max_completion_tokens" in body or "max_tokens" in body:
            raise Rejected("Use max_output_tokens for Responses")
    elif path == "/v1/chat/completions":
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise Rejected("A nonempty messages list is required")
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") not in (
                "user", "assistant", "system", "developer"
            ):
                raise Rejected("Only ordinary text message roles are allowed")
            if msg.get("tool_calls") or msg.get("function_call"):
                raise Rejected("Tool calls are blocked")
            text_parts(msg.get("content"), allow_array=True)
        cap_name = (
            "max_completion_tokens"
            if "max_tokens" not in body
            else "max_tokens"
        )
        if "max_output_tokens" in body or (
            "max_completion_tokens" in body and "max_tokens" in body
        ):
            raise Rejected("Use one Chat Completions output limit")
    else:
        raise Rejected("Only Responses and Chat Completions are allowed", 404)

    cap = body.get(cap_name, DEFAULT_OUTPUT)
    if type(cap) is not int or not 1 <= cap <= MAX_OUTPUT:
        raise Rejected(f"{cap_name} must be an integer from 1 to {MAX_OUTPUT}")
    body[cap_name] = cap
    # Mixdog's OpenAI Chat Completions compatibility layer sends max_tokens
    # even for GPT-5/6 models. The modern OpenAI endpoint rejects that field.
    # Translate it before forwarding, without changing the reserved budget.
    if (path == "/v1/chat/completions" and cap_name == "max_tokens"
            and model.startswith(("gpt-5", "gpt-6", "o3"))):
        body["max_completion_tokens"] = body.pop("max_tokens")
    if not isinstance(body.get("stream", False), bool):
        raise Rejected("stream must be a boolean")
    if path == "/v1/chat/completions" and body.get("stream"):
        opts = body.get("stream_options") or {}
        if not isinstance(opts, dict):
            raise Rejected("stream_options must be an object")
        opts["include_usage"] = True
        body["stream_options"] = opts

    # Plain text tokenizer upper-bound estimate: UTF-8 bytes + generous overhead.
    # This deliberately overestimates input cost to avoid crossing the grant.
    sent = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    input_budget = len(sent) + INPUT_HEADROOM
    return group, input_budget + cap


def reported_usage(obj):
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if type(total) is int and total >= 0:
        return total
    a = usage.get("input_tokens", usage.get("prompt_tokens"))
    b = usage.get("output_tokens", usage.get("completion_tokens"))
    if type(a) is int and type(b) is int and a >= 0 and b >= 0:
        return a + b
    return None


def stream_usage(lines, old):
    # Both Responses and Chat Completions send SSE 'data: {json}' entries.
    data = "".join(line[5:].strip() for line in lines if line.startswith("data:"))
    if not data or data == "[DONE]":
        return old
    try:
        event = json.loads(data)
    except ValueError:
        return old
    found = reported_usage(event)
    if found is None and isinstance(event.get("response"), dict):
        found = reported_usage(event["response"])
    return found if found is not None else old


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalFreeGate/1.0"

    def log_message(self, format, *args):
        # No raw requests, credentials, prompts or upstream error bodies in logs.
        pass

    def send_data(self, status, body, content_type="application/json"):
        payload = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if not isinstance(body, bytes) else body
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self.send_data(200, {"ok": True, "mode": "free-text-only"})
        elif self.path == "/status":
            self.send_data(200, self.server.meter.status())
        elif self.path == "/v1/models":
            try:
                self.send_available_models()
            except Rejected as err:
                self.send_data(err.status, {"error": {"message": str(err), "type": "free_quota_gate"}})
        else:
            self.send_data(404, {"error": "Not found"})

    def send_available_models(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) < 15:
            raise Rejected("A client-supplied OpenAI API key is required to discover models", 401)
        # Model listing has no token-generation charge. Do not accept a
        # hardcoded fake catalog; discover the caller's actual available IDs.
        request = Request(
            self.server.upstream.rstrip("/") + "/v1/models",
            headers={"Authorization": auth, "Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=20) as upstream:
                listed = json.load(upstream)
        except HTTPError as err:
            # Never echo an upstream body that might contain account details.
            raise Rejected(f"OpenAI model discovery returned HTTP {err.code}", err.code)
        except (URLError, TimeoutError, ValueError):
            raise Rejected("OpenAI model discovery failed; no models were advertised", 502)
        if not isinstance(listed, dict) or not isinstance(listed.get("data"), list):
            raise Rejected("OpenAI model discovery returned an unexpected response", 502)
        eligible = BIG_MODELS | SMALL_MODELS
        actual = {
            item["id"]
            for item in listed["data"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and item["id"] in eligible
        }
        models = [m for m in PREFERRED_MODELS if m in actual]
        models.extend(sorted(actual.difference(models)))
        self.send_data(
            200,
            {"object": "list", "data": [
                {"id": model, "object": "model", "created": 0, "owned_by": "openai"}
                for model in models
            ]},
        )

    def do_POST(self):
        try:
            self.handle_request()
        except Rejected as err:
            self.send_data(err.status, {"error": {"message": str(err), "type": "free_quota_gate"}})
        except (BrokenPipeError, ConnectionResetError):
            # A disconnected streaming caller might still have consumed tokens.
            pass
        except Exception:
            self.send_data(502, {"error": {"message": "Local gateway error; no details logged"}})

    def handle_request(self):
        if self.path not in ("/v1/responses", "/v1/chat/completions"):
            raise Rejected("Only text generation is available through this gateway", 404)
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) < 15:
            raise Rejected("A client-supplied OpenAI API key is required", 401)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise Rejected("Invalid content length")
        if not 0 < length <= MAX_REQUEST_BYTES:
            raise Rejected("Request body is empty or exceeds the size limit", 413)
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise Rejected("Only application/json is accepted")
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            raise Rejected("Invalid JSON body")
        group, reservation = validate(self.path, body)
        day = utc_day()
        self.server.meter.reserve(day, group, reservation)
        actual = None
        release = False
        settled = False
        try:
            upstream = self.server.upstream.rstrip("/") + self.path
            headers = {
                "Authorization": auth,
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if body.get("stream") else "application/json",
            }
            request = Request(
                upstream,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                reply = urlopen(request, timeout=self.server.timeout_seconds)
            except HTTPError as err:
                # Rejected upstream requests should not produce model usage.
                release = err.code < 500
                self.server.meter.settle(day, group, reservation, 0 if release else None)
                settled = True
                with err:
                    self.send_data(
                        err.code,
                        err.read(50000),
                        err.headers.get("Content-Type", "application/json"),
                    )
                return
            except URLError:
                self.send_data(502, {"error": "Upstream connection failed"})
                return
            with reply:
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    lines = []
                    for line in reply:
                        text = line.decode("utf-8", "replace").strip()
                        if not text:
                            actual = stream_usage(lines, actual)
                            lines = []
                        else:
                            lines.append(text)
                        self.wfile.write(("%x\r\n" % len(line)).encode("ascii") + line + b"\r\n")
                        self.wfile.flush()
                    if lines:
                        actual = stream_usage(lines, actual)
                    self.server.meter.settle(day, group, reservation, actual)
                    settled = True
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                else:
                    payload = reply.read()
                    try:
                        actual = reported_usage(json.loads(payload))
                    except ValueError:
                        pass
                    self.server.meter.settle(day, group, reservation, actual)
                    settled = True
                    self.send_data(
                        200, payload, reply.headers.get("Content-Type", "application/json")
                    )
        finally:
            if not settled:
                self.server.meter.settle(
                    day, group, reservation, 0 if release else actual
                )


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, meter, upstream, timeout_seconds=180):
        super().__init__(address, GatewayHandler)
        self.meter = meter
        self.upstream = upstream
        self.timeout_seconds = timeout_seconds


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local fail-closed OpenAI free-token gateway")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--db", default=str(Path(__file__).with_name(".state") / "usage.sqlite3"))
    parser.add_argument("--big-cap", type=int, default=DEFAULT_CAPS["big"])
    parser.add_argument("--small-cap", type=int, default=DEFAULT_CAPS["small"])
    args = parser.parse_args(argv)
    if not 0 < args.big_cap <= 250000 or not 0 < args.small_cap <= 2500000:
        parser.error("Caps must be positive and not exceed the Build daily grants")
    meter = Meter(args.db, {"big": args.big_cap, "small": args.small_cap})
    server = GatewayServer(
        ("127.0.0.1", args.port),
        meter,
        os.environ.get("GATE_UPSTREAM", "https://api.openai.com"),
    )
    print(f"Free-text-only API gate at http://127.0.0.1:{server.server_port}/v1", flush=True)
    print(f"Today (UTC) limits: big={args.big_cap}, small={args.small_cap}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
