#!/usr/bin/env bash
# snapshot-cluster.sh - capture the state of everything this POC must not break.
#
# Records, for each protected namespace, every Deployment (name, replica counts,
# images) and every Service (clusterIP, ports). The result is the baseline that
# hack/teardown-cluster.sh diffs against to prove the POC changed nothing outside
# its own namespace.
#
# Volatile fields (timestamps, resourceVersions) live under .meta and are
# deliberately excluded from the comparable payload under .namespaces, so two
# snapshots of an unchanged cluster are byte-identical in the part that matters.
#
# Usage:
#   hack/snapshot-cluster.sh [output.json]
# Default output: deploy/evidence/pre-deploy-state.json

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-${REPO_ROOT}/deploy/evidence/pre-deploy-state.json}"

# Namespaces that are pre-existing and off-limits.
PROTECTED_NAMESPACES=(llm-d-sc homelab-maas)

command -v oc >/dev/null 2>&1 || { echo "FATAL: oc not found in PATH" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "FATAL: jq not found in PATH" >&2; exit 1; }
oc whoami >/dev/null 2>&1 || { echo "FATAL: not logged in to a cluster" >&2; exit 1; }

# Deployments: name, desired/ready/available/updated replicas, container images.
# `// 0` normalises the absent-vs-zero distinction the API makes for replica
# counts, so a scaled-to-0 Deployment compares equal to itself across snapshots.
snapshot_deployments() {
  oc get deployments -n "$1" -o json 2>/dev/null | jq -S '
    [ .items[] | {
        name:              .metadata.name,
        replicas:          (.spec.replicas          // 0),
        readyReplicas:     (.status.readyReplicas   // 0),
        availableReplicas: (.status.availableReplicas // 0),
        updatedReplicas:   (.status.updatedReplicas // 0),
        images:            [ .spec.template.spec.containers[]?.image ] | sort
      } ] | sort_by(.name)'
}

# Services: clusterIP and the full port list.
snapshot_services() {
  oc get services -n "$1" -o json 2>/dev/null | jq -S '
    [ .items[] | {
        name:      .metadata.name,
        type:      .spec.type,
        clusterIP: (.spec.clusterIP // ""),
        ports:     [ .spec.ports[]? | {
                       name:       (.name       // ""),
                       port:       .port,
                       targetPort: (.targetPort | tostring),
                       protocol:   (.protocol   // "TCP")
                     } ] | sort_by(.port, .name)
      } ] | sort_by(.name)'
}

payload='{}'
for ns in "${PROTECTED_NAMESPACES[@]}"; do
  if ! oc get namespace "$ns" >/dev/null 2>&1; then
    echo "FATAL: protected namespace '${ns}' not found - refusing to write a snapshot that would make its deletion look normal" >&2
    exit 1
  fi
  deployments="$(snapshot_deployments "$ns")"
  services="$(snapshot_services "$ns")"
  payload="$(jq -S -n \
    --argjson acc "$payload" \
    --arg ns "$ns" \
    --argjson deployments "$deployments" \
    --argjson services "$services" \
    '$acc + { ($ns): { deployments: $deployments, services: $services } }')"
done

mkdir -p "$(dirname "$OUT")"
jq -S -n \
  --arg capturedAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg server "$(oc whoami --show-server)" \
  --arg user "$(oc whoami)" \
  --argjson namespaces "$payload" \
  '{
     meta: {
       schemaVersion: 1,
       capturedAt: $capturedAt,
       server: $server,
       user: $user,
       note: "Only .namespaces is compared by teardown; .meta is volatile by design."
     },
     namespaces: $namespaces
   }' > "$OUT"

echo "snapshot written: ${OUT}"
for ns in "${PROTECTED_NAMESPACES[@]}"; do
  d=$(jq -r --arg ns "$ns" '.namespaces[$ns].deployments | length' "$OUT")
  s=$(jq -r --arg ns "$ns" '.namespaces[$ns].services    | length' "$OUT")
  echo "  ${ns}: ${d} deployments, ${s} services"
done
