"""B-6 — Degradation and failure (SPEC-BENCH §1).

*What happens to the gateway when the classifier is not there?* SPEC §4.6's
default posture is fail-open: a classifier outage degrades routing quality, it
does not take the gateway down. That is a claim, and this scenario is where it
either survives contact or does not.

| Case | Setup | Assert |
|---|---|---|
| `classifier-down` | filter's `endpoint` points at an unbound port | 100% HTTP 200 via `general`; **added latency is the connect-refused cost, not the full `timeout_ms`** |
| `classifier-slow` | filter's `endpoint` points at `stub_upstream.py --mode tcp-blackhole` | added latency ~= `timeout_ms` and no more; 100% 200 |
| `queue-exhausted` | drive past llm-d-sc's bounded queue (256) to force `RESOURCE_EXHAUSTED` | fail-open holds; the exhausted responses are counted |
| `fail-closed` | a listener configured `on_resource_exhausted: reject` | clean 503s, no hangs, no 5xx storm |

**Why the `classifier-down` latency figure is the one that matters.** A
fail-open path that still waits the full `timeout_ms` on every request is a
fail-open path that has already taken the gateway down — it converts a
classifier outage into a 100 ms tax on every single request. The assertion here
is that the down case costs far LESS than `timeout_ms`, and it is a hard
failure if it does not.

The slow case uses a TCP black hole rather than a sleeping HTTP server on
purpose: the filter speaks gRPC over h2c, so the stall has to happen at or
before the HTTP/2 preface for the filter's own `timeout_ms` to be the thing
that ends the request.

Each case needs its own Praxis listener (they differ only in filter config), so
each is supplied as a parameter and any that is absent is simply skipped:

    python3 bench/harness.py --scenario b6 \\
        --param down_url=http://127.0.0.1:8091 \\
        --param slow_url=http://127.0.0.1:8092 \\
        --param exhausted_url=http://127.0.0.1:8093 \\
        --param reject_url=http://127.0.0.1:8094 \\
        --param timeout_ms=100 --warmup 50 --measured 300
"""

from __future__ import annotations

from _common import make_builder, sc_header
from harness import Arm, assertion

SPEC_ID = "B-6"
DESCRIPTION = "Degradation and failure: classifier down, classifier slow, queue exhausted, fail-closed."
TARGETS_REAL_MODELS = False
NOTES = [
    "Upstreams are stubs. Only the classifier path is broken; the model path is not involved.",
    "'classifier down' is an unbound port and needs no server at all; 'classifier slow' is "
    "bench/stub_upstream.py --mode tcp-blackhole, which accepts TCP and then never speaks.",
    "The queue-exhausted case requires a REAL local llm-d-sc so the bounded queue exists to "
    "be exhausted; a stub cannot produce a genuine RESOURCE_EXHAUSTED.",
]

# A fail-open connect-refused path should cost a small fraction of the timeout
# budget. This is the assertion that separates "degraded" from "already down".
DOWN_LATENCY_FRACTION_OF_TIMEOUT = 0.5
# The slow case should land near timeout_ms: at least this fraction of it (or
# the timeout is not what ended the request) and no more than this multiple.
SLOW_MIN_FRACTION = 0.8
SLOW_MAX_MULTIPLE = 1.5


def _status_tally(records):
    tally = {}
    for r in records:
        s = sc_header(r, "status") or "<absent>"
        tally[s] = tally.get(s, 0) + 1
    return tally


def _all_ok(result):
    records = result.records
    two_hundreds = sum(1 for r in records if r["status"] == 200)
    return assertion(
        "fail_open_returned_200_for_every_request",
        two_hundreds == len(records) and len(records) > 0,
        "%d/%d requests returned HTTP 200 while the classifier was unusable; status tally %s"
        % (two_hundreds, len(records), result.status_counts),
    )


def _routed_to_default(result, expected_statuses):
    tally = _status_tally(result.ok_records())
    seen = sum(tally.get(s, 0) for s in expected_statuses)
    return assertion(
        "degraded_path_recorded_its_reason",
        seen == len(result.ok_records()) and seen > 0,
        "x-llm-d-sc-status tally %s; expected every request to record one of %s and route to "
        "default_cluster per SPEC §4.6" % (tally, list(expected_statuses)),
    )


