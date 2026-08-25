"""B-5 — End-to-end payoff (SPEC-BENCH §1). *The money chart.*

*Does routing pay for itself?* Everything else in this suite measures a cost.
This measures whether the cost buys anything, which is the entire argument for
content-aware routing and the thing nobody has measured.

Three arms over the same labelled prompt subset, against the REAL homelab
models:

| Arm            | Routing                            | Measures |
|----------------|------------------------------------|----------|
| `always-large` | everything -> `ds4-flash-0731`     | today's "just use the big model" |
| `always-small` | everything -> `qwen38-27b`         | the cheap floor |
| `classified`   | `llm_d_sc` decides                 | the proposal |

The claim under test: `classified` p50 lands close to `always-small` while the
hard prompts still reach the strong model — i.e. ~10 ms of classification buys
back seconds of generation.

Capacity discipline (SPEC-BENCH §3) is enforced by code, not by good intentions
--------------------------------------------------------------------------------
`TARGETS_REAL_MODELS = True` makes `harness.py` refuse to run this without
`--allow-homelab`, and refuse outright at any concurrency above 1. Defaults are
10 prompts per class and `max_tokens` 128. Arms pause between each other so a
single-replica llama.cpp server is never hammered back to back. If an endpoint
errors, the arm's error budget is zero and the run stops — SPEC-BENCH §3 says
stop and report, do not retry-storm someone's home lab.

Latency decomposition
---------------------
    total_e2e = praxis_overhead + classify_rtt + upstream_time
                └ from B-1 ─┘   └ x-llm-d-sc-latency-us ┘  └ remainder ┘

`praxis_overhead` is supplied from a completed B-1 run:
`--param praxis_overhead_p50_ms=... --param praxis_overhead_p99_ms=...`.

**Known gap, stated rather than papered over:** SPEC §4.7 emits
`llm_d_sc.latency_us` as filter METADATA (visible to `access_log`) but the
upstream header set is only label/score/classifier/taxonomy-revision/status —
there is no `x-llm-d-sc-latency-us` header. A client-side harness therefore
cannot see the classify RTT per request. When the header is absent this arm
records `decomposition_reconciles: passed=false` with that explanation and the
run exits non-zero, exactly as SPEC-BENCH §0 rule 3 requires of a premise that
cannot be verified. It does NOT publish a decomposition inferred from a
subtraction.

Run:
    python3 bench/harness.py --scenario b5 --allow-homelab \
        --target http://praxis.praxis-poc.svc.cluster.local:8080 \
        --param large_url=... --param small_url=... \
        --param praxis_overhead_p50_ms=0.42 --param praxis_overhead_p99_ms=1.10 \
        --topology in-cluster-job --concurrency 1
"""

from __future__ import annotations

import time

from _common import chat_body, sc_header
from harness import Arm, Request, assertion, reduce_latency

SPEC_ID = "B-5"
DESCRIPTION = "End-to-end payoff against the real models: always-large vs always-small vs classified."
TARGETS_REAL_MODELS = True
NOTES = [
    "Concurrency is fixed at 1 and enforced by the harness (SPEC-BENCH §3).",
    "Generation settings are identical across arms and recorded in the manifest; different "
    "settings between arms would invalidate the comparison entirely.",
    "Arms pause between each other so a single-replica backend is not hammered back to back.",
    "The classify RTT is not observable from a client because the filter emits latency as "
    "metadata, not as an upstream header; the decomposition assertion records that gap.",
]

LARGE_MODEL = "ds4-flash-0731"
SMALL_MODEL = "qwen38-27b"
# The decomposition must reconcile within this fraction of the measured total,
# or SPEC-BENCH §1 B-5 requires us to say so and investigate rather than publish.
RECONCILE_TOLERANCE = 0.05


def _subset(prompts, per_class):
    by_class = {}
    for rec in prompts:
        by_class.setdefault(rec["label"], []).append(rec)
    out = []
    for label in sorted(by_class):
        out.extend(by_class[label][:per_class])
    return out


