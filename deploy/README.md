# Deploying the `llm_d_sc` Praxis filter to OKD

Target cluster: `https://api.ironman.cjlabs.dev:6443` (OKD 4.20 / k8s 1.33).
Everything this POC creates lives in the namespace **`praxis-poc`**.

## The story

A Praxis proxy sits in front of two llama.cpp model servers that already exist on
this cluster. For each incoming OpenAI-shaped request, the `llm_d_sc` filter pulls
the prompt out of the body, asks the **existing** llm-d-sc classifier over gRPC how
complex it is, and picks a Praxis cluster from the returned label. Simple prompts go
to the small 27 B model, hard ones go to the 284 B model.

Nothing about that requires a new model server, and this deployment does not create
one. It consumes what is already running.

```
          Route (edge TLS)
                │
                ▼
        ┌───────────────────┐
        │  praxis  :8080    │   Deployment praxis, 1 replica, namespace praxis-poc
        │                   │
        │  request_id       │
        │  access_log       │
        │  llm_d_sc  ───────┼──── gRPC h2c ───▶ llm-d-sc.llm-d-sc.svc:50051   (REUSED)
        │  headers          │                   ← ranked label
        │  load_balancer    │
        └────────┬──────────┘
                 │  small / general        ▶ llama-server-qwen38.homelab-maas.svc:80    (REUSED)
                 └─ large                  ▶ llama-server-ds4.homelab-maas.svc:8080     (REUSED)
```

## Reuse, never recreate

Everything in this table pre-existed this POC. None of it is redeployed, scaled,
restarted, or modified — not once, not temporarily. Verified live on 2026-08-24.

| Thing | Address | ClusterIP | Status | What we do with it |
|---|---|---|---|---|
| llm-d-sc classifier | `llm-d-sc.llm-d-sc.svc.cluster.local:50051` | `172.30.122.123` | Deployment `llm-d-sc` 1/1 | **Consume** over gRPC. Never scaled. |
| Model "small" | `llama-server-qwen38.homelab-maas.svc.cluster.local:80` | `172.30.51.135` | 1/1, serves `qwen38-27b` | **Consume** as cluster `small` + `general`. |
| Model "large" | `llama-server-ds4.homelab-maas.svc.cluster.local:8080` | `172.30.212.46` | 1/1, serves `ds4-flash-0731` | **Consume** as cluster `large`. |
| ds4 credential | secret `laguna-api-key` in `homelab-maas` | — | — | **Read** its value; copied into `praxis-poc`. Source secret untouched. |

Note the port asymmetry: ds4 is `:8080`, qwen38 is `:80`.

What this POC adds, all in `praxis-poc`:

| Object | Purpose |
|---|---|
| `Namespace praxis-poc` | blast-radius containment |
| `ImageStream praxis-llm-d-sc` | holds the built proxy image |
| `BuildConfig praxis-llm-d-sc` | Docker strategy, Binary source, in-cluster build |
| `ConfigMap praxis-config` | the live Praxis config |
| `ConfigMap praxis-config-classifier-down` | fail-open evidence config (V-5 alternative) |
| `Secret ds4-api-key` | copy of the ds4 token, created at deploy time |
| `Deployment praxis` | 1 replica |
| `Service praxis` | ClusterIP :8080 |
| `Route praxis` | edge TLS |

## Production caveat: the credential goes to every backend

**This is the single most important thing to know before copying this config
anywhere real.**

`llama-server-ds4` requires a bearer token and answers
`401 {"error":{"message":"Invalid API Key"}}` on `/v1/chat/completions` without one.
`llama-server-qwen38` has no key at all. `/v1/models` is unauthenticated on both,
which is a trap: liveness checks pass while completions fail.

The obvious design is to attach the credential only when routing to `large`.
SPEC-K8S §3.1 concluded that is not expressible, because `should_execute` evaluates
`ConditionMatch { path, path_prefix, methods, headers }` against the original
downstream request only — a filter **condition** cannot see `ctx.cluster` or
anything an earlier filter decided. So the shipped default config injects the token
**unconditionally** with the built-in `headers` filter:

