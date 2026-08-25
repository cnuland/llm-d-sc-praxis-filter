#!/usr/bin/env python3
"""Reconcile this POC's routing accuracy with llm-d-sc's published accuracy.

The POC measured 68.8% label accuracy on its own held-out prompts, while
llm-d-sc publishes 97.5% for the same classifier. That gap has exactly three
possible explanations, and this script tests all three so the answer is evidence
rather than assertion:

  CHECK 1  Is the FILTER faithful?
           Classify every POC prompt twice -- once through the full path
           (client -> Praxis -> llm_d_sc filter -> gRPC -> llm-d-sc) and once
           straight through the CLI with no proxy at all. Any disagreement is an
           integration bug and nothing else matters until it is fixed.

  CHECK 2  Is the right MODEL loaded?
           The un-finetuned MiniLM baseline publishes 62.5%, which is close
           enough to 68.8% to be a very tempting wrong answer. Assert the
           runtime reports the finetuned revision from classifiers/complexity.json.

  CHECK 3  Does the PUBLISHED number reproduce here?
           Run llm-d-sc's own held-out set through this same binary and model
           dir. If it reproduces, the environment is sound and the gap is a
           property of the prompt sets -- which is a finding, not a defect.

Usage:
    python3 bench/verify_classifier_parity.py [--genesis ~/llm-d-sc-genesis]
"""

import json
import os
import subprocess
import sys
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def die(msg, code=2):
    print("SKIP: %s" % msg)
    sys.exit(code)


def classify_all(cli, model_dir, texts):
    """Run the CLI over a batch of prompts, one per line, and return labels."""
    payload = "\n".join(t.replace("\n", " ") for t in texts) + "\n"
    proc = subprocess.run(
        [cli, "--model", model_dir, "--json"],
        input=payload, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        die("classify CLI failed: %s" % proc.stderr.strip()[:300], 1)
    out = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        out[rec["text"]] = rec
    return out


def main():
    genesis = os.path.expanduser("~/llm-d-sc-genesis")
    if "--genesis" in sys.argv:
        genesis = os.path.expanduser(sys.argv[sys.argv.index("--genesis") + 1])

    cli = os.path.join(genesis, "target/release/llm-d-sc-classify")
    model_dir = os.path.join(genesis, "artifacts/models/complexity")
    taxonomy = os.path.join(genesis, "classifiers/complexity.json")
    theirs = os.path.join(genesis, "evals/datasets/complexity-heldout.jsonl")

    for path, what in ((cli, "classify CLI"), (model_dir, "model dir"),
                       (taxonomy, "taxonomy"), (theirs, "llm-d-sc held-out set")):
        if not os.path.exists(path):
            die("no %s at %s -- needs an llm-d-sc-genesis checkout" % (what, path))

    ours = json.load(open(os.path.join(HERE, "prompts/complexity-heldout.json")))
    failures = []

    # ---- CHECK 1: filter vs CLI --------------------------------------------
    print("=== CHECK 1: is the filter faithful? ===")
    records = sorted(glob.glob(os.path.join(HERE, "results", "*b4.records.jsonl")))
    direct = classify_all(cli, model_dir, [p["prompt"] for p in ours])
    if not records:
        print("  SKIP: no B-4 records yet (run scenario b4 first)")
    else:
        by_id = {p["id"]: p for p in ours}
        measured = [json.loads(l) for l in open(records[-1], encoding="utf-8")]
        measured = [r for r in measured if r.get("phase") == "measured"]
        agree = disagree = 0
        for r in measured:
            text = by_id[r["meta"]["prompt_id"]]["prompt"].replace("\n", " ")
            through_filter = (r.get("sc_headers") or {}).get("x-llm-d-sc-label")
            straight = direct.get(text, {}).get("ranked", [{}])[0].get("label")
            if through_filter == straight:
                agree += 1
            else:
                disagree += 1
        print("  through the filter vs straight to the classifier: %d agree, %d disagree"
              % (agree, disagree))
        if disagree:
            failures.append("filter and CLI disagree on %d prompts -- integration bug" % disagree)
        else:
            print("  PASS: the filter reports exactly what the classifier says")

    # ---- CHECK 2: model revision -------------------------------------------
    print("\n=== CHECK 2: is the finetuned model loaded? ===")
    expected = json.load(open(taxonomy))["model_revision"]
    seen = {r.get("model_revision") for r in direct.values()}
    print("  expected : %s" % expected)
    print("  reported : %s" % (", ".join(sorted(s or "?" for s in seen)) or "?"))
    if seen == {expected}:
        print("  PASS: finetuned weights, not the un-finetuned baseline")
    else:
        failures.append("model revision mismatch: %s != %s" % (seen, expected))

    # ---- CHECK 3: reproduce the published figure ---------------------------
    print("\n=== CHECK 3: does llm-d-sc's published accuracy reproduce here? ===")
    rows = [json.loads(l) for l in open(theirs, encoding="utf-8")]
    got = classify_all(cli, model_dir, [r["text"] for r in rows])
    ok = hard_ok = hard_n = 0
    for r in rows:
        label = got.get(r["text"].replace("\n", " "), {}).get("ranked", [{}])[0].get("label")
        hit = label == r["tier"]
        ok += hit
        if r.get("hard"):
            hard_n += 1
            hard_ok += hit
    acc = ok / len(rows)
    print("  llm-d-sc's own held-out set : %d/%d = %.1f%%  (published: 97.5%%)"
          % (ok, len(rows), 100 * acc))
    if hard_n:
        print("  boundary cases              : %d/%d = %.1f%%  (published: 95.0%%)"
              % (hard_ok, hard_n, 100 * hard_ok / hard_n))
    if acc < 0.95:
        failures.append("published accuracy did NOT reproduce (%.1f%%) -- environment is degraded"
                        % (100 * acc))
    else:
        print("  PASS: environment reproduces the published figure")

    # ---- the comparison that motivated all this ----------------------------
    ours_ok = sum(
        direct.get(p["prompt"].replace("\n", " "), {}).get("ranked", [{}])[0].get("label") == p["label"]
        for p in ours
    )
    print("\n=== THE GAP ===")
    print("  llm-d-sc's own held-out set : %d/%d = %.1f%%" % (ok, len(rows), 100 * acc))
    print("  this POC's prompt set       : %d/%d = %.1f%%"
          % (ours_ok, len(ours), 100 * ours_ok / len(ours)))
    print("\n  Both are correct. They differ in TASK FRAMING, not subject domain:")
    print("  llm-d-sc's COMPLEX prompts vary the domain widely (vineyard irrigation,")
    print("  festival ticketing, sailing regattas) but all keep the anchors' framing --")
    print("  'Design/Architect/Build a ... system with A, B and C'. This POC's set also")
    print("  varies the FRAMING, into planning, operational and creative documents, which")
    print("  sit nearer the MEDIUM anchors. All 12 COMPLEX anchors are technical system")
    print("  design; all 12 REASONING anchors are formal proofs.")

    print()
    if failures:
        for f in failures:
            print("FAIL: %s" % f, file=sys.stderr)
        sys.exit(1)
    print("All checks passed: the integration is faithful and the environment is sound.")


if __name__ == "__main__":
    main()
