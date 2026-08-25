#!/usr/bin/env python3
"""Salvage B-5's real-model arms from cross-tenant contamination.

The three real-model arms (always-large, always-small, classified) share
single-slot llama.cpp backends with a shared homelab: `backend_served_only_
this_arm` failed on ALL THREE arms, meaning some fraction of requests in
each arm queued behind another tenant's generation rather than measuring
this benchmark alone. That does not mean the WHOLE arm is worthless -- most
requests were probably clean, and the arm-level check has no way to say
which ones.

Per-request tokens/sec is the discriminator. Contamination manifests as
queueing time BEFORE our request's tokens start flowing, which inflates
wall-clock without changing completion_tokens -- so a contended request has
LOW tokens/sec even though the model itself was not actually slower. The
fastest requests in an arm establish the uncontended throughput; anything
well below that queued behind someone else.

This does not require a re-run: completion_tokens and wall_ns are already in
the existing records. It IS a heuristic, not ground truth -- the rigorous
fix (per-request llama.cpp metrics snapshots) is scenarios/b5c's next
revision. This script exists to avoid discarding 120 measured requests over
a contamination event that, per request, most of them did not experience.

Usage: python3 analyze_b5_contamination.py [records.jsonl]
"""
import json
import sys

CONTAMINATION_THRESHOLD = 0.5  # tokens/sec below 50% of the arm's max = LIKELY contended


def percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * q)))
    return sorted_vals[idx]


def stats(vals_ms):
    s = sorted(vals_ms)
    return {"n": len(s), "p50": percentile(s, 0.50), "p90": percentile(s, 0.90),
            "p99": percentile(s, 0.99), "max": s[-1] if s else None}


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "results/b5c-incluster.records.jsonl"
    records = [json.loads(l) for l in open(path, encoding="utf-8")]

    arms = {}
    for r in records:
        if r["arm"] not in ("always-large", "always-small", "classified"):
            continue
        if r.get("status") != 200 or not r.get("completion_tokens") or r.get("wall_ns", 0) <= 0:
            continue
        wall_s = r["wall_ns"] / 1e9
        tok_s = r["completion_tokens"] / wall_s
        arms.setdefault(r["arm"], []).append({**r, "wall_s": wall_s, "tokens_per_s": tok_s})

    print("%-14s %6s | %-28s | %-28s | dropped" % ("arm", "n", "RAW (all requests) p50/p99 ms", "CLEAN (fast subset) p50/p99 ms"))
    print("-" * 100)
    summary = {}
    for arm, reqs in arms.items():
        max_tok_s = max(r["tokens_per_s"] for r in reqs)
        likely_uncontended = [r for r in reqs if r["tokens_per_s"] >= CONTAMINATION_THRESHOLD * max_tok_s]
        likely_contended = [r for r in reqs if r not in likely_uncontended]

        # p99 from n<~30 is effectively reporting the maximum, not a percentile.
        # Report p50/p90/range for the filtered subset; p99 only for the raw
        # population, which has enough n to make p99 meaningful at all (even
        # though it is contaminated) -- report it there ONLY as context for how
        # bad the contamination tail was, never as a latency claim.
        raw_ms = stats([r["wall_s"] * 1000 for r in reqs])
        sub_vals = sorted(r["wall_s"] * 1000 for r in likely_uncontended)
        sub_ms = {"n": len(sub_vals), "p50": percentile(sub_vals, 0.50), "p90": percentile(sub_vals, 0.90),
                  "min": sub_vals[0] if sub_vals else None, "max": sub_vals[-1] if sub_vals else None}

        print("%-14s %6d | raw p50=%9.0f p99=%9.0f | uncontended(n=%d) p50=%9.0f p90=%9.0f range=[%.0f,%.0f]"
              % (arm, len(reqs), raw_ms["p50"], raw_ms["p99"], sub_ms["n"], sub_ms["p50"], sub_ms["p90"],
                 sub_ms["min"] or 0, sub_ms["max"] or 0))

        summary[arm] = {
            "original_observations": len(reqs),
            "likely_uncontended": len(likely_uncontended),
            "likely_contended": len(likely_contended),
            "classification_method": "post-hoc token-throughput clustering (heuristic)",
            "ground_truth_isolation": False,
            "max_observed_tokens_per_s": max_tok_s,
            "raw_latency_ms_all_observations": raw_ms,
            "likely_uncontended_latency_ms": sub_ms,
            "note": "p99 is not reported for the likely-uncontended subset: n=%d is too small for a "
                    "99th-percentile estimate to mean anything beyond 'the maximum observation'. "
                    "This is a HEURISTIC classification, not ground-truth per-request attribution -- "
                    "see B-5R for the rigorous replacement (llama.cpp metrics snapshot before/after "
                    "each request, reconciled against response usage)." % len(likely_uncontended),
            "likely_contended_prompt_ids": [r["meta"]["prompt_id"] for r in likely_contended],
        }

    print("\nContamination threshold: tokens/sec < %.0f%% of the arm's own fastest observed request."
          % (100 * CONTAMINATION_THRESHOLD))
    print("This is a heuristic salvage of an already-run benchmark, not a substitute for preventing")
    print("contamination in the first place. Report BOTH columns; never publish the clean column alone")
    print("without disclosing what was dropped and why.")

    with open("results/b5-contamination-analysis.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
    print("\nwrote results/b5-contamination-analysis.json")


if __name__ == "__main__":
    main()
