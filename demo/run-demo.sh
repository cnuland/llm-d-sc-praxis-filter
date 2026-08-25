#!/usr/bin/env bash
#
# run-demo.sh — end-to-end proof for the `llm_d_sc` Praxis filter (SPEC Phase C).
#
# Brings up, on loopback only:
#   * llm-d-sc              127.0.0.1:50051  (real model, real gRPC — not a stub)
#   * stub upstream small   127.0.0.1:9101
#   * stub upstream large   127.0.0.1:9102
#   * stub upstream general 127.0.0.1:9103
#   * praxis                127.0.0.1:8080   (admin 127.0.0.1:9901)
#
# Then POSTs OpenAI-shaped chat completions whose expected class is not a guess:
# every prompt is a verbatim anchor from llm-d-sc's own complexity taxonomy
# (classifiers/complexity.json). It asserts which cluster each landed on, kills
# the classifier, and re-sends one request to prove the fail-open path still
# answers 200.
#
# Everything is torn down on exit, including on failure. Exits non-zero if any
# assertion failed. All output lands in demo/evidence/<UTC timestamp>/.
#
# Usage:
#   ./demo/run-demo.sh
#
# Overrides (all optional):
#   PRAXIS_BIN=/path/to/praxis        skip binary autodiscovery
#   PRAXIS_HOME=~/praxis              where to look for it (default ~/praxis)
#   LLM_D_SC_HOME=~/llm-d-sc-genesis  classifier repo root
#   SKIP_LATENCY_PROBE=1              don't run the gateway-probe latency capture
#   KEEP_RUNNING=1                    leave everything up after the run

set -Eeuo pipefail

# -----------------------------------------------------------------------------
# Paths and tunables
# -----------------------------------------------------------------------------

DEMO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "${DEMO_DIR}/.." && pwd)

LLM_D_SC_HOME=${LLM_D_SC_HOME:-${HOME}/llm-d-sc-genesis}
LLM_D_SC_BIN=${LLM_D_SC_BIN:-${LLM_D_SC_HOME}/target/release/llm-d-sc-server}
LLM_D_SC_PROBE_BIN=${LLM_D_SC_PROBE_BIN:-${LLM_D_SC_HOME}/target/release/llm-d-sc-gateway-probe}
LLM_D_SC_MODEL_DIR=${LLM_D_SC_MODEL_DIR:-${LLM_D_SC_HOME}/artifacts/models/complexity}
LLM_D_SC_CLASSIFIER=${LLM_D_SC_CLASSIFIER:-complexity}
LLM_D_SC_HOST=127.0.0.1
LLM_D_SC_PORT=50051

PRAXIS_HOME=${PRAXIS_HOME:-${HOME}/praxis}
PRAXIS_BIN=${PRAXIS_BIN:-}
PRAXIS_CONFIG=${PRAXIS_CONFIG:-${DEMO_DIR}/praxis-demo.yaml}
PROXY_HOST=127.0.0.1
PROXY_PORT=8080
ADMIN_HOST=127.0.0.1
ADMIN_PORT=9901

STUB=${DEMO_DIR}/stub-upstream.py
SMALL_PORT=9101
LARGE_PORT=9102
GENERAL_PORT=9103

RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
EVIDENCE_DIR=${DEMO_DIR}/evidence/${RUN_ID}
RUN_LOG=${EVIDENCE_DIR}/run.log
RESULTS_TSV=${EVIDENCE_DIR}/results.tsv

# Populated as children are spawned; drained in reverse by cleanup().
CHILD_PIDS=()
CHILD_NAMES=()
LAST_PID=
LLM_D_SC_PID=

FAILURES=0
CHECKS=0

# Row format shared by the header and the data rows, so they cannot drift.
ROW_FMT='%-40s %-9s %-9s %-5s %-17s %-20s %s%s%s\n'
RULE='---------------------------------------------------------------------------------------------------------------'

# -----------------------------------------------------------------------------
# Output
# -----------------------------------------------------------------------------

if [ -t 1 ]; then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
else
    C_RESET=
    C_BOLD=
    C_RED=
    C_GREEN=
    C_YELLOW=
fi

