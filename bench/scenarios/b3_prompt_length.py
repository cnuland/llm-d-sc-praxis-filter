"""B-3 — Prompt-length sensitivity (SPEC-BENCH §1).

Prompt lengths 32 / 64 / 128 / 256 / 512 tokens, cache-miss, concurrency 1 and
4. This deliberately mirrors llm-d-sc's own published table
(`upstream-staging/docs/performance.md`, "Cache misses, end to end over gRPC")
so the two are directly comparable: the same lengths, the same concurrencies,
the same nearest-rank percentile definition, the same warmup exclusion.

What the comparison buys: llm-d-sc's table is the classifier measured from a
dummy gateway. This table is the same classifier measured through Praxis. The
difference between them is the gateway, and it is the only honest way to say
how much of the added latency is the model forward growing with input length
versus the proxy.

The lengths are APPROXIMATE. Prompts are built from a bank of short common
words on a one-word-one-token assumption; the records file carries the actual
word and character counts so the approximation is auditable rather than
asserted.

Run:
    python3 bench/harness.py --scenario b3 --target http://127.0.0.1:8081 \
        --warmup 100 --measured 300 --concurrency 1,4
"""

from __future__ import annotations

from _common import assert_cache_discipline, assert_classification_happened, make_builder
from harness import Arm, assertion

SPEC_ID = "B-3"
DESCRIPTION = "Prompt-length sensitivity at 32/64/128/256/512 tokens, cache-miss."
TARGETS_REAL_MODELS = False
NOTES = [
    "Lengths mirror llm-d-sc's own performance table so the two are directly comparable.",
    "Token counts are approximate (one bank word treated as one token); the records file "
    "carries actual word and character counts.",
    "SPEC §4.2 truncates the classified text at max_prompt_chars (default 4096). The 512-token "
    "arm sits well inside that, so no arm here is silently truncated.",
]

LENGTHS = [32, 64, 128, 256, 512]


def _length_assertion(result, target_tokens):
    words = {r["meta"].get("words") for r in result.records}
    chars = [r["meta"].get("chars") for r in result.records if r["meta"].get("chars")]
    return assertion(
        "prompt_length_as_intended",
        len(words) == 1 and abs(next(iter(words)) - target_tokens) <= max(4, target_tokens * 0.05),
        "every prompt carried %s words (~%d tokens targeted); character length %d..%d"
        % (sorted(w for w in words), target_tokens, min(chars or [0]), max(chars or [0])),
    )


def arms(cfg):
    classified_url = cfg.param("classified_url", cfg.target)
    lengths = [int(x) for x in cfg.param("lengths", "", str).split(",") if x.strip()] or LENGTHS
    out = []
    for conc in cfg.concurrency:
        for n in lengths:
            out.append(
                Arm(
                    name="classified-miss@%dtok-c%d" % (n, conc),
                    target=classified_url,
                    # The seed keeps each length in its own key namespace, so a
                    # 32-token request can never be served from a 64-token key.
                    build=make_builder("miss", seed="len%d" % n, target_tokens=n,
                                       extra_meta={"arm_kind": "classified-miss", "target_tokens": n}),
                    params={"prompt_tokens": n, "chain": "llm_d_sc -> load_balancer"},
                    warmup=cfg.warmup, measured=cfg.measured, concurrency=conc,
                    cache_mode="miss",
                    assertions=lambda result, ctx, n=n: [
                        assert_classification_happened(result),
                        assert_cache_discipline(result, "miss"),
                        _length_assertion(result, n),
                    ],
                    notes="Cache-miss: every request pays a full llm-d-sc model forward.",
                )
            )
    return out
