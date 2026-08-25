"""B-2 — Body-size sensitivity (SPEC-BENCH §1).

*`StreamBuffer` forces the whole request body to be buffered before routing.
What does that cost?*

SPEC §4.3 makes the filter declare `BodyMode::StreamBuffer`, which causes Praxis
to `pre_read_body()` the ENTIRE body into one frozen chunk before the header
pipeline runs (SPEC §2.2). That is a real architectural cost of body-derived
routing and it belongs in the #1017 discussion measured rather than hand-waved.

Method: sweep the body size while holding the PROMPT at a fixed length, so only
the surrounding JSON grows. Two arms per size — `baseline` (Stream mode, no body
access) and `classified` — and the delta at each size is the buffering cost.

The distinction that makes this valid: the classified text is byte-identical
across every size, so the classifier's own cost is constant and everything that
moves is buffering. The padding lives in a separate JSON field, never inside
`messages[].content`.

Run:
    python3 bench/harness.py --scenario b2 --target http://127.0.0.1:8081 \
        --param baseline_url=http://127.0.0.1:8080 \
        --warmup 100 --measured 500 --concurrency 1
"""

from __future__ import annotations

from _common import (
    assert_cache_discipline,
    assert_classification_happened,
    assert_no_classification,
    make_builder,
)
from harness import assertion, Arm

SPEC_ID = "B-2"
DESCRIPTION = "Body-size sensitivity: the cost of StreamBuffer whole-body buffering."
TARGETS_REAL_MODELS = False
NOTES = [
    "The prompt length is held constant across every size; only the surrounding JSON grows.",
    "SPEC §4.2 defaults max_body_bytes to 1 MiB, so the 1 MB arm sits at the configured "
    "ceiling. A body above it is answered 413 by design and is not part of this sweep.",
]

SIZES = [1024, 8192, 65536, 262144, 1048576]
SIZE_LABELS = {1024: "1 KB", 8192: "8 KB", 65536: "64 KB", 262144: "256 KB", 1048576: "1 MB"}
PROMPT_TOKENS = 32
# The padded body must land within this fraction of its target for the sweep
# axis to mean anything.
SIZE_TOLERANCE = 0.02


def _size_assertion(result, target_bytes):
    sizes = [r["req_bytes"] for r in result.records]
    if not sizes:
        return assertion("body_size_as_intended", False, "no measured records")
    lo, hi = min(sizes), max(sizes)
    within = abs(hi - target_bytes) <= target_bytes * SIZE_TOLERANCE and \
        abs(lo - target_bytes) <= target_bytes * SIZE_TOLERANCE
    return assertion(
        "body_size_as_intended",
        within,
        "request bodies ranged %d..%d bytes against a %d byte target (tolerance %.0f%%)"
        % (lo, hi, target_bytes, SIZE_TOLERANCE * 100),
    )


def arms(cfg):
    baseline_url = cfg.param("baseline_url", None)
    classified_url = cfg.param("classified_url", cfg.target)
    conc = cfg.concurrency[0] if cfg.concurrency else 1
    sizes = [int(s) for s in cfg.param("sizes", "", str).split(",") if s.strip()] or SIZES

    out = []
    for size in sizes:
        label = SIZE_LABELS.get(size, "%d B" % size)
        params = {"body_bytes": size, "body_label": label, "prompt_tokens": PROMPT_TOKENS}
        if baseline_url:
            out.append(
                Arm(
                    name="baseline@%s" % label.replace(" ", ""),
                    target=baseline_url,
                    build=make_builder("miss", seed="b2", target_tokens=PROMPT_TOKENS, pad_to=size,
                                       extra_meta={"arm_kind": "baseline", "body_bytes": size}),
                    params=dict(params, chain="router -> load_balancer", body_mode="Stream"),
                    warmup=cfg.warmup, measured=cfg.measured, concurrency=conc,
                    cache_mode="n/a",
                    assertions=lambda result, ctx, s=size: [
                        assert_no_classification(result),
                        _size_assertion(result, s),
                    ],
                    notes="No body access, so Praxis never buffers the whole body.",
                )
            )
        out.append(
            Arm(
                name="classified@%s" % label.replace(" ", ""),
                target=classified_url,
                build=make_builder("miss", seed="b2", target_tokens=PROMPT_TOKENS, pad_to=size,
                                   extra_meta={"arm_kind": "classified", "body_bytes": size}),
                params=dict(params, chain="llm_d_sc -> load_balancer", body_mode="StreamBuffer"),
                warmup=cfg.warmup, measured=cfg.measured, concurrency=conc,
                cache_mode="miss",
                assertions=lambda result, ctx, s=size: [
                    assert_classification_happened(result),
                    assert_cache_discipline(result, "miss"),
                    _size_assertion(result, s),
                ],
                notes="StreamBuffer: the whole body is drained before routing.",
            )
        )
    return out


def cross_arm_assertions(ctx):
    """The classified text must be identical across sizes, or the sweep is confounded."""
    out = []
    results = ctx["results"]
    classified = {n: r for n, r in results.items() if n.startswith("classified@")}
    token_targets = set()
    for res in classified.values():
        token_targets.update(r["meta"].get("target_tokens") for r in res.records)
    for name in classified:
        out.append((
            name,
            [assertion(
                "prompt_held_constant_across_sizes",
                len(token_targets) == 1,
                "prompt target length across every size arm: %s (must be a single value, or the "
                "sweep varies two things at once)" % sorted(token_targets),
            )],
        ))
    return out
