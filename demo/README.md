# `demo/` — the end-to-end proof (SPEC Phase C)

One command that stands up the whole story on loopback and asserts it:

```bash
./demo/run-demo.sh
```

It prints a table, writes everything to `demo/evidence/<UTC timestamp>/`, tears
every process down on exit, and exits non-zero if any assertion failed.

## What it proves

| # | Claim | How it is asserted |
|---|---|---|
| T-E1 | A simple prompt routes to the **small** model cluster | `served_by == "small"` in the upstream's response body |
| T-E2 | A complex prompt routes to **large**; a reasoning prompt routes to **large** | same |
| T-E3 | With llm-d-sc **killed**, the same request still gets **200** and lands on **general** | fail-open, per SPEC §4.6 |
| — | Provenance headers actually reach the selected upstream | the stub echoes every `x-llm-d-sc-*` header it received |
| — | A client **cannot forge** provenance | every request is sent with `x-llm-d-sc-label: FORGED-BY-CLIENT`; if that value ever reaches the upstream, the check fails |

The three prompts are not chosen by intuition. They are **verbatim anchors** from
llm-d-sc's own complexity taxonomy
(`~/llm-d-sc-genesis/classifiers/complexity.json`), so the expected label is the
taxonomy's own definition of that class:

| Prompt | Label | Top score (measured, `llm-d-sc-classify`) |
|---|---|---|
| `What is the capital of France?` | `SIMPLE` | 0.9996 |
| `Design a microservices architecture for an e-commerce platform with inventory, orders, and payments.` | `COMPLEX` | 0.9998 |
| `Prove by mathematical induction that the sum of 1 to n equals n(n+1)/2.` | `REASONING` | 0.9995 |

## The topology

```
                 ┌──────────────────────────────┐
  curl ─POST──▶  │ praxis            :8080      │
  /v1/chat/      │   request_id                 │
  completions    │   llm_d_sc  ──h2c gRPC──────▶│──▶ llm-d-sc :50051  (real model)
                 │     label ──▶ cluster        │◀── ranked signals + revisions
                 │   load_balancer              │
                 └───┬──────────┬──────────┬────┘
                     │          │          │
             small :9101  large :9102  general :9103     ← stub-upstream.py ×3
```

llm-d-sc produces a **signal**. Praxis keeps **routing authority** — the
label→cluster mapping lives entirely in `praxis-demo.yaml` (SPEC C2).

## Files

| File | What it is |
|---|---|
| `run-demo.sh` | the whole demo: preflight, startup, assertions, fail-open, teardown, evidence |
| `praxis-demo.yaml` | the Praxis config. A worked example of every key in SPEC §4.2 — including the defaults, spelled out |
| `stub-upstream.py` | a trivial HTTP server standing in for a model backend. Python 3 stdlib only. Answers 200 with `{"served_by": ...}` and echoes back every `x-llm-d-sc-*` request header. Run three times: `small`, `large`, `general` |
| `evidence/` | one directory per run (created on first run) |

The stub upstreams are **not models**. They do no inference. The demo proves
*which backend a prompt was routed to*, not that the backend answered well.

## Prerequisites

**llm-d-sc** — already built, model already fetched, and this demo never writes
to that repo:

```bash
ls ~/llm-d-sc-genesis/target/release/llm-d-sc-server
ls ~/llm-d-sc-genesis/artifacts/models/complexity/model.safetensors
```

If either is missing, `run-demo.sh` says so and tells you the command to fix it.

**praxis, built with this filter crate wired in.** The server crate is
`praxis-proxy` but its binary is named `praxis`. Phase A costs exactly one line
in `~/praxis/server/Cargo.toml`:

```toml
[dependencies]
llm-d-sc-praxis-filter = { path = "/Users/<you>/llm-d-sc-praxis-filter" }
```

then:

```bash
cd ~/praxis && cargo build -p praxis-proxy
```

`run-demo.sh` finds the binary itself (release, then debug, then `$PATH`) and
runs `praxis -t -c demo/praxis-demo.yaml` before starting anything. That
subcommand instantiates every filter factory and runs the pipeline ordering
checks, so a binary built *without* the filter fails there with a readable
message instead of crashing three steps later.

## Running it

```bash
./demo/run-demo.sh
```

Useful overrides:

| Variable | Effect |
|---|---|
| `PRAXIS_BIN=/path/to/praxis` | skip binary autodiscovery |
| `PRAXIS_HOME=~/praxis` | where to search (default `~/praxis`) |
| `LLM_D_SC_HOME=~/llm-d-sc-genesis` | classifier repo root |
| `KEEP_RUNNING=1` | leave everything up afterwards so you can poke at `:8080` by hand |
| `SKIP_LATENCY_PROBE=1` | skip the gateway-probe latency capture |
| `RUST_LOG=debug` | passed through to praxis |

Ports used: `8080` (proxy), `9901` (admin), `50051` (llm-d-sc), `9101`/`9102`/`9103`
(stubs). All are checked up front; if one is busy the script names it and shows
you the `lsof` line to find the owner.

