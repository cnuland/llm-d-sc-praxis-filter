"""B-5 — End-to-end payoff, measured from inside the cluster. *The money chart.*

*Does routing pay for itself?* Every other scenario measures a cost. This one
measures whether the cost buys anything, which is the whole argument for
content-aware routing and the thing nobody had measured.

Three arms over the same held-out prompts, against the REAL homelab models,
every one of them through Praxis so the proxy's cost is inside all three
columns rather than only the one under test:

| Arm            | Listener | Chain                                                            |
|----------------|----------|------------------------------------------------------------------|
| `always-large` | :8082    | request_id, access_log, router->large, credential_injection, lb   |
| `always-small` | :8083    | request_id, access_log, router->small, lb                         |
| `classified`   | :8080    | request_id, access_log, llm_d_sc, credential_injection, lb        |

`router` and `llm_d_sc` cannot share a chain -- `check_conflicting_cluster_selectors`
rejects two cluster-selecting filters before one load_balancer -- so the static
arms are separate chains on separate listeners rather than a variant of the
production one. Everything else about them is deliberately identical, including
`credential_injection`, which the `always-large` arm needs for exactly the same
reason the production chain does: ds4 answers 401 without a bearer token, and an
arm that measured 401s would be measuring error latency.

Why this module and not `b5_end_to_end.py`
------------------------------------------
Two things are true in the cluster that were not true when that module was
written, and both change what can be asserted.

**1. The classify RTT is now emitted -- but the real backends cannot echo it.**
The filter sets `x-llm-d-sc-latency-us` on the UPSTREAM request. `b5_end_to_end`
reads it off the client response, which works against a stub started with
`--echo-sc-headers`. llama.cpp echoes nothing; verified live, the response
carries no `x-llm-d-sc-*` header at all. So the per-request join that module
wants is still unavailable here, and SPEC-BENCH §1 B-5 is explicit that the
answer is to say so rather than to infer a component by subtraction.

What this module does instead is measure the same quantity two other ways, both
in-cluster and neither of them a subtraction:

* a `classify-probe` arm: the same prompt set through
  `request_id -> llm_d_sc -> static_response`, which pays the proxy cost and the
  full classification and then answers without an upstream. That is
  `praxis_overhead + classify_rtt` measured per request, with real percentiles.
  Its prompts are a DISJOINT, class-matched slice of the held-out set, because
  reusing the classified arm's prompts would have one of the two arms served
  from llm-d-sc's result cache and neither would mean what it says.
* Praxis's own `llm_d_sc_classify_duration_seconds` summary, read from the admin
  endpoint at the end of the classified arm. In-cluster, independent of the
  client, and a window distribution rather than a per-request join -- which is
  stated wherever it is reported.

`upstream_time` remains the remainder, which is what SPEC-BENCH's own formula
calls it. It is labelled `measured: false` in the output so nobody can mistake
it for something that was timed.

**2. The homelab is shared, and contention is not noise.** Both backends are
single-slot llama.cpp servers. During this benchmark's setup a probe expecting
~1 s returned after 168 s: the server's own timings showed 1.03 s of work behind
155 s of waiting for another tenant's 3 155-token generation. A B-5 table
measured through that is a table of somebody else's session lengths. So each arm
gates on the backend reporting itself idle before it starts, and afterwards
compares the backend's own `tokens_predicted_total` delta against the tokens
this harness actually asked for. Tokens the backend generated that we did not
request are a foreign tenant, and they fail the arm's premise.

Capacity discipline (SPEC-BENCH §3), enforced in code
-----------------------------------------------------
`TARGETS_REAL_MODELS = True` makes `harness.py` refuse to run this without
`--allow-homelab` and refuse outright above concurrency 1. Ten prompts per class,
`max_tokens` 128, `temperature` 0, `stream` false -- identical across all three
arms and recorded in the manifest, because different generation settings between
arms would invalidate the comparison entirely. Arms pause between each other.
Every arm's error budget is zero, so a failing backend stops the run instead of
being retried.

Warmup is zero here, on purpose. Forty requests per arm against a single-replica
llama.cpp server is already the whole budget, and a warmup request would
pre-populate llm-d-sc's result cache for a prompt in the measured window --
understating the very classification cost the arm exists to price. The harness
still asserts warmup exclusion; zero warmup records is the honest form of it.

Run (from the in-cluster driver Pod):
    python3 bench/harness.py --scenario b5c --allow-homelab --concurrency 1 \\
        --topology in-cluster-job --warmup 0 \\
        --target http://praxis.praxis-poc.svc.cluster.local:8080 \\
        --param large_url=http://praxis-bench.praxis-poc.svc.cluster.local:8082 \\
        --param small_url=http://praxis-bench.praxis-poc.svc.cluster.local:8083 \\
        --param probe_url=http://praxis-bench.praxis-poc.svc.cluster.local:8084 \\
        --param metrics_url=http://praxis-bench.praxis-poc.svc.cluster.local:9901/metrics \\
        --param praxis_overhead_p50_ms=0.144833 --param praxis_overhead_p99_ms=0.170873
"""

