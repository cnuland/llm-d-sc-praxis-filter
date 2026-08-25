"""B-7 — Topology, measured in-cluster (SPEC-BENCH §1).

Praxis -> llm-d-sc across a ClusterIP Service, driven from the bench Job INSIDE
the cluster. Directly comparable to llm-d-sc's existing topology table
(`docs/benchmarks/topology.md`: same-Pod vs ClusterIP, ~22 us for the hop) and
adds the row nobody has: proxy-to-classifier across a Service, under real
gateway concurrency.

The trap this scenario is built to avoid
----------------------------------------
**A number measured through `oc port-forward` is not a network measurement.** A
probe from the laptop through the tunnel showed p50 145 ms against an
in-cluster expectation of ~8-12 ms; the tunnel dominates by an order of
magnitude. SPEC-BENCH records this specifically because it is the mistake this
benchmark could ship without noticing.

So the scenario asserts against it: `not_measured_through_a_tunnel` fails if the
run claims an in-cluster topology while pointing at loopback. That check cannot
catch every tunnel, but it catches the one that actually happened.

Run (as `Job praxis-bench`, SPEC-K8S §2):
    python3 bench/harness.py --scenario b7 \\
        --target http://praxis.praxis-poc.svc.cluster.local:8080 \\
        --topology in-cluster-clusterip --warmup 200 --measured 1000 \\
        --concurrency 1,4,16
"""

from __future__ import annotations

import urllib.parse

from _common import assert_cache_discipline, assert_classification_happened, make_builder
from harness import Arm, assertion

SPEC_ID = "B-7"
DESCRIPTION = "In-cluster topology: Praxis -> llm-d-sc across a ClusterIP Service under gateway concurrency."
TARGETS_REAL_MODELS = False
NOTES = [
    "Must be run from a Job inside the cluster. A figure measured through oc port-forward is "
    "not a network measurement.",
    "Comparable to llm-d-sc's own same-Pod vs ClusterIP table by construction: same nearest-rank "
    "percentiles, same warmup exclusion, same cache-mode key discipline.",
    "Upstreams should be in-cluster stubs so the row measures the classifier hop, not generation.",
]

LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _tunnel_assertion(result, topology_label):
    host = urllib.parse.urlsplit(result.arm.target).hostname or ""
    claims_cluster = "cluster" in (topology_label or "").lower()
    suspicious = claims_cluster and host in LOOPBACK
    return assertion(
        "not_measured_through_a_tunnel",
        not suspicious,
        "topology label %r with target host %r. SPEC-BENCH §1 B-7: a measurement taken through "
        "oc port-forward is a measurement of the tunnel — an observed p50 of 145 ms against an "
        "in-cluster expectation of 8-12 ms. Run this from the in-cluster Job."
        % (topology_label, host),
    )


def arms(cfg):
    topology = cfg.args.topology
    out = []
    for conc in cfg.concurrency:
        for cache_mode in ("miss", "hit"):
            out.append(
                Arm(
                    name="clusterip-%s@c%d" % (cache_mode, conc),
                    target=cfg.target,
                    build=make_builder(cache_mode, seed="b7", target_tokens=32,
                                       extra_meta={"topology": topology}),
                    params={"topology": topology, "hop": "praxis -> llm-d-sc (ClusterIP)"},
                    warmup=cfg.warmup, measured=cfg.measured, concurrency=conc,
                    cache_mode=cache_mode,
                    assertions=lambda result, ctx, cm=cache_mode, t=topology: [
                        _tunnel_assertion(result, t),
                        assert_classification_happened(result),
                        assert_cache_discipline(result, cm),
                    ],
                    notes="Cache %s: a hit does no model work, so it isolates the hop." % cache_mode,
                )
            )
    return out
