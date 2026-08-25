# Model-affinity analysis — does the cheap model actually succeed?

Frozen dataset: `bench/prompts/complexity-heldout.json`, sha256 `93e62ce5…`, 128 prompts,
unmodified throughout. Full methodology: `SPEC-AFFINITY.md`. Raw data:
`bench/results/affinity-{generation,judging,judge-controls,matrix,summary}.json*`.

## The question

B-4 reported 77.3% "routing accuracy" — agreement between the classifier's label and a
human-authored tier→cluster mapping. That is not the same claim as "the small model would
have failed 22.7% of the time." This experiment replaces the human label with an observed
outcome: send every prompt to **both** real backends, judge both answers blind, and define
the routing target as **the cheapest model that meets the quality bar** — independent of
what any human called the prompt.

## Three things had to be fixed before the numbers meant anything

This section is here because every one of these would have silently produced a plausible,
wrong number if it had gone unnoticed. None was fixed to move a number in a preferred
direction — each is a correction to what was actually being measured.

### 1. The judge's own output was unparseable 28% of the time

`ds4-flash-0731` (the primary judge) frequently spent its whole token budget in
`reasoning_content` and never emitted the verdict JSON in `content` at all — 36/128 first-pass
judge calls returned HTTP 200 with nothing parseable. Fixed by searching `content`, then
`reasoning_content`, then their concatenation, tolerating markdown fences, and raising the
judge's budget 400→900 tokens. Reduced failures to 10/128 (retried once; those 10 are
genuine, counted, and excluded from every rate below — n=118, not 128).

### 2. `neither_model_suffices` was measuring a token budget, not capability

First pass: 50.8% of prompts had **neither** model's answer meet the bar. Before believing
that, the completion data was checked: **59 of 60 "neither" outcomes had both responses hit
the exact 512-token cap** — cut off mid-answer, not judged complete-and-wrong. Zero "neither"
cases existed where both responses finished naturally and still failed. Re-generated and
re-judged those 60 prompts at `max_tokens=1536`. Result: `neither_model_suffices` dropped to
**16.9%**, and every downstream metric shifted with it (below).

### 3. The judge-reliability controls were comparing mismatched content

The mandatory position-bias control (SPEC-AFFINITY Stage 2) re-judges a subsample with
response order swapped and checks for a flipped verdict. 24 of the 32 control-subsample
prompts had been redone in fix #2 — so the *new* primary verdict (judged on the 1536-token
response) was being compared against the *old* control verdict (judged on the original
512-token truncated response). That is not a position comparison; it is comparing two
different pieces of content. Cleared those 48 stale control calls and re-ran them against
matched, current content. This is what produced the n=23 (not n=14) comparisons below.

## Headline metrics (n=118)

| Metric | Value |
|---|---:|
| Exact four-tier accuracy (predicted label == human label) | 68.6% |
| Human tier→route agreement (the old B-4-style number) | 78.0% |
| **Actual model-selection accuracy** (classifier route == cheapest sufficient model) | **78.6%** |
| Under-routing / quality risk (small chosen, small fails, large succeeds) | 11.9% |
| Over-routing / wasted capacity (large chosen, small would have sufficed) | 5.9% |
| Neither model sufficient (both complete responses judged inadequate) | 16.9% |
| Achievable oracle small-share (fraction where small alone would suffice) | 55.9% |
| Realized classifier small-share (fraction routed small AND small succeeds) | 50.0% |

**The 78.6% actual model-selection accuracy sits almost exactly at the 77.3%/78.0% human-tier
figure.** That is itself informative: the human labels, for all their fuzziness on task
*framing* (F-7), turn out to correlate well with which model actually succeeds. The earlier
worry — that low classifier-label agreement might not reflect real routing failures at all —
is **not** what happened here; agreement-with-labels and agreement-with-outcomes landed in
the same place.

## Judge reliability — read this before trusting the two rows above that depend on it

| Control | Result |
|---|---|
| `verdict` (relative, pairwise "which is better") position-bias flip rate | 11.5% (n=26) |
| **`meets_bar` for the SMALL model's response**, position-bias flip rate | **0.0%** (n=23) |
| **`meets_bar` for the LARGE model's response**, position-bias flip rate | **65.2%** (n=23) |
| Inter-judge agreement (`qwen38-27b` judging the same subsample) | 90.5% (n=21) |

Every headline metric above is built from `meets_bar`, never from `verdict` — that was a
deliberate design choice in SPEC-AFFINITY, and it is what saves this experiment: `verdict`
alone would have been unusable (its first, confound-contaminated reading was 76.5%; even the
corrected reading, 11.5%, is a *relative* judgment this experiment never relies on).

