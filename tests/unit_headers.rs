//! T-U8 — client-supplied `x-llm-d-sc-*` headers are stripped on every path,
//! including the skip path where no classification happens at all.
//!
//! Mirrors `endpoint_selector`'s trust posture: a caller must never be able to
//! forge provenance that a downstream policy filter would believe.

use llm_d_sc_praxis_filter::{LlmDScConfig, LlmDScFilter, testing::ContextHarness};
use praxis_filter::{FilterAction, HttpFilter};

/// A filter pointed at a port with nothing on it — the skip path never dials.
fn filter() -> LlmDScFilter {
    let config: LlmDScConfig = serde_yaml::from_str(
        "endpoint: \"127.0.0.1:1\"\ndefault_cluster: general\n\
         routes:\n  - {label: SIMPLE, cluster: small}\n",
    )
    .expect("test config must deserialize");
    LlmDScFilter::new(config).expect("test config must be valid")
}

#[tokio::test]
async fn t_u8_forged_provenance_headers_are_stripped_when_classification_is_skipped() {
    // A body with no prompt at all: the RPC is skipped entirely, so this
    // exercises the earliest-return path.
    let harness = ContextHarness::post(r#"{"model":"gpt-4"}"#)
        .with_header("x-llm-d-sc-label", "SIMPLE")
        .with_header("x-llm-d-sc-score", "0.9999")
        .with_header("x-llm-d-sc-status", "OK")
        .with_header("x-llm-d-sc-classifier", "forged")
        .with_header("x-llm-d-sc-taxonomy-revision", "forged")
        .with_header("x-llm-d-sc-anything-else", "forged")
        .with_header("x-unrelated", "keep me");
    let mut ctx = harness.context();

    let action = filter().on_request(&mut ctx).await.expect("skip path never errors");

    assert!(
        matches!(action, FilterAction::Continue),
        "a request with no prompt is not a 4xx: the classifier filter is not a request validator"
    );
    assert_eq!(
        ctx.get_metadata("llm_d_sc.status"),
        Some("SKIPPED_NO_PROMPT"),
        "the skip must be recorded, not silent"
    );
    assert_eq!(
        ctx.cluster_name(),
        Some("general"),
        "the skip path still selects default_cluster"
    );

    let removed: Vec<&str> = ctx
        .request_headers_to_remove
        .iter()
        .map(http::HeaderName::as_str)
        .collect();
    for forged in [
        "x-llm-d-sc-label",
        "x-llm-d-sc-score",
        "x-llm-d-sc-status",
        "x-llm-d-sc-classifier",
        "x-llm-d-sc-taxonomy-revision",
        "x-llm-d-sc-anything-else",
    ] {
        assert!(
            removed.contains(&forged),
            "client-supplied {forged} must be stripped, got {removed:?}"
        );
    }
    assert!(
        !removed.contains(&"x-unrelated"),
        "only the x-llm-d-sc-* namespace is the filter's to claim"
    );
}

#[tokio::test]
async fn t_u8_the_filters_own_status_header_replaces_the_forged_one() {
    let harness = ContextHarness::post(r#"{"model":"gpt-4"}"#).with_header("x-llm-d-sc-status", "OK");
    let mut ctx = harness.context();

    let _action = filter().on_request(&mut ctx).await.expect("skip path never errors");

    // The protocol layer applies removals before sets, so upstream sees only
    // the filter's own value.
    let set: Vec<(String, String)> = ctx
        .request_headers_to_set
        .iter()
        .map(|(name, value)| {
            (
                name.as_str().to_owned(),
                value.to_str().expect("ascii value").to_owned(),
            )
        })
        .collect();
    assert!(
        set.contains(&("x-llm-d-sc-status".to_owned(), "SKIPPED_NO_PROMPT".to_owned())),
        "expected the filter's own status header, got {set:?}"
    );
}

#[tokio::test]
async fn t_u8_headers_are_still_stripped_when_emit_headers_is_off() {
    let config: LlmDScConfig = serde_yaml::from_str(
        "endpoint: \"127.0.0.1:1\"\ndefault_cluster: general\nemit_headers: false\n\
         routes:\n  - {label: SIMPLE, cluster: small}\n",
    )
    .expect("test config must deserialize");
    let filter = LlmDScFilter::new(config).expect("valid config");

    let harness = ContextHarness::post("not json").with_header("x-llm-d-sc-label", "COMPLEX");
    let mut ctx = harness.context();
    let _action = filter.on_request(&mut ctx).await.expect("skip path never errors");

    assert!(
        ctx.request_headers_to_remove
            .iter()
            .any(|name| name.as_str() == "x-llm-d-sc-label"),
        "stripping is a security property, not a function of emit_headers"
    );
    assert!(
        ctx.request_headers_to_set.is_empty(),
        "emit_headers: false must not add provenance headers"
    );
}

#[tokio::test]
async fn on_request_body_is_a_no_op_because_the_body_is_pre_read() {
    let harness = ContextHarness::post(r#"{"prompt":"x"}"#);
    let mut ctx = harness.context();
    let mut chunk = Some(bytes::Bytes::from_static(b"chunk"));

    let action = filter()
        .on_request_body(&mut ctx, &mut chunk, false)
        .await
        .expect("body hook never errors");

    assert!(
        matches!(action, FilterAction::BodyDone),
        "the whole body is already available in on_request; chunks are of no interest"
    );
    assert_eq!(
        chunk,
        Some(bytes::Bytes::from_static(b"chunk")),
        "the body is never modified"
    );
}

// -----------------------------------------------------------------------------
// T-U10 — the classify RTT is observable to an outside client
// -----------------------------------------------------------------------------

/// The end-to-end latency decomposition (`total = praxis + classify + upstream`)
/// is only a MEASURED identity if the classify hop is visible from outside the
/// proxy. A benchmark client can time the total and the upstream can report its
/// own share, but nothing outside Praxis can see the classify RPC. Without this
/// header the decomposition could only be inferred by subtraction, which would
/// silently absorb every unrelated cost into "classify".
#[tokio::test]
async fn t_u10_classify_latency_is_emitted_as_a_header_when_an_rpc_happened() {
    // Port 1 refuses instantly: a real (fast) failed RPC, not a skip.
    let harness = ContextHarness::post(r#"{"messages":[{"role":"user","content":"hello"}]}"#);
    let mut ctx = harness.context();

    filter().on_request(&mut ctx).await.expect("fail-open never errors");

    let latency = ctx
        .request_headers_to_set
        .iter()
        .find(|(name, _)| name.as_str() == "x-llm-d-sc-latency-us")
        .map(|(_, value)| value.to_str().expect("header must be ASCII").to_owned())
        .expect("an attempted RPC must report its wall clock to the upstream");

    latency
        .parse::<u64>()
        .expect("x-llm-d-sc-latency-us must be an integer count of microseconds");
}

#[tokio::test]
async fn t_u10_no_latency_header_when_no_rpc_was_attempted() {
    // No prompt -> no RPC -> reporting a duration would be a fabricated zero.
    let harness = ContextHarness::post(r#"{"model":"gpt-4"}"#);
    let mut ctx = harness.context();

    filter().on_request(&mut ctx).await.expect("skip path never errors");

    assert!(
        !ctx.request_headers_to_set
            .iter()
            .any(|(name, _)| name.as_str() == "x-llm-d-sc-latency-us"),
        "the skip path attempted no RPC, so it must not report a latency at all"
    );
}