from __future__ import annotations

import time

import _llamacpp_metrics as lm
import _praxis_metrics as pm
from _common import chat_body
from harness import Arm, Request, assertion, reduce_latency

SPEC_ID = "B-5"
DESCRIPTION = "End-to-end payoff against the real models, in-cluster: always-large vs always-small vs classified."
TARGETS_REAL_MODELS = True
NOTES = [
    "Concurrency is fixed at 1 and enforced by the harness (SPEC-BENCH §3).",
    "Generation settings are identical across all arms and recorded in the manifest; different "
    "settings between arms would invalidate the comparison entirely.",
    "All three arms go through Praxis, so the proxy's cost is inside every column rather than "
    "only the classified one.",
    "Arms pause between each other and each one waits for its backend to report itself idle "
    "before starting; both backends are single-slot llama.cpp servers on a shared home cluster.",
    "The classify RTT is still not joinable per request: the filter sets x-llm-d-sc-latency-us "
    "on the upstream request and llama.cpp does not echo it back (verified live). It is measured "
    "two other ways instead -- a static_response probe arm, and Praxis's own summary -- and "
    "upstream_time remains the remainder, labelled as such.",
    "Warmup is zero by design; a warmup request would seed llm-d-sc's cache for a measured prompt.",
]

LARGE_MODEL = "ds4-flash-0731"
SMALL_MODEL = "qwen38-27b"

# Tokens the backend generated that this harness did not ask for, as a fraction
# of what it did ask for, before the arm is treated as contended. Not zero:
# llama.cpp's counter also moves for speculative-decode bookkeeping, so an exact
# reconciliation would fail for a reason that has nothing to do with tenancy.
FOREIGN_TOKEN_TOLERANCE = 0.05

# How far the two independent in-cluster measurements of `praxis + classify` may
# diverge before the decomposition is reported as not reconciling. Wider than
# SPEC-BENCH's "a few percent" for the sum-to-total form, because one side is a
# rolling-window summary exported by a different process and the two windows do
# not line up exactly.
RECONCILE_TOLERANCE = 0.15


def _by_class(prompts):
    out = {}
    for rec in prompts:
        out.setdefault(rec["label"], []).append(rec)
    return out


def _slice(prompts, per_class, offset=0):
    """`per_class` prompts from each label, starting at `offset`.

    The offset is what keeps the probe arm's prompts disjoint from the measured
    arms' while holding the class balance identical.
    """
    grouped = _by_class(prompts)
    out = []
    for label in sorted(grouped):
        out.extend(grouped[label][offset:offset + per_class])
    return out


def _make_build(prompts, max_tokens, temperature):
    def build(index, phase, run_id):
        rec = prompts[index % len(prompts)]
        return Request(
            body=chat_body(rec["prompt"], model="bench-router", max_tokens=max_tokens,
                           temperature=temperature, stream=False),
            meta={"prompt_key": rec["id"], "prompt_id": rec["id"],
                  "intended_label": rec["label"], "boundary": bool(rec.get("boundary")),
                  "phase": phase},
        )

    return build


# ---------------------------------------------------------------------------
# Per-arm instrumentation: Praxis counters + the backend's own counters
# ---------------------------------------------------------------------------


