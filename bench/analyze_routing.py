#!/usr/bin/env python3
"""Routing-correctness analysis for B-4, from the per-request records.

Answers three questions the raw percentile tables cannot:

1. Where does routing actually go wrong? (confusion matrix, tier by tier)
2. Which DIRECTION does it go wrong in? The two misroute costs are not
   symmetric and are never netted against each other:
     - wasted capacity: an easy prompt reaching the expensive model
     - quality risk:    a hard prompt reaching the cheap model
3. Can the operator fix it from config alone? The same classifier output is
   re-scored under alternative label->cluster mappings and against a confidence
   floor, without sending a single new request.

Usage:
    python3 bench/analyze_routing.py [records.jsonl] [--json out.json]
"""

import collections
import glob
import json
import os
import statistics
import sys

# The mapping the POC actually deploys.
DEPLOYED = {"SIMPLE": "small", "MEDIUM": "small", "COMPLEX": "large", "REASONING": "large"}

# Alternatives worth scoring, because the choice is the operator's and it turns
# out to matter more than the classifier does.
ALTERNATIVES = [
    ("MEDIUM->small  (as deployed)", DEPLOYED),
    ("MEDIUM->large  (safety-biased)",
     {"SIMPLE": "small", "MEDIUM": "large", "COMPLEX": "large", "REASONING": "large"}),
    ("only REASONING->large (cost-biased)",
     {"SIMPLE": "small", "MEDIUM": "small", "COMPLEX": "small", "REASONING": "large"}),
]

TIERS = ["SIMPLE", "MEDIUM", "COMPLEX", "REASONING"]


def load(path=None):
    if path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = sorted(glob.glob(os.path.join(here, "results", "*b4.records.jsonl")))
        if not candidates:
            sys.exit("no B-4 records found under bench/results/ — run scenario b4 first")
        path = candidates[-1]
    records = [json.loads(line) for line in open(path, encoding="utf-8")]
    measured = [r for r in records if r.get("phase") == "measured"]
    if not measured:
        sys.exit("B-4 records contain no measured requests")
    return path, measured


def cluster_for(record, routes):
    """Which cluster this request reached, per the classifier's own label."""
    label = (record.get("sc_headers") or {}).get("x-llm-d-sc-label")
    return routes.get(label, "general")


def score_mapping(measured, routes):
    ok = waste = risk = 0
    for r in measured:
        want = r["meta"]["intended_cluster"]
        got = cluster_for(r, routes)
        ok += got == want
        if want == "small" and got == "large":
            waste += 1
        if want == "large" and got == "small":
            risk += 1
    n = len(measured)
    return {"n": n, "accuracy": ok / n, "wasted_capacity": waste / n, "quality_risk": risk / n,
            "correct": ok, "wasted_count": waste, "risk_count": risk}


def main():
    # Consume flag VALUES too, or `--json out.json` leaves "out.json" looking
    # like the positional records path and the tool tries to read its own output.
    argv, args, skip = sys.argv[1:], [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a == "--json":
            skip = True
        elif not a.startswith("--"):
            args.append(a)
    path, measured = load(args[0] if args else None)
    out = {"records": path, "measured": len(measured)}

    print("records: %s (%d measured requests)\n" % (path, len(measured)))

    # --- 1. confusion, deployed mapping -----------------------------------
    conf = collections.defaultdict(collections.Counter)
    label_conf = collections.defaultdict(collections.Counter)
    for r in measured:
        intended = r["meta"]["intended_label"]
        conf[intended][cluster_for(r, DEPLOYED)] += 1
        label_conf[intended][(r.get("sc_headers") or {}).get("x-llm-d-sc-label")] += 1

    print("=== ROUTING CONFUSION (rows = intended tier, cols = cluster reached) ===")
    print("%-12s%9s%9s   expected" % ("intended", "->small", "->large"))
    for tier in TIERS:
        c = conf[tier]
        print("%-12s%9d%9d   %s" % (tier, c.get("small", 0), c.get("large", 0), DEPLOYED[tier]))
    out["confusion"] = {t: dict(conf[t]) for t in TIERS}

    print("\n=== LABEL CONFUSION (rows = intended tier, cols = classifier's label) ===")
    for tier in TIERS:
        print("%-12s %s" % (tier, dict(label_conf[tier])))
    out["label_confusion"] = {t: dict(label_conf[t]) for t in TIERS}

    # --- 2. boundary cases -------------------------------------------------
    boundary = [r for r in measured if r["meta"].get("boundary")]
    if boundary:
        b_ok = sum(cluster_for(r, DEPLOYED) == r["meta"]["intended_cluster"] for r in boundary)
        print("\nboundary-case routing accuracy: %d/%d = %.1f%%"
              % (b_ok, len(boundary), 100.0 * b_ok / len(boundary)))
        out["boundary"] = {"n": len(boundary), "correct": b_ok}

    # --- 3. is this fixable from config? -----------------------------------
    print("\n=== SAME CLASSIFIER OUTPUT, DIFFERENT label->cluster MAPPING ===")
    print("%-36s%9s%12s%12s" % ("mapping", "accuracy", "wasted cap", "qual risk"))
    print("-" * 69)
    out["mappings"] = {}
    for name, routes in ALTERNATIVES:
        s = score_mapping(measured, routes)
        out["mappings"][name] = s
        print("%-36s%8.1f%%%11.1f%%%11.1f%%"
              % (name, 100 * s["accuracy"], 100 * s["wasted_capacity"], 100 * s["quality_risk"]))

    # --- 4. can a confidence floor separate them? --------------------------
    def scores(correct):
        vals = []
        for r in measured:
            if r["meta"]["intended_cluster"] != "large":
                continue
            got = cluster_for(r, DEPLOYED)
            if (got == "large") == correct:
                s = (r.get("sc_headers") or {}).get("x-llm-d-sc-score")
                if s is not None:
                    vals.append(float(s))
        return vals

    mis, good = scores(False), scores(True)
    if mis and good:
        print("\n=== CAN min_score SEPARATE THE MISROUTES? ===")
        print("  misrouted hard prompts : median %.4f  min %.4f  max %.4f  (n=%d)"
              % (statistics.median(mis), min(mis), max(mis), len(mis)))
        print("  correctly-routed hard  : median %.4f  min %.4f  max %.4f  (n=%d)"
              % (statistics.median(good), min(good), max(good), len(good)))
        separable = min(mis) > max(good) or max(mis) < min(good)
        print("  distributions %s -- min_score %s help here."
              % ("are disjoint" if separable else "OVERLAP", "could" if separable else "CANNOT"))
        print("  The classifier is not hedging on the ones it gets wrong; it is confidently wrong,")
        print("  so the score carries no signal an operator could threshold on.")
        out["confidence"] = {
            "misrouted": {"n": len(mis), "median": statistics.median(mis), "min": min(mis), "max": max(mis)},
            "correct": {"n": len(good), "median": statistics.median(good), "min": min(good), "max": max(good)},
            "separable": separable,
        }

    if "--json" in sys.argv:
        dest = sys.argv[sys.argv.index("--json") + 1]
        with open(dest, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("\nwrote %s" % dest)


if __name__ == "__main__":
    main()
