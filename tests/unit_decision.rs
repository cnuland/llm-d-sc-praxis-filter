//! T-U5 (the SPEC 4.6 decision table, one case per row) and T-U7 (max-by-score).
//!
//! Every case goes through [`LlmDScFilter::decide`], the pure seam that takes a
//! `Result<ClassifyResponse, ClassifyFailure>`, so all ten rows are reachable
//! without a socket.

use llm_d_sc_praxis_filter::{
    LlmDScConfig, LlmDScFilter, Route,
    client::ClassifyFailure,
    pb::{ClassificationStatus, ClassifyResponse, RankedSignal},
};

/// Build a filter from a YAML config fragment appended to the base config.
fn filter(extra: &str) -> LlmDScFilter {
    let yaml = format!(
        "endpoint: \"127.0.0.1:50051\"\ndefault_cluster: general\n\
         routes:\n  - {{label: SIMPLE, cluster: small}}\n  - {{label: COMPLEX, cluster: large}}\n{extra}"
    );
    let config: LlmDScConfig = serde_yaml::from_str(&yaml).expect("test config must deserialize");
    LlmDScFilter::new(config).expect("test config must be valid")
}

/// A response with the given wire status and ranked signals.
fn response(status: ClassificationStatus, ranked: &[(&str, f32)]) -> ClassifyResponse {
    ClassifyResponse {
        request_id: "req-1".to_owned(),
        classifier_id: "complexity".to_owned(),
        model_revision: "c5f55ef4".to_owned(),
        tokenizer_revision: "tok-1".to_owned(),
        taxonomy_revision: "scr-default-anchors-v1".to_owned(),
        status: status as i32,
        ranked: ranked
            .iter()
            .map(|(label, score)| RankedSignal {
                label: (*label).to_owned(),
                score: *score,
            })
            .collect(),
    }
}

/// The cluster a decision selected, or `None` when it rejected.
fn cluster(route: &Route) -> Option<&str> {
    match route {
        Route::Cluster(name) => Some(name.as_str()),
        Route::Reject => None,
    }
}

/// A gRPC failure with the given code.
fn grpc(code: tonic::Code) -> ClassifyFailure {
    ClassifyFailure::status(tonic::Status::new(code, "stub"))
}

// -----------------------------------------------------------------------------
// T-U5 — one case per decision-table row
// -----------------------------------------------------------------------------

#[test]
fn t_u5_row1_ok_mapped_label_routes_to_the_mapped_cluster() {
    let decision = filter("").decide(Ok(response(ClassificationStatus::Ok, &[("COMPLEX", 0.74)])));

    assert_eq!(cluster(&decision.route), Some("large"));
    assert_eq!(decision.status, "OK");
    assert_eq!(decision.label.as_deref(), Some("COMPLEX"));
    assert_eq!(decision.score, Some(0.74));
    let provenance = decision.provenance.expect("a response carries provenance");
    assert_eq!(provenance.classifier_id, "complexity");
    assert_eq!(provenance.taxonomy_revision, "scr-default-anchors-v1");
}

#[test]
fn t_u5_row2_unmapped_label_falls_back_to_default_cluster() {
    // A taxonomy that grew a label the operator has not mapped yet.
    let decision = filter("").decide(Ok(response(ClassificationStatus::Ok, &[("REASONING", 0.9)])));

    assert_eq!(cluster(&decision.route), Some("general"));
    assert_eq!(decision.status, "UNMAPPED_LABEL");
    assert_eq!(
        decision.label.as_deref(),
        Some("REASONING"),
        "the unmapped label is still reported so an operator can see what to add"
    );
}

#[test]
fn t_u5_row3_low_confidence_falls_back_to_default_cluster() {
    let decision = filter("min_score: 0.5\n").decide(Ok(response(ClassificationStatus::Ok, &[("COMPLEX", 0.25)])));

    assert_eq!(cluster(&decision.route), Some("general"));
    assert_eq!(decision.status, "LOW_CONFIDENCE");
    assert_eq!(decision.score, Some(0.25));
}

#[test]
fn t_u5_row3_score_exactly_at_min_score_is_confident_enough() {
    let decision = filter("min_score: 0.5\n").decide(Ok(response(ClassificationStatus::Ok, &[("COMPLEX", 0.5)])));

    assert_eq!(cluster(&decision.route), Some("large"), "min_score is inclusive");
    assert_eq!(decision.status, "OK");
}

