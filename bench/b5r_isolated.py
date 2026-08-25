#!/usr/bin/env python3
"""B-5R -- controlled end-to-end latency, with GROUND-TRUTH per-request isolation.

Replaces B-5's arm-level contamination check with per-request attribution.
The prior salvage (`analyze_b5_contamination.py`) INFERS contention from
tokens/sec clustering -- useful, disclosed as a heuristic, but not proof.
This script PROVES isolation per request, the way it should have been done
the first time:

    idle gate (2 consecutive idle polls)
        |
    snapshot backend counters                     <- T0
        |
    send ONE request, capture response usage
        |
    snapshot backend counters                      <- T1
        |
    reconcile: does (T1 - T0) match what OUR response reported?

If the backend's own token counters moved by more than our request's usage
(plus a small tolerance for counter timing), someone else's generation
landed inside our measurement window -- proven, not inferred. That request
goes to `shared_load`, not discarded, and the harness keeps sampling until
it has `--target` ISOLATED observations or exhausts `--max-attempts`.

Two arms this run considers "always-large" and "always-small" go straight to
Praxis's static-response listeners (:8082/:8083 on the driver pod), matching
the topology of the original B-5 exactly, so this supersedes it rather than
measuring something new. "classified" goes through the real deployed Praxis
listener (llm_d_sc-enabled).

Populations are NEVER mixed for percentiles. A p99 is only ever reported for
a population with enough n to make one meaningful; n<30 gets p50/p90/range.

Usage:
    python3 b5r_isolated.py --arm always-large  --target 40 --max-attempts 200
    python3 b5r_isolated.py --arm always-small  --target 40 --max-attempts 200
    python3 b5r_isolated.py --arm classified    --target 40 --max-attempts 200
    python3 b5r_isolated.py --report   # summarize whatever has been collected so far
"""
import argparse
import base64
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios"))
import _llamacpp_metrics as lm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
CHECKPOINT = os.path.join(RESULTS, "b5r-isolated.jsonl")
PROMPTS_PATH = os.path.join(HERE, "prompts", "complexity-heldout.json")

DRIVER_HOST = "praxis-bench-praxis-poc.apps.ironman.cjlabs.dev"  # if a route exists; else in-cluster exec is used
PRAXIS_ROUTE = "praxis-praxis-poc.apps.ironman.cjlabs.dev"

QWEN38_METRICS = "https://llama-server-qwen38-homelab-maas.apps.ironman.cjlabs.dev/metrics"
DS4_METRICS = "https://llama-server-ds4-homelab-maas.apps.ironman.cjlabs.dev/metrics"

MAX_TOKENS = 128
TOLERANCE_TOKENS = 2  # small slack for counter-sampling timing, not a loophole

SSL_CTX = ssl._create_unverified_context()


def ds4_token():
    out = subprocess.run(
        ["oc", "get", "secret", "laguna-api-key", "-n", "homelab-maas", "-o", "jsonpath={.data.key}"],
        capture_output=True, text=True, check=True,
    ).stdout
    return base64.b64decode(out).decode().strip()


ARM_CONFIG = {
    # (request_url_via_pod_exec, response-model-should-be, uses_ds4_auth, metrics_url, metrics_bearer)
    "always-large": {"backend_metrics": DS4_METRICS, "bearer": True, "expect_model": "ds4-flash-0731"},
    "always-small": {"backend_metrics": QWEN38_METRICS, "bearer": False, "expect_model": "qwen38-27b"},
    # "classified" can land on either backend per-request; both are snapshotted
    # and the reconciliation is checked against WHICHEVER one it actually used.
    "classified": {"backend_metrics": None, "bearer": None, "expect_model": None},
}


def load_prompts():
    sha_path = os.path.join(HERE, "prompts", "complexity-heldout.sha256")
    expected = open(sha_path, encoding="utf-8").read().split()[0]
    import hashlib
    actual = hashlib.sha256(open(PROMPTS_PATH, "rb").read()).hexdigest()
    if actual != expected:
        sys.exit("FROZEN DATASET HASH MISMATCH -- refusing to run")
    return json.load(open(PROMPTS_PATH, encoding="utf-8"))


def exec_pod_request(port, body, timeout=200):
    """POST through the driver pod's own network namespace (matches B-5's topology)."""
    payload = json.dumps(body).replace("'", "'\\''")
    cmd = [
        "oc", "exec", "praxis-bench", "-n", "praxis-poc", "--",
        "sh", "-c",
        "wget -q -T%d -O- --header='Content-Type: application/json' --post-data='%s' "
        "http://praxis-bench.praxis-poc.svc.cluster.local:%d/v1/chat/completions"
        % (timeout, payload, port),
    ]
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30)
        wall_s = time.time() - started
        if proc.returncode != 0 or not proc.stdout.strip():
            return None, wall_s, proc.stderr[:300]
        return json.loads(proc.stdout), wall_s, None
    except Exception as e:  # noqa: BLE001
        return None, time.time() - started, str(e)[:300]