```yaml
- filter: headers
  request_set:
    - name: "Authorization"
      value: "Bearer ${DS4_API_KEY}"
```

This means **every request to qwen38 also carries the ds4 credential.** Verified
live: qwen38 returns 200 and ignores the header. That is acceptable in a homelab
POC and **is not acceptable in production** — a misroute hands a credential to the
wrong upstream, and the blast radius of a routing bug becomes a credential
disclosure rather than a bad answer. Do not soften this when writing it up.

### But Praxis can already do better than that

While building this, a filter turned up that solves the actual problem:
**`credential_injection`** (`filter/src/builtins/http/security/credential_injection/`).

SPEC-K8S §3.1's reasoning is correct about filter *conditions*, but
`credential_injection` does not use conditions. It reads `ctx.cluster` directly
inside its own `on_request` and looks the cluster name up in a map:

```rust
let Some(cluster) = &ctx.cluster else { ... };
let Some(cred) = self.credentials.get(cluster) else { ... };
```

Its own docs describe the precondition as "matches on the cluster name selected by
the router filter earlier in the pipeline" — and our `llm_d_sc` filter sets
`ctx.cluster` exactly as `router` does. It also resolves the value from an
environment variable at construction time, and wraps it in `SecretString`/`Zeroizing`.

Using it removes **both** problems at once:

1. **No credential leak.** The token reaches only `large`. qwen38 never sees it.
2. **No config templating.** The token is read straight from the Secret via the
   process environment, so it never appears in the ConfigMap and never gets
   rendered to a file. The initContainer becomes a passthrough.

`config/praxis-config-credential-injection.yaml` is that config, ready to use:

```
hack/deploy-cluster.sh --config credential-injection
```

It is **not** the default only because SPEC-K8S §3.1 explicitly prescribes the
`headers` approach. It is also **unverified end-to-end** — no image has been built
yet, so it has been validated by schema and dry-run only.

The implication for discussion #1017 is narrower than SPEC-K8S §3.1 assumed. The
gap is not "Praxis cannot attach per-cluster credentials" — it can. The gap is that
a filter *condition* cannot branch on a routing decision made earlier in the same
chain, so this capability has to be built into each filter individually rather than
being composable.

## Why `general` points at the small model

If the classifier is unavailable, unclassified traffic lands on `general`, which is
wired to **qwen38 (small)**. Rationale: a classifier outage should degrade toward
the cheap, fast backend rather than dump every unclassified request onto a 284 B
model. The opposite choice — fail toward the strong model, so quality never drops —
is equally defensible. It is a deployment decision, not a property of the filter;
change the `general` cluster's endpoint to flip it.

## Readiness probe: HTTP, not TCP

**Decision: HTTP probes against the admin listener on :9901.** Evidence:

* `~/praxis/docs/operating/health-checking.md` §"Admin Health Endpoints" documents
  `GET /healthy` (liveness — 200 once the server accepts connections, does not check
  upstreams) and `GET /ready` (readiness — 503 when any cluster has zero healthy
  endpoints).
* `docs/operating/observability.md` lists `/healthy`, `/ready`, `/metrics`; any other
  path is 404.
* `~/praxis/Containerfile`'s own `HEALTHCHECK` runs
  `wget -qO- http://127.0.0.1:9901/healthy`.
* `examples/configs/operations/admin-interface.yaml` configures `admin.address` and
  documents the same three paths.

So a TCP socket probe was **not** needed. Mapping used:

| Probe | Path | Port |
|---|---|---|
| startup | `/healthy` | 9901 |
| liveness | `/healthy` | 9901 |
| readiness | `/ready` | 9901 |

`/ready` is safe here and will not flap on ds4's 401: it only reports degraded for
clusters with **active health checks configured**, and "clusters without one are
always considered healthy" (health-checking.md). This config defines no
`health_check` blocks.

Two consequences worth knowing:

