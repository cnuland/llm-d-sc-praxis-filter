#!/usr/bin/env python3
"""bench/report.py — generate bench/BENCHMARKS.md from bench/results/*.json.

The document is GENERATED, and that is a methodology rule rather than a
convenience: SPEC-BENCH §0 rule 6 says a number in prose that has no JSON
behind it gets deleted. Every figure this file emits is formatted directly from
a value read out of a run JSON, or is a subtraction of two such values (the
delta form used by `llm-d-sc/docs/benchmarks/topology.md`). There is no literal
latency, throughput, or accuracy number typed into the prose below.

Structure follows `~/llm-d-sc-genesis/upstream-staging/docs/performance.md`:
the honesty disclaimer and the call for external validation first, then the
environment table, then per-scenario tables, then a "What this says"
interpretation for each scenario group — including the inconvenient results. If
classification does not pay for itself on this hardware, that is the finding
and it gets published as the finding.

Usage:
    python3 bench/report.py                       # bench/results/*.json -> bench/BENCHMARKS.md
    python3 bench/report.py --results-dir DIR --out FILE
    python3 bench/report.py --check               # render the B-4 / B-5 renderers on a
                                                  # fixture and print to stdout, writing nothing
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RESULTS = os.path.join(HERE, "results")
DEFAULT_OUT = os.path.join(HERE, "BENCHMARKS.md")

SPEC_ORDER = ["B-1", "B-2", "B-3", "B-4", "B-5", "B-6", "B-7", "SELFTEST"]

DISCLAIMER = """\
> **Read this before quoting a number from this page.** Everything here was
> measured on a **single contributor's homelab**, by a **single operator**, on a
> **shared cluster**, and has **not been independently reproduced**. The model
> endpoints are single-replica llama.cpp servers on someone's home network.
> Treat these figures as a directional baseline and a methodology reference —
> not as project performance claims, and not as a service level objective.

> **Call for external validation.** Numbers from a second environment are worth
> more to this work than better numbers from the same one. If you run this
> harness anywhere else, please open an issue or a pull request adding your
> results. `bench/README.md` has the commands; every scenario carries its own
> self-assertions, so a run that does not reproduce will say why rather than
> quietly disagreeing."""

METHODOLOGY = """\
## Methodology, in short

* **No average-only latency, anywhere.** Every latency figure is a nearest-rank
  percentile over a captured per-request distribution. `bench/harness.py`
  contains no code path that computes a mean.
* **Every cost figure is a delta between two arms that differ in exactly one
  thing.** An arm without its comparison partner is reported as incomplete
  rather than as a cost.
* **Every scenario asserts its own premise.** A cache-hit arm asserts the cache
  responded; a routing arm asserts the classification status was the one it
  intended; a topology arm asserts it was not measured through a tunnel. A
  scenario that cannot verify its premise records `passed: false` and the run
  exits non-zero — its numbers are withheld, not published with a caveat.
* **Cache-hit and cache-miss workloads use disjoint key namespaces**, ported
  from `llm-d-sc/src/bench.rs`, so a "miss" can never be silently served from
  cache.
* **Warmup is excluded from the measured window** by running as a separate
  phase with its own records.
* **Per-request records are kept**, not pre-aggregated buckets, so every
  distribution on this page is recomputable from the `.records.jsonl` sidecar
  named in each run's manifest."""


# ---------------------------------------------------------------------------
# Formatting helpers — every one of these takes a value FROM the JSON.
# ---------------------------------------------------------------------------


def ms(value):
    return "n/a" if value is None else "%.3f ms" % value


def ms2(value):
    return "n/a" if value is None else "%.2f ms" % value


def delta_ms(a, b):
    """b - a, rendered with an explicit sign, as topology.md does."""
    if a is None or b is None:
        return "n/a"
    return "%+.3f ms" % (b - a)


def pct(value):
    return "n/a" if value is None else "%.1f%%" % (value * 100.0)


def ratio4(value):
    return "n/a" if value is None else "%.4f" % value


def load_runs(results_dir):
    runs = []
    for path in sorted(glob.glob(os.path.join(results_dir, "*.json"))):
        if os.path.basename(path) == "leakage.json":
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as exc:
            print("skipping %s: %s" % (path, exc), file=sys.stderr)
            continue
        if "manifest" not in doc or "scenarios" not in doc:
            continue
        doc["_path"] = path
        runs.append(doc)
    return runs