def exec_praxis_route_request(body, timeout=200):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        "https://%s/v1/chat/completions" % PRAXIS_ROUTE, data=data, method="POST",
    )
    req.add_header("Content-Type", "application/json")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return json.loads(resp.read()), time.time() - started, None
    except Exception as e:  # noqa: BLE001
        return None, time.time() - started, str(e)[:300]


def metrics_delta(before, after):
    return {
        "prompt_tokens_delta": lm.delta(before, after, "llamacpp:prompt_tokens_total"),
        "generated_tokens_delta": lm.delta(before, after, "llamacpp:tokens_predicted_total"),
    }


def reconcile(delta, usage):
    """True if the backend's own counters moved by ~exactly what OUR request used.

    llama.cpp's `prompt_tokens_total` counts only NEWLY-EVALUATED prompt
    tokens -- it does not double-count a cache hit. The OpenAI-compatible
    `usage.prompt_tokens` field reports the FULL prompt length regardless of
    how much of it was cached. These are different quantities on purpose, and
    comparing prompt_tokens_delta against raw usage.prompt_tokens is wrong
    whenever prompt_tokens_details.cached_tokens > 0 -- which, with a fixed
    chat-template prefix repeated across requests, is most of the time. The
    correct comparison is against usage.prompt_tokens MINUS cached_tokens.
    (Caught live: every always-small request showed prompt_tokens_delta=18
    against usage.prompt_tokens=60 with cached_tokens=42 -- 60-42=18, an
    exact match that the naive comparison was misreading as contamination.)
    """
    if usage is None or delta["generated_tokens_delta"] is None:
        return False, "missing usage or metrics"
    gen_ok = abs(delta["generated_tokens_delta"] - usage.get("completion_tokens", 0)) <= TOLERANCE_TOKENS
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    expected_new_prompt_tokens = usage.get("prompt_tokens", 0) - cached
    prompt_ok = (delta["prompt_tokens_delta"] is None or
                abs(delta["prompt_tokens_delta"] - expected_new_prompt_tokens) <= max(TOLERANCE_TOKENS, 4))
    if gen_ok and prompt_ok:
        return True, "backend counters match our own usage within tolerance"
    return False, ("backend generated %s tokens / processed %s new prompt tokens during our request; "
                   "we asked for %s completion tokens with %s new (uncached) prompt tokens -- "
                   "someone else's generation landed in this window"
                   % (delta["generated_tokens_delta"], delta["prompt_tokens_delta"],
                      usage.get("completion_tokens"), expected_new_prompt_tokens))


def one_attempt(arm, prompt):
    body = {"model": "bench-router", "max_tokens": MAX_TOKENS, "temperature": 0, "stream": False,
            "messages": [{"role": "user", "content": prompt["prompt"]}]}

    if arm == "always-large":
        idle = lm.wait_until_idle(DS4_METRICS, bearer_env="DS4_API_KEY_LOCAL", consecutive=2, max_wait_s=60, poll_s=2)
        before = lm.snapshot(DS4_METRICS, bearer_env="DS4_API_KEY_LOCAL")
        resp, wall_s, err = exec_pod_request(8082, body)
        after = lm.snapshot(DS4_METRICS, bearer_env="DS4_API_KEY_LOCAL")
    elif arm == "always-small":
        idle = lm.wait_until_idle(QWEN38_METRICS, consecutive=2, max_wait_s=60, poll_s=2)
        before = lm.snapshot(QWEN38_METRICS)
        resp, wall_s, err = exec_pod_request(8083, body)
        after = lm.snapshot(QWEN38_METRICS)
    else:  # classified -- snapshot BOTH backends since we don't know which will be hit
        idle_q = lm.wait_until_idle(QWEN38_METRICS, consecutive=2, max_wait_s=60, poll_s=2)
        idle_d = lm.wait_until_idle(DS4_METRICS, bearer_env="DS4_API_KEY_LOCAL", consecutive=2, max_wait_s=60, poll_s=2)
        idle = {"qwen38": idle_q, "ds4": idle_d}
        before = {"qwen38": lm.snapshot(QWEN38_METRICS), "ds4": lm.snapshot(DS4_METRICS, bearer_env="DS4_API_KEY_LOCAL")}
        resp, wall_s, err = exec_praxis_route_request(body)
        after = {"qwen38": lm.snapshot(QWEN38_METRICS), "ds4": lm.snapshot(DS4_METRICS, bearer_env="DS4_API_KEY_LOCAL")}

    usage = (resp or {}).get("usage")
    served_model = (resp or {}).get("model")

    if arm == "classified":
        which = "ds4" if served_model == "ds4-flash-0731" else "qwen38"
        delta = metrics_delta(before[which], after[which])
        isolated, reason = reconcile(delta, usage) if resp else (False, "request failed: %s" % err)
        gated = idle["ds4" if which == "ds4" else "qwen38"].get("gated")
    else:
        delta = metrics_delta(before, after)
        isolated, reason = reconcile(delta, usage) if resp else (False, "request failed: %s" % err)
        gated = idle.get("gated")

    return {
        "arm": arm, "prompt_id": prompt["id"], "intended_label": prompt["label"],
        "unix": time.time(), "wall_s": round(wall_s, 3), "served_model": served_model,
        "usage": usage, "idle_gate": gated, "metrics_delta": delta,
        "isolated": bool(isolated and resp is not None), "reason": reason, "error": err,
    }