def _make_setup(arm_name, metrics_url, backends):
    """Gate on every backend this arm can reach being idle, then snapshot counters."""

    def setup(ctx):
        ctx["b5c/gate/" + arm_name] = {
            b["name"]: lm.wait_until_idle(b["metrics_url"], bearer_env=b.get("bearer_env"))
            for b in backends}
        ctx["b5c/praxis_before/" + arm_name] = pm.scrape(metrics_url) if metrics_url else None
        ctx["b5c/backend_before/" + arm_name] = {
            b["name"]: lm.snapshot(b["metrics_url"], bearer_env=b.get("bearer_env"))
            for b in backends}

    return setup


def _rtt_or_none(quantiles):
    """Drop a summary whose rolling window has emptied.

    `metrics-exporter-prometheus` exports this metric as a SUMMARY over a
    rolling window. If no classification happened inside that window the
    quantiles come back as a row of zeros, which is not "the hop took 0 ms" --
    it is "there is nothing to report". Publishing the zeros would be the worse
    kind of wrong number, so they are turned into an explicit absence.

    This is not hypothetical: in the B-5 shakedown the classified arm's last
    request sat 73 s behind a foreign tenant, its classification fell out of the
    window before the arm ended, and the endpoint duly reported p50 = 0.
    """
    if not quantiles:
        return None
    values = [v for k, v in quantiles.items() if k != "n"]
    if not values or all(v == 0.0 for v in values):
        return {"window_empty": True, "n_cumulative": quantiles.get("n"),
                "note": "the exporter's rolling summary window held no classification when it "
                        "was read, so no quantiles are available for this arm"}
    return quantiles


def _observe(ctx, arm_name, metrics_url, backends):
    cache = "b5c/after/" + arm_name
    if cache in ctx:
        return ctx[cache]
    before = ctx.get("b5c/praxis_before/" + arm_name)
    after = pm.scrape(metrics_url) if metrics_url else None
    obs = {
        "gate": ctx.get("b5c/gate/" + arm_name),
        "classify_total_delta": (pm.classify_total(after) - pm.classify_total(before))
                                if (before and after) else None,
        "classify_ok_delta": ((pm.classify_by_status(after).get("OK", 0.0)
                               - pm.classify_by_status(before).get("OK", 0.0))
                              if (before and after) else None),
        "route_delta": ({k: v - pm.route_by_label(before).get(k, 0.0)
                         for k, v in pm.route_by_label(after).items()
                         if v - pm.route_by_label(before).get(k, 0.0) != 0}
                        if (before and after) else None),
        "praxis_classify_rtt_ms": _rtt_or_none(pm.classify_duration_quantiles(after) if after else None),
        "backend_before": ctx.get("b5c/backend_before/" + arm_name) or {},
        "backend_after": {b["name"]: lm.snapshot(b["metrics_url"], bearer_env=b.get("bearer_env"))
                          for b in backends},
    }
    ctx[cache] = obs
    return obs


def _contention_assertion(result, obs, backend_name):
    """Did this arm have that backend to itself, for the requests that reached it?

    `ours` counts only the responses this backend actually served, identified by
    the upstream's own `model` field -- SPEC-K8S §3.1 makes that the primary
    attribution source. The classified arm reaches both backends, so netting all
    of its tokens against one backend's counter would manufacture a foreign
    tenant that does not exist.
    """
    before = (obs.get("backend_before") or {}).get(backend_name)
    after = (obs.get("backend_after") or {}).get(backend_name)
    name = "backend_served_only_this_arm[%s]" % backend_name
    if not after or after.get("error"):
        return assertion(
            name, False,
            "%s's own counters could not be read (%s), so this arm cannot show that a foreign "
            "tenant was not occupying the single slot. SPEC-BENCH §0 rule 3: an unverifiable "
            "premise is a bug, not a result."
            % (backend_name, (after or {}).get("error", "no snapshot")))
    ours = sum(r.get("completion_tokens") or 0 for r in result.ok_records()
               if r.get("model") == backend_name)
    served = lm.delta(before, after, "llamacpp:tokens_predicted_total")
    if served is None:
        return assertion(name, False,
                         "llamacpp:tokens_predicted_total was not exported by %s" % backend_name)
    foreign = served - ours
    budget = max(1.0, ours * FOREIGN_TOKEN_TOLERANCE)
    return assertion(
        name, foreign <= budget,
        "%s generated %.0f tokens during this arm; this harness asked for %d of them, leaving "
        "%.0f unaccounted (budget %.0f, %.0f%%). Unaccounted tokens mean another tenant was on "
        "the single slot and its generation time landed inside this arm's wall clock. Idle gate "
        "before the arm: %s"
        % (backend_name, served, ours, foreign, budget, FOREIGN_TOKEN_TOLERANCE * 100,
           (obs.get("gate") or {}).get(backend_name)),
    )


