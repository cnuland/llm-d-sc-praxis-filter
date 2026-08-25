#!/usr/bin/env python3
"""Assert the held-out benchmark prompt set does not leak the classifier's anchors.

B-4 (SPEC-BENCH §1) reports a routing-accuracy number. That number is only
meaningful if none of the prompts it is measured on are the very sentences the
classifier was anchored on. A single leaked prompt turns the accuracy figure
into a lie, so this check is an ASSERTION, not a report: it exits non-zero on
any leak and the benchmark run is expected to refuse to proceed.

Three kinds of overlap are checked, because verbatim equality is the weakest of
the three and the easiest to dodge by accident:

1. **Verbatim** — normalised (lowercase, collapsed whitespace, unicode quotes
   folded, terminal punctuation stripped) string equality against every anchor
   in every taxonomy. Zero tolerance.
2. **Near-duplicate** — content-token Jaccard and containment ratios against
   every anchor, so a trivially reworded anchor ("What is the capital of
   Spain?") is caught even though it is not verbatim.
3. **Internal duplication** — the same prompt appearing twice in the held-out
   set, which would silently double-weight one class member.

Schema and balance are validated at the same time, because a prompt set that
is clean but unbalanced is also not a usable measurement.

Usage:
    python3 bench/prompts/check_leakage.py
    python3 bench/prompts/check_leakage.py --json bench/results/leakage.json

Exit codes: 0 clean · 1 leak or schema violation · 2 anchors unavailable.
Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH_DIR = os.path.dirname(HERE)

DEFAULT_PROMPTS = os.path.join(HERE, "complexity-heldout.json")
DEFAULT_ANCHOR_DIR = os.environ.get(
    "BENCH_CLASSIFIERS_DIR",
    os.path.expanduser("~/llm-d-sc-genesis/classifiers"),
)
# complexity.json is the taxonomy actually routed on; cost and sensitivity are
# checked as well because the same author wrote all three and a prompt that
# duplicates a cost anchor is still a prompt the model has seen anchored.
ANCHOR_FILES = ("complexity.json", "cost.json", "sensitivity.json")

VALID_LABELS = ("SIMPLE", "MEDIUM", "COMPLEX", "REASONING")
MIN_PROMPTS = 120
MIN_PER_CLASS = 30

# A near-duplicate is flagged at or above ANY of these thresholds. They are
# deliberately strict: the cost of rewriting a prompt is a minute, the cost of
# publishing a contaminated accuracy number is the whole result.
#
# Two token views are scored, because neither alone is sufficient:
#
# * CONTENT tokens (stopwords removed) catch long reworded anchors, but on a
#   short question they leave so few tokens that one differing noun tanks the
#   ratio -- "What is the capital city of Spain?" against the anchor "What is
#   the capital of France?" scores only 0.25 on content Jaccard, and that is
#   exactly the rewording this check exists to catch.
# * FULL tokens (stopwords kept) catch that structural rewording, because the
#   shared frame is most of the sentence. They are scored at a slightly higher
#   containment bar so that two genuinely different short questions that happen
#   to share a template ("How many ounces are in a pound?" vs "How many
#   teaspoons are in a tablespoon?") are not condemned for sharing English.
JACCARD_LIMIT = 0.60
CONTAINMENT_LIMIT = 0.75
FULL_JACCARD_LIMIT = 0.60
FULL_CONTAINMENT_LIMIT = 0.80

STOPWORDS = frozenset(
    """a an the and or of for to in on at by with from as is are was were be been
    being do does did doing have has had i me my we our you your it its this that
    these those there here what which who whom whose when where why how not no
    but if then than so such into over under out up down about again very can
    could should would will shall may might must""".split()
)

_WS = re.compile(r"\s+")
_TERMINAL_PUNCT = ".?!,;:…"
_TOKEN = re.compile(r"[a-z0-9]+(?:[.'\-][a-z0-9]+)*")


def normalize(text: str) -> str:
    """Fold a prompt to its comparison form.

    Lowercase, NFKC-fold (so curly quotes and their ASCII equivalents compare
    equal), collapse all whitespace runs to one space, and strip terminal
    punctuation. Two prompts that differ only in these respects are the same
    prompt for leakage purposes.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    text = _WS.sub(" ", text).strip().lower()
    return text.rstrip(_TERMINAL_PUNCT).strip()


