# `llm_d_sc` — a Praxis filter that routes by semantic classification

A [Praxis](https://github.com/praxis-proxy/praxis) HTTP filter that extracts the
prompt from an OpenAI-shaped request body, asks **llm-d-sc** to classify it over
the existing `classify.Classify` gRPC contract, and selects a Praxis **cluster**
from the returned ranked label.

llm-d-sc stays a signal producer. Praxis keeps routing authority. The wire
contract has no route field at all (ADR-0001 / AC-010), so the label→cluster
mapping is filter configuration, not classifier output.

Proof of concept for [praxis-proxy discussion #1017](https://github.com/orgs/praxis-proxy/discussions/1017).
The full contract is in [`SPEC.md`](SPEC.md); this README is the operator's view.

```
client ──POST /v1/chat/completions──▶ Praxis listener
                                        │
                 ┌──────────────────────┴───────────────────────┐
                 │ pre-read body (StreamBuffer, whole, one chunk)│
                 └──────────────────────┬───────────────────────┘
                                        │ on_request
                               ┌────────▼─────────┐
                               │  llm_d_sc filter │
                               │ 1 extract prompt │
                               │ 2 gRPC classify ─┼──h2c──▶ llm-d-sc :50051
                               │ 3 label→cluster  │◀── ranked signals + revisions
                               │ 4 ctx.cluster =  │
                               └────────┬─────────┘
                                        │ ctx.cluster
                               ┌────────▼─────────┐
                               │  load_balancer   │──▶ "small"   (e.g. 8B)
                               └──────────────────┘──▶ "large"   (e.g. 70B)
                                                   └──▶ "general" (fallback)
```

**The body is never modified.** The filter is read-only on the payload; it adds
provenance headers and picks a cluster.

## Wiring it in

The crate carries a `[package.metadata.praxis-filters]` marker and calls
`export_filters!`, so Praxis' build-time discovery (`server/build.rs`, via
`cargo metadata`) finds and registers it automatically. Operator cost is one
line in `praxis/server/Cargo.toml`:

```toml
llm-d-sc-praxis-filter = { path = "../../llm-d-sc-praxis-filter" }
```

Then use `llm_d_sc` in a filter chain. A complete, runnable config is in
[`examples/llm-d-sc-routing.yaml`](examples/llm-d-sc-routing.yaml) (and a test
asserts that file still parses and validates).

## Configuration

```yaml
- filter: llm_d_sc
  endpoint: "127.0.0.1:50051"        # required. host:port of llm-d-sc (h2c, no TLS in v0.1)
  default_cluster: general            # required. Fallback for every non-decision path.
  routes:                             # required, non-empty. Ranked label -> cluster.
    - { label: SIMPLE,    cluster: small }
    - { label: MEDIUM,    cluster: small }
    - { label: COMPLEX,   cluster: large }
    - { label: REASONING, cluster: large }
  signal: complexity                  # optional — see the caveat below. Omit by default.
  timeout_ms: 100                     # total budget for the classify RPC
  connect_timeout_ms: 1000
  min_score: 0.0                      # top score below this -> default_cluster
  max_prompt_chars: 4096              # prompt truncated (on a char boundary) before the RPC
  max_body_bytes: 1048576             # StreamBuffer ceiling; 413 above it
  on_unavailable: default_cluster     # default_cluster | reject
  on_resource_exhausted: default_cluster
  status_on_reject: 503               # used by both `reject` modes
  emit_headers: true                  # provenance headers to upstream
```

Unknown fields are rejected and every rule is checked at construction, so a bad
config fails proxy startup rather than a request.

## The one-signal caveat (constraint C3)

One llm-d-sc process serves **exactly one** signal, chosen by its
`LLM_D_SC_CLASSIFIER` environment variable. Its handler compares every entry in
`ClassifyRequest.signals` against that one name and answers gRPC
`INVALID_ARGUMENT` on any mismatch. An **empty** list skips the check entirely
and is always accepted.

So **`signal:` is unset by default and the filter sends `signals: []`.** Set it
only if you want a misconfiguration to be loud, and then it must equal the
server's `LLM_D_SC_CLASSIFIER` exactly. A mismatch is not a 4xx to the client:
it records `llm_d_sc.status = INVALID_ARGUMENT`, logs at `warn`, and follows
your `on_unavailable` posture.

Multi-signal routing means multiple llm-d-sc upstreams, and is out of scope for
v0.1.

## Prompt extraction

First match wins:

1. `messages[]` → the **last** element with `role == "user"`. Its `content` is a
   string, or an array of parts (the `type == "text"` parts, joined by newlines).
2. `prompt` → a string, or an array of strings joined by newlines.
3. `input` → the same shapes (embeddings-style requests).

Anything else — a non-JSON body, an empty body, JSON that is not an object, an
object with none of these fields — takes the skip path:
`ctx.cluster = default_cluster`, `llm_d_sc.status = SKIPPED_NO_PROMPT`, no RPC.
**Never a 4xx**: a classifier filter must not become a request validator.

## What happens when classification fails

| Outcome | Cluster | `llm_d_sc.status` |
|---|---|---|
| `status = OK`, top score ≥ `min_score`, label in `routes` | mapped cluster | `OK` |
| `status = OK`, label not in `routes` | `default_cluster` | `UNMAPPED_LABEL` |
| `status = OK`, top score < `min_score` | `default_cluster` | `LOW_CONFIDENCE` |
| `status = OK`, no ranked signals | `default_cluster` | `NO_SIGNAL` |
| `status = ABSTAIN` | `default_cluster` | `ABSTAIN` |
| `status = UNAVAILABLE` / `UNSPECIFIED` | per `on_unavailable` | `UNAVAILABLE` |
| gRPC `RESOURCE_EXHAUSTED` | per `on_resource_exhausted` | `RESOURCE_EXHAUSTED` |
| gRPC `INVALID_ARGUMENT` | per `on_unavailable` | `INVALID_ARGUMENT` |
| any other gRPC error | per `on_unavailable` | `ERROR` |
| local timeout elapsed | per `on_unavailable` | `TIMEOUT` |
| no prompt found | `default_cluster` | `SKIPPED_NO_PROMPT` |

**The default posture is fail-open.** A classifier outage degrades routing
quality; it does not take the gateway down. `reject` exists for deployments
where an unclassified prompt must not reach a model, and answers with
`status_on_reject`.

`ranked` is reduced with **max-by-score**, not `ranked[0]` — the wire contract
does not promise ordering, so the filter does not depend on it.

## Outputs

`ctx.filter_metadata` (survives every Pingora phase, visible to `access_log`):
`llm_d_sc.status`, `.label`, `.score` (4 dp), `.cluster`, `.classifier_id`,
`.model_revision`, `.tokenizer_revision`, `.taxonomy_revision`, `.latency_us`.

Upstream request headers when `emit_headers: true`: `x-llm-d-sc-label`,
`x-llm-d-sc-score`, `x-llm-d-sc-classifier`, `x-llm-d-sc-taxonomy-revision`,
`x-llm-d-sc-status`.

Metrics: `llm_d_sc_classify_attempt_total`,
`llm_d_sc_classify_total{status}`,
`llm_d_sc_classify_duration_seconds`,
`llm_d_sc_fallback_total{status}`, and
`llm_d_sc_route_total{label,cluster}`. The attempt counter increments only
when a classify RPC is started; `classify_total` also includes
`SKIPPED_NO_PROMPT`. The duration histogram measures only attempted RPCs.
`fallback_total` counts only fail-open fallback decisions; rejected
fail-closed decisions are not included. All label values come from fixed
enums or the configured route table, so cardinality is bounded and counters
can be summed across replicas.

For a bounded benchmark window, use counter increases rather than absolute
values, for example:

```promql
sum(increase(llm_d_sc_classify_attempt_total[30s]))
sum by (status) (increase(llm_d_sc_classify_total[30s]))
sum by (status) (increase(llm_d_sc_fallback_total[30s]))
```

**Security.** Client-supplied `x-llm-d-sc-*` headers are removed on *every* path
— including skip, timeout and reject — so a caller can never forge provenance or
influence a downstream policy filter. This mirrors `endpoint_selector`'s trust
posture. The protocol layer applies removals before the filter's own sets, so
upstream sees only what this filter decided.

## Costs you should know about

* **Whole-body buffering.** Routing on the body means the body must be complete
  before the request is routed. The filter declares
  `BodyAccess::ReadOnly` + `BodyMode::StreamBuffer`, which makes Praxis pre-read
  the entire request body before the header-phase pipeline. That is a real cost
  of body-derived routing, bounded by `max_body_bytes` (default 1 MiB; larger
  bodies get a 413). It applies to the whole chain, not just this filter.
* **Latency.** llm-d-sc's own numbers: ~22 µs network, ~56 µs tokenize,
  7.7–12.3 ms forward on a cache miss, ~0.09 ms on a cache hit. Budget with
  `timeout_ms`.
* **No caching here.** llm-d-sc already has a versioned result cache. A second
  cache in the proxy would duplicate it and risk serving a stale revision.
* **h2c only.** No TLS or mTLS to llm-d-sc in v0.1. That is the obvious next
  step, not a design position.

## Constraints this filter lives inside

* `llm_d_sc` **replaces** `router` in a chain; it does not sit next to it. Two
  cluster-selecting filters before the same `load_balancer` is a Praxis build
  error (`check_conflicting_cluster_selectors`), asserted by T-P2.
* Every `routes[].cluster` **and** `default_cluster` must be defined by the
  downstream `load_balancer` (`check_misaligned_clusters`), asserted by T-P3.
* llm-d-sc is frozen. `proto/classify.proto` is vendored byte-identical from
  `llm-d-sc-genesis`; T-U6 fails if the copy drifts. Nothing here ever patches
  upstream.

## Development

```sh
cargo test          # unit + gRPC integration (in-process stub) + pipeline validation
cargo build         # ships a gRPC client only
```

The `test-server` feature (enabled only by this crate's own dev-dependency)
additionally generates the `classify.Classify` **server** stubs used by the
integration suite's in-process stub, which counts accepted TCP connections to
prove the persistent-channel claim from the server side.

If `rustup` cannot fetch the toolchain Praxis pins, build with
`RUSTUP_TOOLCHAIN=stable`.
