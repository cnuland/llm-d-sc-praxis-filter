"""B-7 — Topology, measured from inside the cluster (SPEC-BENCH §1).

*What does the Praxis -> llm-d-sc hop cost across a ClusterIP Service, under
real gateway concurrency?*

This is the in-cluster sibling of `b7_topology.py`. It exists as a separate
module rather than a flag on that one because it differs in two ways that are
methodological, not cosmetic, and both need to be visible in the file rather
than buried in a parameter:

**1. The upstream is `static_response`, not a model.** SPEC-BENCH §1 B-7
sweeps concurrency 1/4/16 over a thousand requests per arm. Pointing that at
`homelab-maas` would put ~12 000 requests through two single-replica llama.cpp
servers on someone's home cluster, which SPEC-BENCH §3 forbids in as many
words. Terminating the chain in Praxis's own `static_response` filter keeps the
whole sweep off the model endpoints while still paying every cost this scenario
claims to measure: whole-body buffer, prompt extraction, the gRPC round trip
across the ClusterIP Service, and llm-d-sc's model forward. Nothing is stubbed
that is on the path being timed, and no new workload is deployed to do it --
`static_response` is a stock Praxis filter, documented for exactly this use.

**2. The premise is proved from Praxis's counters, not from response headers.**
`b7_topology.py` verifies that classification happened by reading
`x-llm-d-sc-status` off the response, which only works because the local stub
upstream is started with `--echo-sc-headers`. In the cluster there is nothing to
echo: the filter sets provenance on the UPSTREAM request, and `static_response`
never forwards one. So this module asserts against
`llm_d_sc_classify_total` on Praxis's own admin endpoint instead -- the
classified arms must move it by exactly their request count, and the baseline
arms must not move it at all. That is the counter-delta discipline SPEC-BENCH §0
rule 3 actually asks for, and it is a stronger check than the header, because it
cannot be satisfied by an upstream that merely reflects a header back.

The arms
--------
Per concurrency, and per cache mode, two arms that differ in exactly one filter:

| Arm          | Chain                                          |
|--------------|------------------------------------------------|
| `baseline`   | `request_id -> static_response`                |
| `classified` | `request_id -> llm_d_sc -> static_response`    |

The delta between them is the hop plus the classify work. Split by cache mode it
separates into its two parts: a `hit` costs the network round trip and llm-d-sc's
cache lookup with no model forward, so it isolates the *hop*; a `miss` adds the
forward. llm-d-sc's own topology table reports ~22 us for a same-Pod hop, and
this is the row that table does not have -- proxy to classifier, across a
Service, under gateway concurrency.

A caveat that belongs next to the numbers, not in a footnote: `static_response`
answers without draining the request body, so Praxis closes the connection after
each response and every request pays a fresh TCP handshake. That inflates the
ABSOLUTE latency of both arms by a loopback connect. It does not touch the
delta, which is what this scenario reports, because both arms pay it identically.

The trap this scenario is built to avoid
----------------------------------------
**A number measured through `oc port-forward` is not a network measurement.** A
probe from the laptop through the tunnel showed p50 145 ms against an in-cluster
expectation of ~13 ms. `not_measured_through_a_tunnel` fails the run if it claims
an in-cluster topology while pointing at loopback.

Run (from the in-cluster driver Pod):
    python3 bench/harness.py --scenario b7c \\
        --target http://praxis-bench.praxis-poc.svc.cluster.local:8084 \\
        --param baseline_url=http://praxis-bench.praxis-poc.svc.cluster.local:8085 \\
        --param metrics_url=http://praxis-bench.praxis-poc.svc.cluster.local:9901/metrics \\
        --topology in-cluster-job --warmup 200 --measured 1000 --concurrency 1,4,16
"""

from __future__ import annotations

import urllib.parse

import _praxis_metrics as pm
from _common import assert_cache_discipline, make_builder
from harness import Arm, assertion