def _assertions(result, ctx, *, arm_name, expect_model, metrics_url, backends, classified):
    obs = _observe(ctx, arm_name, metrics_url, backends)
    records = result.ok_records()
    models = {}
    for r in records:
        models[r.get("model") or "<none>"] = models.get(r.get("model") or "<none>", 0) + 1
    out = []

    if expect_model is not None:
        out.append(assertion(
            "arm_reached_only_its_intended_backend", set(models) == {expect_model},
            "response model tally %s; this arm exists to measure %s alone, so any other id means "
            "the arm was mis-wired and its column of the comparison is invalid"
            % (models, expect_model)))

    if classified:
        expected = result.arm.warmup + result.arm.measured
        out.append(assertion(
            "classification_observed", obs["classify_total_delta"] is not None
            and abs(obs["classify_total_delta"] - expected) < 0.5,
            "Praxis's llm_d_sc_classify_total moved by %s across this arm; expected exactly %d. "
            "The response-header check the local scenarios use is unavailable here: the filter "
            "sets provenance on the upstream request and llama.cpp does not echo it."
            % (obs["classify_total_delta"], expected)))
        out.append(assertion(
            "every_classification_succeeded", obs["classify_ok_delta"] is not None
            and abs(obs["classify_ok_delta"] - expected) < 0.5,
            "%s of %d classifications returned OK. Routing delta: %s"
            % (obs["classify_ok_delta"], expected, obs["route_delta"])))
        out.append(assertion(
            "traffic_split_across_backends",
            len([m for m in models if m in (LARGE_MODEL, SMALL_MODEL)]) >= 2,
            "response model tally %s; the classified arm must reach both backends or it is "
            "measuring one of the other two arms under a different name" % models))
    elif expect_model is not None:
        out.append(assertion(
            "arm_did_not_classify", obs["classify_total_delta"] == 0,
            "Praxis's llm_d_sc_classify_total moved by %s across this static arm; expected "
            "exactly 0. Any movement means the arm was not statically routed and the comparison "
            "is not what it claims." % obs["classify_total_delta"]))

    for b in backends:
        out.append(_contention_assertion(result, obs, b["name"]))

    if expect_model is not None or classified:
        tokens = [r.get("completion_tokens") for r in records if r.get("completion_tokens")]
        out.append(assertion(
            "generation_accounted", len(tokens) == len(records) and len(records) > 0,
            "%d/%d responses reported completion_tokens; time-per-output-token is only computable "
            "where they did" % (len(tokens), len(records))))
    return out


