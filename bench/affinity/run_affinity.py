#!/usr/bin/env python3
"""Model-affinity ground truth: does the cheap model actually succeed?

Per SPEC-AFFINITY.md. Replaces the human complexity-tier label with OBSERVED
model outcomes as the routing target: ask both models every frozen prompt,
judge both answers blind, and define "cheapest sufficient model" from
`meets_bar`, not from anyone's opinion about what "COMPLEX" means.

Three stages, each independently resumable from its own checkpoint file:

  1. generate  -- both backends, every prompt, one in-flight request per
                  backend (two backends may run concurrently; never two
                  requests to the SAME backend at once)
  2. judge     -- ds4 as primary judge (blind, order-randomised), plus the
                  two mandatory controls: a swapped-order re-judge and an
                  inter-judge (qwen38) re-judge, both on the same subsample
  3. matrix    -- join generation + judging + the classifier's own output
                  into the per-prompt matrix and the summary metrics

Python 3 stdlib only. Safe to Ctrl-C and resume: every stage skips prompts
already present in its checkpoint.

Usage:
    python3 run_affinity.py generate  [--pilot N] [--limit N]
    python3 run_affinity.py judge     [--pilot N] [--limit N]
    python3 run_affinity.py matrix
    python3 run_affinity.py all       [--pilot N]
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import random
import re
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PROMPTS_PATH = os.path.join(REPO, "prompts", "complexity-heldout.json")
SHA_PATH = os.path.join(REPO, "prompts", "complexity-heldout.sha256")
RESULTS = os.path.join(REPO, "results")

GEN_CHECKPOINT = os.path.join(RESULTS, "affinity-generation.jsonl")
JUDGE_CHECKPOINT = os.path.join(RESULTS, "affinity-judging.jsonl")
JUDGE_CONTROL_CHECKPOINT = os.path.join(RESULTS, "affinity-judge-controls.jsonl")
MATRIX_PATH = os.path.join(RESULTS, "affinity-matrix.jsonl")
SUMMARY_PATH = os.path.join(RESULTS, "affinity-summary.json")

GENESIS = os.path.expanduser("~/llm-d-sc-genesis")
CLASSIFY_CLI = os.path.join(GENESIS, "target/release/llm-d-sc-classify")
MODEL_DIR = os.path.join(GENESIS, "artifacts/models/complexity")

SMALL_URL = "https://llama-server-qwen38-homelab-maas.apps.ironman.cjlabs.dev/v1/chat/completions"
LARGE_URL = "https://llama-server-ds4-homelab-maas.apps.ironman.cjlabs.dev/v1/chat/completions"
SMALL_MODEL = "qwen38-27b"
LARGE_MODEL = "ds4-flash-0731"

MAX_TOKENS = 512
TEMPERATURE = 0
JUDGE_MAX_TOKENS = 400
JUDGE_SUBSAMPLE_N = 32

# Never printed, never written to any output file.
_DS4_TOKEN = None
_TOKEN_LOCK = threading.Lock()

SSL_CTX = ssl._create_unverified_context()


def redact(s):
    return re.sub(r"Bearer [A-Za-z0-9._~+/=-]{8,}", "Bearer <REDACTED>", str(s))


def ds4_token():
    global _DS4_TOKEN
    with _TOKEN_LOCK:
        if _DS4_TOKEN is None:
            out = subprocess.run(
                ["oc", "get", "secret", "laguna-api-key", "-n", "homelab-maas",
                 "-o", "jsonpath={.data.key}"],
                capture_output=True, text=True, check=True,
            ).stdout
            import base64
            _DS4_TOKEN = base64.b64decode(out).decode().strip()
        return _DS4_TOKEN


def load_prompts():
    with open(SHA_PATH, encoding="utf-8") as fh:
        expected = fh.read().split()[0]
    actual = hashlib.sha256(open(PROMPTS_PATH, "rb").read()).hexdigest()
    if actual != expected:
        sys.exit("FROZEN DATASET HASH MISMATCH: expected %s got %s -- refusing to run "
                  "against a modified holdout" % (expected, actual))
    return json.load(open(PROMPTS_PATH, encoding="utf-8"))


def read_checkpoint(path):
    if not os.path.exists(path):
        return {}
    rows = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rows[rec["_key"]] = rec
    return rows


def append_checkpoint(path, rec, lock):
    with lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")


def http_post_json(url, payload, timeout, bearer=None):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if bearer:
        req.add_header("Authorization", "Bearer %s" % bearer)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            data = json.loads(resp.read())
            return data, time.time() - started, resp.status, None
    except urllib.error.HTTPError as e:
        return None, time.time() - started, e.code, redact(e.read()[:500].decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001 -- network calls, report and move on
        return None, time.time() - started, 0, redact(str(e))


# ---------------------------------------------------------------------------
# Stage 1: generation
# ---------------------------------------------------------------------------

def chat_payload(prompt_text, model):
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "stream": False,
        "messages": [{"role": "user", "content": prompt_text}],
    }


def generate_one(prompt, backend):
    """One generation call. `wall_s` is retained for audit only -- this
    experiment answers a QUALITY question (does the response meet the bar),
    not a performance one, and a shared homelab backend means wall_s here is
    NOT a controlled latency measurement. Never derive a percentile or a
    latency claim from generation records; that is what B-5 / B-5R measure,
    with an idle gate and (for B-5R) per-request ground-truth reconciliation
    against the backend's own token counters. A slow generation here still
    produces a complete response that gets judged like any other -- quality
    and performance are deliberately decoupled, per SPEC-AFFINITY.
    """
    url = LARGE_URL if backend == "large" else SMALL_URL
    model = LARGE_MODEL if backend == "large" else SMALL_MODEL
    bearer = ds4_token() if backend == "large" else None
    data, wall_s, status, err = http_post_json(url, chat_payload(prompt["prompt"], model), timeout=180, bearer=bearer)
    rec = {
        "_key": "%s:%s" % (prompt["id"], backend),
        "prompt_id": prompt["id"],
        "backend": backend,
        "wall_s": round(wall_s, 3),  # audit-only; see docstring above
        "status": status,
        "error": err,
    }
    if data:
        choice = (data.get("choices") or [{}])[0]
        rec["response_text"] = (choice.get("message") or {}).get("content", "")
        rec["reasoning_text"] = (choice.get("message") or {}).get("reasoning_content", "")
        rec["completion_tokens"] = (data.get("usage") or {}).get("completion_tokens")
        rec["prompt_tokens"] = (data.get("usage") or {}).get("prompt_tokens")
        rec["served_model"] = data.get("model")
    return rec


def cmd_generate(args):
    prompts = load_prompts()
    if args.pilot:
        prompts = _pilot_slice(prompts, args.pilot)
    if args.limit:
        prompts = prompts[: args.limit]

    done = read_checkpoint(GEN_CHECKPOINT)
    lock = threading.Lock()
    todo_small = [p for p in prompts if "%s:small" % p["id"] not in done]
    todo_large = [p for p in prompts if "%s:large" % p["id"] not in done]
    print("generate: %d/%d small pending, %d/%d large pending"
          % (len(todo_small), len(prompts), len(todo_large), len(prompts)))

    def run_backend(queue, backend):
        for i, p in enumerate(queue):
            rec = generate_one(p, backend)
            append_checkpoint(GEN_CHECKPOINT, rec, lock)
            status = rec["status"]
            print("  [%s %d/%d] %-14s status=%s wall=%.1fs tokens=%s"
                  % (backend, i + 1, len(queue), p["id"], status, rec["wall_s"],
                     rec.get("completion_tokens")))
            if status not in (200,):
                print("  WARNING: non-200 from %s -- if this repeats, STOP." % backend, file=sys.stderr)

    # One in-flight request per backend, both backends concurrently.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futures = [
            ex.submit(run_backend, todo_small, "small"),
            ex.submit(run_backend, todo_large, "large"),
        ]
        for f in futures:
            f.result()

    print("generation complete: checkpoint at %s" % GEN_CHECKPOINT)


def _pilot_slice(prompts, n):
    by_tier = {}
    for p in prompts:
        by_tier.setdefault(p["label"], []).append(p)
    out = []
    for tier, items in by_tier.items():
        out.extend(items[: max(1, n // 4)])
    return out


# ---------------------------------------------------------------------------
# Stage 2: judging
# ---------------------------------------------------------------------------

JUDGE_INSTRUCTIONS = """You are a strict, neutral evaluator comparing two AI assistant \
responses to the same user request. You do not know which model produced which response.