say() {
    if [ -d "${EVIDENCE_DIR}" ]; then
        printf '%s\n' "$*" | tee -a "${RUN_LOG}"
    else
        printf '%s\n' "$*"
    fi
}

section() {
    say ""
    say "${C_BOLD}== $* ==${C_RESET}"
}

warn() {
    say "${C_YELLOW}warn:${C_RESET} $*"
}

# Print an actionable message and stop. cleanup() still runs via the EXIT trap.
# Never call this from inside $( ) — the message would be captured, not printed.
die() {
    say ""
    say "${C_RED}${C_BOLD}FATAL${C_RESET} $1"
    shift
    while [ "$#" -gt 0 ]; do
        say "      $1"
        shift
    done
    exit 1
}

# -----------------------------------------------------------------------------
# Process and readiness helpers
# -----------------------------------------------------------------------------

# port_open <host> <port> — true when something accepts a TCP connection there.
# Uses bash's /dev/tcp rather than nc, which is not uniform across platforms.
port_open() {
    (exec 3<>"/dev/tcp/$1/$2") >/dev/null 2>&1
}

port_closed() {
    ! port_open "$1" "$2"
}

http_ok() {
    curl -fsS -m 2 -o /dev/null "$1"
}

# grep_log <file> <pattern>
grep_log() {
    grep -q -- "$2" "$1" 2>/dev/null
}

# wait_for <description> <timeout_secs> <command...>
wait_for() {
    local what=$1
    local timeout=$2
    shift 2
    local attempts=$((timeout * 5))
    local i=0
    while [ "${i}" -lt "${attempts}" ]; do
        if "$@" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.2
        i=$((i + 1))
    done
    say "${C_RED}timed out${C_RESET} after ${timeout}s waiting for ${what}"
    return 1
}

# start_bg <name> <logfile> <command...> — spawn it, remember it, set LAST_PID.
start_bg() {
    local name=$1
    local logfile=$2
    shift 2
    "$@" >"${logfile}" 2>&1 &
    LAST_PID=$!
    CHILD_PIDS+=("${LAST_PID}")
    CHILD_NAMES+=("${name}")
    say "  ${name}: pid ${LAST_PID}, log ${logfile##*/}"
}

# stop_pid <pid> <name> — SIGTERM, wait up to 10s, then SIGKILL.
stop_pid() {
    local pid=$1
    local name=$2
    if [ -z "${pid}" ] || ! kill -0 "${pid}" 2>/dev/null; then
        return 0
    fi
    kill -TERM "${pid}" 2>/dev/null || true
    local i=0
    while [ "${i}" -lt 50 ]; do
        if ! kill -0 "${pid}" 2>/dev/null; then
            break
        fi
        sleep 0.2
        i=$((i + 1))
    done
    if kill -0 "${pid}" 2>/dev/null; then
        warn "${name} (pid ${pid}) ignored SIGTERM; sending SIGKILL"
        kill -KILL "${pid}" 2>/dev/null || true
    fi
    wait "${pid}" 2>/dev/null || true
}

cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    set +e

    local i
    if [ "${KEEP_RUNNING:-0}" = "1" ] && [ "${#CHILD_PIDS[@]}" -gt 0 ]; then
        say ""
        say "KEEP_RUNNING=1 — leaving processes up. Stop them with:"
        i=0
        while [ "${i}" -lt "${#CHILD_PIDS[@]}" ]; do
            say "  kill ${CHILD_PIDS[${i}]}   # ${CHILD_NAMES[${i}]}"
            i=$((i + 1))
        done
        say "evidence: ${EVIDENCE_DIR}"
        exit "${rc}"
    fi

    if [ "${#CHILD_PIDS[@]}" -gt 0 ]; then
        say ""
        say "tearing down..."
        # Reverse order: the proxy first, its dependencies last.
        i=$((${#CHILD_PIDS[@]} - 1))
        while [ "${i}" -ge 0 ]; do
            stop_pid "${CHILD_PIDS[${i}]}" "${CHILD_NAMES[${i}]}"
            i=$((i - 1))
        done
        say "all demo processes stopped."
    fi

    if [ -d "${EVIDENCE_DIR}" ]; then
        say "evidence: ${EVIDENCE_DIR}"
    fi
    exit "${rc}"
}

# -----------------------------------------------------------------------------
# JSON helpers (python3 is already a hard dependency via the stub upstreams)
# -----------------------------------------------------------------------------

# json_get <file> <dotted.path> — empty string when absent or unparseable.
json_get() {
    python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as fh:
        cur = json.load(fh)
except Exception:
    print("")
    sys.exit(0)
for key in sys.argv[2].split("."):
    if isinstance(cur, dict) and key in cur:
        cur = cur[key]
    else:
        print("")
        sys.exit(0)
print(cur if not isinstance(cur, (dict, list)) else json.dumps(cur))
' "$1" "$2" 2>/dev/null
}

# chat_body <prompt> — an OpenAI-shaped chat completion request on stdout.
# Built with json.dumps so a prompt containing quotes cannot break the body.
chat_body() {
    python3 -c '
import json, sys
print(json.dumps({
    "model": "llm-d-sc-demo",
    "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": sys.argv[1]},
    ],
    "max_tokens": 64,
}))
' "$1"
}

