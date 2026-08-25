"""B-1 — Filter overhead at the proxy (SPEC-BENCH §1).

*What does adding `llm_d_sc` cost a request, in isolation?* This is the number
discussion #1017 is actually asking for.

Three arms, identical in every respect except the one thing under test, all
driven against a **stub upstream that returns instantly** so the backend
contributes no variance:

| Arm               | Praxis chain                          | Isolates |
|-------------------|---------------------------------------|----------|
| `baseline`        | `router` -> `load_balancer`           | static routing, no body access |
| `classified-miss` | `llm_d_sc` -> `load_balancer`, unique prompt per request | body buffer + extract + gRPC + model forward |
| `classified-hit`  | `llm_d_sc` -> `load_balancer`, one repeated prompt | cost with llm-d-sc's cache hot |

Two Praxis listeners are required, because SPEC §2.2 makes `llm_d_sc` and
`router` mutually exclusive in one chain (`check_conflicting_cluster_selectors`
is a build error). Pass them as:

    --target http://127.0.0.1:8081            # the classified chain
    --param baseline_url=http://127.0.0.1:8080

Self-assertions (SPEC-BENCH §0 rule 3):
* `baseline_is_unclassified` — the baseline arm's responses carry no
  `x-llm-d-sc-*` provenance. If they do, the operator wired both arms to the
  same chain and every delta on this page is zero by construction.
* `classification_observed` — every classified request came back with
  `x-llm-d-sc-status: OK`.
* `miss_arm_keys_are_unique` / `hit_arm_uses_one_key` — the cache workloads use
  disjoint key namespaces, ported from `llm-d-sc/src/bench.rs`.
* `hit_is_dramatically_cheaper_than_miss` — SPEC-BENCH states outright that if
  the hit arm is not dramatically cheaper, the cache was not exercised and the
  run is invalid. That is asserted here, not hoped for.

Run:
    python3 bench/stub_upstream.py --port 9001 --model small-stub --echo-sc-headers &
    python3 bench/harness.py --scenario b1 --target http://127.0.0.1:8081 \
        --param baseline_url=http://127.0.0.1:8080 \
        --warmup 200 --measured 1000 --concurrency 1,4,16 \
        --topology local-loopback
"""

from __future__ import annotations

from _common import (
    assert_cache_discipline,
    assert_classification_happened,
    assert_no_classification,
    make_builder,
)
from harness import Arm, assertion

SPEC_ID = "B-1"
DESCRIPTION = "Filter overhead at the proxy: baseline vs classified-miss vs classified-hit."
TARGETS_REAL_MODELS = False
NOTES = [
    "Upstream is a local instant-return stub, so the delta between arms is the filter and "
    "the fabric it sits on, not the backend.",
    "Run locally (no cluster network): B-1 measures the filter, not the fabric.",
    "The baseline and classified arms are separate Praxis listeners because SPEC §2.2 makes "
    "`router` and `llm_d_sc` mutually exclusive in one chain.",
]

# The ratio below which a cache hit must sit relative to a miss for the run to
# be considered valid. llm-d-sc's own figures are ~0.09 ms hit against
# 7.7-12.3 ms miss, i.e. a ratio near 0.01; 0.5 is an extremely generous floor
# that still catches "the cache never engaged".
HIT_MISS_RATIO_LIMIT = 0.5

PROMPT_TOKENS = 32