def content_tokens(text: str) -> frozenset:
    """The stopword-stripped token set used for near-duplicate scoring.

    Stopwords are removed so that two unrelated questions do not look similar
    merely because English sentences share their function words.
    """
    toks = {t for t in _TOKEN.findall(normalize(text)) if t not in STOPWORDS}
    return frozenset(toks) or full_tokens(text)


def full_tokens(text: str) -> frozenset:
    """Every token, stopwords included: the structural view of a sentence."""
    return frozenset(_TOKEN.findall(normalize(text)))


def scores(a_content, a_full, b_content, b_full):
    """All four near-duplicate ratios for one prompt/anchor pair."""
    return {
        "jaccard": jaccard(a_content, b_content),
        "containment": containment(a_content, b_content),
        "full_jaccard": jaccard(a_full, b_full),
        "full_containment": containment(a_full, b_full),
    }


def is_near_duplicate(sc):
    return (
        sc["jaccard"] >= JACCARD_LIMIT
        or sc["containment"] >= CONTAINMENT_LIMIT
        or sc["full_jaccard"] >= FULL_JACCARD_LIMIT
        or sc["full_containment"] >= FULL_CONTAINMENT_LIMIT
    )


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: frozenset, b: frozenset) -> float:
    """Overlap as a fraction of the SMALLER token set.

    Catches the case where a short anchor is embedded almost entirely inside a
    longer reworded prompt, which Jaccard alone would score low.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def load_anchors(anchor_dir: str):
    """Return (anchors, missing) where anchors is a list of anchor records."""
    anchors = []
    missing = []
    for fname in ANCHOR_FILES:
        path = os.path.join(anchor_dir, fname)
        if not os.path.exists(path):
            missing.append(path)
            continue
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        taxonomy = doc.get("classifier_id", fname)
        revision = doc.get("taxonomy_revision", "unknown")
        for label, texts in doc.get("anchors", {}).items():
            for text in texts:
                anchors.append(
                    {
                        "taxonomy": taxonomy,
                        "revision": revision,
                        "label": label,
                        "text": text,
                        "norm": normalize(text),
                        "tokens": content_tokens(text),
                        "full": full_tokens(text),
                    }
                )
    return anchors, missing


def validate_schema(prompts, problems):
    """Schema, balance and boundary-flag checks."""
    if not isinstance(prompts, list):
        problems.append("prompt file is not a JSON array")
        return {}
    seen_ids = {}
    counts = {}
    for i, rec in enumerate(prompts):
        where = f"index {i}"
        if not isinstance(rec, dict):
            problems.append(f"{where}: not an object")
            continue
        pid = rec.get("id")
        label = rec.get("label")
        prompt = rec.get("prompt")
        where = f"{pid or where}"
        if not isinstance(pid, str) or not pid:
            problems.append(f"{where}: missing or empty 'id'")
        elif pid in seen_ids:
            problems.append(f"{where}: duplicate id (also at index {seen_ids[pid]})")
        else:
            seen_ids[pid] = i
        if label not in VALID_LABELS:
            problems.append(f"{where}: label {label!r} not in {VALID_LABELS}")
        else:
            counts[label] = counts.get(label, 0) + 1
        if not isinstance(prompt, str) or not prompt.strip():
            problems.append(f"{where}: missing or empty 'prompt'")
        if "boundary" in rec and not isinstance(rec["boundary"], bool):
            problems.append(f"{where}: 'boundary' must be a boolean")
    if len(prompts) < MIN_PROMPTS:
        problems.append(
            f"prompt set has {len(prompts)} prompts, SPEC-BENCH B-4 requires >= {MIN_PROMPTS}"
        )
    for label in VALID_LABELS:
        n = counts.get(label, 0)
        if n < MIN_PER_CLASS:
            problems.append(
                f"class {label} has {n} prompts, SPEC-BENCH B-4 requires >= {MIN_PER_CLASS}"
            )
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--prompts", default=DEFAULT_PROMPTS)
    parser.add_argument(
        "--anchor-dir",
        default=DEFAULT_ANCHOR_DIR,
        help="directory holding complexity.json / cost.json / sensitivity.json",
    )
    parser.add_argument(
        "--allow-missing-anchors",
        action="store_true",
        help="downgrade absent anchor files from a hard failure to a warning. "
        "Only for environments where the classifier tree is genuinely absent; "
        "a run that uses this cannot claim its accuracy number is uncontaminated.",
    )
    parser.add_argument("--json", dest="json_out", default=None, help="write a machine-readable report here")
    parser.add_argument("--top", type=int, default=10, help="how many closest pairs to print")
    args = parser.parse_args(argv)

    with open(args.prompts, encoding="utf-8") as fh:
        prompts = json.load(fh)

    problems = []
    counts = validate_schema(prompts, problems)

    anchors, missing = load_anchors(args.anchor_dir)
    if missing and not args.allow_missing_anchors:
        for path in missing:
            print(f"ERROR: anchor file not found: {path}", file=sys.stderr)
        print(
            "Refusing to certify the prompt set against anchors that were not read.\n"
            "Set BENCH_CLASSIFIERS_DIR or pass --anchor-dir; --allow-missing-anchors\n"
            "downgrades this, but then the leakage claim is unverified.",
            file=sys.stderr,
        )
        return 2

    # Internal duplication inside the held-out set.
    by_norm = {}
    for rec in prompts:
        norm = normalize(rec.get("prompt", ""))
        by_norm.setdefault(norm, []).append(rec.get("id"))
    internal_dupes = {k: v for k, v in by_norm.items() if len(v) > 1}
    for norm, ids in sorted(internal_dupes.items()):
        problems.append(f"internal duplicate prompt shared by {ids}: {norm!r}")

    anchor_by_norm = {}
    for a in anchors:
        anchor_by_norm.setdefault(a["norm"], []).append(a)

    verbatim = []
    near = []
    pairs = []  # every (score) pair, for the "closest surviving pair" report
    for rec in prompts:
        text = rec.get("prompt", "")
        norm = normalize(text)
        toks = content_tokens(text)
        if norm in anchor_by_norm:
            for a in anchor_by_norm[norm]:
                verbatim.append(
                    {
                        "id": rec.get("id"),
                        "prompt": text,
                        "anchor": a["text"],
                        "anchor_taxonomy": a["taxonomy"],
                        "anchor_label": a["label"],
                    }
                )
        full = full_tokens(text)
        best = None
        for a in anchors:
            sc = scores(toks, full, a["tokens"], a["full"])
            # Rank the "closest surviving pair" report by the strongest signal,
            # so the printed pairs really are the nearest misses.
            strength = max(sc.values())
            entry = {
                "id": rec.get("id"),
                "prompt": text,
                "anchor": a["text"],
                "anchor_taxonomy": a["taxonomy"],
                "anchor_label": a["label"],
                "strength": strength,
            }
            entry.update({k: round(v, 4) for k, v in sc.items()})
            if best is None or strength > best["strength"]:
                best = entry
            if is_near_duplicate(sc):
                near.append(dict(entry))
        if best is not None:
            pairs.append(best)

    pairs.sort(key=lambda p: -p["strength"])

    report = {
        "prompts_file": os.path.abspath(args.prompts),
        "anchor_dir": os.path.abspath(args.anchor_dir),
        "anchor_files_read": [f for f in ANCHOR_FILES if os.path.join(args.anchor_dir, f) not in missing],
        "anchor_files_missing": missing,
        "prompt_count": len(prompts),
        "per_class": counts,
        "boundary_count": sum(1 for r in prompts if r.get("boundary")),
        "domain_count": len({r.get("domain") for r in prompts if r.get("domain")}),
        "anchor_count": len(anchors),
        "comparisons": len(prompts) * len(anchors),
        "verbatim_overlaps": len(verbatim),
        "near_duplicates": len(near),
        "internal_duplicates": len(internal_dupes),
        "limits": {
            "content_jaccard": JACCARD_LIMIT,
            "content_containment": CONTAINMENT_LIMIT,
            "full_jaccard": FULL_JACCARD_LIMIT,
            "full_containment": FULL_CONTAINMENT_LIMIT,
        },
        "max_jaccard_observed": round(max((p["jaccard"] for p in pairs), default=0.0), 4),
        "max_containment_observed": round(max((p["containment"] for p in pairs), default=0.0), 4),
        "max_full_jaccard_observed": round(max((p["full_jaccard"] for p in pairs), default=0.0), 4),
        "max_full_containment_observed": round(
            max((p["full_containment"] for p in pairs), default=0.0), 4),
        "closest_pairs": [
            {
                "id": p["id"],
                "jaccard": p["jaccard"],
                "containment": p["containment"],
                "full_jaccard": p["full_jaccard"],
                "full_containment": p["full_containment"],
                "prompt": p["prompt"],
                "anchor": p["anchor"],
                "anchor_taxonomy": p["anchor_taxonomy"],
            }
            for p in pairs[: args.top]
        ],
        "schema_problems": problems,
        "clean": not (verbatim or near or problems),
    }

    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.write("\n")

    print(f"prompts             : {report['prompt_count']} ({report['per_class']})")
    print(f"boundary-marked     : {report['boundary_count']}")
    print(f"distinct domains    : {report['domain_count']}")
    print(f"anchors read        : {report['anchor_count']} from {report['anchor_files_read']}")
    print(f"pairwise comparisons: {report['comparisons']}")
    print(f"verbatim overlaps   : {report['verbatim_overlaps']} (limit 0)")
    print(
        f"near-duplicates     : {report['near_duplicates']} (content jaccard >= {JACCARD_LIMIT}, "
        f"content containment >= {CONTAINMENT_LIMIT}, full jaccard >= {FULL_JACCARD_LIMIT}, "
        f"full containment >= {FULL_CONTAINMENT_LIMIT})"
    )
    print(f"internal duplicates : {report['internal_duplicates']} (limit 0)")
    print(
        f"closest surviving   : content jaccard {report['max_jaccard_observed']} / "
        f"containment {report['max_containment_observed']}; full jaccard "
        f"{report['max_full_jaccard_observed']} / containment "
        f"{report['max_full_containment_observed']}"
    )
    if report["closest_pairs"]:
        print("\nclosest prompt/anchor pairs (all below threshold if this run is clean):")
        for p in report["closest_pairs"]:
            print(f"  content j={p['jaccard']:.3f} c={p['containment']:.3f} | "
                  f"full j={p['full_jaccard']:.3f} c={p['full_containment']:.3f}")
            print(f"    [{p['id']}] {p['prompt']}")
            print(f"    vs [{p['anchor_taxonomy']}] {p['anchor']}")

    if missing:
        print(f"\nWARNING: anchor files not read: {missing}", file=sys.stderr)

    failed = False
    for v in verbatim:
        failed = True
        print(
            f"\nLEAK (verbatim): [{v['id']}] duplicates {v['anchor_taxonomy']}/{v['anchor_label']} anchor\n"
            f"  prompt: {v['prompt']}\n  anchor: {v['anchor']}",
            file=sys.stderr,
        )
    for n in near:
        failed = True
        print(
            f"\nLEAK (near-duplicate: content jaccard={n['jaccard']} containment={n['containment']}, "
            f"full jaccard={n['full_jaccard']} containment={n['full_containment']}):"
            f" [{n['id']}] vs {n['anchor_taxonomy']}/{n['anchor_label']}\n"
            f"  prompt: {n['prompt']}\n  anchor: {n['anchor']}",
            file=sys.stderr,
        )
    for p in problems:
        failed = True
        print(f"\nSCHEMA: {p}", file=sys.stderr)

    if failed:
        print("\nFAILED: the held-out set is contaminated or malformed. "
              "Rewrite the offending prompts; do not lower the thresholds.", file=sys.stderr)
        return 1
    print("\nOK: zero verbatim overlap, zero near-duplicates, schema valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
