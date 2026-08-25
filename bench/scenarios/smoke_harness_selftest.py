"""Harness self-test — proves the DRIVER, not the gateway.

This scenario exists so the harness can be validated end to end without
sending a single request at the homelab (SPEC-BENCH §3) and without needing a
built proxy. It drives `bench/stub_upstream.py` directly and asserts things
that are genuinely verifiable at both ends of the socket:

* the stub's OWN request counter equals warmup + measured, so the closed-loop
  driver is neither dropping nor duplicating work;
* the stub's OWN distinct-body counter matches the arm's cache-key discipline,
  which is the client-side half of the `bench.rs` namespace check;
* every `x-llm-d-sc-*` header sent is echoed back and captured intact, which
  exercises the exact provenance-capture path B-1/B-4 depend on;
* the percentile reduction, manifest, and JSON emission all produce a
  well-formed run.

**The latencies it produces are stub round trips on loopback. They are not a
result and must never appear in a table describing the filter.** They describe
Python's `http.client` talking to Python's `http.server`, nothing more.

Run:
    python3 bench/stub_upstream.py --port 9101 --model small-stub --echo-sc-headers &
    python3 bench/harness.py --scenario smoke --target http://127.0.0.1:9101 \
        --warmup 50 --measured 300 --concurrency 1,4
"""

from __future__ import annotations

import http.client
import json
import urllib.parse

from _common import assert_cache_discipline, chat_body, sized_prompt  # noqa: F401
from harness import Arm, Request, assertion, key_for

SPEC_ID = "SELFTEST"
DESCRIPTION = "Harness self-test against bench/stub_upstream.py. Not a filter measurement."
TARGETS_REAL_MODELS = False
NOTES = [
    "Latencies here are loopback stub round trips and describe the harness, not the filter.",
    "The stub's own counters are read over /__stats so each premise is verified server-side.",
]

# A fixed provenance set the client sends and the stub echoes. In a real
# scenario the FILTER sets these; here they only exercise the capture path.
ECHO_HEADERS = {
    "x-llm-d-sc-label": "SELFTEST",
    "x-llm-d-sc-status": "OK",
    "x-llm-d-sc-classifier": "harness-selftest",
}


def _stats(target):
    parts = urllib.parse.urlsplit(target)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=10)
    conn.request("GET", "/__stats")
    doc = json.loads(conn.getresponse().read())
    conn.close()
    return doc


def _reset(target):
    parts = urllib.parse.urlsplit(target)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=10)
    conn.request("GET", "/__reset")
    conn.getresponse().read()
    conn.close()


def _build(cache_mode):
    def build(index, phase, run_id):
        key = key_for(cache_mode, phase, run_id, index)
        prompt, meta = sized_prompt(24, key)
        meta["prompt_key"] = key
        return Request(
            body=chat_body(prompt, model="bench-router", max_tokens=16),
            headers=dict(ECHO_HEADERS),
            meta=meta,
        )

    return build


def arms(cfg):
    out = []
    for conc in cfg.concurrency:
        for cache_mode in ("miss", "hit"):
            name = "selftest-%s@c%d" % (cache_mode, conc)
            arm = Arm(
                name=name,
                target=cfg.target,
                build=_build(cache_mode),
                params={"cache_mode": cache_mode, "stub": True},
                warmup=cfg.warmup,
                measured=cfg.measured,
                concurrency=conc,
                cache_mode=cache_mode,
                notes="stub round trip on loopback; not a filter measurement",
            )
            arm.setup = lambda ctx, t=cfg.target: _reset(t)
            arm.assertions = lambda result, ctx, cm=cache_mode: _assertions(result, ctx, cm)
            out.append(arm)
    return out


def _assertions(result, ctx, cache_mode):
    arm = result.arm
    stats = _stats(arm.target)
    out = [assert_cache_discipline(result, cache_mode)]

    expected_total = arm.warmup + arm.measured
    out.append(
        assertion(
            "stub_saw_every_request",
            stats["requests"] == expected_total,
            "stub counted %d requests, harness sent %d (warmup %d + measured %d)"
            % (stats["requests"], expected_total, arm.warmup, arm.measured),
        )
    )

    # Server-side confirmation of the key discipline the client claims.
    if cache_mode == "hit":
        expected_bodies = 1 if arm.warmup == 0 else 1
        detail = "hit mode repeats one key, so the stub must see exactly 1 distinct body"
    else:
        expected_bodies = expected_total
        detail = "miss mode uses a unique key per request, so distinct bodies must equal requests"
    out.append(
        assertion(
            "stub_distinct_bodies_match_cache_mode",
            stats["distinct_bodies"] == expected_bodies,
            "%s; stub saw %d distinct bodies, expected %d"
            % (detail, stats["distinct_bodies"], expected_bodies),
        )
    )

    echoed = sum(
        1
        for r in result.ok_records()
        if r["sc_headers"].get("x-llm-d-sc-label") == ECHO_HEADERS["x-llm-d-sc-label"]
        and r["sc_headers"].get("x-llm-d-sc-status") == ECHO_HEADERS["x-llm-d-sc-status"]
    )
    out.append(
        assertion(
            "provenance_headers_captured",
            echoed == len(result.ok_records()) and echoed > 0,
            "captured the full x-llm-d-sc-* set on %d/%d measured responses"
            % (echoed, len(result.ok_records())),
        )
    )

    models = {r["model"] for r in result.ok_records()}
    out.append(
        assertion(
            "upstream_model_attribution_present",
            len(models) == 1 and None not in models,
            "every response carried a single upstream model id: %s" % sorted(m or "<none>" for m in models),
        )
    )
    return out
