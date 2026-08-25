#!/usr/bin/env python3
"""bench/stub_upstream.py — an instant-return OpenAI-shaped upstream for the
stub-driven benchmark scenarios (B-1, B-2, B-3, B-6).

Why a stub at all: SPEC-BENCH §3 forbids pointing high-concurrency load at the
homelab's single-replica llama.cpp servers, and SPEC-BENCH §1 B-1 requires an
upstream that "returns instantly (so the backend contributes no variance)". A
real model backend would make the filter-overhead delta unmeasurable — its own
variance is three orders of magnitude larger than the thing being measured.

This server is intentionally boring:

* returns a fixed, OpenAI-shaped `chat.completion` body with a configurable
  `model` id, so the harness can attribute a response to a backend exactly the
  way it does against the real cluster (SPEC-K8S §3.1: the upstream's own
  `model` field is the attribution source);
* `--echo-sc-headers` copies every received `x-llm-d-sc-*` request header into
  the response, which is how the harness observes what the FILTER decided —
  the filter sets those headers on the upstream request, so without an echo
  the client never sees them;
* `--delay-ms` adds a fixed artificial delay, for scenarios that need a
  non-zero upstream cost;
* `/__stats` exposes the stub's OWN counters (requests served, distinct request
  bodies seen, per-header tallies). This exists so a scenario can prove its
  premise SERVER-SIDE rather than trusting the client, mirroring the way
  `llm-d-sc/src/bench.rs` asserts against the service's cache counters instead
  of assuming its own key discipline held.

Modes other than the OpenAI stub:

* `--mode tcp-blackhole` — accepts TCP connections and then never speaks. Used
  by B-6 "classifier slow": the filter's tonic channel connects, the HTTP/2
  handshake never completes, and the classify RPC hits `timeout_ms`. This is a
  timeout that is genuinely produced by the network, not simulated by config.
* `--mode tcp-refuse` is not implemented and does not need to be: "classifier
  down" is an unbound port, which needs no server at all.

Python 3 standard library only — this must run inside a minimal container.

Examples:
    python3 bench/stub_upstream.py --port 9001 --model small-stub --echo-sc-headers
    python3 bench/stub_upstream.py --port 9002 --model large-stub --delay-ms 50
    python3 bench/stub_upstream.py --mode tcp-blackhole --port 50099
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SC_PREFIX = "x-llm-d-sc-"


class Stats:
    """The stub's own view of what it was asked to do.

    Deliberately server-side: a scenario that asserts "the harness sent exactly
    N measured requests with N distinct bodies" is only meaningful if the count
    comes from the other end of the socket.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.requests = 0
        self.by_path = {}
        self.distinct_body_hashes = set()
        self.sc_header_values = {}
        self.total_request_bytes = 0
        self.started_unix = time.time()

    def record(self, path, body, sc_headers):
        digest = hashlib.blake2b(body, digest_size=16).hexdigest()
        with self._lock:
            self.requests += 1
            self.by_path[path] = self.by_path.get(path, 0) + 1
            self.distinct_body_hashes.add(digest)
            self.total_request_bytes += len(body)
            for name, value in sc_headers.items():
                bucket = self.sc_header_values.setdefault(name, {})
                bucket[value] = bucket.get(value, 0) + 1

    def snapshot(self):
        with self._lock:
            return {
                "requests": self.requests,
                "by_path": dict(self.by_path),
                "distinct_bodies": len(self.distinct_body_hashes),
                "total_request_bytes": self.total_request_bytes,
                "sc_header_values": {k: dict(v) for k, v in self.sc_header_values.items()},
                "uptime_s": round(time.time() - self.started_unix, 3),
            }

    def reset(self):
        with self._lock:
            self.requests = 0
            self.by_path = {}
            self.distinct_body_hashes = set()
            self.sc_header_values = {}
            self.total_request_bytes = 0
            self.started_unix = time.time()


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: the harness reuses connections
    server_version = "llm-d-sc-bench-stub/1"
    sys_version = ""

    # Injected by make_server().
    stats: Stats = None
    model_id: str = "stub-model"
    delay_ms: float = 0.0
    echo_sc: bool = False
    completion_text: str = "stub"
    completion_tokens: int = 1
    quiet: bool = True

    def log_message(self, fmt, *args):  # noqa: D102 - silence per-request logging
        if not self.quiet:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---- helpers ---------------------------------------------------------

    def _sc_headers(self):
        return {
            k.lower(): v
            for k, v in self.headers.items()
            if k.lower().startswith(SC_PREFIX)
        }

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        try:
            return self.rfile.read(int(length))
        except (ValueError, OSError):
            return b""

    def _send_json(self, payload, extra_headers=None, status=200):
        blob = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(blob)

    # ---- routes ----------------------------------------------------------

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/__stats":
            self._send_json(self.stats.snapshot())
            return
        if self.path == "/__reset":
            self.stats.reset()
            self._send_json({"reset": True})
            return
        if self.path in ("/v1/models", "/models"):
            self._send_json(
                {
                    "object": "list",
                    "data": [{"id": self.model_id, "object": "model", "owned_by": "bench-stub"}],
                }
            )
            return
        if self.path in ("/healthz", "/health"):
            self._send_json({"status": "ok"})
            return
        self._send_json({"error": {"message": "not found", "path": self.path}}, status=404)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        body = self._read_body()
        sc = self._sc_headers()
        self.stats.record(self.path, body, sc)

        if self.delay_ms:
            time.sleep(self.delay_ms / 1000.0)

        extra = {}
        if self.echo_sc:
            # The filter sets x-llm-d-sc-* on the UPSTREAM request. Echoing them
            # back is the only way a client-side harness can observe the routing
            # decision without reading the proxy's access log.
            for name, value in sc.items():
                extra[name] = value
            extra["x-llm-d-sc-echo"] = str(len(sc))

        now = int(time.time())
        payload = {
            "id": "chatcmpl-bench-%d" % self.stats.requests,
            "object": "chat.completion",
            "created": now,
            "model": self.model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self.completion_text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": max(1, len(body) // 4),
                "completion_tokens": self.completion_tokens,
                "total_tokens": max(1, len(body) // 4) + self.completion_tokens,
            },
        }
        self._send_json(payload, extra_headers=extra)


