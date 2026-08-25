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

/// Counter: classify attempts, labelled by outcome status.
pub const CLASSIFY_TOTAL: &str = "llm_d_sc_classify_total";

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

/// Record one routing decision.
pub fn record_route(label: String, cluster: String) {
    ::metrics::counter!(ROUTE_TOTAL, "label" => label, "cluster" => cluster).increment(1);
}