# -----------------------------------------------------------------------------
# Preflight
# -----------------------------------------------------------------------------

# Echo the first usable praxis binary, or return 1. No output other than the
# path, so it is safe inside $( ).
find_praxis_bin() {
    local candidate
    for candidate in \
        "${PRAXIS_HOME}/target/release/praxis" \
        "${PRAXIS_HOME}/target/debug/praxis" \
        "${REPO_DIR}/target/release/praxis" \
        "${REPO_DIR}/target/debug/praxis"; do
        if [ -x "${candidate}" ] && [ -f "${candidate}" ]; then
            printf '%s' "${candidate}"
            return 0
        fi
    done
    if command -v praxis >/dev/null 2>&1; then
        command -v praxis
        return 0
    fi
    return 1
}

check_port_free() {
    local port=$1
    local who=$2
    if port_open 127.0.0.1 "${port}"; then
        die "port ${port} (needed by ${who}) is already in use." \
            "Find the owner:" \
            "  lsof -nP -iTCP:${port} -sTCP:LISTEN" \
            "" \
            "A previous run killed with SIGKILL can leave these behind." \
            "Stop the process and re-run."
    fi
}

preflight() {
    section "preflight"

    local tool
    for tool in curl python3; do
        if ! command -v "${tool}" >/dev/null 2>&1; then
            die "required tool '${tool}' is not on PATH."
        fi
    done
    say "  tools:       curl, $(python3 --version 2>&1)"

    # -- llm-d-sc binary ------------------------------------------------------
    if [ ! -x "${LLM_D_SC_BIN}" ]; then
        die "llm-d-sc server binary not found at:" \
            "  ${LLM_D_SC_BIN}" \
            "" \
            "Build it (this demo only ever RUNS that repo, it never writes to it):" \
            "  cd ${LLM_D_SC_HOME} && cargo build --release --bin llm-d-sc-server" \
            "" \
            "Or point the demo elsewhere:" \
            "  LLM_D_SC_BIN=/path/to/llm-d-sc-server $0"
    fi
    say "  llm-d-sc:    ${LLM_D_SC_BIN}"

    # -- ModelCar directory ---------------------------------------------------
    if [ ! -d "${LLM_D_SC_MODEL_DIR}" ]; then
        die "llm-d-sc model directory not found at:" \
            "  ${LLM_D_SC_MODEL_DIR}" \
            "" \
            "Fetch it:     cd ${LLM_D_SC_HOME} && ./hack/fetch-model" \
            "Or override:  LLM_D_SC_MODEL_DIR=/path/to/modelcar $0"
    fi
    local required
    local missing=
    for required in config.json tokenizer.json model.safetensors; do
        if [ ! -f "${LLM_D_SC_MODEL_DIR}/${required}" ]; then
            missing="${missing} ${required}"
        fi
    done
    if [ -n "${missing}" ]; then
        die "llm-d-sc model directory is incomplete: ${LLM_D_SC_MODEL_DIR}" \
            "  missing:${missing}" \
            "" \
            "A directory that merely exists never produces READY. Re-fetch it:" \
            "  cd ${LLM_D_SC_HOME} && ./hack/fetch-model"
    fi
    say "  model dir:   ${LLM_D_SC_MODEL_DIR}"
    say "  classifier:  ${LLM_D_SC_CLASSIFIER}"

    # -- stub upstream --------------------------------------------------------
    if [ ! -f "${STUB}" ]; then
        die "stub upstream script missing: ${STUB}"
    fi

    # -- praxis config --------------------------------------------------------
    if [ ! -f "${PRAXIS_CONFIG}" ]; then
        die "praxis config not found: ${PRAXIS_CONFIG}"
    fi
    say "  config:      ${PRAXIS_CONFIG}"

    # -- praxis binary --------------------------------------------------------
    if [ -n "${PRAXIS_BIN}" ]; then
        if [ ! -x "${PRAXIS_BIN}" ]; then
            die "PRAXIS_BIN is set to '${PRAXIS_BIN}' but that is not an executable file." \
                "Unset it to let the script search, or point it at the real binary."
        fi
    elif ! PRAXIS_BIN=$(find_praxis_bin); then
        die "praxis binary not found." \
            "The server crate is 'praxis-proxy' but its [[bin]] is named 'praxis'." \
            "Looked in:" \
            "  ${PRAXIS_HOME}/target/{release,debug}/praxis" \
            "  ${REPO_DIR}/target/{release,debug}/praxis" \
            "  \$PATH" \
            "" \
            "The proxy has to be built WITH this filter crate wired in:" \
            "  1. add one line to ${PRAXIS_HOME}/server/Cargo.toml under [dependencies]:" \
            "       llm-d-sc-praxis-filter = { path = \"${REPO_DIR}\" }" \
            "  2. cd ${PRAXIS_HOME} && cargo build -p praxis-proxy" \
            "" \
            "Then re-run, or set PRAXIS_BIN=/path/to/praxis explicitly."
    fi
    say "  praxis:      ${PRAXIS_BIN}"
    say "  praxis ver:  $("${PRAXIS_BIN}" --version 2>&1 | head -1)"

    # -- config validation ----------------------------------------------------
    # `praxis -t` instantiates every filter factory and runs the pipeline
    # ordering checks, so an unregistered `llm_d_sc` fails here with a readable
    # error instead of as a startup crash three steps later.
    local validate_out
    if ! validate_out=$("${PRAXIS_BIN}" -t -c "${PRAXIS_CONFIG}" 2>&1); then
        say "${C_RED}praxis rejected the config:${C_RESET}"
        say "${validate_out}"
        die "config validation failed for ${PRAXIS_CONFIG}." \
            "" \
            "If the error mentions an unknown filter 'llm_d_sc', the binary at" \
            "  ${PRAXIS_BIN}" \
            "was built without this crate. Add it to ${PRAXIS_HOME}/server/Cargo.toml:" \
            "  llm-d-sc-praxis-filter = { path = \"${REPO_DIR}\" }" \
            "and rebuild:  cd ${PRAXIS_HOME} && cargo build -p praxis-proxy"
    fi
    say "  config:      ${C_GREEN}valid${C_RESET} — the filter pipeline builds"

    # -- ports ----------------------------------------------------------------
    check_port_free "${LLM_D_SC_PORT}" "llm-d-sc"
    check_port_free "${SMALL_PORT}" "stub upstream 'small'"
    check_port_free "${LARGE_PORT}" "stub upstream 'large'"
    check_port_free "${GENERAL_PORT}" "stub upstream 'general'"
    check_port_free "${PROXY_PORT}" "praxis listener"
    check_port_free "${ADMIN_PORT}" "praxis admin"
    say "  ports:       ${LLM_D_SC_PORT} ${SMALL_PORT} ${LARGE_PORT} ${GENERAL_PORT} ${PROXY_PORT} ${ADMIN_PORT} — all free"
}

