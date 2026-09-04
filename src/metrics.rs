//! Metric names and emit helpers (SPEC §4.7).
//!
//! Every label value comes either from config or from the fixed
//! [`crate::filter::status`] set, so cardinality is bounded by the operator's
//! own route table.
//!
//! That bound is enforced, not assumed. The ranked label on the wire is chosen
//! by llm-d-sc, not by us, so emitting it verbatim would hand an upstream
//! service the ability to mint unbounded Prometheus series — by upgrading its
//! taxonomy, by misconfiguration, or by compromise. Any label outside the
//! configured route table therefore collapses to [`UNMAPPED_LABEL_SENTINEL`].
//! The per-request `x-llm-d-sc-label` header still carries the true label,
//! because a header is per-request and creates no time series.

/// Counter: request-level classification outcomes, labelled by status.
///
/// This includes `SKIPPED_NO_PROMPT`; use [`CLASSIFY_ATTEMPT_TOTAL`] for the
/// number of classifier RPCs actually started.
pub const CLASSIFY_TOTAL: &str = "llm_d_sc_classify_total";

/// Counter: classify RPCs started by this filter.
///
/// Unlike [`CLASSIFY_TOTAL`], this excludes requests for which no prompt was
/// found and therefore no classifier RPC was attempted.
pub const CLASSIFY_ATTEMPT_TOTAL: &str = "llm_d_sc_classify_attempt_total";

/// Counter: fail-open fallback decisions, labelled by outcome status.
pub const FALLBACK_TOTAL: &str = "llm_d_sc_fallback_total";

/// Histogram: wall-clock seconds spent in the classify RPC.
pub const CLASSIFY_DURATION_SECONDS: &str = "llm_d_sc_classify_duration_seconds";

/// Counter: routing decisions, labelled by ranked label and chosen cluster.
pub const ROUTE_TOTAL: &str = "llm_d_sc_route_total";

/// Stand-in emitted instead of a ranked label the operator never configured.
///
/// Keeps `llm_d_sc_route_total` cardinality bounded by the route table while
/// still making "the classifier returned something we do not route on" visible
/// and alertable.
pub const UNMAPPED_LABEL_SENTINEL: &str = "<unmapped>";

/// Record one classify outcome.
///
/// `seconds` is `None` on the skip path, where no RPC was attempted and a
/// zero-latency sample would bias the histogram.
pub fn record_classify(status: &'static str, seconds: Option<f64>) {
    ::metrics::counter!(CLASSIFY_TOTAL, "status" => status).increment(1);
    if let Some(seconds) = seconds {
        ::metrics::histogram!(CLASSIFY_DURATION_SECONDS).record(seconds);
    }
}

/// Record that a classify RPC was started.
pub fn record_attempt() {
    ::metrics::counter!(CLASSIFY_ATTEMPT_TOTAL).increment(1);
}

/// Record one fail-open fallback decision.
///
/// Rejected decisions must pass `fail_open = false` and are intentionally not
/// emitted here. Keeping that guard at the metric boundary prevents callers
/// from accidentally counting fail-closed outcomes as fallbacks.
pub fn record_fallback(status: &'static str, fail_open: bool) {
    if fail_open {
        ::metrics::counter!(FALLBACK_TOTAL, "status" => status).increment(1);
    }
}

/// Record one routing decision.
pub fn record_route(label: String, cluster: String) {
    ::metrics::counter!(ROUTE_TOTAL, "label" => label, "cluster" => cluster).increment(1);
}
