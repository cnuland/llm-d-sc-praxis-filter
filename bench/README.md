# `bench/` — the benchmark harness for the `llm_d_sc` filter

llm-d-sc already benchmarks **the classifier**. This directory benchmarks **the
gateway that uses it**: the classification cost paid at the proxy, whether the
routing is actually correct, and whether the end-to-end payoff exceeds the cost.
That last question is the whole argument for content-aware routing, and it is
the one nobody has measured.

Everything here is Python 3 **standard library only**. No `pip install`, no
virtualenv, no lockfile. SPEC-K8S runs this as a Job inside the cluster on a
minimal image, and SPEC-BENCH §1 B-7 is explicit that a figure measured through
`oc port-forward` is a measurement of the tunnel — so the harness has to be able
to run where the numbers must be taken.

---

## Capacity discipline — read this first

**The model endpoints are single-replica llama.cpp servers on someone's shared
home cluster.** The "large" one is a 284 B IQ3_XXS quant occupying 104 GB. There
is no autoscaler, no spare replica, and no second operator to notice when it
falls over.

These are the rules from SPEC-BENCH §3. They are not advisory:

1. **B-5 concurrency is 1. Never more.** `harness.py` refuses to start a
   real-model scenario at any other concurrency, and refuses to start one at all
   without `--allow-homelab`.
2. **B-5 sample count starts at 10 prompts per class.** Time one pass. Scale up
   only if a full pass is under about ten minutes.
3. **`max_tokens` stays capped low (~128).** This benchmark measures *routing*,
   not generation quality. Long generations buy nothing here and cost a shared
   machine real time.
4. **B-1, B-2, B-3 and B-6 use stub upstreams, never the real models.** All
   high-concurrency load goes to `bench/stub_upstream.py` on loopback. If you
   find yourself pointing a concurrency-16 sweep at `homelab-maas`, stop.
5. **Pause between B-5 arms** so a backend is never hammered back to back. The
   scenario does this for you (`--param pause_s=30`); do not set it to zero.
6. **If an endpoint returns errors or slows dramatically, stop and report.** Do
   not retry-storm someone's home lab. Every arm's error budget is zero by
   default precisely so the run halts instead of grinding on.

Two more standing rules from the surrounding specs:

* **`~/llm-d-sc-genesis` is read-only.** It must stay byte-identical. Each run's
  manifest records its git sha and whether any tracked file was modified, so a
  violation is visible in the evidence rather than discovered later.
* **Nothing outside namespace `praxis-poc` is created, scaled, restarted or
  modified**, save the temporary and verified-restored scale-to-0 in SPEC-K8S
  V-5.

---

## What is here

| File | What it is |
|---|---|
| `harness.py` | The load driver: closed-loop fixed worker pool, warmup exclusion, per-request records, percentile reduction, manifest, self-assertions, JSON emission. |
| `stub_upstream.py` | Instant-return OpenAI-shaped upstream with configurable delay, header echo, and its own server-side counters. Also a TCP black hole for B-6. |
| `report.py` | Generates `BENCHMARKS.md` from `results/*.json`. Prose never holds a number that the JSON does not. |
| `prompts/complexity-heldout.json` | 128 labelled prompts, 32 per class, 8 marked `boundary`, authored for this benchmark. |
| `prompts/check_leakage.py` | Asserts zero verbatim overlap and zero near-duplicates against the classifier anchors. Exits non-zero on any leak. |
| `scenarios/` | One module per scenario, B-1 through B-7, plus a harness self-test. |
| `results/` | Run JSON, per-request `.records.jsonl` sidecars, and `leakage.json`. |

---

## The scenarios