* The admin listener must bind `0.0.0.0`, because the kubelet probes from outside
  the container's network namespace. Praxis refuses a non-loopback admin bind
  unless `insecure_options.allow_public_admin: true`, so that flag is **required**,
  not optional.
* The admin listener has **no authentication**. Port 9901 is therefore deliberately
  not published on the Service or the Route — only the kubelet and `oc port-forward`
  can reach it.

## The credential never lands in git

* The repo contains only `secret.example.yaml`, a placeholder.
* `hack/deploy-cluster.sh` reads the **already-base64-encoded** value from
  `homelab-maas/laguna-api-key` and pipes a Secret manifest straight into
  `oc apply -f -`. The plaintext is never decoded, never written to disk, and never
  placed on a command line where `ps` or shell history would capture it.
* Manifests reference it only via `secretKeyRef`.
* `deploy/.gitignore` blocks anything that could hold it.
* The rendered config (which *does* contain the token) lives in an
  `emptyDir{medium: Memory}` — it never touches a disk.
* `render-config.sh` substitutes via `awk`'s `ENVIRON`, so the value never appears
  in `argv` inside the pod either.

Praxis does **not** expand environment variables in its config file (the only env
lookup in `core/src/config` is the OTLP endpoint fallback), which is why the
templating initContainer exists at all for the default config. The
`credential_injection` variant does not need it.

## Commands

```bash
# 0. Baseline everything we must not break. Do this first.
hack/snapshot-cluster.sh

# 1. Create the namespace, ImageStream, BuildConfig, ConfigMaps and Secret.
#    Stops cleanly if no image has been built yet.
hack/deploy-cluster.sh --prepare-only

# 2. Build the image in-cluster. Must be in-cluster: nodes are amd64, the
#    laptop is arm64. Expect a long first build (pingora + aws-lc-sys + praxis).
CTX="$(hack/stage-build-context.sh)"
oc start-build praxis-llm-d-sc -n praxis-poc --from-dir="$CTX" --follow
rm -rf "$CTX"

# 3. Roll out and run the verification ladder.
hack/deploy-cluster.sh

#    Variants:
hack/deploy-cluster.sh --config credential-injection   # per-cluster credential
hack/deploy-cluster.sh --skip-failopen                 # skip the V-5 alternative

# 4. Remove the POC and prove nothing else moved.
hack/teardown-cluster.sh
```

Manifests alone, without the Secret:

```bash
oc apply -k deploy/
```

Ad-hoc request through the Route:

```bash
ROUTE=$(oc get route praxis -n praxis-poc -o jsonpath='{.spec.host}')
curl -sk "https://$ROUTE/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"routed-by-praxis","max_tokens":32,
       "messages":[{"role":"user","content":"What is the capital of France?"}]}' | jq .model
```

The `model` you send is ignored — llama.cpp answers with whichever model it has
loaded and reports the true id in the response. That response field is what
attributes a request to a backend.

## Verification ladder

`hack/deploy-cluster.sh` runs these and prints PASS/FAIL per rung.

| Rung | Check |
|---|---|
| V-1 | praxis pod Running and Ready |
| V-2 | From **inside the praxis pod**: TCP to the classifier, HTTP `/v1/models` on both models |
| V-3 | SIMPLE prompt → 200, response `model` == `qwen38-27b` |
| V-4 | REASONING prompt → 200, response `model` == `ds4-flash-0731` |
| V-5(alt) | Classifier unreachable → still 200, served via `general` |
| V-6 | `oc logs deploy/praxis` shows the classification in the access log |
| — | Protected namespaces still byte-identical to the baseline |

### V-6 caveat: `access_log` does not render `filter_metadata`

SPEC §4.7 describes `ctx.filter_metadata` as "visible to `access_log`". Reading
`filter/src/builtins/http/observability/access_log.rs`, the built-in filter emits a
**fixed** field set and nothing else:

```
method, path, client_ip, status, duration_ms,
cluster, upstream, request_id, request_body_bytes, response_body_bytes
```