def _summarize(result, ctx, *, arm_name, metrics_url, backends, classified, cfg):
    obs = _observe(ctx, arm_name, metrics_url, backends)
    records = result.ok_records()
    tokens = [r.get("completion_tokens") or 0 for r in records]
    tpot = [(r["wall_ns"] / 1e6) / r["completion_tokens"] for r in records
            if r.get("completion_tokens")]
    by_model = {}
    by_label_model = {}
    for r in records:
        m = r.get("model") or "<none>"
        by_model[m] = by_model.get(m, 0) + 1
        key = "%s->%s" % (r["meta"].get("intended_label"), m)
        by_label_model[key] = by_label_model.get(key, 0) + 1

    extra = {
        "tokens_generated": sum(tokens),
        "time_per_output_token_ms": reduce_latency(tpot) if tpot else {},
        "responses_by_model": by_model,
        "intended_label_to_model": by_label_model,
        "backend_idle_gate": obs.get("gate"),
    }
    if obs.get("backend_after"):
        extra["backend_counters"] = {
            b: {
                "tokens_predicted_delta": lm.delta(obs["backend_before"].get(b),
                                                   obs["backend_after"].get(b),
                                                   "llamacpp:tokens_predicted_total"),
                "prompt_tokens_delta": lm.delta(obs["backend_before"].get(b),
                                                obs["backend_after"].get(b),
                                                "llamacpp:prompt_tokens_total"),
                "harness_completion_tokens": sum(r.get("completion_tokens") or 0
                                                 for r in records if r.get("model") == b),
                "harness_prompt_tokens": sum(r.get("prompt_tokens") or 0
                                             for r in records if r.get("model") == b),
            } for b in obs["backend_after"]}
    if obs.get("backend_after"):
        extra["foreign_generation_tokens"] = {
            b: (extra["backend_counters"][b]["tokens_predicted_delta"]
                - extra["backend_counters"][b]["harness_completion_tokens"])
            if extra["backend_counters"][b]["tokens_predicted_delta"] is not None else None
            for b in obs["backend_after"]}
        extra["foreign_generation_note"] = (
            "tokens the backend generated during this arm that this harness did not request. "
            "These are single-slot llama.cpp servers on a shared home cluster, so a foreign "
            "tenant's generation time lands inside this arm's wall clock rather than beside it.")
    if obs.get("classify_total_delta") is not None:
        extra["praxis_classify_total_delta"] = obs["classify_total_delta"]
        extra["praxis_route_delta"] = obs["route_delta"]
    if classified:
        extra["decomposition"] = _decomposition(result, ctx, obs, cfg)
    return extra


