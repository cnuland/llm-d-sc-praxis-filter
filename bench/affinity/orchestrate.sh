#!/usr/bin/env bash
# Wait for B-5 (bench pod, shared homelab backends) to finish, then run the
# model-affinity experiment: real pilot first, then the full 128-prompt run.
# Every stage is checkpointed (bench/results/affinity-*.jsonl), so this is
# safe to interrupt and safe to re-invoke -- it always resumes rather than
# repeating finished work.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."   # -> bench/
LOG=affinity/orchestrate.log
exec > >(tee -a "$LOG") 2>&1

echo "=== $(date -u +%FT%TZ) orchestrate start ==="

echo "-- waiting for B-5 (praxis-bench pod) to finish --"
for i in $(seq 1 480); do  # up to 4h of 30s polls
    if oc exec praxis-bench -n praxis-poc -- test -f /bench/b5c.done 2>/dev/null; then
        echo "B-5 done marker found after $((i*30))s of polling"
        break
    fi
    if ! oc get pod praxis-bench -n praxis-poc >/dev/null 2>&1; then
        echo "praxis-bench pod is gone -- treating B-5 as finished (or failed); proceeding"
        break
    fi
    sleep 30
done

echo "-- copying final B-5/B-7 results out of the cluster (best-effort) --"
oc exec praxis-bench -n praxis-poc -- cat /bench/bench/results/b5c-incluster.json > results/b5c-incluster.json 2>/dev/null \
    && echo "copied b5c-incluster.json" || echo "b5c-incluster.json not available yet (non-fatal)"
oc exec praxis-bench -n praxis-poc -- cat /bench/bench/results/b5c-incluster.records.jsonl > results/b5c-incluster.records.jsonl 2>/dev/null \
    && echo "copied b5c-incluster.records.jsonl" || true

echo "-- verifying frozen dataset before touching the real backends --"
(cd prompts && shasum -a 256 -c complexity-heldout.sha256) || { echo "HASH MISMATCH -- ABORTING"; exit 1; }

echo "-- real pilot: 8 prompts (2 per tier), against the real backends --"
python3 affinity/run_affinity.py generate --pilot 8
python3 affinity/run_affinity.py judge --pilot 8
python3 affinity/run_affinity.py matrix
echo "-- pilot complete; see output above. Proceeding to the full run. --"

echo "-- full run: all 128 prompts (resumes automatically; pilot's 8 are already checkpointed) --"
python3 affinity/run_affinity.py generate
python3 affinity/run_affinity.py judge
python3 affinity/run_affinity.py matrix

echo "=== $(date -u +%FT%TZ) orchestrate complete ==="
touch affinity/orchestrate.done
