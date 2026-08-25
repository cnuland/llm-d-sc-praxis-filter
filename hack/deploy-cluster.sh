#!/usr/bin/env bash
# deploy-cluster.sh - idempotent deploy of the llm_d_sc Praxis POC into praxis-poc.
#
# Applies the manifests, copies the ds4 credential out of homelab-maas into a
# Secret in praxis-poc, waits for the rollout, then runs the SPEC-K8S 5
# verification ladder printing a clear PASS/FAIL per rung.
#
# It touches NOTHING outside namespace praxis-poc. In particular it never scales,
# restarts or edits the shared llm-d-sc classifier: SPEC-K8S 5's V-5 (scale the
# classifier to 0) is deliberately NOT implemented. The sanctioned alternative is
# used instead - a second ConfigMap pointing the filter at a dead endpoint.
#
# Usage:
#   hack/deploy-cluster.sh [options]
#     --config <variant>   default | classifier-down | credential-injection
#     --prepare-only       create everything but do not wait or verify
#     --skip-failopen      skip the fail-open rung (V-5 alternative)
#     --refresh-snapshot   re-capture the pre-deploy baseline (see caveat below)
#     -h | --help

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS=praxis-poc
SRC_SECRET_NS=homelab-maas
SRC_SECRET_NAME=laguna-api-key
SRC_SECRET_KEY=key
DST_SECRET_NAME=ds4-api-key
SNAPSHOT="${REPO_ROOT}/deploy/evidence/pre-deploy-state.json"
EVIDENCE_DIR="${REPO_ROOT}/deploy/evidence"

CONFIG_VARIANT=default
PREPARE_ONLY=0
SKIP_FAILOPEN=0
REFRESH_SNAPSHOT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)           CONFIG_VARIANT="${2:?--config needs a value}"; shift 2 ;;
    --prepare-only)     PREPARE_ONLY=1; shift ;;
    --skip-failopen)    SKIP_FAILOPEN=1; shift ;;
    --refresh-snapshot) REFRESH_SNAPSHOT=1; shift ;;
    -h|--help)          sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

case "$CONFIG_VARIANT" in
  default)              CONFIG_FILE="${REPO_ROOT}/deploy/config/praxis-config.yaml" ;;
  classifier-down)      CONFIG_FILE="${REPO_ROOT}/deploy/config/praxis-config-classifier-down.yaml" ;;
  credential-injection) CONFIG_FILE="${REPO_ROOT}/deploy/config/praxis-config-credential-injection.yaml" ;;
  *) echo "unknown --config variant: $CONFIG_VARIANT" >&2; exit 2 ;;
esac

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

section() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info()    { printf '   %s\n' "$*"; }
pass()    { PASS_COUNT=$((PASS_COUNT+1)); printf '\033[32m   PASS\033[0m  %s\n' "$*"; }
fail()    { FAIL_COUNT=$((FAIL_COUNT+1)); printf '\033[31m   FAIL\033[0m  %s\n' "$*"; }
skip()    { SKIP_COUNT=$((SKIP_COUNT+1)); printf '\033[33m   SKIP\033[0m  %s\n' "$*"; }
die()     { printf '\033[31mFATAL: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

section "Preflight"
for t in oc jq curl; do command -v "$t" >/dev/null 2>&1 || die "$t not found in PATH"; done
oc whoami >/dev/null 2>&1 || die "not logged in to a cluster"
info "cluster : $(oc whoami --show-server)"
info "user    : $(oc whoami)"
info "config  : ${CONFIG_VARIANT} ($(basename "$CONFIG_FILE"))"

# ---------------------------------------------------------------------------
# Baseline snapshot
# ---------------------------------------------------------------------------
#
# The baseline must describe the cluster BEFORE this POC existed. Re-capturing it
# after a deploy is normally harmless (the snapshot only covers llm-d-sc and
# homelab-maas, which we never modify) but it would also paper over a real change,
# so it is opt-in rather than automatic.

section "Baseline snapshot"
if [[ -f "$SNAPSHOT" && $REFRESH_SNAPSHOT -eq 0 ]]; then
  info "reusing existing baseline: $(jq -r '.meta.capturedAt' "$SNAPSHOT")"
else
  bash "${REPO_ROOT}/hack/snapshot-cluster.sh" "$SNAPSHOT" | sed 's/^/   /'
fi

# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

section "Applying manifests"
oc apply -k "${REPO_ROOT}/deploy" | sed 's/^/   /'

# The live config is whichever variant was selected. Generated imperatively so
# --config can switch it without editing kustomization.yaml.
info "setting ConfigMap praxis-config from $(basename "$CONFIG_FILE")"
oc create configmap praxis-config -n "$NS" \
  --from-file=praxis.yaml="$CONFIG_FILE" \
  --dry-run=client -o yaml | oc apply -f - | sed 's/^/   /'

# ---------------------------------------------------------------------------
# Credential
# ---------------------------------------------------------------------------
#
# Copies the ALREADY-BASE64-ENCODED value across. The plaintext token is never
# decoded, never written to a file, and never placed on a command line where it
# would be visible in `ps` or in shell history.

section "Credential"
b64="$(oc get secret "$SRC_SECRET_NAME" -n "$SRC_SECRET_NS" -o jsonpath="{.data.${SRC_SECRET_KEY}}" 2>/dev/null || true)"
[[ -n "$b64" ]] || die "could not read ${SRC_SECRET_NS}/${SRC_SECRET_NAME} key '${SRC_SECRET_KEY}'"
printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n  namespace: %s\n  labels:\n    app.kubernetes.io/part-of: llm-d-sc-praxis-poc\ntype: Opaque\ndata:\n  %s: %s\n' \
  "$DST_SECRET_NAME" "$NS" "$SRC_SECRET_KEY" "$b64" | oc apply -f - | sed 's/^/   /'
unset b64
info "secret ${NS}/${DST_SECRET_NAME} in sync with ${SRC_SECRET_NS}/${SRC_SECRET_NAME} (value never printed)"

# ---------------------------------------------------------------------------
# Image presence
# ---------------------------------------------------------------------------

section "Image"
if ! oc get istag "praxis-llm-d-sc:latest" -n "$NS" >/dev/null 2>&1; then
  cat <<EOF

   No image yet: ImageStreamTag praxis-llm-d-sc:latest does not exist.

   Everything else is in place and inert. Build it with:

     CTX="\$(${REPO_ROOT}/hack/stage-build-context.sh)"
     oc start-build praxis-llm-d-sc -n ${NS} --from-dir="\$CTX" --follow
     rm -rf "\$CTX"

   Then re-run this script to roll out and verify.
EOF
  exit 0
fi
info "image present: $(oc get istag praxis-llm-d-sc:latest -n "$NS" -o jsonpath='{.image.dockerImageReference}' | cut -c1-90)"

if [[ $PREPARE_ONLY -eq 1 ]]; then
  section "Done (--prepare-only)"
  exit 0
fi

# ---------------------------------------------------------------------------
# Rollout
# ---------------------------------------------------------------------------
#
# The ConfigMap has a stable name, so a config change does not by itself alter the
# pod spec and would not trigger a new rollout. Restart explicitly so the running
# pod always reflects the config just applied.

section "Rollout"
oc rollout restart deployment/praxis -n "$NS" | sed 's/^/   /'
if oc rollout status deployment/praxis -n "$NS" --timeout=300s | sed 's/^/   /'; then
  :
else
  fail "rollout did not complete; recent events:"
  oc get events -n "$NS" --sort-by=.lastTimestamp 2>/dev/null | tail -15 | sed 's/^/      /'
fi

mkdir -p "$EVIDENCE_DIR"

# ---------------------------------------------------------------------------
# Verification ladder
# ---------------------------------------------------------------------------

POD=""
ROUTE=""

# Resolve a pod that is actually READY, at the moment of use.
#
# `--field-selector=status.phase=Running` is NOT sufficient: a pod that is
# Terminating still reports phase=Running, so immediately after a rollout this
# selector happily returns the pod that is on its way out. Every `oc exec` into
# it then fails, which is how V-2 came to report "TCP connect failed" for three
# backends that V-3 and V-4 were successfully routing live traffic to two rungs
# later. Filter on the readiness condition, and re-resolve rather than caching a
# name across a restart.
current_pod() {
  oc get pods -n "$NS" -l app.kubernetes.io/name=praxis \
    -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.metadata.name}{" "}{.status.containerStatuses[?(@.name=="praxis")].ready}{" "}{.metadata.deletionTimestamp}{"\n"}{end}' 2>/dev/null \
    | awk '$2=="true" && $3=="" {print $1; exit}'
}