#[test]
fn t_u5_row4_no_ranked_signals_falls_back_to_default_cluster() {
    let decision = filter("").decide(Ok(response(ClassificationStatus::Ok, &[])));

    assert_eq!(cluster(&decision.route), Some("general"));
    assert_eq!(decision.status, "NO_SIGNAL");
    assert_eq!(decision.label, None);
}

#[test]
fn t_u5_row5_abstain_falls_back_to_default_cluster() {
    let decision = filter("on_unavailable: reject\n").decide(Ok(response(ClassificationStatus::Abstain, &[])));

    assert_eq!(
        cluster(&decision.route),
        Some("general"),
        "ABSTAIN is a valid answer, not a failure, so it is never subject to on_unavailable"
    );
    assert_eq!(decision.status, "ABSTAIN");
}

#[test]
fn t_u5_row6_wire_unavailable_follows_on_unavailable() {
    let fail_open = filter("").decide(Ok(response(ClassificationStatus::Unavailable, &[])));
    assert_eq!(cluster(&fail_open.route), Some("general"));
    assert_eq!(fail_open.status, "UNAVAILABLE");

    let fail_closed = filter("on_unavailable: reject\n").decide(Ok(response(ClassificationStatus::Unavailable, &[])));
    assert_eq!(cluster(&fail_closed.route), None, "reject posture must not route");
    assert_eq!(fail_closed.status, "UNAVAILABLE");
}

#[test]
fn t_u5_row6_wire_unspecified_is_treated_as_unavailable() {
    let decision = filter("").decide(Ok(response(ClassificationStatus::Unspecified, &[])));

    assert_eq!(cluster(&decision.route), Some("general"));
    assert_eq!(decision.status, "UNAVAILABLE");
}

#[test]
fn t_u5_row6_unrecognised_wire_status_is_treated_as_unavailable() {
    // Forward compatibility: a newer server adding a status value must not
    // make the filter fabricate a route.
    let mut raw = response(ClassificationStatus::Ok, &[("COMPLEX", 0.9)]);
    raw.status = 99;
    let decision = filter("").decide(Ok(raw));

    assert_eq!(cluster(&decision.route), Some("general"));
    assert_eq!(decision.status, "UNAVAILABLE");
}

#[test]
fn t_u5_row7_resource_exhausted_follows_on_resource_exhausted() {
    let fail_open = filter("").decide(Err(grpc(tonic::Code::ResourceExhausted)));
    assert_eq!(
        cluster(&fail_open.route),
        Some("general"),
        "default posture is fail-open"
    );
    assert_eq!(fail_open.status, "RESOURCE_EXHAUSTED");

    let fail_closed = filter("on_resource_exhausted: reject\n").decide(Err(grpc(tonic::Code::ResourceExhausted)));
    assert_eq!(cluster(&fail_closed.route), None);
    assert_eq!(fail_closed.status, "RESOURCE_EXHAUSTED");
}

#[test]
fn t_u5_row7_resource_exhausted_is_independent_of_on_unavailable() {
    // The two knobs are separate: discussion #1017 asks specifically about
    // queue-full, which an operator may want to treat differently from an
    // outage.
    let decision = filter("on_unavailable: reject\n").decide(Err(grpc(tonic::Code::ResourceExhausted)));
    assert_eq!(cluster(&decision.route), Some("general"));
}

#[test]
fn t_u5_row8_invalid_argument_follows_on_unavailable() {
    let fail_open = filter("").decide(Err(grpc(tonic::Code::InvalidArgument)));
    assert_eq!(cluster(&fail_open.route), Some("general"));
    assert_eq!(fail_open.status, "INVALID_ARGUMENT");

    let fail_closed = filter("on_unavailable: reject\n").decide(Err(grpc(tonic::Code::InvalidArgument)));
    assert_eq!(cluster(&fail_closed.route), None);
}

#[test]
fn t_u5_row9_other_grpc_errors_follow_on_unavailable() {
    for code in [
        tonic::Code::Unavailable,
        tonic::Code::Internal,
        tonic::Code::Unimplemented,
        tonic::Code::Unknown,
        tonic::Code::DeadlineExceeded,
    ] {
        let decision = filter("").decide(Err(grpc(code)));
        assert_eq!(cluster(&decision.route), Some("general"), "code {code:?}");
        assert_eq!(decision.status, "ERROR", "code {code:?}");
    }
}