def arms(cfg):
    timeout_ms = cfg.param("timeout_ms", 100, float)
    conc = cfg.concurrency[0] if cfg.concurrency else 1
    exhaust_conc = cfg.param("exhaust_concurrency", 300, int)
    out = []

    down_url = cfg.param("down_url", None)
    if down_url:
        arm = Arm(
            name="classifier-down",
            target=down_url,
            build=make_builder("miss", seed="b6down", target_tokens=32,
                               extra_meta={"case": "classifier-down"}),
            params={"case": "classifier down (unbound port)", "timeout_ms": timeout_ms,
                    "on_unavailable": "default_cluster"},
            warmup=cfg.warmup, measured=cfg.measured, concurrency=conc, cache_mode="miss",
            notes="The filter's gRPC endpoint is an unbound port: connect is refused immediately.",
        )
        arm.assertions = lambda result, ctx, t=timeout_ms: [
            _all_ok(result),
            _routed_to_default(result, ("UNAVAILABLE", "ERROR")),
            assertion(
                "added_latency_is_connect_refused_not_the_full_timeout",
                result.latency_ms.get("p99", 0.0) < t * DOWN_LATENCY_FRACTION_OF_TIMEOUT,
                "p50 %.3f ms / p99 %.3f ms against a %.0f ms timeout budget. A fail-open path "
                "that waits the full timeout on every request has already taken the gateway "
                "down; p99 must stay well under %.0f ms."
                % (result.latency_ms.get("p50", 0.0), result.latency_ms.get("p99", 0.0), t,
                   t * DOWN_LATENCY_FRACTION_OF_TIMEOUT),
            ),
        ]
        out.append(arm)

    slow_url = cfg.param("slow_url", None)
    if slow_url:
        arm = Arm(
            name="classifier-slow",
            target=slow_url,
            build=make_builder("miss", seed="b6slow", target_tokens=32,
                               extra_meta={"case": "classifier-slow"}),
            params={"case": "classifier slow (tcp black hole)", "timeout_ms": timeout_ms},
            warmup=cfg.warmup, measured=cfg.measured, concurrency=conc, cache_mode="miss",
            timeout_s=max(30.0, timeout_ms / 1000.0 * 30),
            notes="Peer accepts TCP then never completes the HTTP/2 preface, so timeout_ms ends it.",
        )
        arm.assertions = lambda result, ctx, t=timeout_ms: [
            _all_ok(result),
            _routed_to_default(result, ("TIMEOUT",)),
            assertion(
                "added_latency_is_bounded_by_timeout_ms",
                t * SLOW_MIN_FRACTION <= result.latency_ms.get("p50", 0.0)
                and result.latency_ms.get("p99", 0.0) <= t * SLOW_MAX_MULTIPLE,
                "p50 %.3f ms / p99 %.3f ms against timeout_ms %.0f. p50 below %.0f ms would mean "
                "something other than the timeout ended the request; p99 above %.0f ms would mean "
                "the budget is not being honoured."
                % (result.latency_ms.get("p50", 0.0), result.latency_ms.get("p99", 0.0), t,
                   t * SLOW_MIN_FRACTION, t * SLOW_MAX_MULTIPLE),
            ),
        ]
        out.append(arm)

    exhausted_url = cfg.param("exhausted_url", None)
    if exhausted_url:
        arm = Arm(
            name="queue-exhausted",
            target=exhausted_url,
            build=make_builder("miss", seed="b6exh", target_tokens=32,
                               extra_meta={"case": "queue-exhausted"}),
            params={"case": "llm-d-sc bounded queue driven past 256",
                    "concurrency": exhaust_conc, "on_resource_exhausted": "default_cluster"},
            warmup=cfg.warmup, measured=cfg.measured, concurrency=exhaust_conc, cache_mode="miss",
            notes="Requires a real local llm-d-sc: only it has a bounded queue to exhaust.",
        )
        arm.assertions = lambda result, ctx: [
            _all_ok(result),
            assertion(
                "resource_exhausted_was_actually_provoked",
                _status_tally(result.ok_records()).get("RESOURCE_EXHAUSTED", 0) > 0,
                "x-llm-d-sc-status tally %s. If no request came back RESOURCE_EXHAUSTED the "
                "queue was never exhausted and this arm proves nothing about fail-open under "
                "load; raise --param exhaust_concurrency." % _status_tally(result.ok_records()),
            ),
        ]
        arm.summarize = lambda result, ctx: {"status_tally": _status_tally(result.ok_records())}
        out.append(arm)

    reject_url = cfg.param("reject_url", None)
    if reject_url:
        status_on_reject = cfg.param("status_on_reject", 503, int)
        arm = Arm(
            name="fail-closed",
            target=reject_url,
            build=make_builder("miss", seed="b6rej", target_tokens=32,
                               extra_meta={"case": "fail-closed"}),
            params={"case": "on_resource_exhausted: reject", "status_on_reject": status_on_reject},
            warmup=cfg.warmup, measured=cfg.measured, concurrency=conc, cache_mode="miss",
            expected_status=(status_on_reject,),
            notes="Fail-closed posture: an unclassified prompt must not reach a model.",
        )
        arm.assertions = lambda result, ctx, s=status_on_reject: [
            assertion(
                "rejects_cleanly_with_the_configured_status",
                result.status_counts.get(str(s), 0) == len(result.records) and len(result.records) > 0,
                "status tally %s; expected every request to be answered %d. A mixture, or any "
                "transport error, would mean the reject path hangs or storms rather than "
                "refusing cleanly." % (result.status_counts, s),
            ),
            assertion(
                "no_hangs_on_the_reject_path",
                all(r["error"] is None for r in result.records),
                "%d/%d requests completed without a transport error or timeout"
                % (sum(1 for r in result.records if r["error"] is None), len(result.records)),
            ),
        ]
        out.append(arm)

    if not out:
        raise SystemExit(
            "B-6 needs at least one case URL. Each case is a separate Praxis listener because "
            "the cases differ only in filter config: --param down_url=... slow_url=... "
            "exhausted_url=... reject_url=..."
        )
    return out
