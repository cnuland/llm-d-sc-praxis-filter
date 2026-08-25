#!/usr/bin/env python3
"""bench/harness.py — the load driver for the `llm_d_sc` gateway benchmarks.

Python 3 standard library only, deliberately: SPEC-K8S §2 runs this as a Job
inside the cluster (`Job praxis-bench`) on a minimal image, and a harness that
needs `pip install` is a harness that cannot be run where the numbers must be
measured (SPEC-BENCH §1 B-7: "a number measured through oc port-forward is not
a network measurement").

What it is
----------
A closed-loop driver: a fixed pool of worker threads, each holding one
persistent HTTP connection, each sending the next request as soon as its
previous response has been fully read. Offered load is therefore bounded by the
pool size, never by a rate schedule — which is the correct shape for measuring
a proxy's per-request cost, because it cannot queue work the system has not
accepted.

Methodology rules this file enforces (SPEC-BENCH §0), not merely documents
----------------------------------------------------------------------------
* **Never a mean.** `reduce()` emits p50/p90/p95/p99/max and nothing else.
  There is no code path in this file that computes an average latency.
* **Per-request records, never pre-aggregated.** Every request produces a
  record (timestamp, wall-clock ns, status, response `model`, every
  `x-llm-d-sc-*` header, byte counts, error). They are written to a `.jsonl`
  sidecar so any distribution in the report can be recomputed from source.
* **Warmup is excluded from the window.** Warmup runs as a separate phase with
  its own records, tagged `phase: "warmup"`, and the measured window's clock
  starts after warmup has fully drained.
* **Every arm carries self-assertions.** Five are applied by the harness to
  every arm regardless of the scenario (sample count, transport errors, status
  expectations, warmup exclusion, percentile monotonicity) and the scenario
  adds its own premise checks on top. A failed assertion exits non-zero.
* **Disjoint cache namespaces.** `measure_context`/`warm_context` reproduce the
  `run_id`-namespaced scheme in `~/llm-d-sc-genesis/src/bench.rs`, so a request
  that is meant to be a cache miss can never be silently served from a
  pre-warmed key.

Usage
-----
    python3 bench/harness.py --list
    python3 bench/harness.py --scenario smoke --target http://127.0.0.1:9001 \
        --warmup 50 --measured 300 --concurrency 1,4 --out bench/results/smoke.json
    python3 bench/harness.py --scenario b1 --dry-run

Exit codes: 0 all assertions passed · 1 an assertion failed · 2 setup failed.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import http.client
import importlib.util
import json
import math
import os
import platform
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIO_DIR = os.path.join(HERE, "scenarios")
RESULTS_DIR = os.path.join(HERE, "results")
REPO_ROOT = os.path.dirname(HERE)
GENESIS_ROOT = os.environ.get("BENCH_GENESIS_ROOT", os.path.expanduser("~/llm-d-sc-genesis"))

SC_PREFIX = "x-llm-d-sc-"

# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


def percentile(sorted_values, p):
    """Nearest-rank percentile over an ascending list.

    Identical definition to `llm-d-sc/src/bench.rs::percentile`, so a figure
    produced here is directly comparable with llm-d-sc's published tables
    rather than merely similar to them.
    """
    if not sorted_values:
        return 0.0
    rank = math.ceil((p / 100.0) * len(sorted_values))
    idx = min(max(rank - 1, 0), len(sorted_values) - 1)
    return sorted_values[idx]


def reduce_latency(values_ms):
    """Reduce raw per-request latencies to the published distribution.

    Emits p50/p90/p95/p99/max only. A mean is deliberately absent: SPEC-BENCH
    §0 rule 1 forbids an average-only latency claim, and the surest way to keep
    one out of a report is to never compute it.
    """
    ordered = sorted(values_ms)
    return {
        "n": len(ordered),
        "p50": percentile(ordered, 50),
        "p90": percentile(ordered, 90),
        "p95": percentile(ordered, 95),
        "p99": percentile(ordered, 99),
        "max": ordered[-1] if ordered else 0.0,
    }


# ---------------------------------------------------------------------------
# Cache key namespaces — ported from ~/llm-d-sc-genesis/src/bench.rs
# ---------------------------------------------------------------------------


def measure_context(run_id, index):
    """The measured key for `index` under `run_id`.

    NEVER pre-warmed in miss mode; EXACTLY pre-warmed in hit mode. Unique per
    run, so two runs never share measured keys.
    """
    return "measure-%s-%d" % (run_id, index)


def warm_context(cache_mode, run_id, index):
    """The warmup key for `index`.

    hit  -> exactly the measured key, so the measured request genuinely hits.
    miss -> the disjoint `warm-{i}` namespace, so the measured request cannot.
    """
    if cache_mode == "hit":
        return measure_context(run_id, index)
    return "warm-%d" % index


def seeded_context(seed, base):
    """Prefix a length- or size-specific seed onto a key namespace."""
    return base if not seed else "%s %s" % (seed, base)


def key_for(cache_mode, phase, run_id, index, seed=""):
    """The cache key a request should carry, given its mode and phase.

    A hit-mode arm repeats ONE key across the whole measured window (index 0),
    because "the cache is hot" means the same prompt, not a fresh prompt per
    request. A miss-mode arm uses a unique key per request.
    """
    if cache_mode == "hit":
        return seeded_context(seed, measure_context(run_id, 0))
    if phase == "warmup":
        return seeded_context(seed, warm_context(cache_mode, run_id, index))
    return seeded_context(seed, measure_context(run_id, index))


# ---------------------------------------------------------------------------
# Requests, arms, results
# ---------------------------------------------------------------------------


class Request:
    """One HTTP request the driver will send."""

    __slots__ = ("method", "path", "headers", "body", "meta")

    def __init__(self, method="POST", path="/v1/chat/completions", headers=None, body=b"", meta=None):
        self.method = method
        self.path = path
        self.headers = headers or {}
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.body = body
        self.meta = meta or {}


def assertion(name, passed, detail):
    """One scenario self-check. `passed=False` fails the whole run."""
    return {"name": name, "passed": bool(passed), "detail": str(detail)}


class Arm:
    """One measured window: a single configuration, measured once.

    An "arm" is the unit that appears in the output JSON's `scenarios` list. A
    scenario (B-1, B-2, ...) usually declares several — the whole point of
    SPEC-BENCH §0 rule 2 is that a cost figure is a delta between two arms that
    differ in exactly one respect.
    """

    def __init__(
        self,
        name,
        target,
        build,
        params=None,
        warmup=50,
        measured=300,
        concurrency=1,
        expected_status=(200,),
        cache_mode="n/a",
        timeout_s=30.0,
        allow_errors=0,
        assertions=None,
        summarize=None,
        setup=None,
        teardown=None,
        notes="",
    ):
        self.name = name
        self.target = target
        self.build = build  # (index, phase, run_id) -> Request
        self.params = dict(params or {})
        self.warmup = warmup
        self.measured = measured
        self.concurrency = concurrency
        self.expected_status = tuple(expected_status)
        self.cache_mode = cache_mode
        self.timeout_s = timeout_s
        self.allow_errors = allow_errors
        self.assertions = assertions  # (result, ctx) -> [assertion]
        self.summarize = summarize  # (result, ctx) -> dict
        self.setup = setup
        self.teardown = teardown
        self.notes = notes


class ArmResult:
    """The captured outcome of one arm: raw records plus their reduction."""

    def __init__(self, arm):
        self.arm = arm
        self.records = []  # measured phase only
        self.warmup_records = []
        self.window_s = 0.0
        self.latency_ms = {}
        self.throughput = 0.0
        self.errors = 0
        self.status_counts = {}
        self.assertions = []
        self.extra = {}

    def ok_records(self):
        return [r for r in self.records if r["error"] is None and r["status"] in self.arm.expected_status]

    def latencies_ms(self):
        return [r["wall_ns"] / 1e6 for r in self.ok_records()]

    def to_json(self):
        entry = {
            "name": self.arm.name,
            "params": dict(self.arm.params, **{
                "target": self.arm.target,
                "concurrency": self.arm.concurrency,
                "warmup": self.arm.warmup,
                "measured": self.arm.measured,
                "cache_mode": self.arm.cache_mode,
                "expected_status": list(self.arm.expected_status),
            }),
            "latency_ms": self.latency_ms,
            "throughput": self.throughput,
            "measured_window_s": self.window_s,
            "errors": self.errors,
            "status_counts": self.status_counts,
            "assertions": self.assertions,
        }
        if self.arm.notes:
            entry["notes"] = self.arm.notes
        if self.extra:
            entry["extra"] = self.extra
        return entry


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


class _Counter:
    def __init__(self, limit):
        self._i = 0
        self._limit = limit
        self._lock = threading.Lock()

    def take(self):
        with self._lock:
            if self._i >= self._limit:
                return None
            i = self._i
            self._i += 1
            return i


def _connect(target, timeout_s):
    parts = urllib.parse.urlsplit(target)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        import ssl

        ctx = ssl._create_unverified_context()
        conn = http.client.HTTPSConnection(host, port, timeout=timeout_s, context=ctx)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
    conn.connect()
    return conn


def _drive_phase(arm, phase, count, run_id, sink):
    """Run one phase (warmup or measured) with a fixed closed-loop worker pool.

    Returns the wall-clock duration of the phase. Each worker pre-connects
    BEFORE the clock starts, so TCP establishment is never inside a measured
    per-request latency. Requests are issued back-to-back per worker: the pool
    size is the concurrency, and there is no think time.
    """
    if count <= 0:
        return 0.0
    counter = _Counter(count)
    workers = max(1, arm.concurrency)
    barrier = threading.Barrier(workers + 1)
    lock = threading.Lock()

    def worker(wid):
        conn = None
        try:
            conn = _connect(arm.target, arm.timeout_s)
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            conn = None
            connect_error = "%s: %s" % (type(exc).__name__, exc)
        else:
            connect_error = None
        barrier.wait()  # every worker connected before the clock starts
        local = []
        while True:
            i = counter.take()
            if i is None:
                break
            req = arm.build(i, phase, run_id)
            headers = dict(req.headers)
            headers.setdefault("Content-Type", "application/json")
            headers.setdefault("Content-Length", str(len(req.body)))
            headers.setdefault("Connection", "keep-alive")
            rec = {
                "arm": arm.name,
                "phase": phase,
                "i": i,
                "worker": wid,
                "t_unix": None,
                "wall_ns": None,
                "status": None,
                "model": None,
                "sc_headers": {},
                "req_bytes": len(req.body),
                "resp_bytes": 0,
                "error": connect_error,
                "meta": req.meta,
            }
            if conn is None:
                try:
                    conn = _connect(arm.target, arm.timeout_s)
                    rec["error"] = None
                except Exception as exc:  # noqa: BLE001
                    rec["error"] = "connect: %s: %s" % (type(exc).__name__, exc)
                    rec["t_unix"] = time.time()
                    rec["wall_ns"] = 0
                    local.append(rec)
                    continue
            rec["t_unix"] = time.time()
            t0 = time.perf_counter_ns()
            try:
                conn.request(req.method, req.path, body=req.body, headers=headers)
                resp = conn.getresponse()
                payload = resp.read()  # full body read is part of the measurement
                t1 = time.perf_counter_ns()
                rec["wall_ns"] = t1 - t0
                rec["status"] = resp.status
                rec["resp_bytes"] = len(payload)
                rec["error"] = None
                for name, value in resp.getheaders():
                    lname = name.lower()
                    if lname.startswith(SC_PREFIX):
                        rec["sc_headers"][lname] = value
                if payload[:1] in (b"{", b"["):
                    try:
                        doc = json.loads(payload)
                        if isinstance(doc, dict):
                            rec["model"] = doc.get("model")
                            usage = doc.get("usage")
                            if isinstance(usage, dict):
                                rec["completion_tokens"] = usage.get("completion_tokens")
                                rec["prompt_tokens"] = usage.get("prompt_tokens")
                            choices = doc.get("choices")
                            if isinstance(choices, list) and choices:
                                msg = choices[0].get("message") or {}
                                content = msg.get("content")
                                if isinstance(content, str):
                                    rec["content_chars"] = len(content)
                    except (ValueError, AttributeError):
                        pass
                if resp.will_close:
                    conn.close()
                    conn = None
            except Exception as exc:  # noqa: BLE001 - a failed request is data
                rec["wall_ns"] = time.perf_counter_ns() - t0
                rec["error"] = "%s: %s" % (type(exc).__name__, exc)
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = None
            local.append(rec)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        with lock:
            sink.extend(local)

    threads = [threading.Thread(target=worker, args=(w,), daemon=True) for w in range(workers)]
    for t in threads:
        t.start()
    barrier.wait()
    t0 = time.perf_counter()
    for t in threads:
        t.join()
    return time.perf_counter() - t0


def harness_assertions(result):
    """The five checks the harness applies to EVERY arm.

    A scenario author cannot forget these, which is the point: SPEC-BENCH §0
    rule 3 makes the harness responsible for proving its own methodology, not
    the scenario.
    """
    arm = result.arm
    out = []
    out.append(
        assertion(
            "sample_count",
            len(result.records) == arm.measured,
            "captured %d measured records, expected %d" % (len(result.records), arm.measured),
        )
    )
    out.append(
        assertion(
            "transport_errors_within_budget",
            result.errors <= arm.allow_errors,
            "%d requests errored or returned an unexpected status; budget %d; statuses %s"
            % (result.errors, arm.allow_errors, result.status_counts),
        )
    )
    unexpected = {s: n for s, n in result.status_counts.items() if s not in [str(x) for x in arm.expected_status]}
    out.append(
        assertion(
            "status_as_expected",
            not unexpected or result.errors <= arm.allow_errors,
            "expected %s, saw %s" % (list(arm.expected_status), result.status_counts),
        )
    )
    out.append(
        assertion(
            "warmup_excluded_from_window",
            all(r["phase"] == "measured" for r in result.records)
            and len(result.warmup_records) == arm.warmup,
            "%d warmup requests ran in a separate phase and contributed no measured record"
            % len(result.warmup_records),
        )
    )
    lat = result.latency_ms
    monotone = bool(lat) and lat["p50"] <= lat["p90"] <= lat["p95"] <= lat["p99"] <= lat["max"]
    out.append(
        assertion(
            "percentiles_monotone",
            monotone,
            "p50=%.4f p90=%.4f p95=%.4f p99=%.4f max=%.4f"
            % (lat.get("p50", 0), lat.get("p90", 0), lat.get("p95", 0), lat.get("p99", 0), lat.get("max", 0))
            if lat
            else "no latency samples",
        )
    )
    return out


def run_arm(arm, run_id, ctx, verbose=True):
    result = ArmResult(arm)
    if arm.setup:
        arm.setup(ctx)
    if verbose:
        print(
            "  arm %-28s c=%-3d warmup=%-5d measured=%-6d -> %s"
            % (arm.name, arm.concurrency, arm.warmup, arm.measured, arm.target),
            flush=True,
        )
    # Every arm gets its OWN key namespace, derived from the run id and the arm
    # name. This is enforced HERE rather than left to each scenario's `seed=`,
    # because leaving it to the scenario is exactly how it went wrong: B-1, B-2
    # and B-3 each declared several miss-mode arms that differed only in
    # concurrency or body size, all sharing one seed. The first arm paid the real
    # model forwards and warmed llm-d-sc's cache; every later arm re-sent the
    # identical prompts and measured CACHE HITS while reporting them as misses
    # (B-3 c=4 read 0.386 ms against the c=1 truth of 13.9 ms). The per-arm
    # uniqueness assertion did not catch it, because within any single arm the
    # keys really were unique.
    #
    # `assert_no_cross_arm_key_reuse` checks the invariant after the fact; this
    # line is what makes it hold in the first place.
    arm_run_id = "%s-%s" % (run_id, arm.name)
    _drive_phase(arm, "warmup", arm.warmup, arm_run_id, result.warmup_records)
    result.window_s = _drive_phase(arm, "measured", arm.measured, arm_run_id, result.records)
    result.records.sort(key=lambda r: (r["worker"], r["i"]))

    counts = {}
    errors = 0
    for r in result.records:
        key = str(r["status"]) if r["status"] is not None else "transport-error"
        counts[key] = counts.get(key, 0) + 1
        if r["error"] is not None or r["status"] not in arm.expected_status:
            errors += 1
    result.status_counts = counts
    result.errors = errors
    result.latency_ms = reduce_latency(result.latencies_ms())
    ok_n = len(result.ok_records())
    result.throughput = (ok_n / result.window_s) if result.window_s > 0 else 0.0

    result.assertions = harness_assertions(result)
    if arm.assertions:
        result.assertions.extend(arm.assertions(result, ctx) or [])
    if arm.summarize:
        result.extra = arm.summarize(result, ctx) or {}
    if arm.teardown:
        arm.teardown(ctx)

    if verbose:
        lat = result.latency_ms
        print(
            "      p50=%.3f ms p90=%.3f p95=%.3f p99=%.3f max=%.3f  %.1f req/s  errors=%d"
            % (lat["p50"], lat["p90"], lat["p95"], lat["p99"], lat["max"], result.throughput, result.errors),
            flush=True,
        )
        for a in result.assertions:
            print("      [%s] %s: %s" % ("PASS" if a["passed"] else "FAIL", a["name"], a["detail"]), flush=True)
    return result


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _git_info(path):
    """git sha and working-tree state for a tree, tolerating a missing repo.

    `dirty` counts only TRACKED modifications; untracked files are counted
    separately. The distinction matters when the manifest is used to assert a
    reference tree was left untouched: an untracked editor dotfile is not a
    modification to the code the numbers were produced from.
    """
    info = {"path": path, "sha": None, "dirty": None, "untracked": None, "error": None}
    if not os.path.isdir(path):
        info["error"] = "not a directory"
        return info
    try:
        sha = subprocess.run(
            ["git", "-C", path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if sha.returncode != 0:
            info["error"] = " ".join((sha.stderr or "rev-parse failed").split())[:160]
            return info
        info["sha"] = sha.stdout.strip()
        st = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=20,
        )
        if st.returncode == 0:
            lines = [ln for ln in st.stdout.splitlines() if ln.strip()]
            info["untracked"] = sum(1 for ln in lines if ln.startswith("??"))
            info["dirty"] = any(not ln.startswith("??") for ln in lines)
    except (OSError, subprocess.SubprocessError) as exc:
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
    return info


def _cpu_model():
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        elif sys.platform.startswith("linux"):
            with open("/proc/cpuinfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return platform.processor() or platform.machine() or "unknown"


def _sha256_file(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def build_manifest(args, scenario_name, scenario_module, run_id, records_file, extra=None):
    manifest = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "unix": time.time(),
        "run_id": run_id,
        "scenario": scenario_name,
        "scenario_spec": getattr(scenario_module, "SPEC_ID", scenario_name),
        "scenario_description": getattr(scenario_module, "DESCRIPTION", ""),
        "argv": list(sys.argv),
        "command": " ".join(shlex.quote(a) for a in sys.argv),
        "target": args.target,
        "topology": args.topology,
        "warmup": args.warmup,
        "measured": args.measured,
        "concurrency": args.concurrency,
        "host": {
            "cpu": _cpu_model(),
            "logical_cores": os.cpu_count(),
            "os": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "hostname": platform.node(),
        },
        "git": {
            "filter_crate": _git_info(REPO_ROOT),
            "llm_d_sc_genesis": _git_info(GENESIS_ROOT),
        },
        "records_file": records_file,
        "prompts_file": args.prompts,
        "prompts_sha256": _sha256_file(args.prompts) if args.prompts else None,
        "params": dict(args.param or {}),
        "notes": list(getattr(scenario_module, "NOTES", [])),
    }
    leak_path = os.path.join(RESULTS_DIR, "leakage.json")
    if os.path.exists(leak_path):
        try:
            with open(leak_path, encoding="utf-8") as fh:
                leak = json.load(fh)
            manifest["leakage_check"] = {
                "clean": leak.get("clean"),
                "verbatim_overlaps": leak.get("verbatim_overlaps"),
                "near_duplicates": leak.get("near_duplicates"),
                "anchor_count": leak.get("anchor_count"),
                "comparisons": leak.get("comparisons"),
                "max_jaccard_observed": leak.get("max_jaccard_observed"),
                "max_containment_observed": leak.get("max_containment_observed"),
                "max_full_jaccard_observed": leak.get("max_full_jaccard_observed"),
                "max_full_containment_observed": leak.get("max_full_containment_observed"),
                "limits": leak.get("limits"),
            }
        except (OSError, ValueError):
            pass
    if extra:
        manifest.update(extra)
    return manifest


# ---------------------------------------------------------------------------
# Scenario loading
# ---------------------------------------------------------------------------


def available_scenarios():
    found = {}
    for path in sorted(glob.glob(os.path.join(SCENARIO_DIR, "*.py"))):
        base = os.path.basename(path)
        if base.startswith("_"):
            continue
        stem = base[:-3]
        found[stem] = path
        short = stem.split("_", 1)[0]
        found.setdefault(short, path)
    return found


def load_scenario(name):
    found = available_scenarios()
    if name not in found:
        raise SystemExit(
            "unknown scenario %r; available: %s"
            % (name, ", ".join(sorted(k for k in found if "_" in k)))
        )
    path = found[name]
    # Scenario modules import `harness` and `_common` by bare name so they read
    # like the flat directory they live in.
    for extra in (HERE, SCENARIO_DIR):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    spec = importlib.util.spec_from_file_location("bench_scenario_" + os.path.basename(path)[:-3], path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ScenarioConfig:
    """What a scenario module is handed to build its arms."""

    def __init__(self, args, run_id):
        self.args = args
        self.run_id = run_id
        self.target = args.target
        self.concurrency = args.concurrency
        self.warmup = args.warmup
        self.measured = args.measured
        self.prompts_file = args.prompts
        self.params = dict(args.param or {})
        self.bench_dir = HERE

    def param(self, key, default=None, cast=str):
        if key in self.params:
            return cast(self.params[key])
        return default

    def load_prompts(self):
        with open(self.prompts_file, encoding="utf-8") as fh:
            return json.load(fh)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


CAPACITY_RULES = """\
SPEC-BENCH §3 — capacity discipline (this is someone's shared homelab)

  * The two model endpoints are SINGLE-REPLICA llama.cpp servers. The large one
    is a 284 B IQ3_XXS quant occupying 104 GB.
  * B-5 concurrency is 1. Never more.
  * B-5 starts at 10 prompts per class; scale only if one pass is under ~10 min.
  * max_tokens stays capped low (~128). This measures ROUTING, not generation.
  * B-1/B-2/B-3/B-6 use stub upstreams. High-concurrency load never goes near
    the real backends.
  * If an endpoint errors or slows sharply: STOP and report. Do not retry-storm.

This scenario targets the real model endpoints. Re-run with --allow-homelab
once you have confirmed the above, and only then."""


def parse_kv(values):
    out = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit("--param expects key=value, got %r" % item)
        k, v = item.split("=", 1)
        out[k] = v
    return out



def _cross_arm_key_reuse_assertion(results, scenario_entries):
    """Assert no two miss-mode arms shared a measured cache key.

    Appends a pass/fail assertion to every miss-mode arm in the run, so the
    finding lands in the JSON next to the numbers it would have invalidated.
    """
    keys_by_arm = {}
    for res in results:
        if getattr(res.arm, "cache_mode", "n/a") != "miss":
            continue
        keys_by_arm[res.arm.name] = {
            r["meta"].get("prompt_key") for r in res.records if r.get("meta", {}).get("prompt_key")
        }

    for name, keys in keys_by_arm.items():
        overlaps = {
            other: len(keys & other_keys)
            for other, other_keys in keys_by_arm.items()
            if other != name and keys & other_keys
        }
        detail = (
            "measured keys are disjoint from every other miss-mode arm in this run"
            if not overlaps
            else "SHARES measured keys with %s -- those arms measured llm-d-sc CACHE HITS "
                 "while reporting cache misses" % ", ".join("%s (%d keys)" % kv for kv in sorted(overlaps.items()))
        )
        for entry in scenario_entries:
            if entry["name"] == name:
                entry["assertions"].append(assertion("no_cross_arm_key_reuse", not overlaps, detail))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--scenario", help="scenario module in bench/scenarios (e.g. b1, b4, smoke)")
    p.add_argument("--list", action="store_true", help="list available scenarios and exit")
    p.add_argument("--target", default="http://127.0.0.1:8080", help="base URL under test")
    p.add_argument("--topology", default="local-loopback",
                   help="topology label recorded in the manifest, e.g. local-loopback, in-cluster-job")
    p.add_argument("--warmup", type=int, default=50, help="warmup requests per arm, excluded from the window")
    p.add_argument("--measured", type=int, default=300, help="measured requests per arm")
    p.add_argument("--concurrency", default="1", help="comma-separated worker-pool sizes, e.g. 1,4,16")
    p.add_argument("--prompts", default=os.path.join(HERE, "prompts", "complexity-heldout.json"))
    p.add_argument("--param", action="append", help="scenario parameter key=value (repeatable)")
    p.add_argument("--out", help="output JSON path (default bench/results/<utc>-<scenario>.json)")
    p.add_argument("--dry-run", action="store_true", help="build and print the arm plan, send nothing")
    p.add_argument("--allow-homelab", action="store_true",
                   help="permit a scenario that targets the real model endpoints")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    if args.list:
        for stem, path in sorted(available_scenarios().items()):
            if "_" not in stem:
                continue
            module = load_scenario(stem)
            print("%-26s %-6s %s" % (stem, getattr(module, "SPEC_ID", "?"),
                                     getattr(module, "DESCRIPTION", "").split("\n")[0]))
        return 0

    if not args.scenario:
        p.error("--scenario is required (or --list)")

    args.concurrency = [int(x) for x in str(args.concurrency).split(",") if str(x).strip()]
    args.param = parse_kv(args.param)

    module = load_scenario(args.scenario)
    if getattr(module, "TARGETS_REAL_MODELS", False) and not args.allow_homelab:
        print(CAPACITY_RULES, file=sys.stderr)
        return 2

    run_id = "%s-%d" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), os.getpid())
    cfg = ScenarioConfig(args, run_id)
    ctx = {"config": cfg, "run_id": run_id, "results": {}, "module": module, "extra": {}}

    arms = module.arms(cfg)
    if getattr(module, "TARGETS_REAL_MODELS", False):
        for arm in arms:
            if arm.concurrency != 1:
                print("REFUSING: %s targets the real models at concurrency %d; SPEC-BENCH §3 "
                      "fixes B-5 concurrency at 1." % (arm.name, arm.concurrency), file=sys.stderr)
                return 2

    print("scenario %s (%s): %d arm(s)"
          % (args.scenario, getattr(module, "SPEC_ID", "?"), len(arms)), flush=True)

    if args.dry_run:
        for arm in arms:
            print(json.dumps(
                {
                    "arm": arm.name, "target": arm.target, "concurrency": arm.concurrency,
                    "warmup": arm.warmup, "measured": arm.measured, "cache_mode": arm.cache_mode,
                    "expected_status": list(arm.expected_status), "params": arm.params,
                    "notes": arm.notes,
                }, indent=2))
        print("dry run: no requests were sent")
        return 0

    os.makedirs(RESULTS_DIR, exist_ok=True)
    out_path = args.out or os.path.join(RESULTS_DIR, "%s-%s.json" % (run_id, args.scenario))
    records_path = out_path[:-5] + ".records.jsonl" if out_path.endswith(".json") else out_path + ".records.jsonl"

    results = []
    try:
        with open(records_path, "w", encoding="utf-8") as rec_fh:
            for arm in arms:
                res = run_arm(arm, run_id, ctx, verbose=not args.quiet)
                ctx["results"][arm.name] = res
                results.append(res)
                for r in res.warmup_records + res.records:
                    rec_fh.write(json.dumps(r) + "\n")
    finally:
        if hasattr(module, "finalize"):
            try:
                module.finalize(ctx)
            except Exception as exc:  # noqa: BLE001
                print("scenario finalize failed: %s" % exc, file=sys.stderr)

    scenario_entries = [r.to_json() for r in results]

    # Run-level invariant, applied to EVERY scenario without being asked.
    #
    # A miss-mode arm's premise is that each measured request pays a real model
    # forward. That premise is broken not only by repeating a key inside an arm
    # (which the per-arm check already covers) but by a SECOND arm re-sending the
    # first arm's prompts, which llm-d-sc then serves from its versioned result
    # cache. The second arm still looks internally consistent -- unique keys,
    # 200s, status OK -- while measuring something entirely different from what
    # it reports. That is precisely how B-3's concurrency-4 rows came to read
    # 0.386 ms against a c=1 truth of 13.9 ms.
    #
    # Arms are now key-namespaced by name, so this should never fire. It is kept
    # because the failure mode is silent, plausible, and publishable.
    _cross_arm_key_reuse_assertion(results, scenario_entries)

    if hasattr(module, "cross_arm_assertions"):
        for entry, extra_asserts in module.cross_arm_assertions(ctx) or []:
            for e in scenario_entries:
                if e["name"] == entry:
                    e["assertions"].extend(extra_asserts)

    manifest = build_manifest(args, args.scenario, module, run_id, os.path.abspath(records_path),
                              extra=ctx.get("extra") or None)
    doc = {"manifest": manifest, "scenarios": scenario_entries}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")

    failed = [
        (e["name"], a["name"], a["detail"])
        for e in scenario_entries
        for a in e["assertions"]
        if not a["passed"]
    ]
    total_asserts = sum(len(e["assertions"]) for e in scenario_entries)
    print("\nwrote %s" % out_path)
    print("wrote %s (%d per-request records)"
          % (records_path, sum(len(r.records) + len(r.warmup_records) for r in results)))
    print("assertions: %d passed, %d FAILED" % (total_asserts - len(failed), len(failed)))
    if failed:
        for arm_name, a_name, detail in failed:
            print("  FAIL %s / %s: %s" % (arm_name, a_name, detail), file=sys.stderr)
        print(
            "\nA failed self-assertion means the scenario could not verify its own premise.\n"
            "SPEC-BENCH §0 rule 3: that is a bug, not a result. The JSON is kept so the\n"
            "failure is inspectable, but nothing in it may be published as a measurement.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