User request:
{prompt}

Response 1:
{resp1}

Response 2:
{resp2}

For EACH response, decide: does it adequately and CORRECTLY answer the request, such \
that a user would not need to re-ask a different, stronger model? This is the \
"meets_bar" question. A response can meet the bar while being less polished than the \
other; "the other one is nicer" is not the same as "this one failed."

Also decide overall preference (verdict), and whether the prompt has a single, \
objectively checkable correct answer (verifiable) -- e.g. arithmetic, a specific fact -- \
as opposed to open-ended writing or design.

Respond with ONLY this JSON object, no other text:
{{"response_1": {{"score": <1-5>, "meets_bar": <true|false>, "why": "<20 words>"}}, \
"response_2": {{"score": <1-5>, "meets_bar": <true|false>, "why": "<20 words>"}}, \
"verdict": "<1|2|tie>", "verifiable": <true|false>}}"""


def judge_payload(prompt_text, resp1, resp2, model):
    text = JUDGE_INSTRUCTIONS.format(prompt=prompt_text, resp1=resp1[:3000], resp2=resp2[:3000])
    return {
        "model": model,
        "max_tokens": JUDGE_MAX_TOKENS,
        "temperature": 0,
        "stream": False,
        "messages": [{"role": "user", "content": text}],
    }


def extract_json(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def run_judge(url, model, bearer, prompt_text, resp1, resp2, retries=2):
    for attempt in range(retries + 1):
        data, wall_s, status, err = http_post_json(
            url, judge_payload(prompt_text, resp1, resp2, model), timeout=180, bearer=bearer,
        )
        if data:
            choice = (data.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content", "")
            parsed = extract_json(content)
            if parsed and "verdict" in parsed:
                parsed["_wall_s"] = round(wall_s, 3)
                parsed["_raw_status"] = status
                return parsed
        if attempt < retries:
            continue
    return {"judge_failed": True, "_raw_status": status, "_error": err}


def cmd_judge(args):
    all_prompts = load_prompts()
    if args.pilot:
        all_prompts = _pilot_slice(all_prompts, args.pilot)
    if args.limit:
        all_prompts = all_prompts[: args.limit]
    prompts = {p["id"]: p for p in all_prompts}

    gen = read_checkpoint(GEN_CHECKPOINT)
    done = read_checkpoint(JUDGE_CHECKPOINT)
    lock = threading.Lock()

    pending = []
    for pid in prompts:
        if pid in done:
            continue
        small = gen.get("%s:small" % pid)
        large = gen.get("%s:large" % pid)
        if not small or not large or small.get("status") != 200 or large.get("status") != 200:
            print("  skip %s: generation incomplete or failed" % pid, file=sys.stderr)
            continue
        pending.append((prompts[pid], small, large))

    print("judge: %d/%d prompts pending" % (len(pending), len(prompts)))

    for i, (p, small, large) in enumerate(pending):
        seed = int(hashlib.sha256(p["id"].encode()).hexdigest(), 16)
        rnd = random.Random(seed)
        order_swapped = rnd.random() < 0.5
        resp1 = large["response_text"] if order_swapped else small["response_text"]
        resp2 = small["response_text"] if order_swapped else large["response_text"]
        # response_1/response_2 map to (large,small) if swapped else (small,large)
        verdict = run_judge(LARGE_URL, LARGE_MODEL, ds4_token(), p["prompt"], resp1, resp2)
        rec = {
            "_key": p["id"],
            "prompt_id": p["id"],
            "order_swapped": order_swapped,
            "order_seed": seed % (2**32),
            "judge": "large",
            **verdict,
        }
        append_checkpoint(JUDGE_CHECKPOINT, rec, lock)
        print("  [judge %d/%d] %-14s verdict=%s failed=%s"
              % (i + 1, len(pending), p["id"], rec.get("verdict"), rec.get("judge_failed", False)))

    # --- controls: subsample re-judged with swapped order + qwen38 -----
    control_done = read_checkpoint(JUDGE_CONTROL_CHECKPOINT)
    subsample_ids = sorted(prompts.keys())[:JUDGE_SUBSAMPLE_N]
    print("\ncontrols: re-judging %d prompts (position-bias + inter-judge)" % len(subsample_ids))
    for pid in subsample_ids:
        if pid in gen and "%s:small" % pid not in gen:
            continue
        small = gen.get("%s:small" % pid)
        large = gen.get("%s:large" % pid)
        if not small or not large or small.get("status") != 200 or large.get("status") != 200:
            continue
        p = prompts.get(pid)
        if not p:
            continue
        primary = done.get(pid) or next((r for r in _reread(JUDGE_CHECKPOINT) if r["_key"] == pid), None)

        # Position-bias control: same judge (large), order flipped from primary.
        key_pos = "%s:position" % pid
        if key_pos not in control_done:
            was_swapped = primary["order_swapped"] if primary else False
            resp1 = small["response_text"] if was_swapped else large["response_text"]
            resp2 = large["response_text"] if was_swapped else small["response_text"]
            v = run_judge(LARGE_URL, LARGE_MODEL, ds4_token(), p["prompt"], resp1, resp2)
            append_checkpoint(JUDGE_CONTROL_CHECKPOINT,
                              {"_key": key_pos, "prompt_id": pid, "control": "position_swap", **v}, lock)
            print("  [position-control] %-14s verdict=%s" % (pid, v.get("verdict")))

        # Inter-judge control: qwen38 judges the SAME order as the primary verdict.
        key_inter = "%s:interjudge" % pid
        if key_inter not in control_done:
            was_swapped = primary["order_swapped"] if primary else False
            resp1 = large["response_text"] if was_swapped else small["response_text"]
            resp2 = small["response_text"] if was_swapped else large["response_text"]
            v = run_judge(SMALL_URL, SMALL_MODEL, None, p["prompt"], resp1, resp2)
            append_checkpoint(JUDGE_CONTROL_CHECKPOINT,
                              {"_key": key_inter, "prompt_id": pid, "control": "inter_judge", **v}, lock)
            print("  [inter-judge]      %-14s verdict=%s" % (pid, v.get("verdict")))

    print("judging complete.")


def _reread(path):
    return list(read_checkpoint(path).values())


# ---------------------------------------------------------------------------
# Stage 3: matrix + summary
# ---------------------------------------------------------------------------

def classifier_output(prompts):
    """predicted_tier/score/margin per prompt, via the CLI (cheap, local, no homelab)."""
    payload = "\n".join(p["prompt"].replace("\n", " ") for p in prompts) + "\n"
    proc = subprocess.run(
        [CLASSIFY_CLI, "--model", MODEL_DIR, "--json"],
        input=payload, capture_output=True, text=True, check=True,
    )
    out = {}
    for line, p in zip(proc.stdout.splitlines(), prompts):
        rec = json.loads(line)
        ranked = sorted(rec["ranked"], key=lambda r: -r["score"])
        out[p["id"]] = {
            "predicted_tier": ranked[0]["label"],
            "score": ranked[0]["score"],
            "margin": ranked[0]["score"] - ranked[1]["score"] if len(ranked) > 1 else None,
        }
    return out


ROUTES = {"SIMPLE": "small", "MEDIUM": "small", "COMPLEX": "large", "REASONING": "large"}


def cmd_matrix(args):
    prompts = load_prompts()
    gen = read_checkpoint(GEN_CHECKPOINT)
    judged = read_checkpoint(JUDGE_CHECKPOINT)
    cls = classifier_output(prompts)

    rows = []
    for p in prompts:
        pid = p["id"]
        small, large = gen.get("%s:small" % pid), gen.get("%s:large" % pid)
        j = judged.get(pid)
        if not small or not large or not j or j.get("judge_failed"):
            continue
        swapped = j["order_swapped"]
        # response_1 = large if swapped else small; response_2 = the other.
        r1, r2 = ("response_1", "response_2")
        large_key, small_key = (r1, r2) if swapped else (r2, r1)
        large_j, small_j = j.get(large_key, {}), j.get(small_key, {})

        c = cls.get(pid, {})
        predicted = c.get("predicted_tier")
        classifier_route = ROUTES.get(predicted)

        small_ok = bool(small_j.get("meets_bar"))
        large_ok = bool(large_j.get("meets_bar"))
        if small_ok:
            cheapest = "small"
        elif large_ok:
            cheapest = "large"
        else:
            cheapest = "neither"

        rows.append({
            "prompt_id": pid, "prompt": p["prompt"], "domain": p.get("domain"),
            "boundary": p.get("boundary", False),
            "human_tier": p["label"],
            "predicted_tier": predicted, "score": c.get("score"), "margin": c.get("margin"),
            "classifier_route": classifier_route,
            "small_score": small_j.get("score"), "small_meets_bar": small_ok,
            "large_score": large_j.get("score"), "large_meets_bar": large_ok,
            "verdict": j.get("verdict"), "verifiable": j.get("verifiable"),
            "order_seed": j.get("order_seed"),
            "cheapest_sufficient": cheapest,
            "small_wall_s": small.get("wall_s"), "large_wall_s": large.get("wall_s"),
            "small_completion_tokens": small.get("completion_tokens"),
            "large_completion_tokens": large.get("completion_tokens"),
        })

    with open(MATRIX_PATH, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print("wrote %s (%d rows)" % (MATRIX_PATH, len(rows)))

    summary = _summarize(rows)
    controls = _summarize_controls()
    summary["controls"] = controls
    with open(SUMMARY_PATH, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
    print("wrote %s" % SUMMARY_PATH)
    _print_summary(summary)


def _summarize(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0}

    exact_tier_ok = sum(r["predicted_tier"] == r["human_tier"] for r in rows)
    human_route_ok = sum(r["classifier_route"] == ROUTES.get(r["human_tier"]) for r in rows)
    model_select_ok = sum(r["classifier_route"] == r["cheapest_sufficient"] for r in rows
                          if r["cheapest_sufficient"] != "neither")
    denom_ms = sum(1 for r in rows if r["cheapest_sufficient"] != "neither")

    under = sum(1 for r in rows if r["classifier_route"] == "small"
               and not r["small_meets_bar"] and r["large_meets_bar"])
    over = sum(1 for r in rows if r["classifier_route"] == "large" and r["small_meets_bar"])
    neither = sum(1 for r in rows if r["cheapest_sufficient"] == "neither")

    oracle_small = sum(1 for r in rows if r["cheapest_sufficient"] == "small")
    realized_small = sum(1 for r in rows if r["classifier_route"] == "small" and r["small_meets_bar"])

    out = {
        "n": n,
        "exact_four_tier_accuracy": exact_tier_ok / n,
        "human_tier_route_agreement": human_route_ok / n,
        "actual_model_selection_accuracy": (model_select_ok / denom_ms) if denom_ms else None,
        "under_routing_quality_risk": under / n,
        "over_routing_wasted_capacity": over / n,
        "neither_model_suffices": neither / n,
        "achievable_oracle_small_share": oracle_small / n,
        "realized_classifier_small_share": realized_small / n,
    }

    # Cost function L = lambda_q * P(under) + lambda_c * P(over), scored against
    # the deployed mapping AND two alternatives, since the classifier output is
    # fixed and only the downstream decision rule varies -- scoring, not tuning.
    def score_mapping(routes):
        u = o = 0
        for r in rows:
            route = routes.get(r["predicted_tier"], "general")
            if route == "small" and not r["small_meets_bar"] and r["large_meets_bar"]:
                u += 1
            if route == "large" and r["small_meets_bar"]:
                o += 1
        return u / n, o / n

    mappings = {
        "MEDIUM->small (deployed)": ROUTES,
        "MEDIUM->large (safety-biased)": {"SIMPLE": "small", "MEDIUM": "large", "COMPLEX": "large", "REASONING": "large"},
        "REASONING-only->large (cost-biased)": {"SIMPLE": "small", "MEDIUM": "small", "COMPLEX": "small", "REASONING": "large"},
    }
    cost_table = {}
    for name, routes in mappings.items():
        u_rate, o_rate = score_mapping(routes)
        cost_table[name] = {"under_rate": u_rate, "over_rate": o_rate,
                            "L": {str(w): w * u_rate + o_rate for w in (1, 5, 10, 50)}}
    out["cost_table"] = cost_table
    return out


def _summarize_controls():
    controls = _reread(JUDGE_CONTROL_CHECKPOINT)
    primary = _reread(JUDGE_CHECKPOINT)
    primary_by_id = {r["_key"]: r for r in primary}

    pos = [c for c in controls if c.get("control") == "position_swap" and not c.get("judge_failed")]
    inter = [c for c in controls if c.get("control") == "inter_judge" and not c.get("judge_failed")]

    def normalize_verdict(rec, swapped_relative_to_primary):
        v = rec.get("verdict")
        if v == "tie" or v is None:
            return v
        if swapped_relative_to_primary:
            return {"1": "2", "2": "1"}.get(v, v)
        return v

    flips = 0
    for c in pos:
        p = primary_by_id.get(c["prompt_id"])
        if not p:
            continue
        primary_pref = "large" if ((p["verdict"] == "1") == p["order_swapped"]) else "small" if p["verdict"] in ("1", "2") else "tie"
        control_pref = "large" if ((c["verdict"] == "1") != p["order_swapped"]) else "small" if c["verdict"] in ("1", "2") else "tie"
        if primary_pref != control_pref:
            flips += 1

    agree = 0
    for c in inter:
        p = primary_by_id.get(c["prompt_id"])
        if not p:
            continue
        primary_pref = "large" if ((p["verdict"] == "1") == p["order_swapped"]) else "small" if p["verdict"] in ("1", "2") else "tie"
        inter_pref = "large" if ((c["verdict"] == "1") == p["order_swapped"]) else "small" if c["verdict"] in ("1", "2") else "tie"
        if primary_pref == inter_pref:
            agree += 1

    return {
        "position_bias": {"n": len(pos), "flip_rate": (flips / len(pos)) if pos else None},
        "inter_judge": {"n": len(inter), "agreement_rate": (agree / len(inter)) if inter else None},
    }


def _print_summary(s):
    print("\n=== AFFINITY SUMMARY (n=%d) ===" % s["n"])
    for k in ("exact_four_tier_accuracy", "human_tier_route_agreement",
             "actual_model_selection_accuracy", "under_routing_quality_risk",
             "over_routing_wasted_capacity", "neither_model_suffices",
             "achievable_oracle_small_share", "realized_classifier_small_share"):
        v = s.get(k)
        print("  %-38s %s" % (k, ("%.1f%%" % (100 * v)) if v is not None else "n/a"))
    c = s.get("controls", {})
    pb, ij = c.get("position_bias", {}), c.get("inter_judge", {})
    print("  judge position-bias flip rate     : %s (n=%s)" % (pb.get("flip_rate"), pb.get("n")))
    print("  inter-judge agreement rate        : %s (n=%s)" % (ij.get("agreement_rate"), ij.get("n")))


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["generate", "judge", "matrix", "all"])
    ap.add_argument("--pilot", type=int, default=0, help="small class-balanced slice for a pilot run")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    os.makedirs(RESULTS, exist_ok=True)

    if args.stage in ("generate", "all"):
        cmd_generate(args)
    if args.stage in ("judge", "all"):
        cmd_judge(args)
    if args.stage in ("matrix", "all"):
        cmd_matrix(args)


if __name__ == "__main__":
    main()