# -----------------------------------------------------------------------------
# Startup
# -----------------------------------------------------------------------------

upstream_port() {
    case "$1" in
        small) printf '%s' "${SMALL_PORT}" ;;
        large) printf '%s' "${LARGE_PORT}" ;;
        general) printf '%s' "${GENERAL_PORT}" ;;
        *) printf '%s' "0" ;;
    esac
}

start_upstreams() {
    section "starting stub upstreams"
    local name
    local port
    for name in small large general; do
        port=$(upstream_port "${name}")
        start_bg "stub:${name}" "${EVIDENCE_DIR}/upstream-${name}.log" \
            python3 "${STUB}" --name "${name}" --port "${port}"
    done

    for name in small large general; do
        port=$(upstream_port "${name}")
        if ! wait_for "stub upstream '${name}' on :${port}" 15 http_ok "http://127.0.0.1:${port}/__health"; then
            say "--- stub upstream logs ---"
            tail -n 40 "${EVIDENCE_DIR}"/upstream-*.log 2>/dev/null | tee -a "${RUN_LOG}"
            die "stub upstream '${name}' never became healthy on :${port}." \
                "See the log above and ${EVIDENCE_DIR}/upstream-${name}.log"
        fi
        say "  ${name}: $(curl -fsS -m 2 "http://127.0.0.1:${port}/__health" | tr -d '\n ')"
    done
}

