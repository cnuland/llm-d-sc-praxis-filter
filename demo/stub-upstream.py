#!/usr/bin/env python3
"""Stub model backend for the llm_d_sc Praxis filter demo.

Stands in for a real inference server. It does no inference: it answers every
request with 200 and a JSON body naming itself, and echoes back any
``x-llm-d-sc-*`` request headers it received. That echo is the whole point --
it is how the demo proves the filter's provenance headers actually reached the
upstream that the filter selected.

Python 3 standard library only, so it runs on a stock macOS/Linux box with no
install step.

Usage:
    ./stub-upstream.py --name small --port 9101
    ./stub-upstream.py --name large --port 9102 --host 127.0.0.1

Endpoints:
    GET /__health   readiness probe -> {"served_by": ..., "status": "ok"}
    * (anything)    -> 200 with the identity + echo body described above

Exit: SIGTERM/SIGINT shut the server down cleanly.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Request headers whose values are echoed back to the caller. The filter is
# specified to emit exactly this family (SPEC 4.7).
PROVENANCE_PREFIX = "x-llm-d-sc-"

# Guard against a client that lies about Content-Length or streams forever.
MAX_BODY_BYTES = 8 * 1024 * 1024

# Populated from argv in main(); read by the handler class.
SERVER_NAME = "stub"
SERVER_PORT = 0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class StubHandler(BaseHTTPRequestHandler):
    """Answers every method and path the same way."""

    # HTTP/1.1 + explicit Content-Length so the proxy can keep the upstream
    # connection alive. Under HTTP/1.0 every request would cost a new socket
    # and the demo's connection-reuse story would be untestable.
    protocol_version = "HTTP/1.1"
    server_version = "llm-d-sc-demo-stub"
    sys_version = ""

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        """Replace BaseHTTPRequestHandler's noisy default access log."""
        sys.stderr.write("%s [%s] %s\n" % (_now(), SERVER_NAME, fmt % args))
        sys.stderr.flush()

    def _read_body(self) -> bytes:
        """Drain the request body for both Content-Length and chunked framing.

        Not draining it would leave bytes in the socket and desynchronise the
        next keep-alive request on the same connection.
        """
        encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in encoding:
            return self._read_chunked()

        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return b""
        try:
            length = int(raw_len)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        return self.rfile.read(min(length, MAX_BODY_BYTES))

    def _read_chunked(self) -> bytes:
        chunks = []
        total = 0
        while True:
            line = self.rfile.readline(64)
            if not line:
                break
            try:
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
            except ValueError:
                break
            if size == 0:
                # Consume the trailer section up to the blank line.
                while True:
                    trailer = self.rfile.readline(1024)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break
            total += size
            if total > MAX_BODY_BYTES:
                break
            chunks.append(self.rfile.read(size))
            self.rfile.read(2)  # trailing CRLF
        return b"".join(chunks)

    def _provenance(self) -> "dict[str, str]":
        found = {}
        for key, value in self.headers.items():
            lowered = key.lower()
            if lowered.startswith(PROVENANCE_PREFIX):
                found[lowered] = value
        return found

    def _respond(self, payload: dict, provenance: "dict[str, str]") -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Served-By", SERVER_NAME)
        # Echo the provenance headers onto the response too, so `curl -D-`
        # shows them without needing to parse the body.
        for key, value in provenance.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    # -- request handling -------------------------------------------------

    def _handle(self) -> None:
        body = self._read_body()
        provenance = self._provenance()

        if self.path == "/__health":
            self._respond(
                {"served_by": SERVER_NAME, "port": SERVER_PORT, "status": "ok"},
                {},
            )
            return

        payload = {
            "served_by": SERVER_NAME,
            "port": SERVER_PORT,
            "method": self.command,
            "path": self.path,
            "received_bytes": len(body),
            "request_id": self.headers.get("X-Request-ID", ""),
            "llm_d_sc_headers": provenance,
        }
        self._respond(payload, provenance)

        summary = " ".join("%s=%s" % kv for kv in sorted(provenance.items())) or "(none)"
        self.log_message("%s %s -> 200 provenance: %s", self.command, self.path, summary)

    # Every verb the demo (or a curious operator) might send.
    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle


def main() -> int:
    global SERVER_NAME, SERVER_PORT

    parser = argparse.ArgumentParser(
        description="Stub upstream that identifies itself and echoes x-llm-d-sc-* headers."
    )
    parser.add_argument("--name", required=True, help='identity reported as "served_by" (e.g. small)')
    parser.add_argument("--port", required=True, type=int, help="TCP port to bind")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    args = parser.parse_args()

    SERVER_NAME = args.name
    SERVER_PORT = args.port

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), StubHandler)
    except OSError as exc:
        sys.stderr.write(
            "stub-upstream '%s': cannot bind %s:%d: %s\n"
            "  Something else is already listening. Find it with:\n"
            "    lsof -nP -iTCP:%d -sTCP:LISTEN\n" % (args.name, args.host, args.port, exc, args.port)
        )
        return 1

    httpd.daemon_threads = True

    def shutdown(_signum, _frame):
        # shutdown() must not run on the serving thread or it deadlocks.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    sys.stderr.write("%s [%s] listening on http://%s:%d\n" % (_now(), args.name, args.host, args.port))
    sys.stderr.flush()
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()
    sys.stderr.write("%s [%s] stopped\n" % (_now(), args.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