def _decomposition(result, ctx, obs, cfg):
    """`total_e2e = praxis_overhead + classify_rtt + upstream_time`, with provenance.

    Every entry says where it came from and whether it was measured, because
    SPEC-BENCH §1 B-5 forbids publishing a component that was inferred by
    subtraction as though it had been timed.

    What is measured, per request, in-cluster:
      * `total` -- this arm's own client-side wall clock.
      * `praxis_plus_classify` -- the `classify-probe` arm: the identical proxy
        chain up to and including `llm_d_sc`, terminated by `static_response`
        instead of an upstream. Nothing is subtracted to obtain it.

    What is measured, in-cluster, but as a window distribution rather than a
    per-request join:
      * `classify_rtt_praxis_summary` -- Praxis's own
        `llm_d_sc_classify_duration_seconds`.

    What is measured elsewhere and supplied as a parameter:
      * `praxis_overhead_b1` -- SPEC-BENCH's prescribed source for this term, a
        local-loopback figure, so it is reported but NOT used to reconcile.
      * `praxis_floor_incluster` -- the B-7 control arm
        (`request_id -> static_response`), which is the same chain shape as the
        probe minus `llm_d_sc`, measured in this cluster. That is the floor the
        reconciliation uses.

    What is NOT measured:
      * `upstream_remainder` -- the remainder, exactly as SPEC-BENCH's own
        formula defines it, flagged `upstream_measured: false`.

    The reconciliation that is actually worth something is between two
    independent measurements: `praxis_floor_incluster + classify_rtt_praxis_summary`
    against the probe arm's measured total. Neither side is derived from the
    other, so agreement is evidence and disagreement is a finding.
    """
    total = reduce_latency([r["wall_ns"] / 1e6 for r in result.ok_records()])
    probe = ctx.get("b5c/probe_latency_ms") or {}
    probe_rtt = ctx.get("b5c/probe_praxis_rtt_ms") or {}
    if probe_rtt.get("window_empty"):
        probe_rtt = {}
    praxis_rtt = obs.get("praxis_classify_rtt_ms") or {}

    out = {
        "per_request_join_available": False,
        "per_request_join_note": (
            "The filter emits x-llm-d-sc-latency-us on the UPSTREAM request, and llama.cpp does "
            "not echo it back (verified live: the response carries no x-llm-d-sc-* header at "
            "all). Praxis's built-in access_log records neither the llm_d_sc.* metadata nor a "
            "request id -- observed request_id=\"-\" -- so there is no field to join a "
            "per-request classify time to a per-request total against the real backends. The "
            "probe arm and Praxis's own summary are the honest substitutes, and each is labelled "
            "with what it actually is."),
        "sources": {
            "total": "measured per request, client side, in-cluster",
            "praxis_plus_classify": "measured per request, in-cluster: the classify-probe arm, "
                                    "over a disjoint class-matched slice of the same held-out set",
            "classify_rtt_praxis_summary_probe_window": "measured in-cluster by Praxis itself, "
                                                       "read while the probe arm's requests still "
                                                       "filled the exporter's rolling window. A "
                                                       "window distribution, not a per-request join",
            "classify_rtt_praxis_summary_at_arm_end": "the same metric read at the end of the "
                                                      "classified arm. Often absent: that arm "
                                                      "issues one request every few seconds, so "
                                                      "its classifications age out of the "
                                                      "exporter's window before it finishes",
            "praxis_overhead_b1": "measured: the B-1 baseline arm, local loopback, supplied as a "
                                  "parameter. Reported because SPEC-BENCH names it; not used to "
                                  "reconcile, because it is not an in-cluster figure",
            "praxis_floor_incluster": "measured: the B-7 control arm in this cluster, supplied as "
                                      "a parameter",
            "upstream_remainder": "NOT measured. The remainder, exactly as SPEC-BENCH's own "
                                  "formula defines it",
        },
    }
    for pct in ("p50", "p99"):
        floor = cfg.param("praxis_floor_%s_ms" % pct, None, float)
        row = {
            "total": total.get(pct),
            "praxis_plus_classify": probe.get(pct),
            "classify_rtt_praxis_summary_probe_window": probe_rtt.get(pct),
            "classify_rtt_praxis_summary_at_arm_end": (
                praxis_rtt.get(pct) if not praxis_rtt.get("window_empty") else None),
            "praxis_overhead_b1": cfg.param("praxis_overhead_%s_ms" % pct, None, float),
            "praxis_floor_incluster": floor,
        }
        if total.get(pct) is not None and probe.get(pct) is not None:
            row["upstream_remainder"] = total[pct] - probe[pct]
            row["upstream_measured"] = False
        if floor is not None and probe_rtt.get(pct) is not None and probe.get(pct):
            predicted = floor + probe_rtt[pct]
            row["reconciliation"] = {
                "predicted_praxis_plus_classify": predicted,
                "measured_praxis_plus_classify": probe[pct],
                "residual_ms": probe[pct] - predicted,
                "residual_fraction": abs(probe[pct] - predicted) / probe[pct],
            }
        out[pct] = row
    return out


def _decomposition_assertion(result, ctx, obs, cfg):
    """Are the decomposition's components measured, and do the two independent ones agree?

    SPEC-BENCH §1 B-5: "If the components do not sum to the measured total
    within a few percent, say so and investigate rather than publishing a
    decomposition that does not reconcile." The sum-to-total form of that check
    is vacuous when one term is defined as the remainder, so what is checked
    here is the part that is not vacuous: two independent in-cluster
    measurements of `praxis + classify` -- the probe arm's wall clock, and
    Praxis's own classify summary added to the B-7 in-cluster floor -- must
    agree. Nothing on one side of that comparison is derived from the other.
    """
    probe = ctx.get("b5c/probe_latency_ms") or {}
    praxis_rtt = ctx.get("b5c/probe_praxis_rtt_ms") or {}
    if praxis_rtt.get("window_empty"):
        praxis_rtt = {}
    floor = cfg.param("praxis_floor_p50_ms", None, float)
    have = bool(probe.get("p50")) and bool(praxis_rtt.get("p50")) and floor is not None
    if not have:
        return assertion(
            "decomposition_components_are_measured_not_inferred", False,
            "the decomposition is incomplete: probe_p50=%s praxis_summary_p50=%s "
            "praxis_floor_p50_ms=%s. SPEC-BENCH §1 B-5 requires saying so rather than filling a "
            "gap by subtraction." % (probe.get("p50"), praxis_rtt.get("p50"), floor))
    predicted = floor + praxis_rtt["p50"]
    residual = abs(probe["p50"] - predicted)
    frac = residual / probe["p50"]
    return assertion(
        "decomposition_components_are_measured_not_inferred", frac <= RECONCILE_TOLERANCE,
        "praxis+classify measured per request by the probe arm: %.3f ms p50. Independently "
        "predicted from Praxis's own classify summary (%.3f ms) plus the B-7 in-cluster floor "
        "(%.3f ms): %.3f ms. Residual %.3f ms (%.1f%%, tolerance %.0f%%). upstream_time is the "
        "remainder and is flagged measured=false; no per-request join to a classify time exists, "
        "because llama.cpp does not echo x-llm-d-sc-latency-us."
        % (probe["p50"], praxis_rtt["p50"], floor, predicted, residual, frac * 100,
           RECONCILE_TOLERANCE * 100))


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