start_classifier() {
    section "starting llm-d-sc (real model, real gRPC)"
    local log=${EVIDENCE_DIR}/llm-d-sc.log
    local t0
    local t1
    t0=$(date +%s)

    # `env` because start_bg is a shell function, so a VAR=... prefix would set
    # the variable for the function, not for the process it spawns.
    start_bg "llm-d-sc" "${log}" env \
        LLM_D_SC_MODEL_DIR="${LLM_D_SC_MODEL_DIR}" \
        LLM_D_SC_CLASSIFIER="${LLM_D_SC_CLASSIFIER}" \
        LLM_D_SC_LISTEN="${LLM_D_SC_HOST}:${LLM_D_SC_PORT}" \
        "${LLM_D_SC_BIN}"
    LLM_D_SC_PID=${LAST_PID}

    # llm-d-sc validates the ModelCar layout, loads tokenizer + config +
    # safetensors, and runs a warmup forward BEFORE it prints READY. A
    # directory that merely exists never gets that far, so the log line — not
    # the open port — is the authoritative readiness signal.
    if ! wait_for "llm-d-sc READY" 120 grep_log "${log}" "READY"; then
        say "--- llm-d-sc log ---"
        tail -n 40 "${log}" 2>/dev/null | tee -a "${RUN_LOG}"
        die "llm-d-sc never reported READY." \
            "The log above carries the typed error. Common causes:" \
            "  * incomplete ModelCar dir (${LLM_D_SC_MODEL_DIR})" \
            "  * a stale or partial model.safetensors download" \
            "Re-fetch with:  cd ${LLM_D_SC_HOME} && ./hack/fetch-model"
    fi
    if ! wait_for "llm-d-sc listening on :${LLM_D_SC_PORT}" 15 port_open "${LLM_D_SC_HOST}" "${LLM_D_SC_PORT}"; then
        die "llm-d-sc logged READY but nothing is accepting on ${LLM_D_SC_HOST}:${LLM_D_SC_PORT}."
    fi
    t1=$(date +%s)
    say "  READY in ~$((t1 - t0))s (model load + warmup forward)"
    say "  $(grep READY "${log}" | head -1)"
}

start_proxy() {
    section "starting praxis"
    local log=${EVIDENCE_DIR}/praxis.log
    start_bg "praxis" "${log}" env \
        RUST_LOG="${RUST_LOG:-info}" \
        "${PRAXIS_BIN}" -c "${PRAXIS_CONFIG}"

    if ! wait_for "praxis listener on :${PROXY_PORT}" 30 port_open "${PROXY_HOST}" "${PROXY_PORT}"; then
        say "--- praxis log ---"
        tail -n 40 "${log}" 2>/dev/null | tee -a "${RUN_LOG}"
        die "praxis never bound ${PROXY_HOST}:${PROXY_PORT}." \
            "See the log above and ${log}"
    fi
    if ! wait_for "praxis admin /healthy" 15 http_ok "http://${ADMIN_HOST}:${ADMIN_PORT}/healthy"; then
        warn "praxis admin did not answer /healthy; continuing (the data plane is up)"
    fi
    say "  listening on http://${PROXY_HOST}:${PROXY_PORT} (admin http://${ADMIN_HOST}:${ADMIN_PORT})"
}

# -----------------------------------------------------------------------------
# Requests and assertions
# -----------------------------------------------------------------------------

