"""Scrape Praxis's own Prometheus counters over HTTP.

Used as a SERVER-SIDE premise check, which is the strongest kind SPEC-BENCH §0
rule 3 asks for: "a cache-hit scenario asserts the service's hit counter moved
by exactly the measured count". The client-side equivalent used by the local
scenarios -- reading `x-llm-d-sc-*` off the response -- is unavailable in the
cluster, because the filter sets provenance on the UPSTREAM request and neither
llama.cpp nor `static_response` echoes it back. Praxis's own counters do not
depend on an upstream's cooperation, so they work where the header check cannot.

Two metrics matter here:

* `llm_d_sc_classify_total{status=...}` -- a cumulative counter. Its delta
  across an arm proves exactly how many classifications that arm caused, which
  is how a "this arm classified" or "this arm did NOT classify" premise is
  verified rather than assumed.
* `llm_d_sc_classify_duration_seconds` -- exported by
  `metrics-exporter-prometheus` as a SUMMARY with pre-computed quantiles over a
  rolling window, plus a cumulative `_sum`/`_count`. The quantiles are Praxis's
  own measurement of the Praxis -> llm-d-sc round trip, taken inside the proxy,
  in the cluster. They are a distribution over the exporter's window and are NOT
  joinable to an individual request, and every consumer here says so.

Standard library only, like everything else under `bench/`.
"""

from __future__ import annotations

import urllib.request


def scrape(url, timeout=10.0):
    """Fetch a Prometheus text exposition endpoint into a flat dict.

    Keys are the full sample name including its label set exactly as exported,
    e.g. `llm_d_sc_classify_total{status="OK"}`. Values are floats. Comment and
    TYPE/HELP lines are dropped.
    """
    req = urllib.request.Request(url, headers={"Accept": "text/plain"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    out = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            name, value = line.rsplit(" ", 1)
            out[name.strip()] = float(value)
        except ValueError:
            continue
    return out


def sum_by_prefix(sample, prefix):
    """Total every sample whose name starts with `prefix`."""
    return sum(v for k, v in sample.items() if k.startswith(prefix))


def classify_total(sample):
    """All `llm_d_sc_classify_total` series summed, across every status."""
    return sum_by_prefix(sample, "llm_d_sc_classify_total{")


def classify_by_status(sample):
    """`llm_d_sc_classify_total` broken out by its `status` label."""
    out = {}
    for key, value in sample.items():
        if not key.startswith("llm_d_sc_classify_total{"):
            continue
        inner = key[key.index("{") + 1: key.rindex("}")]
        status = "?"
        for part in inner.split(","):
            if part.startswith("status="):
                status = part.split("=", 1)[1].strip('"')
        out[status] = out.get(status, 0.0) + value
    return out


def route_by_label(sample):
    """`llm_d_sc_route_total` as {"LABEL->cluster": count}."""
    out = {}
    for key, value in sample.items():
        if not key.startswith("llm_d_sc_route_total{"):
            continue
        inner = key[key.index("{") + 1: key.rindex("}")]
        label = cluster = "?"
        for part in inner.split(","):
            if part.startswith("label="):
                label = part.split("=", 1)[1].strip('"')
            elif part.startswith("cluster="):
                cluster = part.split("=", 1)[1].strip('"')
        out["%s->%s" % (label, cluster)] = out.get("%s->%s" % (label, cluster), 0.0) + value
    return out


def classify_duration_quantiles(sample):
    """Praxis's own classify-RTT summary, in MILLISECONDS.

    Returned shape mirrors `harness.reduce_latency` so it can sit next to a
    client-side distribution without a reader having to translate units. `n` is
    the exporter's cumulative sample count, NOT the number of samples inside the
    quantile window -- the window is the exporter's, and the caller must not
    present these as per-request measurements.
    """
    want = {"0.5": "p50", "0.9": "p90", "0.95": "p95", "0.99": "p99", "1": "max"}
    out = {}
    for key, value in sample.items():
        if not key.startswith("llm_d_sc_classify_duration_seconds{"):
            continue
        inner = key[key.index("{") + 1: key.rindex("}")]
        for part in inner.split(","):
            if part.startswith("quantile="):
                q = part.split("=", 1)[1].strip('"')
                if q in want:
                    out[want[q]] = value * 1000.0
    count = sample.get("llm_d_sc_classify_duration_seconds_count")
    if count is not None:
        out["n"] = int(count)
    return out


def delta(before, after):
    """after - before for every counter present in `after`."""
    out = {}
    for key, value in after.items():
        prev = before.get(key, 0.0)
        if value != prev:
            out[key] = value - prev
    return out
