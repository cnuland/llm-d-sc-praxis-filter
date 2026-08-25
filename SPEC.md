# SPEC — `llm_d_sc` filter for Praxis Proxy (POC v0.1)

Status: **SPECIFIED** · Target: proof of concept for
[praxis-proxy discussion #1017](https://github.com/orgs/praxis-proxy/discussions/1017)

## 0. One-line statement

A Praxis HTTP filter that extracts the prompt from an OpenAI-shaped request body,
asks **llm-d-sc** to classify it over the existing gRPC contract, and selects a
Praxis **cluster** from the returned ranked label — so llm-d-sc stays a signal
producer and Praxis keeps routing authority.

## 1. Hard constraints

| # | Constraint | Consequence |
|---|---|---|
| C1 | **llm-d-sc is frozen.** No change to `proto/classify.proto`, the server, the taxonomies, or the container. | The proto is *vendored verbatim* into the filter crate. Any gap is closed on the Praxis side or written up as a finding, never patched upstream. |
| C2 | llm-d-sc returns **no route field** (ADR-0001 / AC-010). | The label→cluster mapping is filter config. Routing policy lives in Praxis. |
| C3 | One llm-d-sc process serves **exactly one signal** (`LLM_D_SC_CLASSIFIER`, default `complexity`). | Multi-signal routing = multiple upstreams; **out of scope for v0.1**. Config carries a single `endpoint`. |
| C4 | POC, not a product. | Prefer the smallest thing that provably works over generality. No new abstractions in Praxis core. |

## 2. Verified facts (read from source, not assumed)

### 2.1 llm-d-sc side — `~/llm-d-sc-genesis`

* Service: `classify.Classify/Classify`, unary, **gRPC over h2c (plaintext)**, default `0.0.0.0:50051`
  (`src/bin/server.rs`).
* Request: `request_id`, `session_id`, `context`, `repeated signals`.
* Response: `request_id`, `classifier_id`, `model_revision`, `tokenizer_revision`,
  `taxonomy_revision`, `status`, `repeated RankedSignal { label, score }`.
* `ClassificationStatus`: `UNSPECIFIED=0, OK=1, ABSTAIN=2, UNAVAILABLE=3`.
* **Interop trap (verified `src/grpc/classify.rs:167-177`)**: every entry in
  `signals` is compared against the *one* signal this instance serves; a mismatch
  returns gRPC `INVALID_ARGUMENT`. An **empty** `signals` list skips the loop and
  is always accepted.
  → **The filter sends `signals: []` by default.** It only sends `[signal]` when the
  operator explicitly sets `signal:` in config, and that value must match the
  server's `LLM_D_SC_CLASSIFIER`.
* gRPC failure statuses the filter must handle:
  * `RESOURCE_EXHAUSTED` — bounded inference queue full (`try_enqueue` rejection, and
    `ClassifyError::ResourceExhausted`).
  * `UNAVAILABLE` — executor stopped / runtime error.
  * `INVALID_ARGUMENT` — unsupported signal (misconfiguration).
* v0.1 taxonomy labels (`classifiers/complexity.json`): `SIMPLE`, `MEDIUM`, `COMPLEX`,
  `REASONING`. (`cost`: MINIMAL/LOW/MODERATE/HIGH. `sensitivity`:
  PUBLIC/INTERNAL/CONFIDENTIAL/REGULATED/NEVER_EGRESS.)
* Dependency versions: `tonic 0.14`, `prost 0.14`, `tonic-prost-build 0.14`.

### 2.2 Praxis side — `~/praxis` (0.5.2 snapshot, edition 2024, MSRV 1.96)

* **`tonic 0.14.6` and `tonic-prost 0.14.6` are already workspace dependencies**
  (pulled in by `opentelemetry-otlp`). Exact version match with llm-d-sc — no new
  third-party surface at the workspace level, and no codegen skew.
* Extension mechanism (`docs/filters/extensions.md`): an external crate marked with
  `[package.metadata.praxis-filters]` that calls `export_filters! { http "name" => factory }`
  is auto-discovered by `server/build.rs` (via `cargo metadata`) and registered at startup.
  **Operator cost: one `Cargo.toml` line in `server/`.**
* `HttpFilter` hooks used: `on_request`, `on_request_body`, plus the capability
  declarations `selects_cluster()`, `selected_clusters()`, `request_body_access()`,
  `request_body_mode()`.
* **Body pre-read, verified** (`protocol/src/http/pingora/handler/request_filter/`):
  1. `compute_body_capabilities` honours `request_body_mode()` **only if the filter
     also declares `request_body_access() != BodyAccess::None`**
     (`filter/src/pipeline/body.rs:accumulate_request_body`).
  2. When the merged mode is `StreamBuffer`, `pre_read_body()` runs **before** the
     header-phase pipeline, drains the whole body, and stores it as **exactly one
     frozen chunk**: `ctx.pre_read_body = Some(VecDeque::from([forwarded]))`.
  3. `HttpFilterContext::buffered_request_body` is `pre_read_body.front().cloned()`
     — i.e. **the complete body is available in `on_request`**, before upstream
     selection (`protocol/src/http/pingora/context.rs:281`).
* **Pipeline validation constraints** (`filter/src/pipeline/checks.rs`):
  * `check_lb_without_cluster_selector` — a `load_balancer` must be preceded by a
    filter whose `selects_cluster()` is true. ✅ ours is.
  * `check_conflicting_cluster_selectors` — **two cluster-selecting filters before the
    same `load_balancer` is a build ERROR.** ⇒ `llm_d_sc` and `router` **cannot** share
    a chain segment. POC configs use `llm_d_sc` *instead of* `router`.
  * `check_misaligned_clusters` — every name in `selected_clusters()` must be defined
    by the downstream `load_balancer`. ⇒ `selected_clusters()` must return the mapped
    clusters **and** `default_cluster`.
* Public API confirmed exported for external crates: `HttpFilter`, `HttpFilterContext`,
  `FilterAction`, `FilterError`, `Rejection`, `BodyAccess`, `BodyMode`,
  `parse_filter_config`, `FilterRegistry`, `export_filters!`.
* Provenance precedent: `endpoint_selector` deliberately ignores client-supplied header
  values to prevent SSRF, trusting only filter-set values. We mirror that posture.

## 3. Architecture

```
client ──HTTP POST /v1/chat/completions──> Praxis listener
                                             │
                    ┌────────────────────────┴───────────────────────┐
                    │ pre-read body (StreamBuffer, whole body, 1 chunk)│
                    └────────────────────────┬───────────────────────┘
                                             │  on_request
                                    ┌────────▼─────────┐
                                    │  llm_d_sc filter │
                                    │ 1 extract prompt │
                                    │ 2 gRPC classify ─┼──h2c──> llm-d-sc :50051
                                    │ 3 label→cluster  │<──ranked signals + revisions
                                    │ 4 ctx.cluster =  │
                                    └────────┬─────────┘
                                             │  ctx.cluster
                                    ┌────────▼─────────┐
                                    │  load_balancer   │──> cluster "small"  (e.g. 8B)
                                    └──────────────────┘──> cluster "large"  (e.g. 70B)
```

**The body is never modified.** The filter is read-only on the payload; it adds
provenance headers and picks a cluster.

## 4. Filter contract

### 4.1 Name

`llm_d_sc`

### 4.2 YAML configuration

```yaml
- filter: llm_d_sc
  endpoint: "127.0.0.1:50051"        # required. host:port of llm-d-sc (h2c, no TLS in v0.1)
  signal: complexity                  # optional. When set, sent in ClassifyRequest.signals
                                      # and MUST match the server's LLM_D_SC_CLASSIFIER.
                                      # Omit (default) to send an empty list — always accepted.
  default_cluster: general            # required. Fallback for every non-decision path.
  routes:                             # required, non-empty. Ranked label -> cluster.
    - label: SIMPLE
      cluster: small
    - label: MEDIUM
      cluster: small
    - label: COMPLEX
      cluster: large
    - label: REASONING
      cluster: large
  timeout_ms: 100                     # default 100. Total budget for the classify RPC.
  min_score: 0.0                      # default 0.0. Top score below this -> default_cluster.
  max_prompt_chars: 4096              # default 4096. Prompt truncated before the RPC.
  max_body_bytes: 1048576             # default 1 MiB. StreamBuffer ceiling (413 above it).
  on_unavailable: default_cluster     # default_cluster | reject   (default: default_cluster)
  on_resource_exhausted: default_cluster  # default_cluster | reject  (default: default_cluster)
  status_on_reject: 503               # default 503. Used by both `reject` modes.
  emit_headers: true                  # default true. Provenance headers to upstream.
  connect_timeout_ms: 1000            # default 1000.
```

`deny_unknown_fields` on every config struct (Praxis convention). Validation at
construction — a bad config fails proxy startup, never a request.

Construction-time validation rules:
* `endpoint` non-empty and parses as a URI authority.
* `routes` non-empty; labels unique; no empty cluster names.
* `default_cluster` non-empty.
* `status_on_reject` in `100..=599`.
* `timeout_ms`, `connect_timeout_ms`, `max_prompt_chars`, `max_body_bytes` all `> 0`.
* `max_body_bytes <= 64 MiB` (Praxis `ABSOLUTE_MAX_BODY_BYTES`).

### 4.3 Trait implementation

| Hook | Value | Why |
|---|---|---|
| `name()` | `"llm_d_sc"` | registry key |
| `selects_cluster()` | `true` | satisfies `check_lb_without_cluster_selector` |
| `selected_clusters()` | every `routes[].cluster` **+ `default_cluster`**, deduped | satisfies `check_misaligned_clusters` |
| `request_body_access()` | `BodyAccess::ReadOnly` | **required** for `StreamBuffer` to be honoured |
| `request_body_mode()` | `BodyMode::StreamBuffer { max_bytes: Some(cfg.max_body_bytes) }` | triggers whole-body pre-read before routing |
| `on_request_body()` | returns `FilterAction::BodyDone` immediately | we never inspect chunks; work happens in `on_request` |
| `on_request()` | the whole flow (§4.5) | body already pre-read and complete here |
| `on_response()` | default (`Continue`) | nothing to do |

### 4.4 Prompt extraction (v0.1, deliberately narrow)

Input: `ctx.buffered_request_body` parsed as JSON (`serde_json`). Order:

1. `messages: [...]` → the **last** element whose `role == "user"`. Its `content` is either
   * a string → use it; or
   * an array of parts → concatenate `part.text` for parts where `type == "text"`, joined by `"\n"`.
2. else `prompt` → a string, or an array of strings joined by `"\n"` (legacy completions).
3. else `input` → string or array of strings (embeddings-shaped).
4. else → **no prompt**: skip the RPC entirely, `ctx.cluster = default_cluster`,
   metadata `llm_d_sc.status = "SKIPPED_NO_PROMPT"`.

Non-JSON, empty body, or JSON that is not an object → same skip path. **Never a 4xx**: a
classifier filter must not become a request validator.

The extracted text is trimmed and truncated to `max_prompt_chars` **on a char boundary**
(not a byte slice — must not panic on multi-byte UTF-8).

### 4.5 Request flow (`on_request`)

```
1. strip client-supplied x-llm-d-sc-* headers      (anti-spoof, always, even on skip)
2. extract prompt (§4.4); none -> default_cluster, record, return Continue
3. build ClassifyRequest {
     request_id: ctx request id if present else generated,
     session_id: "" (v0.1 — no session concept in the filter),
     context:    truncated prompt,
     signals:    cfg.signal.map(|s| vec![s]).unwrap_or_default(),
   }
4. tokio::time::timeout(cfg.timeout_ms, client.classify(req))
5. map outcome -> cluster (§4.6)
6. ctx.cluster = Some(Arc::from(cluster))
7. record filter_metadata + provenance headers + metrics
8. Ok(FilterAction::Continue)
```

`request_id`: prefer an existing `x-request-id` header (Praxis `request_id` filter sets
one) so a classification can be correlated with the proxy access log. Fall back to a
per-request counter-derived id; **never** a random UUID dependency.

### 4.6 Outcome → cluster decision table

| Outcome | Cluster | `llm_d_sc.status` | Notes |
|---|---|---|---|
| gRPC OK, `status = OK`, top score ≥ `min_score`, label in `routes` | mapped cluster | `OK` | the happy path |
| gRPC OK, `status = OK`, label **not** in `routes` | `default_cluster` | `UNMAPPED_LABEL` | forward-compatible with taxonomy growth |
| gRPC OK, `status = OK`, top score < `min_score` | `default_cluster` | `LOW_CONFIDENCE` | |
| gRPC OK, `status = OK`, `ranked` empty | `default_cluster` | `NO_SIGNAL` | |
| gRPC OK, `status = ABSTAIN` | `default_cluster` | `ABSTAIN` | |
| gRPC OK, `status = UNAVAILABLE` or `UNSPECIFIED` | per `on_unavailable` | `UNAVAILABLE` | |
| gRPC `RESOURCE_EXHAUSTED` | per `on_resource_exhausted` | `RESOURCE_EXHAUSTED` | **discussion #1017's explicit question** |
| gRPC `INVALID_ARGUMENT` | per `on_unavailable` | `INVALID_ARGUMENT` | misconfigured `signal:`; logged at `warn` |
| any other gRPC error | per `on_unavailable` | `ERROR` | |
| local timeout elapsed | per `on_unavailable` | `TIMEOUT` | |

**Default posture is fail-open** (`default_cluster`): a classifier outage degrades routing
quality, it does not take the gateway down. `reject` exists for deployments where an
unclassified prompt must not reach a model (e.g. sensitivity-gated egress), and answers
with `status_on_reject` (default 503).

`ranked` is reduced with **max-by-score**, not `ranked[0]` — the wire contract does not
promise ordering, so we do not depend on it.

### 4.7 Outputs

**`ctx.filter_metadata`** (survives every Pingora phase, visible to `access_log`):

| Key | Example |
|---|---|
| `llm_d_sc.status` | `OK` |
| `llm_d_sc.label` | `COMPLEX` |
| `llm_d_sc.score` | `0.7421` (4 dp) |
| `llm_d_sc.cluster` | `large` |
| `llm_d_sc.classifier_id` | `complexity` |
| `llm_d_sc.model_revision` | `c5f55ef4…` |
| `llm_d_sc.tokenizer_revision` | … |
| `llm_d_sc.taxonomy_revision` | `scr-default-anchors-v1` |
| `llm_d_sc.latency_us` | `8213` |

**Upstream request headers** when `emit_headers: true`:
`x-llm-d-sc-label`, `x-llm-d-sc-score`, `x-llm-d-sc-classifier`,
`x-llm-d-sc-taxonomy-revision`, `x-llm-d-sc-status`, `x-llm-d-sc-latency-us`.

`x-llm-d-sc-latency-us` exists for the benchmark, and the reason is worth stating:
the end-to-end decomposition `total = praxis + classify + upstream` is only a
**measured** identity if the classify hop is visible from outside the proxy. A client
can time the total; the upstream can report its own share; nothing outside Praxis can
see the classify RPC. Without this header the decomposition could only be produced by
subtraction, which silently absorbs every unrelated cost into "classify" and would make
the headline benchmark chart unfalsifiable. It is emitted only when an RPC was actually
attempted — the skip path reports no duration rather than a fabricated zero (T-U10).

**Security**: client-supplied `x-llm-d-sc-*` headers are removed on *every* path —
including skip, timeout, and reject — so a caller can never forge provenance or
influence a downstream policy filter. Mirrors `endpoint_selector`'s trust posture.

**Metrics** (`metrics` crate, already a Praxis dependency):
* `llm_d_sc_classify_total{status}` counter — `status` is the fixed enum above
* `llm_d_sc_classify_duration_seconds` histogram
* `llm_d_sc_route_total{label,cluster}` counter — `cluster` comes from config

Cardinality is bounded **by enforcement, not by assumption**. The ranked label on the
wire is chosen by llm-d-sc, not by Praxis, so emitting it verbatim would let an upstream
service mint unbounded Prometheus series by growing its taxonomy, by misconfiguration,
or by compromise. `llm_d_sc_route_total` therefore emits a ranked label only when that
label appears in the operator's own `routes` table; anything else collapses to the
sentinel `<unmapped>`. The per-request `x-llm-d-sc-label` header and the
`llm_d_sc.label` metadata still carry the true label — a header creates no time series,
so bounding the metric costs no observability. Tested by **T-U9**, including a case that
feeds 1000 distinct server-supplied labels and asserts exactly one series results.

### 4.8 gRPC client

* Built **once at filter construction** from `Endpoint::from_shared("http://{endpoint}")`
  with `.connect_timeout(...)`, `.tcp_nodelay(true)`, `.http2_keep_alive_interval(...)`,
  `.keep_alive_while_idle(true)`, then **`.connect_lazy()`**.
  * `connect_lazy` means: no blocking I/O in `from_config`, the proxy starts even if
    llm-d-sc is not up yet, and tonic reconnects transparently. Matches llm-d-sc's own
    persistent-channel design (I-008: N calls, one TCP accept).
* Per request: `ClassifyClient::new(channel.clone())` — a `Channel` clone is a cheap
  handle over the same multiplexed HTTP/2 connection, not a new connection.
* Codegen: `tonic-prost-build` in `build.rs` over the **vendored, byte-identical**
  `proto/classify.proto`. Vendoring keeps the crate buildable standalone; a test asserts
  the vendored file still matches upstream (§6, T-U6).

## 5. Deliverables

### Phase A — standalone crate `~/llm-d-sc-praxis-filter`

```
Cargo.toml            [package.metadata.praxis-filters] marker; praxis-proxy-filter path dep
build.rs              tonic-prost-build over proto/classify.proto (client only, no server)
proto/classify.proto  vendored verbatim from llm-d-sc-genesis
src/lib.rs            export_filters! { http "llm_d_sc" => LlmDScFilter::from_config }
src/config.rs         serde config + construction-time validation
src/prompt.rs         JSON prompt extraction (§4.4) — pure, heavily unit-tested
src/client.rs         tonic channel + classify call + status mapping
src/filter.rs         HttpFilter impl
src/metrics.rs        metric names/helpers
tests/                unit + integration (stub gRPC server)
examples/llm-d-sc-routing.yaml   runnable Praxis config
README.md             what it does, how to wire it, the C3 one-signal caveat
```

Praxis side, Phase A: **exactly one line** in `~/praxis/server/Cargo.toml`.

### Phase B — in-tree diff against `~/praxis`

Mechanical port of the same code to
`filter/src/builtins/http/traffic_management/llm_d_sc/`, behind a **`llm-d-sc-filter`
cargo feature** (off by default), registered in `filter/src/registry.rs`, following the
`basic-auth-filter` feature-gate precedent exactly. Plus `examples/configs/ai/`,
`docs/filters/http/traffic_management/llm_d_sc.md`, and an integration test under
`tests/integration/tests/suite/examples/`. Produced as a reviewable `git diff` on the
`~/praxis` baseline commit — the artifact to attach to discussion #1017.

Phase B starts only after Phase A demonstrably routes traffic.

### Phase C — local end-to-end demo

`demo/` in the filter crate:
* `run-demo.sh` — starts real llm-d-sc, two stub upstreams, and Praxis; curls three
  prompts; prints which cluster each landed on.
* llm-d-sc: `LLM_D_SC_MODEL_DIR=~/llm-d-sc-genesis/artifacts/models/complexity`
  `LLM_D_SC_CLASSIFIER=complexity LLM_D_SC_LISTEN=127.0.0.1:50051`
  `~/llm-d-sc-genesis/target/release/llm-d-sc-server` — **already built, model already
  fetched. No changes to that repo.**
* Upstreams: two trivial HTTP servers that echo which one they are (`small` / `large`).
* Expected: `"What is the capital of France?"` → `small`;
  `"Design a microservices architecture for..."` → `large`;
  classifier stopped → both → `general` (fail-open), proxy still 200s.

## 6. Test plan

Unit (no network):
* **T-U1** config: valid parse; unknown field rejected; empty `routes` rejected; duplicate
  label rejected; bad status code rejected; zero timeout rejected.
* **T-U2** `selected_clusters()` contains every mapped cluster **and** `default_cluster`, deduped.
* **T-U3** prompt extraction: chat string content; chat array-of-parts content; multiple
  messages picks the **last** user; system-only → none; `prompt` string; `prompt` array;
  `input`; malformed JSON → none; empty body → none.
* **T-U4** truncation at `max_prompt_chars` never panics on multi-byte UTF-8 (emoji/CJK at
  the boundary).
* **T-U5** decision table (§4.6) — one case per row, exercised through a seam that takes a
  `Result<ClassifyResponse, Status>` so every branch is reachable without a socket.
* **T-U6** vendored `proto/classify.proto` is byte-identical to
  `~/llm-d-sc-genesis/proto/classify.proto` (skipped with a clear message if absent).
* **T-U7** max-by-score, not first-element: a response whose highest score is at index 2
  still routes to index 2's label.
* **T-U8** client-supplied `x-llm-d-sc-label` is stripped even when classification is
  skipped.

Integration (in-process stub gRPC server implementing `classify.Classify`):
* **T-I1** OK + `COMPLEX` → `ctx.cluster == "large"`, metadata + headers populated.
* **T-I2** stub returns `RESOURCE_EXHAUSTED` → `default_cluster`, `status` metadata set,
  `Continue` (fail-open).
* **T-I3** same, with `on_resource_exhausted: reject` → `FilterAction::Reject` with 503.
* **T-I4** stub sleeps past `timeout_ms` → `default_cluster`, `TIMEOUT`.
* **T-I5** endpoint refused (nothing listening) → `default_cluster`, no panic, and the
  proxy still starts (proves `connect_lazy`).
* **T-I6** two sequential requests use **one** TCP connection (stub counts accepts) —
  the persistent-channel claim, measured server-side, mirroring llm-d-sc's own I-008.
* **T-I7** stub asserts it received `signals: []` when `signal:` is unset, and
  `signals: ["complexity"]` when it is set.

Pipeline validation:
* **T-P1** `llm_d_sc` + `load_balancer` builds clean.
* **T-P2** `llm_d_sc` + `router` + `load_balancer` **fails** to build with the conflicting-
  cluster-selector error — asserted so the constraint is documented by a test, not a comment.
* **T-P3** a `routes[].cluster` absent from the `load_balancer` fails to build.

E2E (Phase C, scripted, evidence captured to `demo/evidence/`):
* **T-E1** simple prompt → `small`. **T-E2** complex prompt → `large`.
* **T-E3** classifier killed mid-run → `general`, HTTP 200, degraded not down.

## 7. Non-goals for v0.1 (state them, don't build them)

* Multiple signals / multiple llm-d-sc upstreams in one filter (C3).
* TLS or mTLS to llm-d-sc (h2c only; note it as the obvious next step).
* Streaming or response-phase classification.
* Caching in the filter — llm-d-sc already has a versioned result cache (~0.09 ms hit);
  adding a second one in the proxy would duplicate it and risk serving a stale revision.
* Sensitivity-based egress policy. The metadata is emitted so a *separate* policy filter
  could do this; the POC does not.
* Any change whatsoever to `~/llm-d-sc-genesis`.

## 8. Risks

| Risk | Mitigation |
|---|---|
| `rust-toolchain.toml` pins 1.96.0; host has 1.97.1 | let rustup fetch 1.96.0; if offline, `RUSTUP_TOOLCHAIN=stable` and record it in the evidence. |
| First build pulls the pingora fork + full praxis dep tree (slow) | run it early, in the background, before writing code. |
| `praxis-proxy-filter` may not be on crates.io at 0.5.2 | Phase A uses a **path dependency** to `~/praxis` — deliberate, and correct for a POC. |
| Classification adds 8–12 ms to p50 on a cache miss | measured and reported in the demo, not hidden. llm-d-sc's own numbers: 22 µs network, 56 µs tokenize, 7.7–12.3 ms forward, 0.09 ms cached. |
| `StreamBuffer` forces whole-body buffering for the chain | bounded by `max_body_bytes` (default 1 MiB); called out in the README as a real cost of body-derived routing. |

## 9. Definition of done

1. `cargo test` green in `~/llm-d-sc-praxis-filter` (all T-U*, T-I*, T-P*).
2. `cargo build` green in `~/praxis` with the filter crate wired in.
3. `demo/run-demo.sh` prints simple→small, complex→large, classifier-down→general,
   with captured output in `demo/evidence/`.
4. `git diff` in `~/praxis` is Phase B's reviewable artifact; Phase A's praxis diff is
   one `Cargo.toml` line.
5. `~/llm-d-sc-genesis` is byte-for-byte unchanged (`git status` clean).