def arms(cfg):
    prompts = _subset(cfg.load_prompts(), cfg.param("per_class", 10, int))
    max_tokens = cfg.param("max_tokens", 128, int)
    temperature = cfg.param("temperature", 0.0, float)
    pause_s = cfg.param("pause_s", 30, int)
    gen = {"max_tokens": max_tokens, "temperature": temperature, "stream": False}

    def make_build(model_field):
        def build(index, phase, run_id):
            rec = prompts[index % len(prompts)]
            meta = {
                "prompt_key": rec["id"],
                "prompt_id": rec["id"],
                "intended_label": rec["label"],
                "boundary": bool(rec.get("boundary")),
            }
            return Request(
                body=chat_body(rec["prompt"], model=model_field, max_tokens=max_tokens,
                               temperature=temperature, stream=False),
                meta=meta,
            )

        return build

    specs = [
        ("always-large", cfg.param("large_url", None), LARGE_MODEL,
         "everything to the 284 B model: today's 'just use the big one'"),
        ("always-small", cfg.param("small_url", None), SMALL_MODEL,
         "everything to the 27 B model: the cheap floor"),
        ("classified", cfg.param("classified_url", cfg.target), None,
         "llm_d_sc decides: the proposal under test"),
    ]

    out = []
    for name, url, expect_model, note in specs:
        if url is None:
            continue
        arm = Arm(
            name=name,
            target=url,
            build=make_build("bench-router"),
            params=dict(gen, prompts=len(prompts), per_class=cfg.param("per_class", 10, int),
                        expected_model=expect_model),
            warmup=cfg.param("warmup_override", min(cfg.warmup, 4), int),
            measured=len(prompts),
            concurrency=1,
            cache_mode="n/a",
            timeout_s=cfg.param("timeout_s", 300.0, float),
            allow_errors=0,
            notes=note,
        )
        arm.teardown = lambda ctx, s=pause_s: time.sleep(s)
        arm.assertions = lambda result, ctx, em=expect_model, n=name: _assertions(result, ctx, em, n, cfg)
        arm.summarize = lambda result, ctx, n=name: _summarize(result, ctx, n, cfg)
        out.append(arm)
    return out


def _assertions(result, ctx, expected_model, arm_name, cfg):
    records = result.ok_records()
    out = []
    models = {}
    for r in records:
        models[r.get("model") or "<none>"] = models.get(r.get("model") or "<none>", 0) + 1

    if expected_model is not None:
        # Premise: this arm really did pin every request to one backend.
        out.append(
            assertion(
                "arm_reached_only_its_intended_backend",
                set(models) == {expected_model},
                "response model tally %s; this arm exists to measure %s alone, so any other id "
                "means the arm was mis-wired and its column of the comparison is invalid"
                % (models, expected_model),
            )
        )
    else:
        # The classified arm's premise: classification actually happened, and
        # traffic genuinely split across both backends. An arm that classified
        # perfectly but sent everything to one model has not tested routing.
        statuses = {}
        for r in records:
            s = sc_header(r, "status") or "<absent>"
            statuses[s] = statuses.get(s, 0) + 1
        out.append(
            assertion(
                "classification_observed",
                statuses.get("OK", 0) == len(records) and len(records) > 0,
                "x-llm-d-sc-status tally %s over %d requests" % (statuses, len(records)),
            )
        )
        out.append(
            assertion(
                "traffic_split_across_backends",
                len([m for m in models if m in (LARGE_MODEL, SMALL_MODEL)]) >= 2,
                "response model tally %s; the classified arm must reach both backends or it is "
                "measuring one of the other two arms under a different name" % models,
            )
        )
        out.append(_decomposition_assertion(result, cfg))

    tokens = [r.get("completion_tokens") for r in records if r.get("completion_tokens")]
    out.append(
        assertion(
            "generation_accounted",
            len(tokens) == len(records) and len(records) > 0,
            "%d/%d responses reported completion_tokens; time-per-output-token is only "
            "computable where they did" % (len(tokens), len(records)),
        )
    )
    return out