SPEC_ID = "B-7"
DESCRIPTION = "In-cluster topology: Praxis -> llm-d-sc across a ClusterIP Service under gateway concurrency."
TARGETS_REAL_MODELS = False
NOTES = [
    "Run from a Pod inside the cluster. A figure measured through oc port-forward is a "
    "measurement of the tunnel, not of the network.",
    "The upstream is Praxis's own static_response filter, not a model endpoint: SPEC-BENCH §3 "
    "keeps a concurrency-16 sweep away from the single-replica homelab backends. Everything on "
    "the path being timed is real -- body buffer, extract, gRPC across the ClusterIP Service, "
    "and llm-d-sc's forward.",
    "Classification is verified from Praxis's own llm_d_sc_classify_total counter, which must "
    "move by exactly the arm's request count on a classified arm and by exactly zero on a "
    "baseline arm. Response-header provenance is unavailable in-cluster because nothing echoes "
    "the upstream request headers back.",
    "static_response answers without draining the request body, so each response closes the "
    "connection and every request pays a fresh TCP handshake. Both arms pay it identically, so "
    "the reported delta is unaffected; the absolute figures include it.",
    "Percentiles are nearest-rank and warmup is excluded, matching llm-d-sc's own topology "
    "table so the rows are directly comparable rather than merely similar.",
]

LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _tunnel_assertion(result, topology_label):
    host = urllib.parse.urlsplit(result.arm.target).hostname or ""
    claims_cluster = "cluster" in (topology_label or "").lower()
    return assertion(
        "not_measured_through_a_tunnel",
        not (claims_cluster and host in LOOPBACK),
        "topology label %r with target host %r. SPEC-BENCH §1 B-7: a measurement taken through "
        "oc port-forward is a measurement of the tunnel -- an observed p50 of 145 ms against an "
        "in-cluster expectation of ~13 ms." % (topology_label, host),
    )


def _counter_key(arm_name):
    return "b7c_metrics_before/" + arm_name


def _make_setup(metrics_url, arm_name):
    def setup(ctx):
        ctx[_counter_key(arm_name)] = pm.scrape(metrics_url) if metrics_url else None

    return setup


def _observed(ctx, metrics_url, arm_name):
    """Snapshot after the arm, once, and memoise it for summarize()."""
    cache = "b7c_metrics_delta/" + arm_name
    if cache in ctx:
        return ctx[cache]
    before = ctx.get(_counter_key(arm_name))
    if before is None or not metrics_url:
        ctx[cache] = None
        return None
    after = pm.scrape(metrics_url)
    ctx[cache] = {
        "classify_total_delta": pm.classify_total(after) - pm.classify_total(before),
        "classify_by_status_before": pm.classify_by_status(before),
        "classify_by_status_after": pm.classify_by_status(after),
        "route_by_label_delta": {
            k: v - pm.route_by_label(before).get(k, 0.0) for k, v in pm.route_by_label(after).items()
            if v - pm.route_by_label(before).get(k, 0.0) != 0
        },
        # Praxis's OWN view of the hop. A distribution over the exporter's
        # rolling window, not a per-request join -- never presented as one.
        "praxis_classify_rtt_ms": pm.classify_duration_quantiles(after),
    }
    return ctx[cache]


def _assertions(result, ctx, *, classified, metrics_url, topology, cache_mode):
    out = [_tunnel_assertion(result, topology), assert_cache_discipline(result, cache_mode)]
    obs = _observed(ctx, metrics_url, result.arm.name)
    expected = result.arm.warmup + result.arm.measured
    if obs is None:
        out.append(assertion(
            "classification_premise_verified", False,
            "no metrics_url supplied, so the arm could not prove whether classification "
            "happened. SPEC-BENCH §0 rule 3: a scenario that cannot verify its own premise is "
            "a bug, not a result. Pass --param metrics_url=<praxis admin>/metrics.",
        ))
        return out
    moved = obs["classify_total_delta"]
    if classified:
        out.append(assertion(
            "classification_premise_verified", abs(moved - expected) < 0.5,
            "Praxis's llm_d_sc_classify_total moved by %.0f across this arm; expected exactly "
            "%d (warmup %d + measured %d). Status breakdown after the arm: %s"
            % (moved, expected, result.arm.warmup, result.arm.measured,
               obs["classify_by_status_after"]),
        ))
        ok_delta = (obs["classify_by_status_after"].get("OK", 0.0)
                    - obs["classify_by_status_before"].get("OK", 0.0))
        out.append(assertion(
            "every_classification_succeeded", abs(ok_delta - expected) < 0.5,
            "%.0f of %d classifications returned status OK; a TIMEOUT or UNAVAILABLE would mean "
            "the arm timed a fail-open path rather than the hop it claims to time. Routing "
            "delta: %s" % (ok_delta, expected, obs["route_by_label_delta"]),
        ))
    else:
        out.append(assertion(
            "baseline_is_unclassified", abs(moved) < 0.5,
            "Praxis's llm_d_sc_classify_total moved by %.0f across the baseline arm; expected "
            "exactly 0. Any movement means the control arm went through the filter and the "
            "delta against it is meaningless." % moved,
        ))
    return out