def arms(cfg):
    baseline_url = cfg.param("baseline_url", None)
    classified_url = cfg.param("classified_url", cfg.target)
    max_tokens = cfg.param("max_tokens", 16, int)

    out = []
    for conc in cfg.concurrency:
        if baseline_url:
            out.append(
                Arm(
                    name="baseline@c%d" % conc,
                    target=baseline_url,
                    build=make_builder("miss", target_tokens=PROMPT_TOKENS, max_tokens=max_tokens,
                                       extra_meta={"arm_kind": "baseline"}),
                    params={"chain": "router -> load_balancer", "prompt_tokens": PROMPT_TOKENS},
                    warmup=cfg.warmup, measured=cfg.measured, concurrency=conc,
                    cache_mode="n/a",
                    assertions=lambda result, ctx: [assert_no_classification(result)],
                    notes="Praxis with static routing; no body access, no gRPC.",
                )
            )
        out.append(
            Arm(
                name="classified-miss@c%d" % conc,
                target=classified_url,
                build=make_builder("miss", target_tokens=PROMPT_TOKENS, max_tokens=max_tokens,
                                   extra_meta={"arm_kind": "classified-miss"}),
                params={"chain": "llm_d_sc -> load_balancer", "prompt_tokens": PROMPT_TOKENS},
                warmup=cfg.warmup, measured=cfg.measured, concurrency=conc,
                cache_mode="miss",
                assertions=lambda result, ctx: [
                    assert_classification_happened(result),
                    assert_cache_discipline(result, "miss"),
                ],
                notes="Unique prompt per request: full cost including llm-d-sc's model forward.",
            )
        )
        out.append(
            Arm(
                name="classified-hit@c%d" % conc,
                target=classified_url,
                build=make_builder("hit", target_tokens=PROMPT_TOKENS, max_tokens=max_tokens,
                                   extra_meta={"arm_kind": "classified-hit"}),
                params={"chain": "llm_d_sc -> load_balancer", "prompt_tokens": PROMPT_TOKENS},
                warmup=cfg.warmup, measured=cfg.measured, concurrency=conc,
                cache_mode="hit",
                assertions=lambda result, ctx: [
                    assert_classification_happened(result),
                    assert_cache_discipline(result, "hit"),
                ],
                notes="One repeated prompt: llm-d-sc's versioned result cache is hot.",
            )
        )
    return out


def cross_arm_assertions(ctx):
    """The delta checks that need more than one arm to evaluate.

    SPEC-BENCH §1 B-1: "If `classified-hit` is not dramatically cheaper than
    `classified-miss`, the cache is not being exercised and the run is invalid
    — assert it."
    """
    out = []
    results = ctx["results"]
    for name, res in results.items():
        if not name.startswith("classified-hit@"):
            continue
        conc = name.split("@", 1)[1]
        miss = results.get("classified-miss@" + conc)
        if miss is None:
            out.append((name, [assertion("hit_is_dramatically_cheaper_than_miss", False,
                                         "no matching classified-miss arm at %s to compare against" % conc)]))
            continue
        hit_p50 = res.latency_ms.get("p50", 0.0)
        miss_p50 = miss.latency_ms.get("p50", 0.0)
        ratio = (hit_p50 / miss_p50) if miss_p50 > 0 else float("inf")
        out.append((
            name,
            [assertion(
                "hit_is_dramatically_cheaper_than_miss",
                ratio <= HIT_MISS_RATIO_LIMIT,
                "hit p50 %.3f ms vs miss p50 %.3f ms (ratio %.3f, must be <= %.2f). A ratio "
                "near 1 means llm-d-sc's cache never engaged and the arms are not measuring "
                "what they claim." % (hit_p50, miss_p50, ratio, HIT_MISS_RATIO_LIMIT),
            )],
        ))

    # The baseline delta is the headline. Assert it is even computable.
    for name, res in results.items():
        if not name.startswith("classified-miss@"):
            continue
        conc = name.split("@", 1)[1]
        base = results.get("baseline@" + conc)
        out.append((
            name,
            [assertion(
                "baseline_arm_present_for_delta",
                base is not None,
                "SPEC-BENCH §0 rule 2 forbids a cost figure without a comparable before/after. "
                + ("baseline@%s captured, delta is computable." % conc if base is not None
                   else "no baseline@%s arm: pass --param baseline_url=... pointing at a Praxis "
                        "listener whose chain is `router -> load_balancer`." % conc),
            )],
        ))
    return out