# send <slug> <prompt> — POST it. Response body and headers land in the evidence
# dir; stdout is "<http_code>\t<served_by>\t<echoed label>\t<echoed status>".
send() {
    local slug=$1
    local prompt=$2
    local req=${EVIDENCE_DIR}/${slug}.request.json
    local body=${EVIDENCE_DIR}/${slug}.response.json
    local hdrs=${EVIDENCE_DIR}/${slug}.response.headers
    local code

    chat_body "${prompt}" >"${req}"

    # The forged x-llm-d-sc-label exercises the anti-spoof strip (SPEC 4.7):
    # whatever the upstream echoes back must never be FORGED-BY-CLIENT.
    code=$(curl -sS -m 20 \
        -o "${body}" -D "${hdrs}" -w '%{http_code}' \
        -X POST "http://${PROXY_HOST}:${PROXY_PORT}/v1/chat/completions" \
        -H 'Content-Type: application/json' \
        -H 'x-llm-d-sc-label: FORGED-BY-CLIENT' \
        --data-binary "@${req}" 2>>"${RUN_LOG}") || code=000

    printf '%s\t%s\t%s\t%s\n' \
        "${code}" \
        "$(json_get "${body}" served_by)" \
        "$(json_get "${body}" 'llm_d_sc_headers.x-llm-d-sc-label')" \
        "$(json_get "${body}" 'llm_d_sc_headers.x-llm-d-sc-status')"
}

# record <slug> <expected cluster> <expect-provenance:yes|no> <prompt>
record() {
    local slug=$1
    local expected=$2
    local want_provenance=$3
    local prompt=$4
    local line code served echoed_label echoed_status verdict notes display

    line=$(send "${slug}" "${prompt}")
    code=$(printf '%s' "${line}" | cut -f1)
    served=$(printf '%s' "${line}" | cut -f2)
    echoed_label=$(printf '%s' "${line}" | cut -f3)
    echoed_status=$(printf '%s' "${line}" | cut -f4)

    display=${prompt}
    if [ "${#display}" -gt 39 ]; then
        display="${display:0:36}..."
    fi

    verdict=PASS
    notes=
    CHECKS=$((CHECKS + 1))

    if [ "${code}" != "200" ]; then
        verdict=FAIL
        notes="${notes} http=${code} (want 200)"
    fi
    if [ "${served}" != "${expected}" ]; then
        verdict=FAIL
        notes="${notes} served_by='${served}' (want '${expected}')"
    fi
    if [ "${echoed_label}" = "FORGED-BY-CLIENT" ]; then
        verdict=FAIL
        notes="${notes} client-supplied x-llm-d-sc-label reached the upstream (anti-spoof broken)"
    fi
    if [ "${want_provenance}" = "yes" ] && [ -z "${echoed_label}" ]; then
        verdict=FAIL
        notes="${notes} no x-llm-d-sc-label reached the upstream"
    fi
    if [ "${verdict}" = "FAIL" ]; then
        FAILURES=$((FAILURES + 1))
    fi

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "${display}" "${expected}" "${served:--}" "${code}" \
        "${echoed_label:--}" "${echoed_status:--}" "${verdict}" "${notes# }" \
        >>"${RESULTS_TSV}"
}

print_results_table() {
    local display expected served code echoed_label echoed_status verdict notes colour
    say ""
    # shellcheck disable=SC2059  # ROW_FMT is a constant format string, not data
    printf "${ROW_FMT}" \
        "PROMPT" "EXPECTED" "ACTUAL" "HTTP" "LABEL" "STATUS" "" "RESULT" "" | tee -a "${RUN_LOG}"
    printf '%s\n' "${RULE}" | tee -a "${RUN_LOG}"
    while IFS=$'\t' read -r display expected served code echoed_label echoed_status verdict notes; do
        if [ "${verdict}" = "PASS" ]; then
            colour=${C_GREEN}
        else
            colour=${C_RED}
        fi
        # shellcheck disable=SC2059  # ROW_FMT is a constant format string, not data
        printf "${ROW_FMT}" \
            "${display}" "${expected}" "${served}" "${code}" \
            "${echoed_label}" "${echoed_status}" "${colour}" "${verdict}" "${C_RESET}" \
            | tee -a "${RUN_LOG}"
        if [ -n "${notes}" ]; then
            say "    ${C_RED}^ ${notes}${C_RESET}"
        fi
    done <"${RESULTS_TSV}"
    printf '%s\n' "${RULE}" | tee -a "${RUN_LOG}"
    say "cumulative: ${CHECKS} checks, ${FAILURES} failed."
}