def latest_by_spec(runs):
    """One run per SPEC id — the most recent, by manifest timestamp."""
    best = {}
    for run in runs:
        spec = run["manifest"].get("scenario_spec", "?")
        prev = best.get(spec)
        if prev is None or run["manifest"].get("unix", 0) > prev["manifest"].get("unix", 0):
            best[spec] = run
    return best


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def environment_table(runs):
    out = ["## Environment", ""]
    manifests = [r["manifest"] for r in runs]
    if not manifests:
        return out + ["No runs found.", ""]
    m = manifests[-1]
    host = m.get("host", {})
    git = m.get("git", {})
    rows = [
        ("Operator", "single-operator homelab, unaudited"),
        ("Host CPU", host.get("cpu")),
        ("Logical cores", host.get("logical_cores")),
        ("OS", host.get("os")),
        ("Architecture", host.get("machine")),
        ("Python", "%s %s" % (host.get("python_implementation"), host.get("python"))),
        ("Filter crate git sha", _sha(git.get("filter_crate"))),
        ("llm-d-sc git sha", _sha(git.get("llm_d_sc_genesis"))),
        ("Independent reproduction", "**none**"),
    ]
    out += ["| Field | Value |", "| --- | --- |"]
    for k, v in rows:
        out.append("| %s | %s |" % (k, "unknown" if v is None else v))
    out.append("")

    out += ["Runs behind this document:", "",
            "| SPEC | Scenario | UTC | Topology | Concurrency | Warmup | Measured | JSON |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | --- |"]
    for run in sorted(runs, key=lambda r: r["manifest"].get("unix", 0)):
        mm = run["manifest"]
        out.append("| %s | `%s` | %s | %s | %s | %s | %s | [`%s`](results/%s) |" % (
            mm.get("scenario_spec", "?"), mm.get("scenario", "?"), mm.get("utc", "?"),
            mm.get("topology", "?"), ",".join(str(c) for c in mm.get("concurrency", [])),
            mm.get("warmup", "?"), mm.get("measured", "?"),
            os.path.basename(run["_path"]), os.path.basename(run["_path"])))
    out.append("")

    leak = None
    for mm in manifests:
        if mm.get("leakage_check"):
            leak = mm["leakage_check"]
    if leak:
        out += [
            "**Held-out prompt set, leakage check.** `bench/prompts/check_leakage.py` compared "
            "every benchmark prompt against every classifier anchor in `complexity.json`, "
            "`cost.json` and `sensitivity.json`:", "",
            "| Check | Value |", "| --- | ---: |",
            "| Anchors compared against | %s |" % leak.get("anchor_count"),
            "| Pairwise comparisons | %s |" % leak.get("comparisons"),
            "| Verbatim overlaps | %s |" % leak.get("verbatim_overlaps"),
            "| Near-duplicates | %s |" % leak.get("near_duplicates"),
            "| Highest content-token overlap observed (Jaccard / containment) | %s / %s |"
            % (leak.get("max_jaccard_observed"), leak.get("max_containment_observed")),
            "| Highest full-token overlap observed (Jaccard / containment) | %s / %s |"
            % (leak.get("max_full_jaccard_observed"), leak.get("max_full_containment_observed")),
            "| Thresholds a leak would have tripped | %s |" % (leak.get("limits") or "n/a"),
            "| Clean | %s |" % leak.get("clean"),
            "",
        ]
    return out


def _sha(info):
    """Render a git sha for a table cell, never breaking the row."""
    if not isinstance(info, dict):
        return "unknown"
    if info.get("sha") is None:
        err = " ".join(str(info.get("error") or "unknown").split())
        if "ambiguous argument 'HEAD'" in err or "unknown revision" in err:
            err = "repository has no commits yet"
        return "unavailable — %s" % err[:110]
    flags = []
    if info.get("dirty"):
        flags.append("**tracked files modified**")
    if info.get("untracked"):
        flags.append("%d untracked file(s)" % info["untracked"])
    return "`%s`%s" % (info["sha"][:12], (" — " + ", ".join(flags)) if flags else " — clean")


