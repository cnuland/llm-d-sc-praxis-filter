# SPEC-BENCH — benchmarking the `llm_d_sc` filter

Extends `SPEC.md` and `SPEC-K8S.md`.

llm-d-sc already benchmarks **the classifier**. This document benchmarks **the gateway
that uses it** — the classification cost paid at the proxy, whether the routing is
actually correct, and whether the end-to-end payoff exceeds the cost. That last question
is the whole argument for content-aware routing, and nobody has measured it yet.

## 0. Methodology rules — inherited, non-negotiable

Taken from `~/llm-d-sc-genesis/AGENTS.md` and its `docs/benchmarks/`. These are the
house rules; a number that breaks one of them is not evidence.

1. **No average-only latency. Ever.** Every latency claim is p50/p90/p95/p99/max over a
   captured distribution.
2. **No performance claim without comparable before/after.** Every "cost of X" figure is
   a delta between two runs that differ *only* in X.
3. **The harness proves its own methodology.** A cache-hit scenario asserts the service's
   hit counter moved by exactly the measured count; a routing scenario asserts the
   `x-llm-d-sc-status` seen was the one the scenario intended. A scenario that cannot
   verify its own premise is a bug, not a result.
4. **Cache hit/miss workloads use disjoint key namespaces**, so a "miss" can never be
   silently served from cache. Copy the `run_id`-namespaced scheme in
   `~/llm-d-sc-genesis/src/bench.rs`.
5. **Every result carries a manifest**: git sha of both trees, image digests, model ids
   and revisions, taxonomy revision, node/CPU, topology, warmup and measured counts,
   timestamp, and the exact command.
6. **Published docs are generated** from the JSON. A number in prose that has no JSON
   behind it gets deleted.
7. **Disclose the environment honestly**: single homelab, single operator, not
   independently reproduced, shared cluster. Say it in the document, not a footnote.
8. **Corrections are published, not quietly replaced.** If a number turns out wrong,
   the doc says so and explains why — the precedent is set in
   `docs/benchmarks/topology.md`.

## 1. Scenario matrix

### B-1 — Filter overhead at the proxy (the #1017 number)

*Question: what does adding `llm_d_sc` cost a request, in isolation?*

Three arms, identical in every other respect, against a **local stub upstream** that
returns instantly (so the backend contributes no variance):

| Arm | Chain | Isolates |
|---|---|---|
| `baseline` | `router` → `load_balancer` | Praxis with static routing |
| `classified-miss` | `llm_d_sc` → `load_balancer`, unique prompt per request | full cost: body buffer + extract + gRPC + model forward |
| `classified-hit` | `llm_d_sc` → `load_balancer`, one repeated prompt | cost with llm-d-sc's cache hot |

Report p50/p90/p95/p99/max per arm and the **two deltas**. Expected shape from
llm-d-sc's own figures: hit ≈ +0.1–0.3 ms, miss ≈ +8–13 ms. If `classified-hit` is not
dramatically cheaper than `classified-miss`, the cache is not being exercised and the
run is invalid — assert it.

Concurrency: 1, 4, 16. Run **locally** (no cluster network) so this measures the filter,
not the fabric.

### B-2 — Body-size sensitivity (the Praxis-side cost nobody asked about yet)

*Question: `StreamBuffer` forces the whole request body to be buffered before routing.
What does that cost?*

Sweep body size — 1 KB, 8 KB, 64 KB, 256 KB, 1 MB — with the prompt held at a fixed
length so **only** the surrounding JSON grows. Two arms: `baseline` (Stream mode, no
body access) vs `classified`. The delta at each size is the buffering cost.

This is a genuine architectural cost of body-derived routing and it belongs in the
#1017 discussion. It has to be measured, not hand-waved.

### B-3 — Prompt-length sensitivity

Prompt lengths 32 / 64 / 128 / 256 / 512 tokens, cache-miss, concurrency 1 and 4.
Mirrors llm-d-sc's own table so the two are directly comparable, and shows how much of
the gateway's added latency is the model forward growing with input length.

### B-4 — Routing correctness (new — llm-d-sc measured classification, not routing)

*Question: does the right prompt reach the right tier?*

A labelled prompt set of **≥120 prompts**, ~30 per complexity class, authored for this
benchmark and **held out from the classifier's anchors**. Verbatim-overlap check against
`~/llm-d-sc-genesis/classifiers/complexity.json` anchors, asserted to be zero — copy the
leakage assertion `hack/benchmark-report` already makes.

Drive them through the gateway; record the cluster each landed on from the
`x-llm-d-sc-*` headers. Emit:
* **routing confusion matrix** (rows = intended tier, cols = actual cluster)
* routing accuracy, and per-class precision/recall
* the **misroute cost**: how many SIMPLE prompts reached the 284 B model (wasted
  capacity) and how many REASONING prompts reached the 27 B one (quality risk). These
  two errors are not symmetric and must be reported separately.

### B-5 — End-to-end payoff (the money chart)

*Question: does routing pay for itself?*

Against the **real homelab models**, three arms over the same labelled prompt set
(a small subset — see §3 capacity limits):

| Arm | Routing | Measures |
|---|---|---|
| `always-large` | everything → `ds4-flash-0731` (284 B) | today's "just use the big model" |
| `always-small` | everything → `qwen38-27b` (27 B) | the cheap floor |
| `classified` | `llm_d_sc` decides | the proposal |