def _decomposition_assertion(result, cfg):
    """Does praxis_overhead + classify_rtt + upstream_time reconcile with the total?

    SPEC-BENCH §1 B-5: "If the components do not sum to the measured total
    within a few percent, say so and investigate rather than publishing a
    decomposition that does not reconcile."
    """
    records = result.ok_records()
    classify_us = [
        float(sc_header(r, "latency-us")) for r in records if sc_header(r, "latency-us") is not None
    ]
    if not classify_us:
        return assertion(
            "decomposition_reconciles",
            False,
            "no x-llm-d-sc-latency-us header on any response, so classify_rtt is not observable "
            "from a client. SPEC §4.7 emits llm_d_sc.latency_us as filter METADATA (access log) "
            "but not as an upstream header. Until the filter emits it, the stacked decomposition "
            "cannot be built from measurement and must not be inferred by subtraction.",
        )
    overhead_p50 = cfg.param("praxis_overhead_p50_ms", None, float)
    if overhead_p50 is None:
        return assertion(
            "decomposition_reconciles",
            False,
            "praxis_overhead is a B-1 number and was not supplied; pass "
            "--param praxis_overhead_p50_ms=<value from the B-1 JSON>. SPEC-BENCH §0 rule 2 "
            "forbids inventing it.",
        )
    total = reduce_latency([r["wall_ns"] / 1e6 for r in records])
    classify = reduce_latency([v / 1000.0 for v in classify_us])
    remainder_p50 = total["p50"] - overhead_p50 - classify["p50"]
    residual = abs(total["p50"] - (overhead_p50 + classify["p50"] + remainder_p50))
    return assertion(
        "decomposition_reconciles",
        residual <= total["p50"] * RECONCILE_TOLERANCE,
        "p50 total %.2f ms = praxis %.2f + classify %.2f + upstream %.2f (residual %.4f ms, "
        "tolerance %.0f%%)" % (total["p50"], overhead_p50, classify["p50"], remainder_p50,
                               residual, RECONCILE_TOLERANCE * 100),
    )


def _summarize(result, ctx, arm_name, cfg):
    records = result.ok_records()
    tokens = [r.get("completion_tokens") or 0 for r in records]
    tpot = [
        (r["wall_ns"] / 1e6) / r["completion_tokens"]
        for r in records
        if r.get("completion_tokens")
    ]
    by_model = {}
    for r in records:
        m = r.get("model") or "<none>"
        by_model[m] = by_model.get(m, 0) + 1
    by_label_cluster = {}
    for r in records:
        key = "%s->%s" % (r["meta"].get("intended_label"), r.get("model") or "<none>")
        by_label_cluster[key] = by_label_cluster.get(key, 0) + 1

    extra = {
        "tokens_generated": sum(tokens),
        "time_per_output_token_ms": reduce_latency(tpot) if tpot else {},
        "responses_by_model": by_model,
        "intended_label_to_model": by_label_cluster,
    }

    classify_us = [
        float(sc_header(r, "latency-us")) for r in records if sc_header(r, "latency-us") is not None
    ]
    overhead_p50 = cfg.param("praxis_overhead_p50_ms", None, float)
    overhead_p99 = cfg.param("praxis_overhead_p99_ms", None, float)
    if classify_us and overhead_p50 is not None:
        total = reduce_latency([r["wall_ns"] / 1e6 for r in records])
        classify = reduce_latency([v / 1000.0 for v in classify_us])
        extra["decomposition"] = {
            "p50": {
                "total": total["p50"],
                "praxis_overhead": overhead_p50,
                "classify_rtt": classify["p50"],
                "upstream": total["p50"] - overhead_p50 - classify["p50"],
                "source": "praxis_overhead from B-1 param; classify_rtt from x-llm-d-sc-latency-us",
            },
            "p99": {
                "total": total["p99"],
                "praxis_overhead": overhead_p99 if overhead_p99 is not None else overhead_p50,
                "classify_rtt": classify["p99"],
                "upstream": total["p99"] - (overhead_p99 if overhead_p99 is not None else overhead_p50)
                - classify["p99"],
                "source": "praxis_overhead from B-1 param; classify_rtt from x-llm-d-sc-latency-us",
            },
        }
    else:
        extra["decomposition_unavailable"] = (
            "classify_rtt is not observable from a client: the filter emits llm_d_sc.latency_us "
            "as metadata, not as an x-llm-d-sc-latency-us upstream header (SPEC §4.7)."
        )
    return extra


def cross_arm_assertions(ctx):
    """Identical generation settings across arms, or the comparison is void."""
    results = ctx["results"]
    settings = {}
    for name, res in results.items():
        settings[name] = (res.arm.params.get("max_tokens"), res.arm.params.get("temperature"),
                          res.arm.params.get("stream"))
    distinct = set(settings.values())
    out = []
    for name in results:
        out.append((
            name,
            [assertion(
                "generation_settings_identical_across_arms",
                len(distinct) == 1,
                "per-arm (max_tokens, temperature, stream): %s. SPEC-BENCH §1 B-5: different "
                "generation settings between arms would invalidate the comparison entirely."
                % settings,
            )],
        ))
    return out