| Scenario | Question | Target | Concurrency |
|---|---|---|---|
| `b1` | What does adding `llm_d_sc` cost a request, in isolation? | stub upstream | 1, 4, 16 |
| `b2` | What does `StreamBuffer` whole-body buffering cost as bodies grow? | stub upstream | 1 |
| `b3` | How does cost scale with prompt length? | stub upstream | 1, 4 |
| `b4` | Does the right prompt reach the right tier? | stub upstreams behind a real gateway | 1 |
| `b5` | **Does routing pay for itself?** | **real models** | **1, gated** |
| `b6` | What happens when the classifier is down, slow, or overloaded? | stub upstream + broken classifier endpoints | 1, and one saturation arm |
| `b7` | What does the Praxis → llm-d-sc hop cost across a Service? | in-cluster | 1, 4, 16 |

```bash
python3 bench/harness.py --list
python3 bench/harness.py --scenario b1 --dry-run    # prints the arm plan, sends nothing
```

---

## Running each scenario

### First: prove the harness, without touching anything

```bash
python3 bench/stub_upstream.py --port 9101 --model small-stub --echo-sc-headers &
python3 bench/harness.py --scenario smoke --target http://127.0.0.1:9101 \
    --warmup 100 --measured 400 --concurrency 1,4 --topology local-loopback-stub-only
```

