# SPEC-K8S — homelab deployment of the `llm_d_sc` Praxis filter

Extends `SPEC.md`. Target cluster: `https://api.ironman.cjlabs.dev:6443` (OKD 4.20 / k8s 1.33).

## 1. What already exists — reuse, never recreate

Discovered by inspection on 2026-08-24. **Nothing in this table may be redeployed,
scaled, restarted, or modified.**

| Thing | Address | Detail |
|---|---|---|
| **llm-d-sc classifier** | `llm-d-sc.llm-d-sc.svc.cluster.local:50051` | Running 5d7h. `LLM_D_SC_CLASSIFIER=complexity`, `LLM_D_SC_MODEL_DIR=/models`, digest-pinned image `llm-d-sc@sha256:b5aea3c2…`, ModelCar init container. Deployment `llm-d-sc`, 1/1. |
| **Model endpoint "large"** | `llama-server-ds4.homelab-maas.svc.cluster.local:8080` | llama.cpp, model id **`ds4-flash-0731`**, 284 B params, IQ3_XXS, 104 GB, `n_ctx` 131072. |
| **Model endpoint "small"** | `llama-server-qwen38.homelab-maas.svc.cluster.local:80` | llama.cpp, model id **`qwen38-27b`**, 27.3 B params, Q4_K_S, 20 GB, `n_ctx` 65536. |

Both model endpoints are OpenAI-compatible (`/v1/models`, `/v1/chat/completions`),
verified live. **Note the port asymmetry: ds4 is `:8080`, qwen38 is `:80`.** Both are
single-replica llama.cpp servers, so benchmark concurrency against them stays low
(§SPEC-BENCH) — this is a shared homelab, not a load-test rig.

Cluster facts that shape the plan:
* Nodes are **amd64**; the dev laptop is arm64 ⇒ **the Praxis image must be built
  in-cluster**. Binary-source `BuildConfig`, exactly as `llm-d-sc` and
  `llm-d-sc-modelcar` already do in that namespace.
* **No NetworkPolicy** in `llm-d-sc` or `homelab-maas` ⇒ cross-namespace ClusterIP
  traffic works without extra objects.
* Internal registry route: `default-route-openshift-image-registry.apps.ironman.cjlabs.dev`.
* `kube:admin`; `create builds` and `create imagestreams` confirmed in `llm-d-sc`.

## 2. What we add

Everything new lives in namespace **`praxis-poc`** (new). Keeping it out of `llm-d-sc`
means the whole POC can be deleted with one `oc delete project` and cannot damage the
existing classifier's objects.

| Object | Purpose |
|---|---|
| `Namespace praxis-poc` | blast-radius containment |
| `ImageStream praxis-llm-d-sc` | holds the built proxy image |
| `BuildConfig praxis-llm-d-sc` | Docker strategy, **Binary** source, built from a tarball of `~/praxis` + `~/llm-d-sc-praxis-filter` |
| `ConfigMap praxis-config` | the Praxis YAML (§3) |
| `Deployment praxis` | 1 replica, the proxy |
| `Service praxis` | ClusterIP :8080 |
| `Route praxis` | edge TLS, for driving it from the laptop |
| `Job praxis-bench` | in-cluster benchmark driver (see SPEC-BENCH) |

`Deployment praxis` readiness: HTTP GET on the Praxis admin/health path if one exists,
else a TCP socket probe on 8080. **Determine which by reading
`~/praxis/docs/operating/` and the admin-interface example config — do not guess.**

## 3. Cluster Praxis config (ConfigMap)

```yaml
listeners:
  - name: http
    address: "0.0.0.0:8080"
    filter_chains: [ai-routing]

filter_chains:
  - name: ai-routing
    filters:
      - filter: request_id
      - filter: access_log
      - filter: llm_d_sc
        endpoint: "llm-d-sc.llm-d-sc.svc.cluster.local:50051"
        default_cluster: general
        timeout_ms: 100
        routes:
          - {label: SIMPLE,    cluster: small}
          - {label: MEDIUM,    cluster: small}
          - {label: COMPLEX,   cluster: large}
          - {label: REASONING, cluster: large}
      - filter: load_balancer
        clusters:
          - name: small
            endpoints: ["llama-server-qwen38.homelab-maas.svc.cluster.local:80"]
          - name: large
            endpoints: ["llama-server-ds4.homelab-maas.svc.cluster.local:8080"]
          - name: general
            endpoints: ["llama-server-qwen38.homelab-maas.svc.cluster.local:80"]
```

`general` (the fail-open target) deliberately points at the **small** model: if the
classifier is unavailable, unclassified traffic should land on the cheaper, faster
backend rather than the 284 B one. State this tradeoff in the README — the opposite
choice (fail to the strong model) is equally defensible and is a deployment decision.

### 3.1 Heterogeneous-backend findings — RESOLVED empirically 2026-08-24

Both questions below were tested live against the real endpoints. The answers are
settled; implement them, do not re-investigate.

**The `model` field: a non-issue.** The two backends serve different model ids
(`qwen38-27b` vs `ds4-flash-0731`), but an OpenAI client sends one `"model"` in the body.
Tested with a deliberately bogus id (`"definitely-not-a-real-model"`): llama.cpp
**ignores the field entirely** and answers with its single loaded model, reporting the
true id in the response `model` field. So a client may send anything; the response tells
you which backend actually served it. **This is what the benchmark uses for
attribution** — the upstream's own `model` field, cross-checked against our
`x-llm-d-sc-*` headers. Two independent attribution sources that must agree.