## Expected output

```
== classified routing (T-E1 / T-E2) ==

PROMPT                                   EXPECTED  ACTUAL    HTTP  LABEL             STATUS               RESULT
---------------------------------------------------------------------------------------------------------------
What is the capital of France?           small     small     200   SIMPLE            OK                   PASS
Design a microservices architecture ...  large     large     200   COMPLEX           OK                   PASS
Prove by mathematical induction that...  large     large     200   REASONING         OK                   PASS
---------------------------------------------------------------------------------------------------------------
cumulative: 3 checks, 0 failed.

== fail-open: classifier killed mid-run (T-E3) ==
Stopping llm-d-sc. Routing quality degrades; the gateway does not.
  llm-d-sc stopped; :50051 is closed.

PROMPT                                   EXPECTED  ACTUAL    HTTP  LABEL             STATUS               RESULT
---------------------------------------------------------------------------------------------------------------
What is the capital of France?           general   general   200   -                 -                    PASS
---------------------------------------------------------------------------------------------------------------
cumulative: 4 checks, 0 failed.

== result ==
PASS — 4/4 checks green.
```

The `LABEL` and `STATUS` columns are read out of the **upstream's** echo, not out
of the proxy's own log — they are proof the headers travelled, not a claim that
they were set.

## Evidence

Each run writes `demo/evidence/<UTC timestamp>/`, with `evidence/latest`
symlinked to the newest:

```
run.log                  everything the script printed
summary.json             {"checks": 4, "failures": 0, "result": "PASS"}
results.tsv              machine-readable rows
environment.txt          host, tool versions, binary paths, git SHAs of all three repos
praxis-demo.yaml         the exact config used
praxis.log               proxy stdout/stderr
llm-d-sc.log             classifier stdout/stderr, including the READY line
upstream-{small,large,general}.log
<slug>.request.json      the exact body sent
<slug>.response.json     the upstream's identity + echoed provenance
<slug>.response.headers  raw response headers
praxis-metrics.txt       /metrics scrape (llm_d_sc_* series)
classify-latency.json    gateway-probe percentiles
```

## The latency caveat — measured, not hidden

Classification is not free. Measured on this host (Apple silicon, 50 samples,
`llm-d-sc-gateway-probe` against the same llm-d-sc process the demo uses):

```
cache MISS  p50 = 7.9 ms   p99 = 12.9 ms
cache HIT   p50 = 0.09 ms  p99 = 0.12 ms
```

So a **first-seen prompt adds roughly 8–13 ms** to the request, and a **repeated
prompt adds under 0.1 ms**. That cost is dominated by the model forward pass;
the loopback gRPC round trip is tens of microseconds.

Three things follow, and the demo is built around them:

* **The filter does not cache.** llm-d-sc already has a versioned result cache
  keyed on the taxonomy revision. A second cache in the proxy would duplicate it
  and risk serving a classification from a stale revision (SPEC §7).
* **`timeout_ms: 100`** in `praxis-demo.yaml` is ~8× the p50 miss. It is a
  circuit breaker for a sick classifier, not a tuning knob you should be near.
* **`run-demo.sh` sends one un-asserted warm-up request** before the measured
  ones. Without it the first assertion would also pay TCP + HTTP/2 connection
  setup (the gRPC channel is built with `connect_lazy`) inside that 100 ms
  budget, and the timings in the evidence would be misleading.

The other real cost is memory, not time: `request_body_mode: StreamBuffer` makes
Praxis buffer the **whole request body** before routing, because the routing
decision is derived from the body. `max_body_bytes` (1 MiB here) bounds it.

llm-d-sc itself takes **~4 s to reach READY** from cold (validate the ModelCar
layout, load tokenizer + config + safetensors, run a warmup forward), or ~1 s
with a warm page cache. `run-demo.sh` waits for the literal `READY` log line
rather than for the port to open, because llm-d-sc binds only after warmup
succeeds — a model directory that merely *exists* never produces READY.

## What this demo does not do

* No TLS to llm-d-sc. h2c only in v0.1 (SPEC §7); the obvious next step.
* One signal only. `LLM_D_SC_CLASSIFIER=complexity`, one endpoint (SPEC C3).
  Multi-signal routing would mean multiple llm-d-sc upstreams.
* No real inference behind the clusters.
* No `router` filter. Two cluster-selecting filters ahead of the same
  `load_balancer` is a build error in Praxis, so `llm_d_sc` replaces `router`
  rather than joining it (SPEC §2.2). `praxis-demo.yaml` says so inline.

## Poking at it by hand

```bash
KEEP_RUNNING=1 ./demo/run-demo.sh

curl -s -X POST http://127.0.0.1:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"x","messages":[{"role":"user","content":"Analyze the time complexity of this recursive algorithm using the Master theorem."}]}' \
  | python3 -m json.tool

curl -s http://127.0.0.1:9901/metrics | grep llm_d_sc
```

Then stop the processes with the `kill` lines the script prints on exit.