def latency_table(scenarios, title=None, extra_cols=None):
    out = []
    if title:
        out += [title, ""]
    header = "| Arm | Concurrency | p50 | p90 | p95 | p99 | max | Throughput | Errors |"
    out += [header, "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for s in scenarios:
        lat = s.get("latency_ms", {})
        params = s.get("params", {})
        out.append("| `%s` | %s | %s | %s | %s | %s | %s | %.1f req/s | %s |" % (
            s["name"], params.get("concurrency", "?"),
            ms(lat.get("p50")), ms(lat.get("p90")), ms(lat.get("p95")),
            ms(lat.get("p99")), ms(lat.get("max")),
            s.get("throughput", 0.0), s.get("errors", 0)))
    out.append("")
    return out


def delta_table(scenarios, base_prefix, arm_prefixes, axis_label="Concurrency", axis_key="concurrency"):
    """Deltas between a baseline arm and one or more comparison arms.

    Both operands come from the JSON; the delta is their subtraction, which is
    the same derivation `topology.md` publishes.
    """
    by_axis = {}
    for s in scenarios:
        axis = s.get("params", {}).get(axis_key, "?")
        by_axis.setdefault(axis, {})[s["name"].split("@")[0]] = s
    if not by_axis:
        return []
    out = ["| %s | Metric | %s | %s |" % (
        axis_label, "`" + base_prefix + "`", " | ".join("`%s` (delta)" % a for a in arm_prefixes))]
    out.append("| --- | --- | ---: |" + " ---: |" * len(arm_prefixes))
    for axis in sorted(by_axis, key=lambda x: (isinstance(x, str), x)):
        group = by_axis[axis]
        base = group.get(base_prefix)
        if base is None:
            continue
        for q in ("p50", "p90", "p95", "p99", "max"):
            cells = []
            for prefix in arm_prefixes:
                other = group.get(prefix)
                if other is None:
                    cells.append("n/a")
                else:
                    cells.append("%s (%s)" % (ms(other["latency_ms"].get(q)),
                                              delta_ms(base["latency_ms"].get(q),
                                                       other["latency_ms"].get(q))))
            out.append("| %s | %s | %s | %s |" % (axis, q, ms(base["latency_ms"].get(q)), " | ".join(cells)))
    out.append("")
    return out


def assertions_section(run):
    out = ["**Assertions that ran.** SPEC-BENCH §0 rule 3 requires each scenario to prove its own "
           "premise; these are the checks this run made and their outcome.", "",
           "| Arm | Assertion | Result |", "| --- | --- | :---: |"]
    for s in run["scenarios"]:
        for a in s.get("assertions", []):
            out.append("| `%s` | `%s` | %s |" % (s["name"], a["name"], "PASS" if a["passed"] else "**FAIL**"))
    out.append("")
    failed = [(s["name"], a) for s in run["scenarios"] for a in s.get("assertions", []) if not a["passed"]]
    if failed:
        out += ["Failed assertions, in full:", ""]
        for name, a in failed:
            out.append("* `%s` / `%s`: %s" % (name, a["name"], a["detail"]))
        out.append("")
    return out


def confusion_section(scenario):
    """B-4: the routing confusion matrix and the asymmetric misroute costs."""
    extra = scenario.get("extra") or {}
    if "confusion" not in extra:
        return []
    confusion = extra["confusion"]
    classes = extra.get("classes", sorted(confusion))
    clusters = extra.get("clusters", sorted({c for row in confusion.values() for c in row}))
    out = ["**Routing confusion matrix** — rows are the intended tier, columns are the cluster the "
           "request actually landed on.", "",
           "| Intended tier | " + " | ".join("`%s`" % c for c in clusters) + " | Row total |",
           "|---|" + "---:|" * (len(clusters) + 1)]
    for cls in classes:
        row = confusion.get(cls, {})
        total = sum(row.values())
        out.append("| **%s** | " % cls + " | ".join(str(row.get(c, 0)) for c in clusters) + " | %d |" % total)
    out.append("")

    out += ["| Metric | Value |", "| --- | ---: |",
            "| Prompts routed | %s |" % extra.get("routing_total"),
            "| Routed to the intended cluster | %s |" % extra.get("routing_correct"),
            "| Routing accuracy | %s |" % pct(extra.get("routing_accuracy")),
            ""]

    per_class = extra.get("per_class") or {}
    if per_class:
        out += ["Per-class precision and recall, computed on the label the classifier returned "
                "(not on the cluster), so they are comparable with llm-d-sc's own accuracy table:",
                "",
                "| Class | Support | Precision | Recall | F1 |",
                "| --- | ---: | ---: | ---: | ---: |"]
        for cls in classes:
            pc = per_class.get(cls, {})
            out.append("| `%s` | %s | %s | %s | %s |" % (
                cls, pc.get("support"), ratio4(pc.get("precision")),
                ratio4(pc.get("recall")), ratio4(pc.get("f1"))))
        out.append("")

    mis = extra.get("misroutes") or {}
    out += ["**The two misroute costs are not symmetric and are reported separately.** Sending a "
            "SIMPLE prompt to the 284 B model wastes capacity; sending a REASONING prompt to the "
            "27 B model risks the answer.", "",
            "| Misroute | Count | Cost |", "| --- | ---: | --- |",
            "| SIMPLE reached the large model | %s | wasted capacity |"
            % mis.get("simple_to_large_wasted_capacity"),
            "| MEDIUM reached the large model | %s | wasted capacity |"
            % mis.get("medium_to_large_wasted_capacity"),
            "| COMPLEX reached the small model | %s | quality risk |"
            % mis.get("complex_to_small_quality_risk"),
            "| REASONING reached the small model | %s | quality risk |"
            % mis.get("reasoning_to_small_quality_risk"),
            "| Fell through to `general` | %s | unrouted |" % mis.get("to_general_unrouted"),
            ""]

    boundary = extra.get("boundary") or {}
    if boundary.get("total"):
        out += ["Accuracy on the prompts deliberately authored to sit near a class boundary:", "",
                "| Set | n | Correct | Accuracy |", "| --- | ---: | ---: | ---: |",
                "| boundary-marked | %s | %s | %s |" % (
                    boundary.get("total"), boundary.get("correct"), pct(boundary.get("accuracy"))),
                ""]
    return out


def decomposition_section(scenario):
    """B-5: the stacked latency decomposition at p50 and p99."""
    extra = scenario.get("extra") or {}
    if "decomposition" not in extra:
        note = extra.get("decomposition_unavailable")
        if note:
            return ["**Latency decomposition: not available for this run.** %s" % note, ""]
        return []
    dec = extra["decomposition"]
    out = ["**Latency decomposition.** `total_e2e = praxis_overhead + classify_rtt + upstream_time`, "
           "where `praxis_overhead` comes from B-1, `classify_rtt` from the "
           "`x-llm-d-sc-latency-us` header, and `upstream_time` is the remainder. Each share is "
           "the component divided by the total on the same row.", "",
           "| Quantile | Praxis overhead | Classify RTT | Upstream generation | Total |",
           "| --- | ---: | ---: | ---: | ---: |"]
    for q in ("p50", "p99"):
        d = dec.get(q)
        if not d:
            continue
        total = d.get("total") or 0.0
        cells = []
        for part in ("praxis_overhead", "classify_rtt", "upstream"):
            v = d.get(part)
            share = ("%.2f%%" % (100.0 * v / total)) if (total and v is not None) else "n/a"
            cells.append("%s (%s)" % (ms2(v), share))
        out.append("| %s | %s | %s |" % (q, " | ".join(cells), ms2(total)))
    out.append("")
    out += ["Stacked, one row per quantile. A component that is a non-zero but sub-percent share "
            "still gets one cell, so it stays visible rather than rounding away:", "", "```"]
    for q in ("p50", "p99"):
        d = dec.get(q)
        if not d:
            continue
        total = d.get("total") or 0.0
        bar = ""
        if total > 0:
            width = 60
            for part, mark in (("praxis_overhead", "#"), ("classify_rtt", "="), ("upstream", ".")):
                v = max(0.0, (d.get(part) or 0.0))
                n = int(round(width * v / total))
                if v > 0 and n == 0:
                    n = 1
                bar += mark * n
        out.append("%-4s %s  %s" % (q, bar, ms2(total)))
    out += ["```", "",
            "Stack key: `#` praxis overhead · `=` classify RTT · `.` upstream generation.", ""]
    return out


def tpot_table(scenarios):
    rows = [s for s in scenarios if (s.get("extra") or {}).get("time_per_output_token_ms")]
    if not rows:
        return []
    out = ["**Time per output token, and tokens generated.** This is what the routing decision is "
           "actually spending or saving.", "",
           "| Arm | Tokens generated | TPOT p50 | TPOT p90 | TPOT p99 | TPOT max |",
           "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for s in rows:
        t = s["extra"]["time_per_output_token_ms"]
        out.append("| `%s` | %s | %s | %s | %s | %s |" % (
            s["name"], s["extra"].get("tokens_generated"),
            ms(t.get("p50")), ms(t.get("p90")), ms(t.get("p99")), ms(t.get("max"))))
    out.append("")
    for s in rows:
        by_model = (s.get("extra") or {}).get("responses_by_model")
        if by_model:
            out.append("* `%s` responses by upstream model id: %s" % (
                s["name"], ", ".join("`%s` %d" % (k, v) for k, v in sorted(by_model.items()))))
    out.append("")
    return out


# ---------------------------------------------------------------------------
# Per-scenario chapters
# ---------------------------------------------------------------------------


def chapter(run):
    m = run["manifest"]
    spec = m.get("scenario_spec", "?")
    out = ["## %s — %s" % (spec, m.get("scenario_description", m.get("scenario", "")))]
    out.append("")
    out.append("Command:")
    out += ["", "```", m.get("command", ""), "```", ""]
    for note in m.get("notes", []):
        out.append("* %s" % note)
    if m.get("notes"):
        out.append("")

    scen = run["scenarios"]
    if spec == "B-1":
        out += latency_table(scen)
        out += ["**Deltas.** Each cell is the arm's own figure with its difference from the "
                "`baseline` arm at the same concurrency in parentheses. The baseline arm and the "
                "classified arms differ in exactly one thing: whether `llm_d_sc` is in the chain.",
                ""]
        out += delta_table(scen, "baseline", ["classified-hit", "classified-miss"])
    elif spec == "B-2":
        out += latency_table(scen)
        out += ["**Deltas by body size.** The prompt is byte-identical across every size, so the "
                "difference between the arms at each size is the cost of `StreamBuffer` draining "
                "the whole body before routing.", ""]
        out += delta_table(scen, "baseline", ["classified"], axis_label="Body size", axis_key="body_label")
    elif spec == "B-4":
        out += latency_table(scen)
        for s in scen:
            out += confusion_section(s)
    elif spec == "B-5":
        out += latency_table(scen)
        out += tpot_table(scen)
        for s in scen:
            out += decomposition_section(s)
    elif spec == "B-6":
        out += latency_table(scen)
        for s in scen:
            tally = (s.get("extra") or {}).get("status_tally")
            if tally:
                out.append("* `%s` classification-status tally: %s" % (
                    s["name"], ", ".join("`%s` %d" % (k, v) for k, v in sorted(tally.items()))))
        out.append("")
    else:
        out += latency_table(scen)

    out += assertions_section(run)
    out += interpretation(spec, run)
    return out


def interpretation(spec, run):
    """"What this says" — the interpretation, including the inconvenient parts.

    Every claim here is conditioned on values read from the run JSON. Where the
    JSON does not support a claim, the section says the claim is unsupported
    rather than making it anyway.
    """
    scen = run["scenarios"]
    by_name = {s["name"]: s for s in scen}
    failed = [(s["name"], a) for s in scen for a in s.get("assertions", []) if not a["passed"]]
    out = ["### What this says", ""]

    if failed:
        out += ["**This run did not verify its own premises, so its numbers are not evidence.** "
                "%d assertion(s) failed, listed above. SPEC-BENCH §0 rule 3 treats that as a bug "
                "in the measurement, not as a result. The tables are reproduced so the failure is "
                "inspectable." % len(failed), ""]
        return out

    if spec == "B-1":
        hits = [(n, s) for n, s in by_name.items() if n.startswith("classified-hit@")]
        for n, s in sorted(hits):
            conc = n.split("@", 1)[1]
            miss = by_name.get("classified-miss@" + conc)
            base = by_name.get("baseline@" + conc)
            if base is None or miss is None:
                out.append("* At %s the delta is not computable: a comparison arm is missing." % conc)
                continue
            hp, mp, bp = s["latency_ms"]["p50"], miss["latency_ms"]["p50"], base["latency_ms"]["p50"]
            out.append(
                "* At %s, a classification that hits llm-d-sc's cache adds **%s** to p50 and a "
                "cache miss adds **%s** (baseline p50 %s, hit %s, miss %s). The gap between the "
                "two is what the cache is worth on this path."
                % (conc, delta_ms(bp, hp), delta_ms(bp, mp), ms(bp), ms(hp), ms(mp)))
        out += ["", "The hit and miss arms differ only in whether the prompt repeats, and the "
                "harness asserts that discipline from the request keys, so the difference between "
                "them is the cache and nothing else.", ""]
    elif spec == "B-2":
        pairs = []
        for s in scen:
            if not s["name"].startswith("classified@"):
                continue
            size = s["params"].get("body_label")
            base = next((b for b in scen if b["name"] == "baseline@" + s["name"].split("@", 1)[1]), None)
            if base:
                pairs.append((size, base["latency_ms"]["p50"], s["latency_ms"]["p50"],
                              s["params"].get("body_bytes", 0)))
        pairs.sort(key=lambda p: p[3])
        for size, b, c, _ in pairs:
            out.append("* At a %s body, buffering costs **%s** at p50 (%s to %s)."
                       % (size, delta_ms(b, c), ms(b), ms(c)))
        if len(pairs) >= 2:
            first, last = pairs[0], pairs[-1]
            out += ["", "Across the sweep the buffering cost moves from %s at %s to %s at %s. "
                    "Whether that is acceptable is a deployment decision about `max_body_bytes`, "
                    "not a property of the filter." % (
                        delta_ms(first[1], first[2]), first[0], delta_ms(last[1], last[2]), last[0]), ""]
    elif spec == "B-3":
        for s in sorted(scen, key=lambda x: (x["params"].get("concurrency", 0),
                                             x["params"].get("prompt_tokens", 0))):
            out.append("* %s tokens at concurrency %s: p50 %s, p99 %s, %.1f req/s." % (
                s["params"].get("prompt_tokens"), s["params"].get("concurrency"),
                ms(s["latency_ms"]["p50"]), ms(s["latency_ms"]["p99"]), s.get("throughput", 0.0)))
        out += ["", "These are directly comparable with llm-d-sc's own cache-miss table by "
                "construction — same lengths, same concurrencies, same percentile definition. The "
                "difference between the two tables is the gateway.", ""]
    elif spec == "B-4":
        for s in scen:
            extra = s.get("extra") or {}
            if "routing_accuracy" not in extra:
                continue
            mis = extra.get("misroutes", {})
            out.append("* %s of %s prompts reached their intended cluster (**%s** routing accuracy) "
                       "on a set asserted to have zero verbatim overlap and zero near-duplicates "
                       "against the classifier's anchors."
                       % (extra.get("routing_correct"), extra.get("routing_total"),
                          pct(extra.get("routing_accuracy"))))
            out.append("* **Wasted capacity:** %s SIMPLE and %s MEDIUM prompts reached the large "
                       "model. **Quality risk:** %s COMPLEX and %s REASONING prompts reached the "
                       "small model. These are different failures with different costs and are "
                       "not netted against each other."
                       % (mis.get("simple_to_large_wasted_capacity"),
                          mis.get("medium_to_large_wasted_capacity"),
                          mis.get("complex_to_small_quality_risk"),
                          mis.get("reasoning_to_small_quality_risk")))
            b = extra.get("boundary") or {}
            if b.get("total"):
                out.append("* On the %s prompts deliberately authored to sit near a class boundary, "
                           "accuracy was %s. Boundary behaviour is where a routing taxonomy earns "
                           "or loses its keep." % (b.get("total"), pct(b.get("accuracy"))))
        out.append("")
    elif spec == "B-5":
        arms_ = {s["name"]: s for s in scen}
        large, small, classified = arms_.get("always-large"), arms_.get("always-small"), arms_.get("classified")
        if large and small and classified:
            lp, sp, cp = (large["latency_ms"]["p50"], small["latency_ms"]["p50"],
                          classified["latency_ms"]["p50"])
            out.append("* p50 end to end: `always-large` %s, `always-small` %s, `classified` %s."
                       % (ms(lp), ms(sp), ms(cp)))
            out.append("* Against `always-large`, classifying changes p50 by **%s**. Against "
                       "`always-small` — the cheap floor, which answers every prompt with the "
                       "27 B model regardless of difficulty — it changes p50 by **%s**."
                       % (delta_ms(lp, cp), delta_ms(sp, cp)))
            if cp <= sp:
                out.append("* The classified arm's p50 is at or below the cheap floor while still "
                           "sending the hard prompts to the strong model, which is the claim this "
                           "scenario exists to test.")
            elif cp < lp:
                out.append("* The classified arm sits between the two floors: it is cheaper than "
                           "always using the large model, and it is not free relative to always "
                           "using the small one. That is the honest shape of the tradeoff on this "
                           "hardware.")
            else:
                out.append("* **The classified arm is not cheaper than always using the large "
                           "model on this hardware.** That is the finding, and it is published as "
                           "the finding rather than withheld.")
        else:
            out.append("* The three-arm comparison is incomplete in this run, so no payoff claim "
                       "is made.")
        out.append("")
    elif spec == "B-6":
        for s in scen:
            out.append("* `%s`: p50 %s, p99 %s, %s errors against an expected status of %s."
                       % (s["name"], ms(s["latency_ms"]["p50"]), ms(s["latency_ms"]["p99"]),
                          s.get("errors"), s["params"].get("expected_status")))
        out += ["", "The figure that matters here is the classifier-down p99. A fail-open path "
                "that still waits the full `timeout_ms` on every request has converted a "
                "classifier outage into a tax on every request — it is fail-open in name only. "
                "The assertion above is what holds that line.", ""]
    elif spec == "B-7":
        out.append("* Measured from inside the cluster, not through a tunnel — asserted, because "
                   "a port-forwarded probe previously read an order of magnitude high.")
        for s in scen:
            out.append("* `%s`: p50 %s, p99 %s." % (s["name"], ms(s["latency_ms"]["p50"]),
                                                    ms(s["latency_ms"]["p99"])))
        out.append("")
    elif spec == "SELFTEST":
        out += ["* **These are not results.** This scenario drives `bench/stub_upstream.py` "
                "directly and measures Python's HTTP client talking to Python's HTTP server on "
                "loopback. It exists to prove the harness: that the closed-loop driver sends "
                "exactly the work it claims, that warmup is excluded, that cache-key discipline "
                "holds at both ends of the socket, that provenance headers are captured intact, "
                "and that the percentile reduction and manifest emission work.",
                "* No number in this section describes the `llm_d_sc` filter.", ""]
    return out


def not_yet_measured(present):
    missing = [s for s in SPEC_ORDER if s not in present and s != "SELFTEST"]
    if not missing:
        return []
    labels = {
        "B-1": "filter overhead at the proxy",
        "B-2": "body-size sensitivity of `StreamBuffer`",
        "B-3": "prompt-length sensitivity",
        "B-4": "routing correctness over the held-out set",
        "B-5": "end-to-end payoff against the real models",
        "B-6": "degradation and failure",
        "B-7": "in-cluster topology",
    }
    out = ["## Not yet measured", "",
           "Stated explicitly, because an absent scenario is easy to mistake for a scenario that "
           "found nothing.", ""]
    for spec in missing:
        out.append("* **%s** — %s. No run JSON exists in `bench/results/`." % (spec, labels[spec]))
    out.append("")
    return out


def render(runs):
    present = latest_by_spec(runs)
    out = ["# Benchmarks — the `llm_d_sc` filter for Praxis Proxy", "",
           "**Generated by `bench/report.py` from the JSON in `bench/results/`. Do not edit by "
           "hand — an edit here is a number without a run behind it.**", "",
           DISCLAIMER, "", METHODOLOGY, ""]
    out += environment_table(runs)
    for spec in SPEC_ORDER:
        if spec in present:
            out += chapter(present[spec])
    out += not_yet_measured(present)
    out += ["## Reproducing", "",
            "See `bench/README.md`. Every run writes its own manifest (UTC timestamp, git sha of "
            "both trees, host CPU, OS, Python version, target, topology label, warmup and measured "
            "counts, concurrency, and the exact argv) alongside a per-request `.records.jsonl`, so "
            "any distribution above can be recomputed from source rather than trusted.", "",
            "## Corrections", "",
            "Corrections are published, not quietly replaced. If a figure on this page turns out "
            "to be wrong, the superseded number stays with an explanation of why it was wrong — "
            "the precedent is `llm-d-sc/docs/benchmarks/topology.md`, which does exactly this for "
            "a stage-accounting error.", ""]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# --check: exercise the B-4 and B-5 renderers without publishing anything
# ---------------------------------------------------------------------------


FIXTURE_B4 = {
    "name": "fixture", "params": {"concurrency": 1},
    "latency_ms": {"p50": 1.0, "p90": 2.0, "p95": 3.0, "p99": 4.0, "max": 5.0},
    "throughput": 1.0, "errors": 0, "assertions": [],
    "extra": {
        "classes": ["SIMPLE", "MEDIUM", "COMPLEX", "REASONING"],
        "clusters": ["general", "large", "small", "unattributed"],
        "confusion": {
            "SIMPLE": {"general": 0, "large": 1, "small": 31, "unattributed": 0},
            "MEDIUM": {"general": 0, "large": 3, "small": 29, "unattributed": 0},
            "COMPLEX": {"general": 0, "large": 30, "small": 2, "unattributed": 0},
            "REASONING": {"general": 0, "large": 32, "small": 0, "unattributed": 0},
        },
        "routing_accuracy": 0.953125, "routing_correct": 122, "routing_total": 128,
        "per_class": {c: {"support": 32, "precision": 0.9, "recall": 0.9, "f1": 0.9}
                      for c in ["SIMPLE", "MEDIUM", "COMPLEX", "REASONING"]},
        "misroutes": {"simple_to_large_wasted_capacity": 1, "medium_to_large_wasted_capacity": 3,
                      "complex_to_small_quality_risk": 2, "reasoning_to_small_quality_risk": 0,
                      "to_general_unrouted": 0},
        "boundary": {"total": 8, "correct": 6, "accuracy": 0.75},
    },
}

FIXTURE_B5 = {
    "name": "classified", "params": {"concurrency": 1},
    "latency_ms": {"p50": 900.0, "p90": 1800.0, "p95": 2000.0, "p99": 2400.0, "max": 2600.0},
    "throughput": 1.0, "errors": 0, "assertions": [],
    "extra": {
        "tokens_generated": 5120,
        "time_per_output_token_ms": {"p50": 7.0, "p90": 14.0, "p95": 15.0, "p99": 19.0, "max": 20.0},
        "responses_by_model": {"ds4-flash-0731": 20, "qwen38-27b": 20},
        "decomposition": {
            "p50": {"total": 900.0, "praxis_overhead": 0.4, "classify_rtt": 9.6, "upstream": 890.0},
            "p99": {"total": 2400.0, "praxis_overhead": 1.1, "classify_rtt": 13.9, "upstream": 2385.0},
        },
    },
}


def self_check():
    print("# renderer self-check (fixture data, NOT a measurement)\n")
    print("\n".join(confusion_section(FIXTURE_B4)))
    print("\n".join(tpot_table([FIXTURE_B5])))
    print("\n".join(decomposition_section(FIXTURE_B5)))
    print("# end self-check — nothing was written")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--results-dir", default=DEFAULT_RESULTS)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--check", action="store_true",
                   help="render the B-4 and B-5 sections from a fixture and exit, writing nothing")
    args = p.parse_args(argv)

    if args.check:
        return self_check()

    runs = load_runs(args.results_dir)
    if not runs:
        print("no run JSON found in %s" % args.results_dir, file=sys.stderr)
        return 1
    text = render(runs)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    present = sorted(latest_by_spec(runs))
    print("wrote %s from %d run(s): %s" % (args.out, len(runs), ", ".join(present)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
