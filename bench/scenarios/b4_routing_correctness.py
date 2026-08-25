"""B-4 — Routing correctness (SPEC-BENCH §1).

*Does the right prompt reach the right tier?* llm-d-sc measured CLASSIFICATION
accuracy against its own held-out set. Nobody has measured whether the gateway
built on top of it actually ROUTES correctly, which is a different question:
it includes prompt extraction, the label->cluster mapping, the score floor, the
fail-open paths, and every place a decision can be dropped between the gRPC
response and `ctx.cluster`.

Method
------
Drive `bench/prompts/complexity-heldout.json` (128 prompts, 32 per class,
8 marked `boundary`) through the gateway one at a time and record which cluster
each landed on.

**Leakage is asserted before a single request is sent.** `arms()` runs
`bench/prompts/check_leakage.py` and refuses to build if it does not exit 0. A
routing-accuracy number measured on the classifier's own anchors is not a
result, it is a restatement of the anchor file.

Attribution uses BOTH independent sources SPEC-K8S §3.1 identifies and asserts
they agree:
1. the `x-llm-d-sc-label` provenance header the filter set (mapped through the
   configured routing table), and
2. the upstream's own response `model` field.
A disagreement means either the filter set a header it did not act on, or the
load balancer sent the request somewhere else. Either is a bug, and the arm
reports `passed: false` rather than a confusion matrix built on a guess.

Emitted in `extra` (all consumed by `bench/report.py`):
* `confusion` — rows = intended tier, cols = actual cluster (SPEC-BENCH's ask)
* `label_confusion` — rows = intended tier, cols = observed label
* `routing_accuracy`, and per-class `precision`/`recall`/`f1`
* `misroutes` — **`simple_to_large` (wasted capacity) and `reasoning_to_small`
  (quality risk) are counted and reported separately**, because SPEC-BENCH is
  explicit that these two errors are not symmetric.
* `boundary` — accuracy split across the deliberately boundary-ish prompts.

Warmup uses synthetic prompts from a namespace disjoint from the held-out set,
so the measured pass is a genuine first encounter rather than a lap through
llm-d-sc's warm cache.

Run (upstreams may be stubs — routing correctness does not need real models,
and SPEC-BENCH §3 says load stays off the homelab):
    python3 bench/stub_upstream.py --port 9001 --model small-stub --echo-sc-headers &
    python3 bench/stub_upstream.py --port 9002 --model large-stub --echo-sc-headers &
    python3 bench/harness.py --scenario b4 --target http://127.0.0.1:8081 \
        --warmup 20 --concurrency 1
"""

from __future__ import annotations

import os
import subprocess
import sys

from _common import (
    DEFAULT_LABEL_TO_CLUSTER,
    DEFAULT_MODEL_TO_CLUSTER,
    chat_body,
    observed_clusters,
    sc_header,
    sized_prompt,
)
from harness import Arm, Request, assertion, key_for

SPEC_ID = "B-4"
DESCRIPTION = "Routing correctness over a held-out labelled prompt set: does the right prompt reach the right tier?"
TARGETS_REAL_MODELS = False
NOTES = [
    "Prompt set is held out from the classifier anchors; leakage is asserted before the run "
    "builds its arms, not after the numbers exist.",
    "Upstreams may be stubs: this measures the routing decision, not generation.",
    "Attribution requires the upstream to echo x-llm-d-sc-* headers, and is cross-checked "
    "against the upstream's own response model field.",
]