def arms(cfg):
    prompts = cfg.load_prompts()
    per_class = cfg.param("per_class", 10, int)
    max_tokens = cfg.param("max_tokens", 128, int)
    temperature = cfg.param("temperature", 0.0, float)
    pause_s = cfg.param("pause_s", 30, int)
    metrics_url = cfg.param("metrics_url", None)
    gen = {"max_tokens": max_tokens, "temperature": temperature, "stream": False}

    measured_prompts = _slice(prompts, per_class, offset=0)
    probe_prompts = _slice(prompts, per_class, offset=per_class)

    small_backend = {"name": SMALL_MODEL,
                     "metrics_url": cfg.param(
                         "small_metrics_url",
                         "http://llama-server-qwen38.homelab-maas.svc.cluster.local:80/metrics")}
    large_backend = {"name": LARGE_MODEL,
                     "metrics_url": cfg.param(
                         "large_metrics_url",
                         "http://llama-server-ds4.homelab-maas.svc.cluster.local:8080/metrics"),
                     "bearer_env": cfg.param("large_metrics_bearer_env", "DS4_API_KEY")}

    specs = [
        ("classify-probe", cfg.param("probe_url", None), None, [], False,
         "praxis + full classification, answered by static_response so no model is contacted. "
         "Disjoint class-matched prompts, so neither this arm nor `classified` is served from "
         "llm-d-sc's cache because of the other.", probe_prompts, 0),
        ("always-large", cfg.param("large_url", None), LARGE_MODEL, [large_backend], False,
         "everything to the 284 B model: today's 'just use the big one'", measured_prompts, pause_s),
        ("always-small", cfg.param("small_url", None), SMALL_MODEL, [small_backend], False,
         "everything to the 27 B model: the cheap floor", measured_prompts, pause_s),
        ("classified", cfg.param("classified_url", cfg.target), None,
         [small_backend, large_backend], True,
         "llm_d_sc decides: the proposal under test", measured_prompts, 0),
    ]

    out = []
    for name, url, expect_model, backends, classified, note, plist, pause in specs:
        if url is None:
            continue
        arm = Arm(
            name=name,
            target=url,
            build=_make_build(plist, max_tokens, temperature),
            params=dict(gen, prompts=len(plist), per_class=per_class,
                        expected_model=expect_model,
                        prompt_ids=[p["id"] for p in plist]),
            warmup=cfg.param("warmup_override", 0, int),
            measured=len(plist),
            concurrency=1,
            cache_mode="n/a",
            timeout_s=cfg.param("timeout_s", 600.0, float),
            allow_errors=0,
            notes=note,
        )
        arm.setup = _make_setup(name, metrics_url, backends)
        arm.teardown = (lambda ctx, s=pause: time.sleep(s)) if pause else None
        arm.assertions = (lambda result, ctx, n=name, em=expect_model, b=backends, c=classified:
                          _arm_assertions(result, ctx, n, em, metrics_url, b, c, cfg))
        arm.summarize = (lambda result, ctx, n=name, b=backends, c=classified:
                         _summarize(result, ctx, arm_name=n, metrics_url=metrics_url,
                                    backends=b, classified=c, cfg=cfg))
        out.append(arm)
    return out