This drives the stub directly. It measures Python talking to Python and **its
latencies are not results** — it exists to prove that the driver sends exactly
the work it claims (checked against the stub's own counters), that warmup is
excluded, that cache-key discipline holds at both ends of the socket, that
`x-llm-d-sc-*` capture works, and that the JSON comes out well-formed.

### Leakage check — before B-4, every time

```bash
python3 bench/prompts/check_leakage.py --json bench/results/leakage.json
```

`b4` runs this itself and refuses to build its arms if it does not exit 0. A
routing-accuracy number measured on the classifier's own anchor sentences is not
a measurement, it is a restatement of the anchor file.

Three things are checked against all three taxonomies (`complexity`, `cost`,
`sensitivity`): normalised verbatim equality, near-duplication, and internal
duplication inside the held-out set. Near-duplication is scored on two token
views — content tokens with stopwords removed, which catches long reworded
anchors, and full tokens, which catches short structural rewordings that the
content view is too sparse to see (*"What is the capital city of Spain?"* against
the anchor *"What is the capital of France?"* scores only 0.25 on content
Jaccard, and is caught at 0.63 on the full view). Exit codes: `0` clean, `1`
leak or schema violation, `2` anchors unavailable — an unread anchor file is a
failure, not a pass, because the claim would be unverified.

### B-1 — filter overhead

Needs **two Praxis listeners**, because SPEC §2.2 makes `router` and `llm_d_sc`
mutually exclusive in one chain (`check_conflicting_cluster_selectors` is a
build error, asserted by test T-P2). The stub upstream must echo provenance
headers or the scenario cannot verify that classification happened at all.

```bash
python3 bench/stub_upstream.py --port 9001 --model small-stub --echo-sc-headers &
# praxis :8080  chain = request_id -> router       -> load_balancer(:9001)
# praxis :8081  chain = request_id -> llm_d_sc     -> load_balancer(:9001)
python3 bench/harness.py --scenario b1 \
    --target http://127.0.0.1:8081 \
    --param baseline_url=http://127.0.0.1:8080 \
    --warmup 200 --measured 1000 --concurrency 1,4,16 \
    --topology local-loopback
```

Measures: p50/p90/p95/p99/max per arm and the two deltas — the cost with
llm-d-sc's cache hot, and the cost of a full classification. If the hit arm is
not dramatically cheaper than the miss arm the cache was never exercised, and
`hit_is_dramatically_cheaper_than_miss` fails the run rather than publishing it.

### B-2 — body-size sensitivity

```bash
python3 bench/harness.py --scenario b2 \
    --target http://127.0.0.1:8081 --param baseline_url=http://127.0.0.1:8080 \
    --warmup 100 --measured 500 --concurrency 1
```

Measures: the cost of `StreamBuffer` draining the whole body before routing, at
1 KB / 8 KB / 64 KB / 256 KB / 1 MB. The prompt is held byte-identical across
every size, so only the surrounding JSON grows and the delta is buffering alone.
This is a genuine architectural cost of body-derived routing and it belongs in
the #1017 discussion measured rather than hand-waved.

### B-3 — prompt-length sensitivity

```bash
python3 bench/harness.py --scenario b3 --target http://127.0.0.1:8081 \
    --warmup 100 --measured 300 --concurrency 1,4
```

Measures: cache-miss cost at 32 / 64 / 128 / 256 / 512 tokens. Deliberately
mirrors llm-d-sc's own cache-miss table so the two are directly comparable —
same lengths, same concurrencies, same nearest-rank percentile definition. The
difference between the two tables is the gateway.

### B-4 — routing correctness

```bash
python3 bench/stub_upstream.py --port 9001 --model small-stub --echo-sc-headers &
python3 bench/stub_upstream.py --port 9002 --model large-stub --echo-sc-headers &
# praxis :8081  llm_d_sc  routes SIMPLE,MEDIUM -> small(:9001)  COMPLEX,REASONING -> large(:9002)
python3 bench/harness.py --scenario b4 --target http://127.0.0.1:8081 \
    --warmup 20 --concurrency 1
```

Measures: the routing confusion matrix (rows = intended tier, cols = actual
cluster), routing accuracy, per-class precision/recall, and the **two misroute
costs reported separately** — SIMPLE prompts that reached the 284 B model
(wasted capacity) and REASONING prompts that reached the 27 B one (quality
risk). Those errors are not symmetric and are never netted against each other.

Attribution uses both sources SPEC-K8S §3.1 identifies — the `x-llm-d-sc-*`
provenance headers and the upstream's own response `model` field — and asserts
they agree. Real models are not required and should not be used: this measures
the routing decision, not generation.

### B-5 — end-to-end payoff (**the gated one**)

```bash
python3 bench/harness.py --scenario b5 --allow-homelab \
    --target http://praxis.praxis-poc.svc.cluster.local:8080 \
    --param large_url=http://llama-server-ds4.homelab-maas.svc.cluster.local:8080 \
    --param small_url=http://llama-server-qwen38.homelab-maas.svc.cluster.local:80 \
    --param per_class=10 --param max_tokens=128 --param pause_s=30 \
    --param praxis_overhead_p50_ms=<from the B-1 JSON> \
    --param praxis_overhead_p99_ms=<from the B-1 JSON> \
    --topology in-cluster-job --concurrency 1
```

Measures: total end-to-end wall clock per arm, tokens generated, time per output
token, and the latency decomposition. `praxis_overhead` is not invented — it is
a value copied from a completed B-1 run, which is why it is a parameter.

Re-read the capacity rules at the top of this file before running this. Without
`--allow-homelab` the harness prints them and exits 2.

### B-6 — degradation and failure

Each case is a separate Praxis listener, because the cases differ only in filter
config. Any case whose URL is not supplied is skipped.

```bash
python3 bench/stub_upstream.py --port 9001 --model small-stub --echo-sc-headers &
python3 bench/stub_upstream.py --mode tcp-blackhole --port 50099 &   # 'classifier slow'
# :8091 endpoint -> an unbound port          (classifier down)
# :8092 endpoint -> 127.0.0.1:50099          (classifier slow)
# :8093 endpoint -> a real local llm-d-sc    (queue exhausted)
# :8094 as :8093 but on_resource_exhausted: reject   (fail-closed)
python3 bench/harness.py --scenario b6 \
    --param down_url=http://127.0.0.1:8091 \
    --param slow_url=http://127.0.0.1:8092 \
    --param exhausted_url=http://127.0.0.1:8093 \
    --param reject_url=http://127.0.0.1:8094 \
    --param timeout_ms=100 --param exhaust_concurrency=300 \
    --warmup 50 --measured 300
```

Measures: that fail-open holds, and — the figure that actually matters — that
the classifier-down path costs the connect-refused round trip rather than the
full `timeout_ms`. A fail-open path that still waits the whole budget on every
request has converted a classifier outage into a tax on every request; it is
fail-open in name only. That is asserted, not assumed.

The queue-exhausted case needs a **real local llm-d-sc**, because only it has a
bounded queue to exhaust. A stub cannot produce a genuine `RESOURCE_EXHAUSTED`,
and the arm fails rather than pretending it did.

### B-7 — in-cluster topology

Run from `Job praxis-bench` **inside** the cluster:

```bash
python3 bench/harness.py --scenario b7 \
    --target http://praxis.praxis-poc.svc.cluster.local:8080 \
    --topology in-cluster-clusterip \
    --warmup 200 --measured 1000 --concurrency 1,4,16
```

Measures: Praxis → llm-d-sc across a ClusterIP Service under real gateway
concurrency — the third row that extends llm-d-sc's existing same-Pod vs
ClusterIP table. The scenario asserts it was not measured through a tunnel: a
port-forwarded probe once read p50 145 ms against an in-cluster expectation of
8–12 ms, and that trap is the one this suite could most easily ship without
noticing.

---

## Generating the document

```bash
python3 bench/report.py                 # results/*.json -> bench/BENCHMARKS.md
python3 bench/report.py --check         # render the B-4 / B-5 sections on a fixture, write nothing
```

`BENCHMARKS.md` is **generated**. Editing it by hand puts a number on the page
with no run behind it, which SPEC-BENCH §0 rule 6 says gets deleted. If a figure
needs to change, change the run.

---

## What the harness guarantees, and what it does not

**Guaranteed, in code:**

* No mean is ever computed. `reduce_latency` emits p50/p90/p95/p99/max and
  nothing else; there is no averaging code path in `harness.py`.
* Warmup runs as a separate phase and contributes no measured record.
  `warmup_excluded_from_window` asserts it on every arm.
* Every request produces a record — timestamp, wall-clock ns, HTTP status,
  response `model`, every `x-llm-d-sc-*` header, byte counts, error — written to
  a `.records.jsonl` sidecar. Nothing is pre-aggregated, so every distribution is
  recomputable.
* Cache-hit and cache-miss workloads use disjoint, run-id-namespaced keys,
  ported from `llm-d-sc/src/bench.rs`.
* Each worker pre-connects before the phase clock starts, so TCP establishment
  is never inside a measured per-request latency.
* Five self-assertions are applied to every arm by the harness itself, so a
  scenario author cannot forget them; scenarios add their own premise checks on
  top. **Any failed assertion exits non-zero.**
* Every run carries a manifest: UTC timestamp, git sha and working-tree state of
  both trees, host CPU, OS, Python version, target, topology label, warmup and
  measured counts, concurrency, and the exact argv.

**Not guaranteed — stated so it is not assumed:**

* **The classify RTT is not visible to a client.** SPEC §4.7 emits
  `llm_d_sc.latency_us` as filter metadata (visible to `access_log`) but the
  upstream header set is only label/score/classifier/taxonomy-revision/status.
  There is no `x-llm-d-sc-latency-us` header, so B-5's stacked decomposition
  cannot be built from measurement. When the header is absent the scenario
  records `decomposition_reconciles: passed=false` and explains why, rather than
  inferring the classify time by subtraction and publishing it as measured.
* **Provenance headers only reach the harness if the upstream echoes them.** The
  filter sets them on the *upstream* request, not the client response. The stub
  supports `--echo-sc-headers`; the real llama.cpp backends do not echo, so
  against real models attribution falls back to the response `model` field
  alone and the cross-check assertion has only one source to work with.
* **Prompt token counts are approximate.** Synthetic prompts are built from a
  bank of short common words on a one-word-one-token assumption. The records
  carry the actual word and character counts so the approximation is auditable.
* **llm-d-sc's cache counters are not read by this harness.** `bench.rs` asserts
  against the service's own hit/miss counters; from outside the proxy those are
  not reachable, so the client-side equivalents are key discipline (asserted
  structurally) plus the hit-versus-miss latency ratio (asserted numerically).
