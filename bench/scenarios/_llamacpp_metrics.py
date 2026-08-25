"""Read a llama.cpp server's own Prometheus counters.

Why this exists: B-5's three arms are only comparable if each one had the
backend to itself. These are single-replica llama.cpp servers with ONE slot on a
shared home cluster, and a foreign tenant's request does not merely add noise --
it blocks the slot. During this benchmark's setup a probe expecting ~1 s came
back after 168 s, and the server's own timings showed 1.03 s of work behind
155 s of queueing for somebody else's 3 155-token generation. A B-5 table
measured through that would be a table of another tenant's session lengths.

So contention is checked, not hoped for:

* `llamacpp:requests_processing` / `requests_deferred` gate the START of an arm.
* `llamacpp:tokens_predicted_total` and `prompt_tokens_total` are cumulative, so
  their delta across an arm can be compared against what the harness itself
  asked for. Tokens the server generated that this harness did not request are
  a foreign tenant, and they are reported per arm.

`llama-server-qwen38` serves `/metrics` unauthenticated. `llama-server-ds4`
requires the bearer token, which is read from the `DS4_API_KEY` environment
variable supplied by the same Secret Praxis uses. The value is never logged,
never written to a file, and never placed in argv; on any failure this module
reports the exception TYPE and a fixed message rather than the exception text,
so a token echoed inside a URL or a server error can never reach the results.
"""

from __future__ import annotations

import os
import time
import urllib.request

# Counters worth carrying into the evidence. Names are llama.cpp's own.
INTERESTING = (
    "llamacpp:requests_processing",
    "llamacpp:requests_deferred",
    "llamacpp:tokens_predicted_total",
    "llamacpp:prompt_tokens_total",
    "llamacpp:n_decode_total",
)


def _redact(exc):
    """Never let an exception body carry a credential into the results."""
    return "%s (detail suppressed: an error string may echo an Authorization header)" % type(exc).__name__


def scrape(url, bearer_env=None, timeout=10.0):
    """Fetch llama.cpp `/metrics`. Returns (sample_dict, error_or_None)."""
    headers = {"Accept": "text/plain"}
    if bearer_env:
        token = os.environ.get(bearer_env)
        if not token:
            return None, "%s is not set in this pod, so this endpoint cannot be read" % bearer_env
        headers["Authorization"] = "Bearer " + token
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - reported, never raised, never echoed
        return None, _redact(exc)
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
    return out, None


def snapshot(url, bearer_env=None):
    sample, err = scrape(url, bearer_env=bearer_env)
    if sample is None:
        return {"error": err, "unix": time.time()}
    return dict({k: sample.get(k) for k in INTERESTING}, unix=time.time())


def wait_until_idle(url, bearer_env=None, consecutive=3, max_wait_s=900.0, poll_s=3.0):
    """Block until the backend reports no request in flight.

    Returns a dict describing what happened, which is recorded as evidence
    whether or not the wait succeeded. `consecutive` clean samples are required
    so an arm does not start in the gap between two of a foreign tenant's turns.

    A timeout is NOT an exception: the arm still runs, and the scenario's own
    contention assertion decides whether the resulting numbers are usable. That
    keeps the decision in the assertion layer, where SPEC-BENCH §0 rule 3 puts
    it, instead of in an error path.
    """
    started = time.time()
    clean = 0
    samples = []
    while time.time() - started < max_wait_s:
        sample, err = scrape(url, bearer_env=bearer_env)
        if sample is None:
            return {"gated": False, "reason": err, "waited_s": time.time() - started}
        busy = (sample.get("llamacpp:requests_processing") or 0.0) + (
            sample.get("llamacpp:requests_deferred") or 0.0)
        samples.append(busy)
        if busy <= 0.0:
            clean += 1
            if clean >= consecutive:
                return {"gated": True, "waited_s": time.time() - started,
                        "consecutive_idle_samples": clean}
        else:
            clean = 0
        time.sleep(poll_s)
    return {"gated": False, "reason": "still busy after %.0f s" % (time.time() - started),
            "waited_s": time.time() - started, "last_busy_samples": samples[-10:]}


def delta(before, after, key):
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    b, a = before.get(key), after.get(key)
    if b is None or a is None:
        return None
    return a - b