CLASSES = ("SIMPLE", "MEDIUM", "COMPLEX", "REASONING")
BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_leakage_check(prompts_file):
    """Refuse to build the arms unless the held-out set is provably clean."""
    script = os.path.join(BENCH_DIR, "prompts", "check_leakage.py")
    out_json = os.path.join(BENCH_DIR, "results", "leakage.json")
    proc = subprocess.run(
        [sys.executable, script, "--prompts", prompts_file, "--json", out_json],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(
            "B-4 refuses to run: the held-out prompt set failed its leakage check "
            "(exit %d). A routing-accuracy number measured on the classifier's own anchors "
            "is not a measurement." % proc.returncode
        )
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "clean"


def arms(cfg):
    prompts = cfg.load_prompts()
    leak_summary = _run_leakage_check(cfg.prompts_file)
    repeats = cfg.param("repeats", 1, int)
    max_tokens = cfg.param("max_tokens", 16, int)
    conc = cfg.concurrency[0] if cfg.concurrency else 1
    label_to_cluster = dict(DEFAULT_LABEL_TO_CLUSTER)
    model_to_cluster = dict(DEFAULT_MODEL_TO_CLUSTER)
    for pair in (cfg.param("model_map", "", str) or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            model_to_cluster[k.strip()] = v.strip()
    expect_status = tuple(s.strip() for s in cfg.param("expect_status", "OK", str).split(","))

    ordered = list(prompts) * repeats

    def build(index, phase, run_id):
        if phase == "warmup":
            # Disjoint synthetic namespace: warming must not pre-classify a
            # held-out prompt, or the measured pass is a cache replay.
            key = key_for("miss", "warmup", run_id, index, seed="b4warm")
            text, meta = sized_prompt(32, key)
            meta.update({"prompt_key": key, "warmup": True})
            return Request(body=chat_body(text, max_tokens=max_tokens), meta=meta)
        rec = ordered[index % len(ordered)]
        meta = {
            "prompt_key": rec["id"],
            "prompt_id": rec["id"],
            "intended_label": rec["label"],
            "intended_cluster": label_to_cluster.get(rec["label"]),
            "boundary": bool(rec.get("boundary")),
            "domain": rec.get("domain"),
        }
        return Request(body=chat_body(rec["prompt"], max_tokens=max_tokens), meta=meta)

    arm = Arm(
        name="routing-heldout",
        target=cfg.param("classified_url", cfg.target),
        build=build,
        params={
            "prompts": len(prompts),
            "repeats": repeats,
            "requests": len(ordered),
            "label_to_cluster": label_to_cluster,
            "model_to_cluster": model_to_cluster,
            "expect_status": list(expect_status),
            "leakage_check": leak_summary,
        },
        warmup=cfg.warmup,
        measured=len(ordered),
        concurrency=conc,
        cache_mode="n/a",
        notes="One pass over the held-out set; warmup uses a disjoint synthetic namespace.",
    )
    arm.assertions = lambda result, ctx: _assertions(result, label_to_cluster, model_to_cluster,
                                                     expect_status, prompts)
    arm.summarize = lambda result, ctx: _summarize(result, label_to_cluster, model_to_cluster)
    return [arm]


def _cluster_of(record, label_to_cluster, model_to_cluster):
    """The cluster a record landed on, preferring provenance over the model id."""
    label = sc_header(record, "label")
    if label and label in label_to_cluster:
        return label_to_cluster[label]
    model = record.get("model")
    if model and model in model_to_cluster:
        return model_to_cluster[model]
    status = sc_header(record, "status")
    if status and status != "OK":
        # Every non-OK status routes to default_cluster by SPEC §4.6.
        return "general"
    return None


def _assertions(result, label_to_cluster, model_to_cluster, expect_status, prompts):
    records = result.ok_records()
    out = []

    # 1. The premise: classification actually happened, with the intended status.
    statuses = {}
    for r in records:
        s = sc_header(r, "status") or "<absent>"
        statuses[s] = statuses.get(s, 0) + 1
    intended = sum(statuses.get(s, 0) for s in expect_status)
    out.append(
        assertion(
            "classification_status_as_intended",
            intended == len(records) and len(records) > 0,
            "x-llm-d-sc-status tally %s; intended %s on %d/%d requests. A non-intended status "
            "is a real finding, but it must be declared: re-run with "
            "--param expect_status=OK,ABSTAIN to record it deliberately."
            % (statuses, list(expect_status), intended, len(records)),
        )
    )

    # 2. Both attribution sources must agree, per SPEC-K8S §3.1.
    _, _, disagreements = observed_clusters(records, label_to_cluster, model_to_cluster)
    out.append(
        assertion(
            "attribution_sources_agree",
            not disagreements,
            "%d/%d requests where the x-llm-d-sc provenance and the upstream's own model field "
            "disagreed about which cluster served them%s"
            % (len(disagreements), len(records),
               "" if not disagreements else "; first: %s" % disagreements[0]),
        )
    )

    # 3. Every request must be attributable at all, or the matrix has holes.
    unattributed = [r["i"] for r in records if _cluster_of(r, label_to_cluster, model_to_cluster) is None]
    out.append(
        assertion(
            "every_request_attributed",
            not unattributed,
            "%d/%d requests could not be attributed to a cluster from either source. Start the "
            "stub upstreams with --echo-sc-headers, or add --param model_map=<id>=<cluster>."
            % (len(unattributed), len(records)),
        )
    )

    # 4. Warmup must not have touched the held-out prompts.
    heldout_ids = {p["id"] for p in prompts}
    warm_leak = [r for r in result.warmup_records if r["meta"].get("prompt_key") in heldout_ids]
    out.append(
        assertion(
            "warmup_did_not_prewarm_heldout_prompts",
            not warm_leak,
            "%d warmup requests used a held-out prompt (expected 0; warmup runs in a disjoint "
            "synthetic namespace so the measured pass is a genuine first encounter)"
            % len(warm_leak),
        )
    )

    # 5. Coverage: every prompt in the set was actually sent.
    sent = {r["meta"].get("prompt_id") for r in records}
    out.append(
        assertion(
            "every_heldout_prompt_measured",
            heldout_ids.issubset(sent),
            "%d/%d held-out prompts appear in the measured window"
            % (len(heldout_ids & sent), len(heldout_ids)),
        )
    )
    return out


def _summarize(result, label_to_cluster, model_to_cluster):
    records = result.ok_records()
    clusters = sorted({c for c in label_to_cluster.values()} | {"general", "unattributed"})
    confusion = {row: {col: 0 for col in clusters} for row in CLASSES}
    label_confusion = {row: {col: 0 for col in list(CLASSES) + ["<none>"]} for row in CLASSES}

    correct = 0
    boundary_total = boundary_correct = 0
    misroutes = {
        "simple_to_large_wasted_capacity": 0,
        "medium_to_large_wasted_capacity": 0,
        "complex_to_small_quality_risk": 0,
        "reasoning_to_small_quality_risk": 0,
        "to_general_unrouted": 0,
    }
    per_prompt = []

    for r in records:
        intended = r["meta"].get("intended_label")
        if intended not in CLASSES:
            continue
        expected_cluster = label_to_cluster.get(intended)
        actual = _cluster_of(r, label_to_cluster, model_to_cluster) or "unattributed"
        if actual not in confusion[intended]:
            confusion[intended][actual] = 0
        confusion[intended][actual] += 1

        observed_label = sc_header(r, "label") or "<none>"
        if observed_label not in label_confusion[intended]:
            label_confusion[intended][observed_label] = 0
        label_confusion[intended][observed_label] += 1

        hit = actual == expected_cluster
        correct += 1 if hit else 0
        if r["meta"].get("boundary"):
            boundary_total += 1
            boundary_correct += 1 if hit else 0
        if not hit:
            if intended == "SIMPLE" and actual == "large":
                misroutes["simple_to_large_wasted_capacity"] += 1
            elif intended == "MEDIUM" and actual == "large":
                misroutes["medium_to_large_wasted_capacity"] += 1
            elif intended == "COMPLEX" and actual == "small":
                misroutes["complex_to_small_quality_risk"] += 1
            elif intended == "REASONING" and actual == "small":
                misroutes["reasoning_to_small_quality_risk"] += 1
            if actual == "general":
                misroutes["to_general_unrouted"] += 1
        per_prompt.append({
            "prompt_id": r["meta"].get("prompt_id"),
            "intended_label": intended,
            "observed_label": observed_label,
            "expected_cluster": expected_cluster,
            "actual_cluster": actual,
            "boundary": bool(r["meta"].get("boundary")),
            "correct": hit,
            "latency_ms": r["wall_ns"] / 1e6,
        })

    n = sum(sum(row.values()) for row in confusion.values())
    per_class = {}
    for cls in CLASSES:
        tp = label_confusion[cls].get(cls, 0)
        fn = sum(v for k, v in label_confusion[cls].items() if k != cls)
        fp = sum(label_confusion[other].get(cls, 0) for other in CLASSES if other != cls)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        per_class[cls] = {
            "support": tp + fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }

    return {
        "clusters": clusters,
        "classes": list(CLASSES),
        "confusion": confusion,
        "label_confusion": label_confusion,
        "routing_accuracy": (correct / n) if n else 0.0,
        "routing_correct": correct,
        "routing_total": n,
        "per_class": per_class,
        "misroutes": misroutes,
        "boundary": {
            "total": boundary_total,
            "correct": boundary_correct,
            "accuracy": (boundary_correct / boundary_total) if boundary_total else 0.0,
        },
        "per_prompt": per_prompt,
    }