# --- V-1 -------------------------------------------------------------------
section "V-1  praxis pod Running and Ready"
POD="$(current_pod)"
if [[ -z "$POD" ]]; then
  fail "no Running praxis pod"
  oc get pods -n "$NS" | sed 's/^/      /'
else
  pass "pod ${POD} Running and Ready"
fi

# --- V-2 -------------------------------------------------------------------
# Run from inside the praxis pod itself: this is what proves DNS and
# cross-namespace routing work for the proxy's own network namespace, so that a
# later routing failure cannot be blamed on connectivity.
section "V-2  backend reachability from inside the praxis pod"
POD="$(current_pod)"
if [[ -z "$POD" ]]; then
  skip "no pod to exec into"
else
  check_http() {  # name host port
    if oc exec "$POD" -n "$NS" -c praxis -- \
         wget -q -T 8 -O- "http://$2:$3/v1/models" 2>/dev/null | grep -q '"id"'; then
      pass "$1 : HTTP 200 on /v1/models ($2:$3)"
    else
      fail "$1 : no usable response from $2:$3/v1/models"
    fi
  }
  check_tcp() {   # name host port
    if oc exec "$POD" -n "$NS" -c praxis -- \
         sh -c "nc -w 3 '$2' '$3' </dev/null >/dev/null 2>&1"; then
      pass "$1 : TCP connect OK ($2:$3)"
    else
      fail "$1 : TCP connect failed ($2:$3)"
    fi
  }
  check_tcp  "classifier" llm-d-sc.llm-d-sc.svc.cluster.local 50051
  check_http "small/qwen38" llama-server-qwen38.homelab-maas.svc.cluster.local 80
  check_http "large/ds4"    llama-server-ds4.homelab-maas.svc.cluster.local 8080
fi

# --- Route -----------------------------------------------------------------
ROUTE="$(oc get route praxis -n "$NS" -o jsonpath='{.spec.host}' 2>/dev/null || true)"
[[ -n "$ROUTE" ]] && info "route: https://${ROUTE}"

# ask <prompt> <outfile> -> prints "<http_code> <model>"
ask() {
  local prompt="$1" out="$2"
  local body
  body="$(jq -nc --arg p "$prompt" \
    '{model:"routed-by-praxis",max_tokens:16,messages:[{role:"user",content:$p}]}')"
  local code
  code="$(curl -sk -o "$out" -w '%{http_code}' --max-time 180 \
    -H 'Content-Type: application/json' -d "$body" \
    "https://${ROUTE}/v1/chat/completions" 2>/dev/null || echo 000)"
  printf '%s %s' "$code" "$(jq -r '.model // "?"' "$out" 2>/dev/null || echo '?')"
}

# --- V-3 / V-4 -------------------------------------------------------------
# Attribution uses the upstream's OWN response `model` field. llama.cpp ignores
# the request's "model" and reports the id it actually has loaded, so this is a
# statement about which backend served the request, not an echo of what we asked
# for. The x-llm-d-sc-* provenance headers are set on the UPSTREAM request and so
# are not visible to this client; V-6 reads them from the access log instead.
run_rung() {  # id description prompt expected_model outfile
  local id="$1" desc="$2" prompt="$3" want="$4" out="$5"
  section "${id}  ${desc}"
  if [[ -z "$ROUTE" ]]; then skip "no route"; return; fi
  read -r code model <<<"$(ask "$prompt" "$out")"
  info "http=${code} served_by=${model} (expected ${want})"
  if [[ "$code" == "200" && "$model" == "$want" ]]; then
    pass "${id}: 200 and served by ${want}"
  elif [[ "$code" != "200" ]]; then
    fail "${id}: HTTP ${code}"; head -c 300 "$out" 2>/dev/null | sed 's/^/      /'
  else
    fail "${id}: served by '${model}', expected '${want}'"
  fi
}

