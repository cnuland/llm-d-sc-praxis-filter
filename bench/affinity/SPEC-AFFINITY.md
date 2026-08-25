# SPEC-AFFINITY — does the *cheap model actually succeed*?

Replaces the human complexity tier with **observed model outcomes** as the routing
target. This is the experiment that tells us whether we have a mediocre complexity
classifier, or whether generic prompt complexity is the wrong abstraction for model
routing.

## The question being corrected

B-4 measured **77.3% routing accuracy**. That number means:

> 77.3% agreement with a human-authored complexity-tier → model mapping.

It does **not** mean:

> Qwen would have failed 22.7% of those requests.

Those are different claims and only the second one matters operationally. A prompt
labelled COMPLEX that Qwen answers perfectly is not a routing error. A prompt labelled
MEDIUM that Qwen consistently botches *is* one, even though the label was arguably right.

So: forget the human labels for the routing target. Ask both models, judge both answers,
and define the target as **the cheapest model that meets the quality bar**.

## Frozen inputs — nothing here may be tuned

- `bench/prompts/complexity-heldout.json`, sha256 `93e62ce5…`, `chmod 444`
- classifier anchors, taxonomy, thresholds, model weights: **unchanged**
- No prompt is edited, no label revised, no anchor added, for any reason, until this
  evaluation is complete and reported.

## Design

### Stage 1 — generation (256 calls)

Every one of the 128 prompts goes to **both** models, independent of what the classifier
said:

| role | endpoint | model id |
|---|---|---|
| small | `llama-server-qwen38.homelab-maas.svc:80` | `qwen38-27b` |
| large | `llama-server-ds4.homelab-maas.svc:8080` | `ds4-flash-0731` (needs bearer token) |

Identical generation settings for both, recorded in the manifest:
`temperature: 0`, `max_tokens: 512`, `stream: false`, no system prompt.

**Concurrency: 1 per backend, 2 across backends.** They are separate single-replica pods
each running `--parallel 1`, so one in-flight request each is exactly the intended load
and halves wall time. Never two in flight to the same backend.

Checkpoint after every prompt. A 2-hour run that loses everything on an interruption is
a run that will not be repeated.

### Stage 2 — judging

The hard part, and the part most likely to be wrong. Be honest about it.

**Primary judge: `ds4-flash-0731`.** One call per prompt, seeing both answers.

Controls, because a 284 B model judging its own output against a 27 B model's is exactly
the setup self-preference bias was named for:

1. **Blind.** Answers are presented as "Response 1" / "Response 2". Never name a model.
2. **Order randomised** per prompt, seeded and recorded, so position bias does not align
   with model identity.
3. **Position-bias control:** re-judge a ≥30-prompt subsample with the order swapped.
   Report how often the verdict flips. A high flip rate invalidates the pairwise verdict
   (though not necessarily the absolute pass/fail).
4. **Inter-judge control:** have `qwen38-27b` judge the same subsample. Report agreement
   with the primary judge. Disagreement is reported, never averaged away.

The judge returns **strict JSON**:

```json
{
  "response_1": {"score": 1-5, "meets_bar": true|false, "why": "<20 words"},
  "response_2": {"score": 1-5, "meets_bar": true|false, "why": "<20 words"},
  "verdict": "1" | "2" | "tie",
  "verifiable": true|false
}
```

`meets_bar` is the operative field: *"does this response adequately and correctly answer
the request, such that a user would not need to re-ask a stronger model?"* The routing
target is built from `meets_bar`, not from `verdict` — "the big model wrote something
nicer" is not the same as "the small model failed".

`verifiable` flags prompts with a single objectively checkable answer, so those can be
separated out and, where practical, checked deterministically. A judged score on
"what is 12 × 14" is not evidence of anything.

Retry a malformed JSON response at most twice, then record the prompt as
`judge_failed` and **exclude it from headline rates**, counted and reported separately.

### Stage 3 — the per-prompt matrix

One row per prompt, written as JSONL so it can be re-analysed without re-running:

| column | source |
|---|---|
| `prompt_id`, `prompt`, `domain`, `boundary` | frozen dataset |
| `human_tier` | frozen dataset |
| `predicted_tier`, `score`, `margin` | llm-d-sc (already captured; margin = top − second) |
| `classifier_route` | `predicted_tier` → configured mapping |
| `small_score`, `small_meets_bar` | judge |
| `large_score`, `large_meets_bar` | judge |
| `verdict`, `verifiable`, `order_seed` | judge |
| `cheapest_sufficient` | `small` if `small_meets_bar` else `large` if `large_meets_bar` else `neither` |
| `small_wall_s`, `large_wall_s`, `*_completion_tokens` | stage 1 |

## Metrics — reported separately, never averaged

| metric | definition |
|---|---|
| exact four-tier accuracy | `predicted_tier == human_tier` |
| human tier→route agreement | `classifier_route == route(human_tier)` (the old 77.3%) |
| **actual model-selection accuracy** | `classifier_route == cheapest_sufficient` |
| **under-routing / quality risk** | routed `small` **and** `small_meets_bar == false` **and** `large_meets_bar == true` |
| **over-routing / wasted capacity** | routed `large` **and** `small_meets_bar == true` |
| achievable oracle savings | fraction where `cheapest_sufficient == small` |
| realized classifier savings | fraction routed `small` **and** `small_meets_bar == true` |
| neither-model-suffices | fraction where both fail — not a routing error, a capability gap |

Also report the **cost function** the user proposed, which is the honest way to compare
mappings when the two error types have different weights:

```
L = λ_q · P(under-route) + λ_c · P(over-route),   λ_q >> λ_c
```

Tabulate `L` for λ_q/λ_c ∈ {1, 5, 10, 50} across the candidate mappings
(`MEDIUM→small`, `MEDIUM→large`, `REASONING-only→large`) **and** against the oracle.
Since the classifier output is fixed, this is scoring, not tuning.

## What this can and cannot conclude

It **can** distinguish:

- **(A) the model is not capable enough** — a stronger backbone would separate the
  classes better
- **(B) the training distribution is too narrow** — the model is fine, the anchors are
  not diverse enough in *framing* (F-7 already points here)
- **(C) four-class prompt complexity is the wrong abstraction** — the tiers do not
  predict model success at all, and routing needs candidate-conditioned model-affinity
  scoring instead

The discriminating signal: if `actual model-selection accuracy` is **much higher** than
77.3%, the human tiers were the problem and the classifier is fine for routing. If it is
**about the same or worse**, complexity tiers genuinely do not predict which model
succeeds, which is finding (C) and is far more consequential than either.

It **cannot** conclude anything about llm-d-sc as a runtime; that is measured elsewhere
and looks good. And it rests on an LLM judge, whose biases are controlled for but not
eliminated — every headline number carries that caveat.

## Cost

Measured pilot at `max_tokens: 512`: qwen38 **32.2 s**, ds4 **46.5 s**.

- Stage 1: 128 prompts, both backends in parallel → bounded by ds4 ≈ **1.7 h**
- Stage 2: 128 judge calls, long prefill but short output (~200 tokens) ≈ **0.6 h**
- Stage 3 controls: ~60 extra judge calls ≈ **0.3 h**

**≈ 2.5 h wall clock.** Checkpoint everything. If a backend starts erroring or slows
sharply, stop and report — a degraded homelab is worse than a missing benchmark.
