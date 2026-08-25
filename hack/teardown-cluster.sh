#!/usr/bin/env bash
# teardown-cluster.sh - remove the POC and prove nothing else moved.
#
# Deletes exactly one thing: namespace/praxis-poc. Then re-snapshots the
# protected namespaces and diffs them against deploy/evidence/pre-deploy-state.json,
# failing loudly if anything outside the POC namespace changed.
#
# The namespace name is hard-coded on purpose. This script takes no target
# argument, so there is no way to point it at llm-d-sc or homelab-maas by typo.
#
# Usage:
#   hack/teardown-cluster.sh [--yes] [--timeout <seconds>]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Hard-coded. Not a parameter. Not derived from anything.
readonly NS=praxis-poc
readonly PROTECTED=(llm-d-sc homelab-maas)

SNAPSHOT="${REPO_ROOT}/deploy/evidence/pre-deploy-state.json"
ASSUME_YES=0
TIMEOUT=300

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)  ASSUME_YES=1; shift ;;
    --timeout) TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

section() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info()    { printf '   %s\n' "$*"; }
ok()      { printf '\033[32m   OK\033[0m    %s\n' "$*"; }
bad()     { printf '\033[31m   FAIL\033[0m  %s\n' "$*"; }
die()     { printf '\033[31mFATAL: %s\033[0m\n' "$*" >&2; exit 1; }

section "Preflight"
for t in oc jq; do command -v "$t" >/dev/null 2>&1 || die "$t not found in PATH"; done
oc whoami >/dev/null 2>&1 || die "not logged in to a cluster"
info "cluster: $(oc whoami --show-server)"

# Without the baseline there is nothing to verify against, and a teardown that
# cannot prove innocence is worse than no teardown.
[[ -f "$SNAPSHOT" ]] || die "baseline missing: ${SNAPSHOT}
  Nothing to verify against. Capture one with hack/snapshot-cluster.sh before tearing down."
info "baseline: $(jq -r '.meta.capturedAt' "$SNAPSHOT")"

section "Target"
if oc get namespace "$NS" >/dev/null 2>&1; then
  info "namespace/${NS} exists and will be deleted"
  oc get all -n "$NS" --no-headers 2>/dev/null | wc -l | xargs printf '   objects in namespace: %s\n'
else
  info "namespace/${NS} does not exist - nothing to delete, will still verify"
fi

if [[ $ASSUME_YES -eq 0 ]] && [[ -t 0 ]]; then
  printf '\n   Delete namespace/%s? [y/N] ' "$NS"
  read -r reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "   aborted"; exit 0; }
fi

section "Delete"
if oc get namespace "$NS" >/dev/null 2>&1; then
  oc delete namespace "$NS" --wait=true --timeout="${TIMEOUT}s" | sed 's/^/   /' || \
    info "delete returned non-zero (namespace may still be terminating)"
else
  info "already absent"
fi

# Namespace deletion is asynchronous; a diff taken too early is meaningless.
info "waiting for namespace/${NS} to disappear..."
deadline=$(( $(date +%s) + TIMEOUT ))
while oc get namespace "$NS" >/dev/null 2>&1; do
  if [[ $(date +%s) -ge $deadline ]]; then
    bad "namespace/${NS} still present after ${TIMEOUT}s (phase: $(oc get ns "$NS" -o jsonpath='{.status.phase}' 2>/dev/null))"
    break
  fi
  sleep 3
done
oc get namespace "$NS" >/dev/null 2>&1 || ok "namespace/${NS} is gone"

section "Verifying nothing outside ${NS} changed"

FAILED=0

# The protected namespaces must still be there at all.
for ns in "${PROTECTED[@]}"; do
  if oc get namespace "$ns" >/dev/null 2>&1; then ok "namespace/${ns} still exists"
  else bad "namespace/${ns} IS MISSING"; FAILED=1; fi
done

after="$(mktemp)"
trap 'rm -f "$after"' EXIT
if ! bash "${REPO_ROOT}/hack/snapshot-cluster.sh" "$after" >/dev/null 2>&1; then
  bad "could not capture a post-teardown snapshot"
  FAILED=1
else
  # Only .namespaces is compared. .meta holds the capture timestamp and would
  # differ on every run by design.
  if diff -u <(jq -S .namespaces "$SNAPSHOT") <(jq -S .namespaces "$after") > /tmp/teardown-diff.txt 2>&1; then
    ok "llm-d-sc and homelab-maas are byte-identical to the pre-deploy baseline"
    for ns in "${PROTECTED[@]}"; do
      d=$(jq -r --arg n "$ns" '.namespaces[$n].deployments | length' "$after")
      s=$(jq -r --arg n "$ns" '.namespaces[$n].services    | length' "$after")
      r=$(jq -r --arg n "$ns" '[.namespaces[$n].deployments[] | "\(.name)=\(.readyReplicas)/\(.replicas)"] | join(" ")' "$after")
      info "${ns}: ${d} deployments, ${s} services"
      info "   ${r}"
    done
  else
    bad "SOMETHING OUTSIDE ${NS} CHANGED - review this diff carefully:"
    sed 's/^/      /' /tmp/teardown-diff.txt
    info "(full diff also at /tmp/teardown-diff.txt)"
    FAILED=1
  fi
fi

section "Result"
if [[ $FAILED -eq 0 ]]; then
  printf '\033[32m   Teardown clean. Only namespace/%s was removed.\033[0m\n\n' "$NS"
  exit 0
fi
printf '\033[31m   Teardown verification FAILED. See above.\033[0m\n\n'
exit 1
