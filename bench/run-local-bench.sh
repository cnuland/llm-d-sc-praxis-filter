#!/usr/bin/env bash
# Run the LOCAL benchmark scenarios (B-1, B-2, B-3, B-4, B-6) end to end.
#
# Local means: real llm-d-sc, real Praxis with the real filter, STUB upstreams.
# No homelab model endpoint is contacted by anything in this script. B-5 and B-7
# target real models and run separately, in-cluster, under --allow-homelab.
#
# Everything is torn down on any exit path, including interrupt.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH="${REPO_ROOT}/bench"
GENESIS="${HOME}/llm-d-sc-genesis"
PRAXIS_BIN="${HOME}/praxis/target/release/praxis"
SC_BIN="${GENESIS}/target/release/llm-d-sc-server"
MODEL_DIR="${GENESIS}/artifacts/models/complexity"

PIDS=()
FAILED=0

cleanup() {
    echo
    echo "tearing down..."
    for pid in "${PIDS[@]:-}"; do
        [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null
    done
    sleep 1
    for pid in "${PIDS[@]:-}"; do
        [[ -n "${pid}" ]] && kill -9 "${pid}" 2>/dev/null
    done
    wait 2>/dev/null
    echo "all benchmark processes stopped."
}
trap cleanup EXIT INT TERM

need() { [[ -x "$1" ]] || { echo "MISSING: $1 — $2" >&2; exit 1; }; }
need "${PRAXIS_BIN}" "build it: cd ~/praxis && cargo build --release -p praxis-proxy --bin praxis"
need "${SC_BIN}"     "build it: cd ~/llm-d-sc-genesis && cargo build --release"
[[ -d "${MODEL_DIR}" ]] || { echo "MISSING model dir ${MODEL_DIR}" >&2; exit 1; }

wait_port() {
    local port="$1" name="$2" tries="${3:-60}"
    for _ in $(seq 1 "${tries}"); do
        nc -z 127.0.0.1 "${port}" 2>/dev/null && return 0
        sleep 0.5
    done
    echo "TIMED OUT waiting for ${name} on :${port}" >&2
    return 1
}

echo "== stub upstreams =="
python3 "${BENCH}/stub_upstream.py" --port 9001 --model small-stub --echo-sc-headers >"${BENCH}/results/stub-small.log" 2>&1 &
PIDS+=($!)
python3 "${BENCH}/stub_upstream.py" --port 9002 --model large-stub --echo-sc-headers >"${BENCH}/results/stub-large.log" 2>&1 &
PIDS+=($!)
wait_port 9001 "stub small" || exit 1
wait_port 9002 "stub large" || exit 1
echo "  small :9001, large :9002 (both echo x-llm-d-sc-*)"

echo "== llm-d-sc (real classifier, real model) =="
LLM_D_SC_MODEL_DIR="${MODEL_DIR}" \
LLM_D_SC_CLASSIFIER=complexity \
LLM_D_SC_LISTEN=127.0.0.1:50051 \
    "${SC_BIN}" >"${BENCH}/results/llm-d-sc.log" 2>&1 &
PIDS+=($!)
# The gRPC port opens only AFTER the warmup forward, so waiting on the port is
# waiting on readiness — no separate readiness poll needed.
wait_port 50051 "llm-d-sc" 120 || { tail -5 "${BENCH}/results/llm-d-sc.log"; exit 1; }
echo "  ready: $(grep -o 'READY.*' "${BENCH}/results/llm-d-sc.log" | head -1)"

echo "== slow classifier stub (B-6 timeout case) =="
"${REPO_ROOT}/target/debug/examples/slow-classifier" 127.0.0.1:50052 500 \
    >"${BENCH}/results/slow-classifier.log" 2>&1 &
PIDS+=($!)
wait_port 50052 "slow-classifier stub" || exit 1
echo "  :50052 sleeping 500 ms per answer (filter budget is 100 ms)"

echo "== praxis (baseline :8080 | classified :8081) =="
"${PRAXIS_BIN}" -c "${BENCH}/praxis-bench.yaml" >"${BENCH}/results/praxis.log" 2>&1 &
PIDS+=($!)
wait_port 8080 "praxis baseline" || exit 1
wait_port 8081 "praxis classified" || exit 1
for p in 8091 8092 8093 8094; do wait_port "$p" "praxis degradation :$p" || exit 1; done
echo "  both listeners up"

echo
echo "== sanity: one request through each listener =="
BASE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST http://127.0.0.1:8080/v1/chat/completions \
    -H 'Content-Type: application/json' -d '{"messages":[{"role":"user","content":"What is the capital of France?"}]}')
