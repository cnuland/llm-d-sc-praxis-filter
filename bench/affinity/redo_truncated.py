#!/usr/bin/env python3
"""Re-generate + re-judge prompts whose original 512-token completions hit
the cap on BOTH backends, which made "neither model meets the bar" almost
entirely a truncation artifact rather than a capability finding: 59/60 of
the original "neither" outcomes had both completions at exactly 512 tokens.

This appends NEW records under the SAME `_key` as the original generation/
judging entries. `read_checkpoint()` builds its dict by iterating the file
top-to-bottom and keying on `_key`, so a later record for the same key wins
when the file is re-read -- these redo records supersede the truncated ones
without needing to rewrite history.

Usage: python3 redo_truncated.py [--limit N] [--max-tokens 1536]
"""
import argparse
import json
import os
import sys
import threading
import time
import concurrent.futures

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_affinity as ra  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-tokens", type=int, default=1536)
    args = ap.parse_args()

    neither_ids = json.load(open("/tmp/neither_ids.json"))
    if args.limit:
        neither_ids = neither_ids[: args.limit]
    print("redoing %d prompts at max_tokens=%d" % (len(neither_ids), args.max_tokens))

    ra.MAX_TOKENS = args.max_tokens  # module-level, read by chat_payload()

    all_prompts = {p["id"]: p for p in ra.load_prompts()}
    targets = [all_prompts[pid] for pid in neither_ids]

    lock = threading.Lock()

    def gen_backend(queue, backend):
        for i, p in enumerate(queue):
            rec = ra.generate_one(p, backend)
            ra.append_checkpoint(ra.GEN_CHECKPOINT, rec, lock)
            print("  [%s %d/%d] %-16s status=%s wall=%.1fs tokens=%s"
                  % (backend, i + 1, len(queue), p["id"], rec["status"], rec["wall_s"],
                     rec.get("completion_tokens")))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(gen_backend, targets, "small"), ex.submit(gen_backend, targets, "large")]
        for f in futs:
            f.result()
    print("redo generation complete.\n")

    gen = ra.read_checkpoint(ra.GEN_CHECKPOINT)
    for i, p in enumerate(targets):
        small, large = gen.get("%s:small" % p["id"]), gen.get("%s:large" % p["id"])
        if not small or not large or small.get("status") != 200 or large.get("status") != 200:
            print("  skip judge for %s: generation incomplete" % p["id"], file=sys.stderr)
            continue
        seed = int.from_bytes(p["id"].encode(), "little") % (2**31)
        import random
        rnd = random.Random(seed)
        order_swapped = rnd.random() < 0.5
        resp1 = large["response_text"] if order_swapped else small["response_text"]
        resp2 = small["response_text"] if order_swapped else large["response_text"]
        verdict = ra.run_judge(ra.LARGE_URL, ra.LARGE_MODEL, ra.ds4_token(), p["prompt"], resp1, resp2)
        rec = {"_key": p["id"], "prompt_id": p["id"], "order_swapped": order_swapped,
              "order_seed": seed, "judge": "large", **verdict}
        ra.append_checkpoint(ra.JUDGE_CHECKPOINT, rec, lock)
        print("  [judge %d/%d] %-16s verdict=%s failed=%s completion_tokens now small=%s large=%s"
              % (i + 1, len(targets), p["id"], rec.get("verdict"), rec.get("judge_failed", False),
                 small.get("completion_tokens"), large.get("completion_tokens")))

    print("\nredo complete. Run `python3 run_affinity.py matrix` to regenerate the final summary.")


if __name__ == "__main__":
    main()