#[test]
fn t_u5_row10_local_timeout_follows_on_unavailable() {
    let fail_open = filter("").decide(Err(ClassifyFailure::Timeout));
    assert_eq!(cluster(&fail_open.route), Some("general"));
    assert_eq!(fail_open.status, "TIMEOUT");

    let fail_closed = filter("on_unavailable: reject\n").decide(Err(ClassifyFailure::Timeout));
    assert_eq!(cluster(&fail_closed.route), None);
    assert_eq!(fail_closed.status, "TIMEOUT");
}

// -----------------------------------------------------------------------------
// T-U7 — max-by-score, not ranked[0]
// -----------------------------------------------------------------------------

#[test]
fn t_u7_highest_score_wins_even_at_index_two() {
    let decision = filter("").decide(Ok(response(
        ClassificationStatus::Ok,
        &[("SIMPLE", 0.10), ("MEDIUM", 0.20), ("COMPLEX", 0.90)],
    )));

    assert_eq!(
        decision.label.as_deref(),
        Some("COMPLEX"),
        "the wire contract does not promise ordering, so ranked[0] must not be assumed"
    );
    assert_eq!(cluster(&decision.route), Some("large"));
    assert_eq!(decision.score, Some(0.90));
}

#[test]
fn t_u7_first_element_is_not_privileged_when_it_is_the_lowest() {
    let decision = filter("").decide(Ok(response(
        ClassificationStatus::Ok,
        &[("COMPLEX", 0.01), ("SIMPLE", 0.99)],
    )));

    assert_eq!(decision.label.as_deref(), Some("SIMPLE"));
    assert_eq!(cluster(&decision.route), Some("small"));
}

#[test]
fn t_u7_nan_scores_do_not_panic_or_win() {
    // total_cmp orders NaN above every real number, so a NaN-only response
    // must still produce a decision rather than a panic.
    let decision = filter("").decide(Ok(response(
        ClassificationStatus::Ok,
        &[("SIMPLE", f32::NAN), ("COMPLEX", 0.5)],
    )));

    assert!(
        matches!(decision.route, Route::Cluster(_)),
        "a NaN score must still produce a route, not a panic"
    );
}

// -----------------------------------------------------------------------------
// T-U9 — metric label cardinality is bounded by CONFIG, not by the server
// -----------------------------------------------------------------------------

#[test]
fn t_u9_configured_label_is_emitted_verbatim() {
    let f = filter("");
    let d = f.decide(Ok(response(ClassificationStatus::Ok, &[("COMPLEX", 0.9)])));
    assert_eq!(
        f.metric_label(&d),
        "COMPLEX",
        "a label the operator routes on must appear in the metric as itself"
    );
}

#[test]
fn t_u9_unconfigured_label_collapses_to_the_sentinel() {
    let f = filter("");
    // A taxonomy the operator never configured — exactly what a classifier
    // upgrade, a misconfiguration, or a compromise would produce.
    let d = f.decide(Ok(response(
        ClassificationStatus::Ok,
        &[("SOME_BRAND_NEW_LABEL", 0.9)],
    )));
    assert_eq!(
        f.metric_label(&d),
        "<unmapped>",
        "an unconfigured label must not become a Prometheus series"
    );
    // The per-request header still carries the truth: bounding the METRIC must
    // not cost us the observability of what actually came back.
    assert_eq!(
        d.label.as_deref(),
        Some("SOME_BRAND_NEW_LABEL"),
        "the decision must retain the real label for headers and metadata"
    );
}

#[test]
fn t_u9_unbounded_distinct_labels_produce_one_series() {
    let f = filter("");
    let emitted: std::collections::HashSet<String> = (0..1_000)
        .map(|i| {
            let label = format!("ATTACKER_LABEL_{i}");
            let d = f.decide(Ok(response(ClassificationStatus::Ok, &[(&label, 0.9)])));
            f.metric_label(&d)
        })
        .collect();
    assert_eq!(
        emitted.len(),
        1,
        "1000 distinct server-supplied labels must collapse to a single metric label, got {emitted:?}"
    );
}

#[test]
fn t_u9_labelless_decision_is_attributed_to_its_status() {
    let f = filter("");
    let d = f.decide(Err(ClassifyFailure::Timeout));
    assert_eq!(
        f.metric_label(&d),
        "TIMEOUT",
        "a decision with no label must be attributed to its status, not dropped"
    );
}