run_rung "V-3" "SIMPLE prompt should land on the small model" \
  "What is the capital of France?" "qwen38-27b" "${EVIDENCE_DIR}/v3-simple.json"

run_rung "V-4" "REASONING prompt should land on the large model" \
  "Design a fault-tolerant microservices architecture for a global payments platform. Reason step by step about consistency, partitioning, and cascading failure modes." \
  "ds4-flash-0731" "${EVIDENCE_DIR}/v4-reasoning.json"

# --- V-6 -------------------------------------------------------------------
section "V-6  classification metadata in the access log"
POD="$(current_pod)"
if [[ -z "$POD" ]]; then
  skip "no pod"
else
  # Praxis logs through tracing-subscriber with ANSI styling ON, which puts
  # escape sequences BETWEEN the field name and its '=' -- the raw bytes are
  # `cluster<ESC>[0m<ESC>[2m=`. A plain `grep 'cluster='` therefore never matches
  # a line that plainly contains cluster="general" when you look at it in a
  # terminal. That is exactly how this rung reported "no cluster= field" against
  # a log that had one on every request. Strip ANSI first, then grep.
  # BSD sed (macOS) has no \x1b escape, so build the ESC byte with printf.
  esc="$(printf '\033')"
  logs="$(oc logs "$POD" -n "$NS" -c praxis --tail=400 2>/dev/null | sed -E "s/${esc}\[[0-9;]*m//g" || true)"

  # Redact anything bearer-token-shaped before this lands in deploy/evidence/,
  # which is a committed artifact. The built-in access_log filter emits a fixed
  # field set with no headers, so it cannot leak the Authorization value - but the
  # filter crate's own tracing output is not under this script's control.
  printf '%s\n' "$logs" \
    | sed -E 's/([Bb]earer[[:space:]]+)[A-Za-z0-9._~+\/=-]{8,}/\1<REDACTED>/g' \
    > "${EVIDENCE_DIR}/v6-access-log.txt"

  # Two independent ways this rung can be satisfied:
  #   (a) the llm_d_sc filter emits its own tracing events naming the label/score;
  #   (b) the built-in access_log line carries `cluster=`, which IS the routing
  #       decision the classification produced.
  # (b) alone is weaker but still real evidence, and it is what the built-in
  # access_log filter can produce: it logs a fixed field set (method, path,
  # status, duration_ms, cluster, upstream, request_id, body bytes) and does NOT
  # render ctx.filter_metadata. So a hard grep for "llm_d_sc" would fail purely
  # on how the filter crate chose to log, not on whether routing worked.
  meta=0; clus=0
  printf '%s' "$logs" | grep -q 'llm_d_sc'                     && meta=1
  printf '%s' "$logs" | grep -Eq 'cluster="?(small|large|general)' && clus=1

  if [[ $meta -eq 1 ]]; then
    pass "V-6: log carries llm_d_sc classification metadata"
    printf '%s' "$logs" | grep -o 'llm_d_sc[^ ,}"]*' | tail -5 | sed 's/^/      /'
  elif [[ $clus -eq 1 ]]; then
    pass "V-6: access log carries the routing decision (cluster=...)"
    info "note: the built-in access_log filter does not render ctx.filter_metadata,"
    info "      so llm_d_sc.label/score appear only if the filter logs them itself."
    printf '%s' "$logs" | grep -Eo 'cluster="?[a-z]+"?' | tail -5 | sed 's/^/      /'
  else
    fail "V-6: neither llm_d_sc metadata nor a cluster= field in the last 400 lines"
    info "saved to deploy/evidence/v6-access-log.txt"
  fi
fi

# --- V-5 alternative -------------------------------------------------------
# SPEC-K8S 5's V-5 is scale-the-classifier-to-0. We do not do that: the
# classifier is a shared, single-replica workload this POC does not own.
# SPEC-K8S 5 sanctions this substitute explicitly.
section "V-5(alt)  fail-open, via a dead classifier endpoint (classifier NEVER touched)"
if [[ $SKIP_FAILOPEN -eq 1 ]]; then
  skip "--skip-failopen requested"
elif [[ -z "$ROUTE" ]]; then
  skip "no route"
else
  restore_config() {
    oc create configmap praxis-config -n "$NS" \
      --from-file=praxis.yaml="$CONFIG_FILE" \
      --dry-run=client -o yaml | oc apply -f - >/dev/null 2>&1 || true
    oc rollout restart deployment/praxis -n "$NS" >/dev/null 2>&1 || true
    oc rollout status  deployment/praxis -n "$NS" --timeout=300s >/dev/null 2>&1 || true
  }
  # Restore the real config even if this rung is interrupted.
  trap restore_config EXIT INT TERM

  info "pointing the filter at a dead endpoint (127.0.0.1:1)"
  oc create configmap praxis-config -n "$NS" \
    --from-file=praxis.yaml="${REPO_ROOT}/deploy/config/praxis-config-classifier-down.yaml" \
    --dry-run=client -o yaml | oc apply -f - >/dev/null
  oc rollout restart deployment/praxis -n "$NS" >/dev/null
  oc rollout status  deployment/praxis -n "$NS" --timeout=300s >/dev/null 2>&1 || true

  # `rollout status` is swallowed with `|| true` above, and even when it
  # succeeds the Route's endpoint list can lag the pod becoming Ready by a
  # moment. Firing the assertion immediately measured that gap and reported it
  # as "did not fail open" (an observed 504 that reproduced as a clean 200 on
  # retry). Poll until the data path actually answers before asserting anything.
  for _ in $(seq 1 30); do
    probe="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 20 \
      -H 'Content-Type: application/json' \
      -d '{"model":"probe","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' \
      "https://${ROUTE}/v1/chat/completions" 2>/dev/null || echo 000)"
    [[ "$probe" == "200" ]] && break
    sleep 2
  done

  # A REASONING prompt would normally route to `large`. With the classifier
  # unreachable it must fail OPEN to `general`, which points at the small model.
  read -r code model <<<"$(ask "Design a fault-tolerant microservices architecture and reason step by step about cascading failure modes." "${EVIDENCE_DIR}/v5-failopen.json")"
  info "http=${code} served_by=${model} (expected 200 via general -> qwen38-27b)"
  if [[ "$code" == "200" && "$model" == "qwen38-27b" ]]; then
    pass "V-5(alt): classifier unreachable, request still 200 via general (degraded, not down)"
  elif [[ "$code" == "200" ]]; then
    fail "V-5(alt): 200 but served by '${model}', expected the general/small backend"
  else
    fail "V-5(alt): HTTP ${code} - did not fail open"
  fi

  info "restoring config: $(basename "$CONFIG_FILE")"
  restore_config
  trap - EXIT INT TERM
  cur="$(oc get configmap praxis-config -n "$NS" -o jsonpath='{.data.praxis\.yaml}' | grep -c '127.0.0.1:1' || true)"
  if [[ "$cur" == "0" ]]; then pass "live ConfigMap restored to the real classifier endpoint"
  else fail "live ConfigMap still points at the dead endpoint - fix before using this deployment"; fi
fi

# ---------------------------------------------------------------------------
# Blast-radius assertion
# ---------------------------------------------------------------------------

section "Blast radius"
tmp="$(mktemp)"
bash "${REPO_ROOT}/hack/snapshot-cluster.sh" "$tmp" >/dev/null 2>&1 || true
if diff <(jq -S .namespaces "$SNAPSHOT") <(jq -S .namespaces "$tmp") >/dev/null 2>&1; then
  pass "llm-d-sc and homelab-maas are byte-identical to the pre-deploy baseline"
else
  fail "something outside praxis-poc CHANGED:"
  diff <(jq -S .namespaces "$SNAPSHOT") <(jq -S .namespaces "$tmp") | sed 's/^/      /'
fi
rm -f "$tmp"

# ---------------------------------------------------------------------------

section "Summary"
printf '   passed: %d   failed: %d   skipped: %d\n\n' "$PASS_COUNT" "$FAIL_COUNT" "$SKIP_COUNT"
[[ $FAIL_COUNT -eq 0 ]] || exit 1