**Authentication: real, and it shapes the config.** `llama-server-ds4` runs with
`LLAMA_API_KEY` (from secret `laguna-api-key` in `homelab-maas`) and returns
`401 {"error":{"message":"Invalid API Key"}}` on `/v1/chat/completions` without a
bearer token. `/v1/models` is unauthenticated, which is why liveness checks pass while
completions fail — an easy trap. `llama-server-qwen38` has **no** key.

**CORRECTION.** An earlier revision of this section claimed per-cluster credential
injection was "not expressible in today's Praxis contract" and prescribed injecting the
token unconditionally. **That was wrong**, and it is corrected here rather than quietly
replaced, because the wrong version prescribed the less safe design.

Praxis ships **`credential_injection`**, which reads `ctx.cluster` **directly** in its own
`on_request` — it does not go through filter conditions at all — and resolves each
cluster's credential from an `env_var` at construction
(`filter/src/builtins/http/security/credential_injection/filter.rs:143`). It composes
with `llm_d_sc` exactly as one would hope: our filter sets `ctx.cluster`, and
`credential_injection` attaches the right secret for that cluster and nothing else.

**This is the POC's configuration, and it is a positive result for #1017:** the new
filter composes with an existing security filter, with zero changes to either.

```yaml
- filter: llm_d_sc          # sets ctx.cluster
    ...
- filter: credential_injection
  clusters:
    - name: large           # ds4 only
      header: Authorization
      env_var: DS4_API_KEY
      header_prefix: "Bearer "
      strip_client_credential: true
    # `small` and `general` get nothing — qwen38 needs no key
- filter: load_balancer
```

**The narrower finding that survives, and is still worth reporting:** filter
*conditions* cannot branch on a routing decision. `should_execute` evaluates
`ConditionMatch { path, path_prefix, methods, headers }` against
`Request { headers, method, uri }` — the original downstream request only, never
`ctx.cluster`, `ctx.filter_metadata`, or headers set by an earlier filter
(`filter/src/condition/request.rs`). So a filter that wants to act on a classification
must read the context itself, as `credential_injection` does; it cannot be gated
declaratively in YAML. That is a real composability limit, and it is a much smaller
claim than the one this section originally made.

The unconditional-`headers` variant is retained as an alternative config for comparison,
but it must be labelled as what it is: acceptable in a homelab POC, **not** acceptable in
production, because a misroute leaks a credential to the wrong upstream.

## 4. Build

`Containerfile.praxis` (new, in the filter crate repo — **not** in `~/praxis`):
* Stage 1: `rust:1.96` (match `rust-toolchain.toml`), copy both trees, build
  `cargo build --release -p praxis-proxy` with the filter crate wired in via the
  one-line `server/Cargo.toml` dependency (Phase A) or the feature flag (Phase B).
* Stage 2: minimal runtime base, copy the binary, non-root user, `USER 1001`
  (OpenShift runs with an arbitrary UID — do **not** assume root or a fixed UID).
* Model the multi-stage layout on `~/praxis/Containerfile`, which already solves this.

Build via `oc start-build praxis-llm-d-sc --from-dir=<staging> --follow`. The staging dir
is assembled by a script so the build context is reproducible and excludes `target/`.

Expect a long first build (pingora + aws-lc-sys + the praxis tree). Give the
`BuildConfig` generous resources and a long `completionDeadlineSeconds`. If the build
runs out of memory, lower codegen parallelism (`CARGO_BUILD_JOBS`) rather than switching
to a debug build — **the benchmark numbers must come from a release build**, and a debug
binary would silently invalidate every latency figure.

## 5. Verification ladder — each rung must pass before the next

1. **V-1** `oc get pods -n praxis-poc` → praxis Running and Ready.
2. **V-2** Direct backend reachability *from inside the praxis pod*: `curl` both model
   services and the classifier port. Proves DNS + cross-namespace routing before any
   filter logic is blamed.
3. **V-3** A SIMPLE prompt through the Route returns 200 and the response came from
   `qwen38-27b`. Attribution comes from the **`x-llm-d-sc-*` provenance headers plus the
   upstream response's own `model` field**, not from a guess.
4. **V-4** A REASONING prompt lands on `ds4-flash-0731`.
5. **V-5** Fail-open: scale the classifier to 0 → request still returns 200 via
   `general`. **Scale it back to 1 immediately afterwards and confirm Ready.** This is
   the one moment the POC touches a pre-existing object; it must be restored, and the
   restoration must be verified, not assumed.
6. **V-6** `oc logs deploy/praxis` shows the classification metadata in the access log.

If V-5 is judged too invasive at run time, the equivalent evidence can be produced by
pointing the filter at a dead endpoint via a second ConfigMap. Prefer that if there is
any doubt — the existing classifier is not ours to interrupt.

## 6. Teardown

`hack/teardown-cluster.sh` deletes **only** `namespace/praxis-poc` and asserts that
`llm-d-sc`, `homelab-maas` deployments, and their replica counts are byte-identical to a
snapshot taken before deployment. The snapshot is captured at deploy time into
`deploy/evidence/pre-deploy-state.json`.

## 7. Non-negotiables

* **No new model server.** Not vLLM, not llama.cpp, not a stub that pretends to be one.
* **No modification** to `llm-d-sc`, `homelab-maas`, or any object outside `praxis-poc`,
  except the temporary, verified-restored scale-to-0 in V-5.
* **No `git push`, no image push to any external registry.** The internal cluster
  registry is the only push target, and it is required for the deployment to exist.
* `~/llm-d-sc-genesis` stays byte-identical.