def _summarize(result, ctx, *, metrics_url):
    obs = _observed(ctx, metrics_url, result.arm.name)
    if obs is None:
        return {}
    return {
        "praxis_classify_total_delta": obs["classify_total_delta"],
        "praxis_route_delta": obs["route_by_label_delta"],
        "praxis_classify_rtt_ms": obs["praxis_classify_rtt_ms"],
        "praxis_classify_rtt_note": (
            "Praxis's own summary of the classify RPC, exported as pre-computed quantiles over "
            "the exporter's rolling window. In-cluster and independent of the client, but a "
            "window distribution, not a per-request join to the rows above."
        ),
    }


def arms(cfg):
    topology = cfg.args.topology
    baseline_url = cfg.param("baseline_url", None)
    metrics_url = cfg.param("metrics_url", None)
    out = []
    for conc in cfg.concurrency:
        for cache_mode in ("miss", "hit"):
            specs = [("classified", cfg.target, True)]
            if baseline_url:
                specs.insert(0, ("baseline", baseline_url, False))
            for label, url, classified in specs:
                name = "%s-%s@c%d" % (label, cache_mode, conc)
                out.append(
                    Arm(
                        name=name,
                        target=url,
                        build=make_builder(cache_mode, seed="b7c-" + name, target_tokens=32,
                                           extra_meta={"topology": topology, "arm_kind": label}),
                        params={
                            "topology": topology,
                            "chain": ("request_id -> llm_d_sc -> static_response" if classified
                                      else "request_id -> static_response"),
                            "hop": ("praxis -> llm-d-sc (ClusterIP Service)" if classified
                                    else "none: control arm, no classifier contacted"),
                        },
                        warmup=cfg.warmup, measured=cfg.measured, concurrency=conc,
                        cache_mode=cache_mode,
                        setup=_make_setup(metrics_url, name),
                        assertions=(lambda result, ctx, c=classified, m=metrics_url, t=topology,
                                    cm=cache_mode: _assertions(result, ctx, classified=c,
                                                               metrics_url=m, topology=t,
                                                               cache_mode=cm)),
                        summarize=lambda result, ctx, m=metrics_url: _summarize(result, ctx, metrics_url=m),
                        notes=("Cache %s. %s" % (
                            cache_mode,
                            "A hit does no model forward, so it isolates the network hop plus "
                            "llm-d-sc's cache lookup." if cache_mode == "hit" else
                            "A miss adds llm-d-sc's model forward on top of the hop.")),
                    )
                )
    return out


def cross_arm_assertions(ctx):
    """The B-7 payoff: classified minus baseline, per concurrency and cache mode.

    Attached to the classified arms so the delta lands in the JSON next to the
    distribution it was computed from, and so the run fails if the control arm
    is not cheaper than the arm that does strictly more work.
    """
    results = ctx["results"]
    out = []
    deltas = {}
    for name, res in results.items():
        if not name.startswith("classified-"):
            continue
        base_name = "baseline-" + name.split("-", 1)[1]
        base = results.get(base_name)
        if base is None:
            continue
        d = {k: res.latency_ms[k] - base.latency_ms[k] for k in ("p50", "p90", "p95", "p99", "max")}
        deltas[name] = d
        res.extra["hop_delta_ms_vs_baseline"] = dict(
            d, baseline_arm=base_name,
            note="classified minus baseline at the same concurrency and cache mode. Both arms "
                 "terminate in the same static_response and pay the same per-request connection "
                 "setup, so the delta is the Praxis -> llm-d-sc hop plus llm-d-sc's work.")
        out.append((name, [assertion(
            "hop_costs_something_measurable",
            d["p50"] > 0.0,
            "p50 delta vs %s is %+.3f ms. The classified arm does strictly more work than the "
            "control -- a gRPC round trip across a ClusterIP Service -- so a delta at or below "
            "zero means the arms were not what they claim to be." % (base_name, d["p50"]),
        )]))
    ctx["extra"]["b7_hop_deltas_ms"] = deltas
    return out