Metrics per arm: total end-to-end wall clock (p50/p90/p95/p99/max), tokens generated,
and **time per output token**. The claim under test: `classified` p50 lands close to
`always-small` while sending the hard prompts to the strong model — i.e. the ~10 ms
classification cost buys back seconds of generation time.

**Latency decomposition per request**, which is the thing that makes this credible:

```
total_e2e = praxis_overhead + classify_rtt + upstream_time
            └ from B-1 ─┘   └ x-llm-d-sc-latency-us ┘  └ remainder ┘
```

Report the decomposition as a stacked breakdown at p50 and p99. If the components do
not sum to the measured total within a few percent, **say so and investigate** rather
than publishing a decomposition that does not reconcile.

Fixed generation settings across all arms (`max_tokens`, `temperature: 0`, same
`stream: false`), recorded in the manifest. Different generation settings between arms
would invalidate the comparison entirely.

### B-6 — Degradation and failure

| Case | Setup | Assert |
|---|---|---|
| classifier down | endpoint unreachable | 100% HTTP 200 via `general`; added latency ≈ connect-refused cost, **not** the full `timeout_ms` |
| classifier slow | stub that sleeps past `timeout_ms` | added latency ≈ `timeout_ms` and no more; 100% 200 |
| queue exhausted | drive concurrency past llm-d-sc's bound (256) to force `RESOURCE_EXHAUSTED` | fail-open holds; count the exhausted responses |
| fail-closed | `on_resource_exhausted: reject` | clean 503s, no hangs, no 5xx storm |

The "classifier down" latency figure matters: a fail-open path that still waits the full
timeout on every request is a fail-open path that has already taken the gateway down.

### B-7 — Topology (in-cluster, extends llm-d-sc's existing topology table)

Praxis → llm-d-sc across a ClusterIP Service, measured from the **bench Job inside the
cluster**. Directly comparable to llm-d-sc's existing same-Pod vs ClusterIP numbers
(22 µs for the hop). Adds the third row nobody has: *proxy-to-classifier across a
Service, under real gateway concurrency*.

**A number measured through `oc port-forward` is not a network measurement.** A probe
from the laptop through the tunnel showed p50 145 ms against an in-cluster expectation
of ~8–12 ms — the tunnel dominates by an order of magnitude. Every cluster latency
figure comes from a Job running inside the cluster. This trap is recorded here because
it is exactly the mistake this benchmark could ship without noticing.

## 2. Harness

`bench/` in the filter crate. Rust (matching llm-d-sc's approach) if it can reuse the
percentile/manifest machinery cleanly; otherwise Python 3 stdlib + a small async driver.
**Decide by reading `~/llm-d-sc-genesis/src/bench.rs` first** — reuse beats reinvention,
and comparability with the existing numbers matters more than language preference.

Requirements:
* per-request records, not pre-aggregated buckets, so distributions are recomputable
* percentile reducer shared with the reporting layer
* concurrency via a fixed worker pool, closed-loop, with warmup excluded from the window
* one JSON per run: `{manifest, scenarios: [{name, params, latency_ms{p50,p90,p95,p99,max}, throughput, errors, assertions}]}`
* `bench/report.py` generates `BENCHMARKS.md` from the JSON — prose never holds a number
  that JSON does not
* **every scenario carries its self-check** (rule 3) and the run fails loudly if a
  premise is violated

## 3. Capacity discipline — this is a shared homelab

The two model endpoints are **single-replica llama.cpp servers on someone's home
cluster**, and the 284 B model is a 104 GB IQ3_XXS quant.

* B-5 concurrency: **1**. Never more.
* B-5 sample count: start at **10 prompts per class**, measure how long one pass takes,
  and only scale up if a full pass is under ~10 minutes.
* `max_tokens` capped low (≈128) — this benchmark measures *routing*, not generation
  quality.
* B-1/B-2/B-3 use **stub upstreams**, never the real models. High-concurrency load goes
  nowhere near the homelab backends.
* Between B-5 arms, pause briefly so a backend is not hammered back-to-back.
* If a model endpoint returns errors or slows dramatically, **stop and report** — do not
  retry-storm someone's home lab.

## 4. Deliverables

* `bench/` harness + labelled prompt set + leakage assertion
* `bench/results/*.json` — raw, per-run, manifested
* `BENCHMARKS.md` — generated, publication-quality, structured like
  `~/llm-d-sc-genesis/upstream-staging/docs/performance.md`: environment table first,
  honesty disclaimer up top, call for external validation, then the tables
* A short **"What this says"** section per scenario group — the interpretation, including
  the results that are inconvenient. If classification does not pay for itself on this
  hardware, that is the finding and it gets published as the finding.

## 5. Definition of done

1. B-1 through B-4, B-6 complete locally with JSON + generated doc.
2. B-5 and B-7 complete in-cluster against the real endpoints, from an in-cluster Job.
3. Every number in `BENCHMARKS.md` traces to a JSON file in `bench/results/`.
4. Every scenario's self-assertions passed, and the doc says which assertions ran.
5. The homelab is exactly as it was found — verified against the pre-deploy snapshot.