def read_checkpoint():
    if not os.path.exists(CHECKPOINT):
        return []
    return [json.loads(l) for l in open(CHECKPOINT, encoding="utf-8") if l.strip()]


def append_checkpoint(rec):
    with open(CHECKPOINT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * q)))
    return sorted_vals[idx]


def run_arm(arm, target, max_attempts):
    os.environ.setdefault("DS4_API_KEY_LOCAL", ds4_token())
    prompts = load_prompts()
    existing = [r for r in read_checkpoint() if r["arm"] == arm]
    isolated_so_far = sum(1 for r in existing if r["isolated"])
    print("arm=%s: %d isolated already collected (target %d)" % (arm, isolated_so_far, target))

    attempt = len(existing)
    idx = attempt % len(prompts)
    while isolated_so_far < target and attempt < max_attempts:
        prompt = prompts[idx % len(prompts)]
        idx += 1
        rec = one_attempt(arm, prompt)
        rec["attempt"] = attempt
        append_checkpoint(rec)
        attempt += 1
        if rec["isolated"]:
            isolated_so_far += 1
            print("  attempt %3d: ISOLATED   (%d/%d)  wall=%.1fs  %s"
                  % (attempt, isolated_so_far, target, rec["wall_s"], prompt["id"]))
        else:
            print("  attempt %3d: shared_load  wall=%.1fs  reason=%s"
                  % (attempt, rec["wall_s"], rec["reason"]))

    if isolated_so_far < target:
        print("\nINCONCLUSIVE: only %d/%d isolated observations after %d attempts."
              % (isolated_so_far, target, attempt))
        print("Reporting shared-load data separately; NOT publishing this as a controlled result.")
    else:
        print("\nOK: %d isolated observations collected for arm=%s" % (isolated_so_far, arm))


def cmd_report():
    all_recs = read_checkpoint()
    print("%-14s %8s %8s | %-30s" % ("arm", "isolated", "shared", "isolated latency (ms)"))
    print("-" * 90)
    for arm in ("always-large", "always-small", "classified"):
        recs = [r for r in all_recs if r["arm"] == arm]
        iso = [r for r in recs if r["isolated"]]
        shared = [r for r in recs if not r["isolated"]]
        vals = sorted(r["wall_s"] * 1000 for r in iso)
        if len(vals) >= 30:
            stat = "n=%d p50=%.0f p90=%.0f p99=%.0f" % (
                len(vals), percentile(vals, .5), percentile(vals, .9), percentile(vals, .99))
        elif vals:
            stat = "n=%d p50=%.0f p90=%.0f range=[%.0f,%.0f] (n<30: no p99)" % (
                len(vals), percentile(vals, .5), percentile(vals, .9), vals[0], vals[-1])
        else:
            stat = "no isolated observations yet"
        print("%-14s %8d %8d | %s" % (arm, len(iso), len(shared), stat))
    with open(os.path.join(RESULTS, "b5r-summary.json"), "w", encoding="utf-8") as fh:
        json.dump({
            arm: {
                "isolated_n": len([r for r in all_recs if r["arm"] == arm and r["isolated"]]),
                "shared_load_n": len([r for r in all_recs if r["arm"] == arm and not r["isolated"]]),
                "isolated_latency_ms": {
                    "p50": percentile(sorted(r["wall_s"] * 1000 for r in all_recs if r["arm"] == arm and r["isolated"]), .5),
                    "p90": percentile(sorted(r["wall_s"] * 1000 for r in all_recs if r["arm"] == arm and r["isolated"]), .9),
                },
                "ground_truth_isolation": True,
                "method": "per-request llama.cpp metrics snapshot (T0/T1) reconciled against response usage",
            }
            for arm in ("always-large", "always-small", "classified")
        }, fh, indent=2, sort_keys=True)
    print("\nwrote results/b5r-summary.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=list(ARM_CONFIG.keys()))
    ap.add_argument("--target", type=int, default=40)
    ap.add_argument("--max-attempts", type=int, default=200)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    os.makedirs(RESULTS, exist_ok=True)
    if args.report:
        cmd_report()
        return
    if not args.arm:
        sys.exit("--arm is required unless --report")
    run_arm(args.arm, args.target, args.max_attempts)


if __name__ == "__main__":
    main()
