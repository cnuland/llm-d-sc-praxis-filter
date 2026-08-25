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

CONTAMINATION_THRESHOLD = 0.5  # tokens/sec below 50% of the arm's max = contended


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
        clean = [r for r in reqs if r["tokens_per_s"] >= CONTAMINATION_THRESHOLD * max_tok_s]
        contaminated = [r for r in reqs if r not in clean]

        raw_ms = stats([r["wall_s"] * 1000 for r in reqs])
        clean_ms = stats([r["wall_s"] * 1000 for r in clean])
        clean_tok_s = stats([r["tokens_per_s"] * 1000 for r in clean])  # scaled x1000 for the shared percentile()

        print("%-14s %6d | p50=%9.0f p99=%9.0f | p50=%9.0f p99=%9.0f | %d/%d (%.0f%%)"
              % (arm, len(reqs), raw_ms["p50"], raw_ms["p99"], clean_ms["p50"], clean_ms["p99"],
                 len(contaminated), len(reqs), 100 * len(contaminated) / len(reqs)))

        summary[arm] = {
            "n_total": len(reqs), "n_clean": len(clean), "n_contaminated": len(contaminated),
            "contamination_rate": len(contaminated) / len(reqs),
            "max_observed_tokens_per_s": max_tok_s,
            "raw_latency_ms": raw_ms,
            "clean_latency_ms": clean_ms,
            "clean_tokens_per_s_x1000": clean_tok_s,
            "contaminated_prompt_ids": [r["meta"]["prompt_id"] for r in contaminated],
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