There is no header output (so it cannot leak the injected `Authorization` — good)
and no `filter_metadata` rendering (so `llm_d_sc.label` / `.score` / `.status` will
**not** appear there unless the filter crate emits its own `tracing` events).

V-6 therefore accepts either form of evidence and says which it found:

1. the log contains `llm_d_sc` metadata (the filter logging for itself), or
2. the access-log line carries `cluster=small|large|general`, which *is* the routing
   decision the classification produced — weaker, but real, and it is what the
   built-in filter can produce unaided.

If richer access-log attribution is wanted, that is a Praxis-side change
(`access_log` rendering `filter_metadata`) and worth raising alongside #1017.

Evidence written to `deploy/evidence/` is passed through a bearer-token redaction
filter first, since that directory is committed.

### V-5 is deliberately not implemented as written

SPEC-K8S §5 describes V-5 as "scale the classifier to 0, then scale it back".
**This deployment never does that.** The llm-d-sc classifier is a shared,
single-replica workload this POC does not own, and it is the one rung that can
degrade someone else's running system. SPEC-K8S §5 sanctions the substitute used
instead:

> "the equivalent evidence can be produced by pointing the filter at a dead endpoint
> via a second ConfigMap. Prefer that if there is any doubt — the existing classifier
> is not ours to interrupt."

There is doubt, so `praxis-config-classifier-down.yaml` points the filter at
`127.0.0.1:1` — guaranteed to refuse inside the pod's own network namespace, no DNS
needed. It produces the same fail-open path a scaled-to-0 classifier would, and the
classifier is never touched. The script restores the real config afterwards, via an
`EXIT`/`INT`/`TERM` trap so an interrupted run cannot leave the dead endpoint live.

## Files

```
Containerfile.praxis                       multi-stage build (rust:1.96-alpine -> alpine)
hack/snapshot-cluster.sh                   capture the protected-namespace baseline
hack/stage-build-context.sh                assemble a clean build context, print its path
hack/deploy-cluster.sh                     idempotent deploy + verification ladder
hack/teardown-cluster.sh                   delete praxis-poc only, then diff the baseline
deploy/00-namespace.yaml   10-imagestream.yaml   20-buildconfig.yaml
deploy/30-deployment.yaml  40-service.yaml       50-route.yaml
deploy/secret.example.yaml                 placeholder, never the real key
deploy/kustomization.yaml                  static resources + generated ConfigMaps
deploy/config/praxis-config.yaml                        live config (source of truth)
deploy/config/praxis-config-credential-injection.yaml   per-cluster credential variant
deploy/config/praxis-config-classifier-down.yaml        fail-open evidence config
deploy/runtime/render-config.sh            initContainer: substitute the token
deploy/evidence/pre-deploy-state.json      the baseline teardown verifies against
```

`deploy/config/praxis-config.yaml` is the single source of truth: the ConfigMap is
generated from it *and* the image build runs `praxis --validate` against it, so a
config that could not start the proxy fails the build rather than the rollout.

## Build notes

* **The build must run in-cluster.** Nodes are amd64; the dev laptop is arm64.
* The build context is both trees staged as siblings (`praxis/` and
  `llm-d-sc-praxis-filter/`), so the relative path dependency already present in
  `praxis/server/Cargo.toml` (`../../llm-d-sc-praxis-filter`) resolves unchanged.
  Wiring is idempotent: an already-wired tree is left alone, an unwired one is
  patched, and a duplicate `[dependencies]` entry is treated as a build failure.
* `target/` and `.git/` are excluded — this crate's `target/` alone is ~1.9 GB. The
  staged context is ~9.7 MB and byte-reproducible across runs.
* If the build is OOM-killed, lower `CARGO_BUILD_JOBS` (a build ARG). **Do not**
  switch to a debug build: every benchmark latency number depends on `--release`.
* The runtime image works with an arbitrary OpenShift UID: writable paths are owned
  by group 0 with `chmod g=u`, `USER 1001` is only a non-root default, and the pod
  spec sets no `runAsUser`/`fsGroup` so the SCC can assign them.