def make_server(host, port, model_id, delay_ms, echo_sc, completion_text, quiet):
    stats = Stats()

    class Handler(StubHandler):
        pass

    Handler.stats = stats
    Handler.model_id = model_id
    Handler.delay_ms = delay_ms
    Handler.echo_sc = echo_sc
    Handler.completion_text = completion_text
    Handler.completion_tokens = max(1, len(completion_text.split()))
    Handler.quiet = quiet

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = Server((host, port), Handler)
    return httpd, stats


class BlackholeHandler(socketserver.BaseRequestHandler):
    """Accept the connection and say nothing, forever (until the client gives up).

    B-6 "classifier slow" needs a peer that completes the TCP handshake and
    then stalls, so the filter's own `timeout_ms` is what ends the request. A
    sleeping HTTP server would not do: the filter speaks gRPC over h2c, so the
    stall has to happen at or before the HTTP/2 preface.
    """

    def handle(self):
        try:
            self.request.settimeout(None)
            while True:
                data = self.request.recv(4096)
                if not data:
                    return
        except OSError:
            return


def run_blackhole(host, port):
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = Server((host, port), BlackholeHandler)
    print(f"tcp-blackhole listening on {host}:{port} (accepts, never replies)", flush=True)
    srv.serve_forever()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--mode", choices=("openai", "tcp-blackhole"), default="openai")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9001)
    p.add_argument("--model", default="stub-model", help="the id reported in the response 'model' field")
    p.add_argument("--delay-ms", type=float, default=0.0, help="fixed artificial upstream delay")
    p.add_argument(
        "--echo-sc-headers",
        action="store_true",
        help="copy received x-llm-d-sc-* request headers into the response",
    )
    p.add_argument("--completion", default="ok", help="the assistant content returned")
    p.add_argument("--verbose", action="store_true", help="log every request")
    args = p.parse_args(argv)

    if args.mode == "tcp-blackhole":
        run_blackhole(args.host, args.port)
        return 0

    httpd, _stats = make_server(
        args.host, args.port, args.model, args.delay_ms, args.echo_sc_headers,
        args.completion, not args.verbose,
    )
    print(
        f"stub upstream listening on http://{args.host}:{args.port} "
        f"model={args.model} delay_ms={args.delay_ms} echo_sc={args.echo_sc_headers}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
