#!/usr/bin/env bash
# stage-build-context.sh - assemble a clean, reproducible build context.
#
# The Praxis image needs BOTH trees in one context: the praxis workspace and this
# filter crate (SPEC 5, Phase A wires them with a single path dependency). This
# script copies them into a temp dir as siblings, mirroring the on-laptop layout
# so relative path dependencies resolve identically inside the container:
#
#   <context>/praxis/                   <- the Praxis workspace
#   <context>/llm-d-sc-praxis-filter/   <- this crate
#   <context>/Containerfile.praxis      <- the build recipe (BuildConfig dockerfilePath)
#
# `target/` and `.git/` are excluded: this crate's target/ alone is ~1.9 GB, and
# shipping it would push two gigabytes of host-arch (arm64) artifacts into an
# amd64 in-cluster build that cannot use them anyway.
#
# stdout is ONLY the context path, so callers can do:
#   CTX="$(hack/stage-build-context.sh)"
# All diagnostics go to stderr.
#
# Usage:
#   hack/stage-build-context.sh [destination-dir]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRAXIS_ROOT="${PRAXIS_ROOT:-/Users/cnuland/praxis}"

log() { echo "$@" >&2; }

command -v rsync >/dev/null 2>&1 || { log "FATAL: rsync not found in PATH"; exit 1; }

[[ -f "${PRAXIS_ROOT}/Cargo.toml" ]] || {
  log "FATAL: praxis workspace not found at ${PRAXIS_ROOT} (override with PRAXIS_ROOT=...)"; exit 1; }
[[ -f "${REPO_ROOT}/Cargo.toml" ]] || {
  log "FATAL: filter crate Cargo.toml not found at ${REPO_ROOT}"; exit 1; }
[[ -f "${REPO_ROOT}/Containerfile.praxis" ]] || {
  log "FATAL: Containerfile.praxis not found at ${REPO_ROOT}"; exit 1; }

if [[ $# -ge 1 ]]; then
  CONTEXT="$1"
  mkdir -p "$CONTEXT"
else
  CONTEXT="$(mktemp -d "${TMPDIR:-/tmp}/praxis-build-context.XXXXXX")"
fi

# Excludes applied to BOTH trees.
#   target/     - build artifacts, host-arch, ~1.9 GB in this crate
#   .git/       - history is irrelevant to a build and carries no value in-cluster
#   .env*       - never let a credential file ride along into a build context
# The remaining entries keep the context deterministic (no editor/OS droppings).
EXCLUDES=(
  --exclude='target/'
  --exclude='.git/'
  --exclude='.env'
  --exclude='.env.*'
  --exclude='.envrc'
  --exclude='.DS_Store'
  --exclude='*.swp'
  --exclude='node_modules/'
)

log "staging build context -> ${CONTEXT}"

# --delete makes re-running against an explicit destination idempotent rather
# than cumulative, so a stale file from a previous staging can never leak in.
rsync -a --delete "${EXCLUDES[@]}" "${PRAXIS_ROOT}/"  "${CONTEXT}/praxis/"
rsync -a --delete "${EXCLUDES[@]}" "${REPO_ROOT}/"    "${CONTEXT}/llm-d-sc-praxis-filter/"

# The Containerfile sits at the context root so the BuildConfig's
# dockerfilePath (relative to the context root) is simply the file name.
cp "${REPO_ROOT}/Containerfile.praxis" "${CONTEXT}/Containerfile.praxis"

# Sanity: the two things whose absence would produce a confusing build failure.
[[ -f "${CONTEXT}/praxis/server/Cargo.toml" ]] || { log "FATAL: staged praxis tree is missing server/Cargo.toml"; exit 1; }
[[ -f "${CONTEXT}/llm-d-sc-praxis-filter/src/lib.rs" ]] || { log "FATAL: staged filter crate is missing src/lib.rs"; exit 1; }

if [[ -d "${CONTEXT}/praxis/target" || -d "${CONTEXT}/llm-d-sc-praxis-filter/target" ]]; then
  log "FATAL: target/ leaked into the build context"; exit 1
fi

size="$(du -sh "$CONTEXT" | cut -f1 | tr -d ' ')"
files="$(find "$CONTEXT" -type f | wc -l | tr -d ' ')"
log "context ready: ${files} files, ${size}"

echo "$CONTEXT"