But the asymmetry in `meets_bar` itself is real and was not expected: judging whether the
**small** model's response is adequate is rock-stable regardless of which slot it's shown in.
Judging whether the **large** model's response is adequate flips nearly two-thirds of the
time on the identical response pair, position swapped. This was checked against the
staleness confound (fix #3) and persists on matched, current content — it is not an
artifact of the fixes above.

**Practical consequence**: `over_routing_wasted_capacity` (5.9%) depends only on
`small_meets_bar` — trustworthy. `under_routing_quality_risk` (11.9%) and
`achievable_oracle_small_share` (55.9%) depend on `large_meets_bar` for the cases where
small fails — these carry a real, disclosed reliability caveat. A plausible explanation is
that the judge (itself a member of the `ds4` family) is inconsistent evaluating its own
family's longer, more discursive output depending on where in the prompt it appears; that is
a hypothesis, not a finding — the finding is the 65.2% flip rate itself.

## Concrete examples

**Small model succeeded despite a "hard" human label:**
- `hp-complex-031` (COMPLEX, predicted MEDIUM): *"Rewrite this twelve page policy document
  into plain language without changing its meaning."* — a rewriting task, not a design task;
  consistent with F-7's framing-generalization finding, and here the small model's answer
  was judged adequate.
- `hp-reasoning-002` (REASONING, predicted REASONING, routed large, also judged adequate on
  small): *"Show that the square root of 3 cannot be written as a ratio of two integers."*

**Small model failed despite an "easy" human label:**
- `hp-simple-018` (SIMPLE): *"At what temperature does ethanol freeze?"* — a single verifiable
  fact; the small model's answer did not meet the bar. This is the kind of case a confidence
  floor could plausibly catch (a factual lookup failure), unlike F-7's confidently-wrong
  framing mismatches.
- `hp-medium-016` (MEDIUM): *"Show me a small JavaScript snippet that debounces a search
  input by 300ms."* — ordinarily comfortable small-model territory; failed here.

## What the mapping choice costs, on real outcomes (not human labels)

Cost function `L = λ_q · P(under-route) + λ_c · P(over-route)`, scored against the SAME
classifier output — this is scoring a decision rule, not re-tuning the classifier:

| Mapping | under-rate | over-rate | L (λ_q=5) | L (λ_q=10) |
|---|---:|---:|---:|---:|
| `MEDIUM→large` (safety-biased) | 1.7% | 25.4% | **0.339** | **0.424** |
| `MEDIUM→small` (deployed) | 11.9% | 5.9% | 0.653 | 1.246 |
| `REASONING`-only→large (cost-biased) | 20.3% | 5.9% | 1.076 | 2.093 |

On *observed outcomes* (not human labels), the safety-biased mapping is unambiguously
cheaper once quality risk is weighted at all seriously (λ_q ≥ 5) — the same qualitative
conclusion F-7 reached from label-agreement data, now confirmed against what actually
happens when the escalated prompts are answered for real.

## A / B / C — is this a model, a training-distribution, or an abstraction problem?

Per SPEC-AFFINITY's framing:

- **Not primarily (A), model capability.** The classifier's *label* agrees with the model
  outcome about as often as it agrees with the human tier (∼78% either way). A stronger
  backbone might sharpen the framing-generalization gap from F-7, but this experiment gives
  no evidence that raw capability is the bottleneck.
- **Leans (B), training-distribution / anchor coverage**, consistent with F-7: the anchors'
  narrow framing (technical system-design for COMPLEX, formal proofs for REASONING) is the
  more actionable target, and it is fixable without retraining (custom anchors, no rebuild).
- **Some (C), abstraction mismatch, and this experiment is the evidence for it**: 16.9% of
  prompts have *no* sufficient model at either size (even after removing the truncation
  artifact) — for those, a four-tier complexity label was never going to be the right
  question, because neither candidate model can answer them regardless of routing. That
  argues for the 0.2+ roadmap direction already sketched in F-7: model-affinity / outcome
  scoring as a complement to, not a replacement for, complexity classification.

## Honest limitations

- n=118 (of 128; 10 excluded for unparseable judge output, disclosed above).
- One operator, one homelab, one judge model family (ds4), not independently reproduced.
- The large-side `meets_bar` instability (65.2% flip) is disclosed, not resolved. Metrics
  depending on it should be read with that caveat attached every time they are cited.
- `max_tokens=1536` still truncated some fraction of responses (uncounted here); a
  genuinely unbounded budget was out of scope for a shared homelab (SPEC-BENCH §3).