echo "  baseline   http=${BASE}"

# The stub echoes received x-llm-d-sc-* into RESPONSE headers, so -D is required;
# the body alone cannot prove classification happened. It especially cannot here,
# because `general` (the fail-open target) and `small` both point at :9001 — a
# body saying "small-stub" is exactly what a SILENTLY FAILING filter would also
# produce. The provenance header is the only thing that distinguishes
# "classified as SIMPLE" from "classification failed, fell open".
CLS_HDRS=$(curl -s -D - -o /dev/null --max-time 10 -X POST http://127.0.0.1:8081/v1/chat/completions \
    -H 'Content-Type: application/json' -d '{"messages":[{"role":"user","content":"What is the capital of France?"}]}')
LABEL=$(grep -i '^x-llm-d-sc-label:' <<<"${CLS_HDRS}" | tr -d '\r' | awk '{print $2}')
STATUS=$(grep -i '^x-llm-d-sc-status:' <<<"${CLS_HDRS}" | tr -d '\r' | awk '{print $2}')
echo "  classified label=${LABEL:-<none>} status=${STATUS:-<none>}"
if [[ "${STATUS}" != "OK" ]]; then
    echo "  FATAL: classified listener reported status='${STATUS:-<none>}', not OK." >&2
    echo "  Either the filter is not running or llm-d-sc is not answering; benchmarking" >&2
    echo "  either one would measure the fail-open path and call it classification." >&2
    exit 1
fi

# A COMPLEX prompt must reach :9002. `small` and `general` are both :9001, so
# this is the only check that proves the filter can steer to a DIFFERENT cluster
# rather than merely annotating a request it always sends to the same place.
CLS_LARGE=$(curl -s --max-time 10 -X POST http://127.0.0.1:8081/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"messages":[{"role":"user","content":"Design a microservices architecture for an e-commerce platform with inventory, orders, and payments."}]}')
if ! grep -q 'large-stub' <<<"${CLS_LARGE}"; then
    echo "  FATAL: a COMPLEX prompt did not reach the large cluster. Got: ${CLS_LARGE}" >&2
    exit 1
fi
echo "  complex prompt -> large-stub (cluster steering confirmed)"

run() {
    local name="$1"; shift
    echo
    echo "======================================================================"
    echo "== ${name}"
    echo "======================================================================"
    if python3 "${BENCH}/harness.py" "$@"; then
        echo "-- ${name}: OK"
    else
        echo "-- ${name}: FAILED (rc=$?)" >&2
        FAILED=$((FAILED + 1))
    fi
}

run "B-1 filter overhead" --scenario b1 \
    --target http://127.0.0.1:8081 --param baseline_url=http://127.0.0.1:8080 \
    --warmup 200 --measured 1000 --concurrency 1,4,16 --topology local-loopback

run "B-2 body-size sensitivity" --scenario b2 \
    --target http://127.0.0.1:8081 --param baseline_url=http://127.0.0.1:8080 \
    --warmup 100 --measured 500 --concurrency 1 --topology local-loopback

run "B-3 prompt-length sensitivity" --scenario b3 \
    --target http://127.0.0.1:8081 \
    --warmup 100 --measured 300 --concurrency 1,4 --topology local-loopback

run "B-4 routing correctness" --scenario b4 \
    --target http://127.0.0.1:8081 \
    --warmup 20 --concurrency 1 --topology local-loopback

run "B-6 degradation and failure" --scenario b6 \
    --target http://127.0.0.1:8081 --param baseline_url=http://127.0.0.1:8080 \
    --param down_url=http://127.0.0.1:8091 \
    --param slow_url=http://127.0.0.1:8092 \
    --param exhausted_url=http://127.0.0.1:8093 \
    --param reject_url=http://127.0.0.1:8094 \
    --param timeout_ms=100 \
    --warmup 50 --measured 300 --concurrency 1,4 --topology local-loopback

echo
echo "======================================================================"
if (( FAILED == 0 )); then
    echo "ALL LOCAL SCENARIOS COMPLETED"
else
    echo "${FAILED} SCENARIO(S) FAILED — see output above"
fi
echo "results: ${BENCH}/results/"
echo "======================================================================"
exit "${FAILED}"