# -----------------------------------------------------------------------------
# Evidence capture
# -----------------------------------------------------------------------------

capture_environment() {
    local env_file=${EVIDENCE_DIR}/environment.txt
    {
        printf 'run_id              %s\n' "${RUN_ID}"
        printf 'date_utc            %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
        printf 'uname               %s\n' "$(uname -a)"
        printf 'bash                %s\n' "${BASH_VERSION}"
        printf 'python3             %s\n' "$(python3 --version 2>&1)"
        printf 'curl                %s\n' "$(curl --version 2>&1 | head -1)"
        printf 'praxis_bin          %s\n' "${PRAXIS_BIN}"
        printf 'praxis_version      %s\n' "$("${PRAXIS_BIN}" --version 2>&1 | head -1)"
        printf 'praxis_config       %s\n' "${PRAXIS_CONFIG}"
        printf 'llm_d_sc_bin        %s\n' "${LLM_D_SC_BIN}"
        printf 'llm_d_sc_model_dir  %s\n' "${LLM_D_SC_MODEL_DIR}"
        printf 'llm_d_sc_classifier %s\n' "${LLM_D_SC_CLASSIFIER}"
        printf 'filter_repo         %s @ %s\n' "${REPO_DIR}" "$(git -C "${REPO_DIR}" rev-parse --short HEAD 2>/dev/null || echo n/a)"
        printf 'praxis_repo         %s @ %s\n' "${PRAXIS_HOME}" "$(git -C "${PRAXIS_HOME}" rev-parse --short HEAD 2>/dev/null || echo n/a)"
        printf 'llm_d_sc_repo       %s @ %s\n' "${LLM_D_SC_HOME}" "$(git -C "${LLM_D_SC_HOME}" rev-parse --short HEAD 2>/dev/null || echo n/a)"
    } >"${env_file}"
    cp "${PRAXIS_CONFIG}" "${EVIDENCE_DIR}/praxis-demo.yaml" 2>/dev/null || true
    say "  environment: recorded in ${env_file##*/}"
}

capture_metrics() {
    local out=${EVIDENCE_DIR}/praxis-metrics.txt
    if ! curl -fsS -m 5 "http://${ADMIN_HOST}:${ADMIN_PORT}/metrics" -o "${out}" 2>/dev/null; then
        warn "could not scrape http://${ADMIN_HOST}:${ADMIN_PORT}/metrics"
        return 0
    fi
    section "metrics (${out##*/})"
    if ! grep -E '^llm_d_sc' "${out}" | sed 's/^/  /' | tee -a "${RUN_LOG}"; then
        say "  (no llm_d_sc_* series exported)"
    fi
}

