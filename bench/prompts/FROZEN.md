# FROZEN — external holdout, do not tune against

```
file    : complexity-heldout.json
sha256  : 93e62ce511cb11adc53c44952c5b9bc1fb41ab54db2c42f6c24510b28fd4aa3e
frozen  : 2026-08-24
prompts : 128  (32 SIMPLE / 32 MEDIUM / 32 COMPLEX / 32 REASONING)
domains : 91
boundary: 8 marked
leakage : 0 verbatim, 0 near-duplicate vs complexity/cost/sensitivity anchors
          (17,664 pairwise comparisons, `check_leakage.py`)
```

The file is `chmod 444`. Verify before every use:

```bash
shasum -a 256 -c bench/prompts/complexity-heldout.sha256
```

## Why this is frozen

This set was authored independently of the classifier's anchors and, on first
contact, dropped four-tier accuracy from llm-d-sc's published **97.5%** (its own
held-out set) to **68.8%**. That makes it valuable precisely because nothing has
been tuned against it. Its value is destroyed the moment anyone edits a prompt,
adjusts a label, or adds an anchor in response to a specific failure — at which
point it becomes another in-distribution set that reports a flattering number.

## Frozen against — nothing in this list may change while it is in use

- the prompts and their `label` / `boundary` / `domain` fields
- classifier anchors or taxonomy definitions (`classifiers/*.json`)
- `min_score`, `routes`, or any filter threshold, **when reporting against it**
- model weights or the classifier backend

Alternative label→cluster mappings and thresholds may be **scored** against it
(see `analyze_routing.py`), because scoring is not tuning: the classifier output
is fixed and only the downstream decision rule varies. What is forbidden is
changing the classifier or the prompts *because of* a result observed here.

## Intended use

An external holdout for comparing classifiers on equal terms — the current
MiniLM-based complexity model, an un-finetuned baseline, a custom classifier, a
ModernBERT variant — run through **exactly this set**, unmodified, so the
comparison isolates the classifier rather than the evaluation.

## Known limitation, stated up front

The labels are one author's judgement of four fuzzy tiers. Disagreement with the
classifier is not automatically classifier error; it may be two reasonable people
meaning different things by "COMPLEX". That is exactly why the model-affinity
experiment (`bench/affinity/`) exists: it replaces the human tier with observed
model outcomes, which need no one's opinion about what "COMPLEX" means.
