"""Shared helpers for the bench scenarios.

Everything here is deliberately small and explicit. The scenarios are the
methodology; hiding their construction behind clever helpers would make the
methodology unreviewable, which defeats the purpose.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import Request, assertion, key_for  # noqa: E402

# SPEC-K8S §1. The response `model` field is the upstream's own attribution of
# which backend served a request; SPEC-K8S §3.1 makes it the primary attribution
# source because llama.cpp ignores the request's `model` field entirely.
DEFAULT_MODEL_TO_CLUSTER = {
    "qwen38-27b": "small",
    "ds4-flash-0731": "large",
    # Stub upstreams used by the local scenarios name themselves after the
    # cluster they stand in for, so the same attribution code works unchanged.
    "small-stub": "small",
    "large-stub": "large",
    "general-stub": "general",
}

# SPEC-K8S §3 routing table. Intended tier -> cluster.
DEFAULT_LABEL_TO_CLUSTER = {
    "SIMPLE": "small",
    "MEDIUM": "small",
    "COMPLEX": "large",
    "REASONING": "large",
}

# A bank of short, common, mostly single-wordpiece words. Used to build prompts
# of a target length. Approximate by construction — see `sized_prompt`.
WORD_BANK = (
    "the quick brown fox jumps over a lazy dog while nine red trucks haul cold "
    "steel pipes past the old mill road where two grey cats watch the rain fall "
    "on the flat tin roof and the river runs wide and slow toward the south bay"
).split()


def sized_prompt(target_tokens, key):
    """A prompt of roughly `target_tokens` tokens, made unique by `key`.

    The length is APPROXIMATE and the report says so: one bank word is treated
    as one token, which holds for common short English words under a wordpiece
    tokenizer but is not exact. The returned metadata records the word and
    character counts so the actual size is recoverable from the records file
    rather than trusted.
    """
    words = []
    i = 0
    while len(words) < max(1, target_tokens - 4):
        words.append(WORD_BANK[i % len(WORD_BANK)])
        i += 1
    text = " ".join(words)
    # The uniqueness key is PREFIXED. The filter truncates to max_prompt_chars
    # from the end (SPEC §4.4), so a suffixed key would be the first thing cut
    # on a long prompt — silently turning a miss workload into a hit workload.
    text = "%s %s" % (key, text)
    return text, {"target_tokens": target_tokens, "words": len(words) + 1, "chars": len(text)}


def chat_body(prompt, model="bench-router", max_tokens=16, temperature=0.0, stream=False, pad_to=None):
    """An OpenAI-shaped chat completion request body.

    `pad_to` grows the JSON around the prompt to an exact byte size (B-2), by
    appending a filler field. The PROMPT is untouched by padding: B-2 must vary
    only the body size, never the classified text, or the delta measures two
    things at once.
    """
    doc = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    blob = json.dumps(doc).encode("utf-8")
    if pad_to is not None and pad_to > len(blob):
        # Reserve the exact overhead of the extra key, then size the filler.
        doc["bench_filler"] = ""
        overhead = len(json.dumps(doc).encode("utf-8"))
        fill = max(0, pad_to - overhead)
        doc["bench_filler"] = "x" * fill
        blob = json.dumps(doc).encode("utf-8")
    return blob


def sc_header(record, suffix):
    return record.get("sc_headers", {}).get("x-llm-d-sc-" + suffix)


def distinct_prompt_count(records):
    return len({r["meta"].get("prompt_key") for r in records})


def observed_clusters(records, label_to_cluster, model_to_cluster):
    """Attribute each record to a cluster from BOTH independent sources.

    Returns (from_headers, from_model, disagreements). SPEC-K8S §3.1 requires
    the provenance headers and the upstream's own `model` field to agree; a
    disagreement is a routing bug or an attribution bug and either way the
    scenario has not verified its premise.
    """
    from_headers = []
    from_model = []
    disagreements = []
    for r in records:
        label = sc_header(r, "label")
        hdr_cluster = label_to_cluster.get(label) if label else None
        model_cluster = model_to_cluster.get(r.get("model")) if r.get("model") else None
        from_headers.append(hdr_cluster)
        from_model.append(model_cluster)
        if hdr_cluster is not None and model_cluster is not None and hdr_cluster != model_cluster:
            disagreements.append(
                {"i": r["i"], "label": label, "header_cluster": hdr_cluster,
                 "model": r.get("model"), "model_cluster": model_cluster}
            )
    return from_headers, from_model, disagreements


def assert_classification_happened(result, expected_status="OK"):
    """The routing scenarios' shared premise check (SPEC-BENCH §0 rule 3).

    A routing measurement is worthless unless the requests were actually
    classified. The evidence is the `x-llm-d-sc-status` header the filter set on
    the upstream request; the stub upstream must be started with
    `--echo-sc-headers` for it to reach the client at all.
    """
    records = result.ok_records()
    statuses = {}
    for r in records:
        statuses[sc_header(r, "status") or "<absent>"] = statuses.get(sc_header(r, "status") or "<absent>", 0) + 1
    seen = statuses.get(expected_status, 0)
    if "<absent>" in statuses and len(statuses) == 1:
        return assertion(
            "classification_observed",
            False,
            "no x-llm-d-sc-* headers reached the client on any of %d requests. The filter "
            "sets provenance on the UPSTREAM request, so the upstream must echo them: start "
            "bench/stub_upstream.py with --echo-sc-headers. Without this the scenario cannot "
            "verify that classification happened, so it must not be published." % len(records),
        )
    return assertion(
        "classification_observed",
        seen == len(records) and len(records) > 0,
        "x-llm-d-sc-status was %s on %d/%d measured requests; full tally %s"
        % (expected_status, seen, len(records), statuses),
    )


def assert_no_classification(result):
    """The baseline arm's premise: no filter ran, so no provenance exists.

    `x-llm-d-sc-echo` is the STUB's own bookkeeping -- it reports how many
    provenance headers the stub received, and it is present (as "0") even when
    the answer is none. It shares the `x-llm-d-sc-` prefix, so a naive "did any
    prefixed header come back" test is true for every baseline response and this
    assertion failed 3000/3000 times against a baseline that was, in fact,
    perfectly unclassified. Count real provenance only.
    """
    records = result.ok_records()

    def provenance_count(rec):
        headers = rec.get("sc_headers") or {}
        echoed = headers.get("x-llm-d-sc-echo")
        if echoed is not None:
            # The stub counted them at the point of receipt: authoritative.
            try:
                return int(echoed)
            except (TypeError, ValueError):
                pass
        return sum(1 for k in headers if k != "x-llm-d-sc-echo")

    with_headers = sum(1 for r in records if provenance_count(r) > 0)
    return assertion(
        "baseline_is_unclassified",
        with_headers == 0,
        "%d/%d baseline responses carried x-llm-d-sc-* provenance (expected 0; a non-zero "
        "count means the baseline arm went through the filter and the delta is meaningless)"
        % (with_headers, len(records)),
    )


def assert_cache_discipline(result, cache_mode):
    """Prove the arm's cache workload was what it claimed, from the requests sent.

    Ported in spirit from `llm-d-sc/src/bench.rs`, which asserts against the
    service's own hit/miss counters. The filter deliberately has no cache of its
    own (SPEC §7) and llm-d-sc's counters are not exposed to this harness, so
    the strongest CLIENT-side check is key discipline: a hit arm must repeat
    exactly one key, a miss arm must never repeat one. The latency delta
    assertion in B-1 is what actually confirms the cache responded.
    """
    records = result.records
    distinct = distinct_prompt_count(records)
    if cache_mode == "hit":
        return assertion(
            "hit_arm_uses_one_key",
            distinct == 1,
            "%d distinct prompt keys across %d measured requests (expected exactly 1)"
            % (distinct, len(records)),
        )
    return assertion(
        "miss_arm_keys_are_unique",
        distinct == len(records),
        "%d distinct prompt keys across %d measured requests (expected all distinct; a "
        "repeat would be served from llm-d-sc's cache and would not be a miss)"
        % (distinct, len(records)),
    )


def make_builder(cache_mode, seed="", model="bench-router", max_tokens=16, pad_to=None,
                 target_tokens=24, path="/v1/chat/completions", extra_meta=None):
    """Build the per-request factory the driver calls.

    The prompt key comes from `harness.key_for`, which reproduces the
    `run_id`-namespaced warm/measure split from `llm-d-sc/src/bench.rs`.
    """

    def build(index, phase, run_id):
        key = key_for(cache_mode, phase, run_id, index, seed=seed)
        prompt, size_meta = sized_prompt(target_tokens, key)
        body = chat_body(prompt, model=model, max_tokens=max_tokens, pad_to=pad_to)
        meta = {"prompt_key": key, "phase": phase}
        meta.update(size_meta)
        if extra_meta:
            meta.update(extra_meta)
        return Request(path=path, body=body, meta=meta)

    return build