latency_probe() {
    if [ "${SKIP_LATENCY_PROBE:-0}" = "1" ] || [ ! -x "${LLM_D_SC_PROBE_BIN}" ]; then
        return 0
    fi
    section "classify latency — the cost this filter adds per request"
    local out=${EVIDENCE_DIR}/classify-latency.json
    if ! "${LLM_D_SC_PROBE_BIN}" --target "${LLM_D_SC_HOST}:${LLM_D_SC_PORT}" \
        --topology same-host --samples 50 >"${out}" 2>>"${RUN_LOG}"; then
        warn "gateway-probe failed; skipping the latency capture"
        return 0
    fi
    # gateway-probe writes its JSON report to stdout and a human summary to
    # stderr, so read the JSON rather than tailing the file.
    local summary
    summary=$(python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as fh:
        d = json.load(fh)
    miss, hit = d["cache_miss_rtt_ms"], d["cache_hit_rtt_ms"]
    print("cache MISS p50=%.2f ms p99=%.2f ms | cache HIT p50=%.2f ms p99=%.2f ms  (n=%s)"
          % (miss["p50"], miss["p99"], hit["p50"], hit["p99"], d.get("samples", "?")))
except Exception as exc:
    print("could not parse the probe report: %s" % exc)
' "${out}")
    say "  ${summary}"
    say "  That is the classification cost the filter adds per request. It is real:"
    say "  a cache miss costs a model forward; a repeat prompt is served from"
    say "  llm-d-sc's own versioned result cache. Full report in ${out##*/}."
}

write_summary() {
    local summary=${EVIDENCE_DIR}/summary.json
    if ! python3 -c '
import json, sys
run_id, evidence, checks, failures, out = sys.argv[1:6]
payload = {
    "run_id": run_id,
    "evidence_dir": evidence,
    "checks": int(checks),
    "failures": int(failures),
    "result": "PASS" if int(failures) == 0 else "FAIL",
}
with open(out, "w") as fh:
    json.dump(payload, fh, indent=2)
    fh.write("\n")
' "${RUN_ID}" "${EVIDENCE_DIR}" "${CHECKS}" "${FAILURES}" "${summary}"; then
        warn "could not write ${summary}"
    fi
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

main() {
    mkdir -p "${EVIDENCE_DIR}"
    ln -snf "${RUN_ID}" "${DEMO_DIR}/evidence/latest"
    : >"${RESULTS_TSV}"

    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    say "${C_BOLD}llm_d_sc Praxis filter — end-to-end demo${C_RESET}"
    say "run id:   ${RUN_ID}"
    say "evidence: ${EVIDENCE_DIR}"

    preflight
    capture_environment

    start_upstreams
    start_classifier
    start_proxy

    section "warm-up (not asserted)"
    # Primes three things at once: the lazily-connected gRPC channel, the
    # upstream keep-alive pool, and llm-d-sc's result cache. Without it the
    # first asserted request would pay TCP + HTTP/2 setup inside the filter's
    # 100 ms budget, and the timings in the evidence would be misleading.
    local warm
    warm=$(send warmup "Warm up the classification path before the measured requests.")
    say "  http $(printf '%s' "${warm}" | cut -f1), served_by=$(printf '%s' "${warm}" | cut -f2)"

    section "classified routing (T-E1 / T-E2)"
    say "Every prompt below is a verbatim anchor from"
    say "  ${LLM_D_SC_HOME}/classifiers/complexity.json"
    say "so the expected label is the taxonomy's own definition of that class, not a guess."
    say "Each request also carries a forged 'x-llm-d-sc-label: FORGED-BY-CLIENT' header;"
    say "the filter must strip it, so LABEL below can never be FORGED-BY-CLIENT."

    record simple small yes \
        "What is the capital of France?"
    record complex large yes \
        "Design a microservices architecture for an e-commerce platform with inventory, orders, and payments."
    record reasoning large yes \
        "Prove by mathematical induction that the sum of 1 to n equals n(n+1)/2."

    print_results_table

    capture_metrics
    latency_probe

    section "fail-open: classifier killed mid-run (T-E3)"
    say "Stopping llm-d-sc. Routing quality degrades; the gateway does not."
    stop_pid "${LLM_D_SC_PID}" "llm-d-sc"
    if ! wait_for "llm-d-sc :${LLM_D_SC_PORT} to close" 15 port_closed "${LLM_D_SC_HOST}" "${LLM_D_SC_PORT}"; then
        die "llm-d-sc is still accepting on :${LLM_D_SC_PORT} after SIGTERM;" \
            "the fail-open assertion would prove nothing. Aborting."
    fi
    say "  llm-d-sc stopped; :${LLM_D_SC_PORT} is closed."

    : >"${RESULTS_TSV}"
    # The same SIMPLE prompt that routed to 'small' a moment ago. With the
    # classifier gone it must fall back to default_cluster and still answer 200.
    # No provenance label is expected — there was no classification.
    record failopen general no \
        "What is the capital of France?"
    print_results_table

    write_summary

    section "result"
    if [ "${FAILURES}" -eq 0 ]; then
        say "${C_GREEN}${C_BOLD}PASS${C_RESET} — ${CHECKS}/${CHECKS} checks green."
        say "  simple -> small, complex -> large, reasoning -> large, classifier down -> general (200)."
        return 0
    fi
    say "${C_RED}${C_BOLD}FAIL${C_RESET} — ${FAILURES} of ${CHECKS} checks failed. See ${EVIDENCE_DIR}."
    return 1
}

main "$@"