def _arm_assertions(result, ctx, name, expect_model, metrics_url, backends, classified, cfg):
    out = _assertions(result, ctx, arm_name=name, expect_model=expect_model,
                      metrics_url=metrics_url, backends=backends, classified=classified)
    if name == "classify-probe":
        # This arm IS a measurement of praxis + classify, so record it for the
        # decomposition and prove it actually classified.
        obs = _observe(ctx, name, metrics_url, backends)
        expected = result.arm.warmup + result.arm.measured
        out.append(assertion(
            "probe_arm_classified_every_request",
            obs["classify_total_delta"] is not None
            and abs(obs["classify_total_delta"] - expected) < 0.5,
            "Praxis's llm_d_sc_classify_total moved by %s across the probe arm; expected exactly "
            "%d. The probe only means `praxis + classify` if every request was classified."
            % (obs["classify_total_delta"], expected)))
        out.append(assertion(
            "probe_arm_contacted_no_model",
            all((r.get("model") or "") == "b7-static" for r in result.ok_records()),
            "every probe response must come from static_response (model b7-static), not from a "
            "model endpoint; tally %s"
            % {r.get("model"): 1 for r in result.ok_records()}))
        ctx["b5c/probe_latency_ms"] = dict(result.latency_ms)
        # Praxis's own view of the SAME requests, read immediately after a dense
        # burst so the exporter's rolling window is still full of them. Pairing
        # the cross-check here rather than at the end of the classified arm is
        # deliberate: the classified arm issues one request every few seconds
        # against a model, so by the time it ends most of its classifications
        # have aged out of the window.
        ctx["b5c/probe_praxis_rtt_ms"] = obs.get("praxis_classify_rtt_ms")
    if classified:
        obs = _observe(ctx, name, metrics_url, backends)
        out.append(_decomposition_assertion(result, ctx, obs, cfg))
    return out


def cross_arm_assertions(ctx):
    """Identical generation settings across the three comparable arms, or the comparison is void."""
    results = ctx["results"]
    compared = {n: r for n, r in results.items() if n != "classify-probe"}
    settings = {n: (r.arm.params.get("max_tokens"), r.arm.params.get("temperature"),
                    r.arm.params.get("stream")) for n, r in compared.items()}
    prompt_sets = {n: tuple(r.arm.params.get("prompt_ids") or ()) for n, r in compared.items()}
    distinct_settings = set(settings.values())
    distinct_prompts = set(prompt_sets.values())
    out = []
    for name in compared:
        out.append((name, [
            assertion(
                "generation_settings_identical_across_arms", len(distinct_settings) == 1,
                "per-arm (max_tokens, temperature, stream): %s. SPEC-BENCH §1 B-5: different "
                "generation settings between arms would invalidate the comparison entirely."
                % settings),
            assertion(
                "same_prompts_across_arms", len(distinct_prompts) == 1,
                "the three compared arms must run the identical prompt list, or the columns are "
                "not comparable; %d distinct prompt lists seen" % len(distinct_prompts)),
        ]))

    # The payoff, recorded next to the numbers it came from.
    if {"always-large", "always-small", "classified"} <= set(results):
        big, small, cls = (results["always-large"], results["always-small"], results["classified"])
        ctx["extra"]["b5_payoff_ms"] = {
            "p50": {"always_large": big.latency_ms["p50"], "always_small": small.latency_ms["p50"],
                    "classified": cls.latency_ms["p50"],
                    "classified_vs_always_large": cls.latency_ms["p50"] - big.latency_ms["p50"],
                    "classified_vs_always_small": cls.latency_ms["p50"] - small.latency_ms["p50"]},
            "p99": {"always_large": big.latency_ms["p99"], "always_small": small.latency_ms["p99"],
                    "classified": cls.latency_ms["p99"],
                    "classified_vs_always_large": cls.latency_ms["p99"] - big.latency_ms["p99"],
                    "classified_vs_always_small": cls.latency_ms["p99"] - small.latency_ms["p99"]},
        }
    return out
