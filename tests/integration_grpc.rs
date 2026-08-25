//! T-I1 .. T-I7 — the filter against an in-process stub `classify.Classify`
//! server over real h2c.

use std::time::Duration;

use llm_d_sc_praxis_filter::{
    LlmDScConfig, LlmDScFilter,
    pb::{ClassificationStatus, ClassifyResponse, RankedSignal},
    testing::{ContextHarness, StubBehaviour, StubServer},
};
use praxis_filter::{FilterAction, HttpFilter};

/// A chat request body carrying one user turn.
const CHAT_BODY: &str = r#"{"messages":[{"role":"user","content":"Design a microservices architecture"}]}"#;

/// Build a filter pointed at `endpoint` with the given extra config lines.
fn filter(endpoint: &str, extra: &str) -> LlmDScFilter {
    let yaml = format!(
        "endpoint: \"{endpoint}\"\ndefault_cluster: general\n\
         routes:\n  - {{label: SIMPLE, cluster: small}}\n  - {{label: COMPLEX, cluster: large}}\n{extra}"
    );
    let config: LlmDScConfig = serde_yaml::from_str(&yaml).expect("test config must deserialize");
    LlmDScFilter::new(config).expect("test config must be valid")
}

/// A successful classification of `label` at `score`.
fn ok_response(label: &str, score: f32) -> ClassifyResponse {
    ClassifyResponse {
        request_id: "req-1".to_owned(),
        classifier_id: "complexity".to_owned(),
        model_revision: "c5f55ef4".to_owned(),
        tokenizer_revision: "tok-1".to_owned(),
        taxonomy_revision: "scr-default-anchors-v1".to_owned(),
        status: ClassificationStatus::Ok as i32,
        ranked: vec![RankedSignal {
            label: label.to_owned(),
            score,
        }],
    }
}

/// The value queued for an upstream request header, if any.
fn header(ctx: &praxis_filter::HttpFilterContext<'_>, name: &str) -> Option<String> {
    ctx.request_headers_to_set
        .iter()
        .find(|(header, _)| header.as_str() == name)
        .map(|(_, value)| value.to_str().expect("ascii header value").to_owned())
}

// -----------------------------------------------------------------------------
// T-I1
// -----------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn t_i1_ok_complex_routes_to_large_with_metadata_and_headers() {
    let stub = StubServer::start(StubBehaviour::Respond(Box::new(ok_response("COMPLEX", 0.7421))))
        .await
        .expect("stub must bind");
    let filter = filter(&stub.endpoint(), "timeout_ms: 5000\n");

    let harness = ContextHarness::post(CHAT_BODY).with_header("x-request-id", "trace-42");
    let mut ctx = harness.context();
    let action = filter.on_request(&mut ctx).await.expect("happy path never errors");

    assert!(matches!(action, FilterAction::Continue));
    assert_eq!(ctx.cluster_name(), Some("large"), "COMPLEX maps to the large cluster");

    assert_eq!(ctx.get_metadata("llm_d_sc.status"), Some("OK"));
    assert_eq!(ctx.get_metadata("llm_d_sc.label"), Some("COMPLEX"));
    assert_eq!(ctx.get_metadata("llm_d_sc.score"), Some("0.7421"), "score is 4 dp");
    assert_eq!(ctx.get_metadata("llm_d_sc.cluster"), Some("large"));
    assert_eq!(ctx.get_metadata("llm_d_sc.classifier_id"), Some("complexity"));
    assert_eq!(ctx.get_metadata("llm_d_sc.model_revision"), Some("c5f55ef4"));
    assert_eq!(ctx.get_metadata("llm_d_sc.tokenizer_revision"), Some("tok-1"));
    assert_eq!(
        ctx.get_metadata("llm_d_sc.taxonomy_revision"),
        Some("scr-default-anchors-v1")
    );
    assert!(
        ctx.get_metadata("llm_d_sc.latency_us").is_some(),
        "the RPC latency must be recorded for the access log"
    );

    assert_eq!(header(&ctx, "x-llm-d-sc-label").as_deref(), Some("COMPLEX"));
    assert_eq!(header(&ctx, "x-llm-d-sc-score").as_deref(), Some("0.7421"));
    assert_eq!(header(&ctx, "x-llm-d-sc-classifier").as_deref(), Some("complexity"));
    assert_eq!(
        header(&ctx, "x-llm-d-sc-taxonomy-revision").as_deref(),
        Some("scr-default-anchors-v1")
    );
    assert_eq!(header(&ctx, "x-llm-d-sc-status").as_deref(), Some("OK"));

    let seen = stub.requests();
    assert_eq!(seen.len(), 1, "exactly one classify call");
    assert_eq!(
        seen[0].request_id, "trace-42",
        "the proxy's x-request-id must be reused so the classification correlates with the access log"
    );
    assert_eq!(
        seen[0].context, "Design a microservices architecture",
        "the extracted prompt is what gets classified"
    );
    assert!(seen[0].session_id.is_empty(), "v0.1 has no session concept");
}

#[tokio::test(flavor = "multi_thread")]
async fn t_i1_without_x_request_id_a_counter_derived_id_is_sent() {
    let stub = StubServer::start(StubBehaviour::Respond(Box::new(ok_response("SIMPLE", 0.9))))
        .await
        .expect("stub must bind");
    let filter = filter(&stub.endpoint(), "timeout_ms: 5000\n");

    let harness = ContextHarness::post(CHAT_BODY);
    let mut ctx = harness.context();
    let _action = filter.on_request(&mut ctx).await.expect("happy path");

    let seen = stub.requests();
    assert!(
        seen[0].request_id.starts_with("llm-d-sc-"),
        "expected a counter-derived id, got {:?}",
        seen[0].request_id
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn t_i1_the_prompt_is_truncated_before_the_rpc() {
    let stub = StubServer::start(StubBehaviour::Respond(Box::new(ok_response("SIMPLE", 0.9))))
        .await
        .expect("stub must bind");
    let filter = filter(&stub.endpoint(), "timeout_ms: 5000\nmax_prompt_chars: 5\n");

    let harness = ContextHarness::post(r#"{"prompt":"🙂🙂🙂🙂🙂🙂🙂🙂"}"#);
    let mut ctx = harness.context();
    let _action = filter.on_request(&mut ctx).await.expect("happy path");

    assert_eq!(
        stub.requests()[0].context,
        "🙂🙂🙂🙂🙂",
        "truncation is by character, on a char boundary"
    );
}

// -----------------------------------------------------------------------------
// T-I2 / T-I3 — RESOURCE_EXHAUSTED
// -----------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn t_i2_resource_exhausted_fails_open_to_default_cluster() {
    let stub = StubServer::start(StubBehaviour::Fail(
        tonic::Code::ResourceExhausted,
        "inference queue is full".to_owned(),
    ))
    .await
    .expect("stub must bind");
    let filter = filter(&stub.endpoint(), "timeout_ms: 5000\n");

    let harness = ContextHarness::post(CHAT_BODY);
    let mut ctx = harness.context();
    let action = filter.on_request(&mut ctx).await.expect("fail-open never errors");

    assert!(
        matches!(action, FilterAction::Continue),
        "a full inference queue degrades routing quality, it does not take the gateway down"
    );
    assert_eq!(ctx.cluster_name(), Some("general"));
    assert_eq!(ctx.get_metadata("llm_d_sc.status"), Some("RESOURCE_EXHAUSTED"));
    assert_eq!(ctx.get_metadata("llm_d_sc.cluster"), Some("general"));
    assert_eq!(header(&ctx, "x-llm-d-sc-status").as_deref(), Some("RESOURCE_EXHAUSTED"));
}

#[tokio::test(flavor = "multi_thread")]
async fn t_i3_resource_exhausted_with_reject_posture_answers_503() {
    let stub = StubServer::start(StubBehaviour::Fail(
        tonic::Code::ResourceExhausted,
        "inference queue is full".to_owned(),
    ))
    .await
    .expect("stub must bind");
    let filter = filter(&stub.endpoint(), "timeout_ms: 5000\non_resource_exhausted: reject\n");

    let harness = ContextHarness::post(CHAT_BODY);
    let mut ctx = harness.context();
    let action = filter
        .on_request(&mut ctx)
        .await
        .expect("reject is an action, not an error");

    match action {
        FilterAction::Reject(rejection) => assert_eq!(rejection.status, 503, "status_on_reject defaults to 503"),
        other => panic!("expected Reject, got {other:?}"),
    }
    assert_eq!(ctx.get_metadata("llm_d_sc.status"), Some("RESOURCE_EXHAUSTED"));
    assert!(
        ctx.cluster.is_none(),
        "a rejected request must not also carry a cluster selection"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn t_i3_status_on_reject_is_configurable() {
    let stub = StubServer::start(StubBehaviour::Fail(tonic::Code::ResourceExhausted, "full".to_owned()))
        .await
        .expect("stub must bind");
    let filter = filter(
        &stub.endpoint(),
        "timeout_ms: 5000\non_resource_exhausted: reject\nstatus_on_reject: 429\n",
    );

    let harness = ContextHarness::post(CHAT_BODY);
    let mut ctx = harness.context();
    let action = filter.on_request(&mut ctx).await.expect("reject is an action");

    match action {
        FilterAction::Reject(rejection) => assert_eq!(rejection.status, 429),
        other => panic!("expected Reject, got {other:?}"),
    }
}

// -----------------------------------------------------------------------------
// T-I4 — local timeout
// -----------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn t_i4_slow_classifier_hits_the_local_budget_and_falls_back() {
    let stub = StubServer::start(StubBehaviour::SlowRespond(
        Duration::from_millis(1500),
        Box::new(ok_response("COMPLEX", 0.99)),
    ))
    .await
    .expect("stub must bind");
    let filter = filter(&stub.endpoint(), "timeout_ms: 50\n");

    let harness = ContextHarness::post(CHAT_BODY);
    let mut ctx = harness.context();
    let started = std::time::Instant::now();
    let action = filter.on_request(&mut ctx).await.expect("timeout is not an error");
    let elapsed = started.elapsed();

    assert!(matches!(action, FilterAction::Continue));
    assert_eq!(ctx.cluster_name(), Some("general"));
    assert_eq!(ctx.get_metadata("llm_d_sc.status"), Some("TIMEOUT"));
    assert!(
        elapsed < Duration::from_secs(1),
        "the timeout must actually bound the request, took {elapsed:?}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn t_i4_timeout_with_reject_posture_answers_the_configured_status() {
    let stub = StubServer::start(StubBehaviour::SlowRespond(
        Duration::from_millis(1500),
        Box::new(ok_response("COMPLEX", 0.99)),
    ))
    .await
    .expect("stub must bind");
    let filter = filter(&stub.endpoint(), "timeout_ms: 50\non_unavailable: reject\n");

    let harness = ContextHarness::post(CHAT_BODY);
    let mut ctx = harness.context();
    let action = filter.on_request(&mut ctx).await.expect("reject is an action");

    match action {
        FilterAction::Reject(rejection) => assert_eq!(rejection.status, 503),
        other => panic!("expected Reject, got {other:?}"),
    }
    assert_eq!(ctx.get_metadata("llm_d_sc.status"), Some("TIMEOUT"));
}

// -----------------------------------------------------------------------------
// T-I5 — nothing listening
// -----------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn t_i5_refused_endpoint_falls_back_without_panicking() {
    // Bind and immediately drop, so the port is almost certainly free and the
    // connect is refused rather than filtered.
    let endpoint = {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("ephemeral bind");
        listener.local_addr().expect("local addr").to_string()
    };

    // Construction succeeds with nothing listening: this is the `connect_lazy`
    // property that lets the proxy start before llm-d-sc does.
    let filter = filter(&endpoint, "timeout_ms: 2000\n");

    let harness = ContextHarness::post(CHAT_BODY);
    let mut ctx = harness.context();
    let action = filter
        .on_request(&mut ctx)
        .await
        .expect("a refused connect is not an error");

    assert!(matches!(action, FilterAction::Continue));
    assert_eq!(ctx.cluster_name(), Some("general"), "fail-open to default_cluster");
    let status = ctx.get_metadata("llm_d_sc.status").expect("status recorded");
    assert!(
        status == "ERROR" || status == "UNAVAILABLE",
        "a transport failure should record a failure status, got {status}"
    );
}

// -----------------------------------------------------------------------------
// T-I6 — one TCP connection for N calls
// -----------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn t_i6_two_sequential_requests_share_one_tcp_connection() {
    let stub = StubServer::start(StubBehaviour::Respond(Box::new(ok_response("SIMPLE", 0.8))))
        .await
        .expect("stub must bind");
    let filter = filter(&stub.endpoint(), "timeout_ms: 5000\n");

    for _ in 0..2 {
        let harness = ContextHarness::post(CHAT_BODY);
        let mut ctx = harness.context();
        let _action = filter.on_request(&mut ctx).await.expect("happy path");
        assert_eq!(ctx.cluster_name(), Some("small"));
    }

    assert_eq!(stub.requests().len(), 2, "both calls must reach the server");
    assert_eq!(
        stub.accepted_connections(),
        1,
        "N calls over one persistent HTTP/2 channel must produce exactly ONE accept; \
         measured server-side so the client cannot assert its own good behaviour"
    );
}

// -----------------------------------------------------------------------------
// T-I7 — the signals field
// -----------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn t_i7_signals_are_empty_when_signal_is_unset() {
    let stub = StubServer::start(StubBehaviour::Respond(Box::new(ok_response("SIMPLE", 0.8))))
        .await
        .expect("stub must bind");
    let filter = filter(&stub.endpoint(), "timeout_ms: 5000\n");

    let harness = ContextHarness::post(CHAT_BODY);
    let mut ctx = harness.context();
    let _action = filter.on_request(&mut ctx).await.expect("happy path");

    assert!(
        stub.requests()[0].signals.is_empty(),
        "llm-d-sc rejects any signal that is not the one it serves; an empty list is always accepted"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn t_i7_signals_carry_the_configured_signal_when_set() {
    let stub = StubServer::start(StubBehaviour::Respond(Box::new(ok_response("SIMPLE", 0.8))))
        .await
        .expect("stub must bind");
    let filter = filter(&stub.endpoint(), "timeout_ms: 5000\nsignal: complexity\n");

    let harness = ContextHarness::post(CHAT_BODY);
    let mut ctx = harness.context();
    let _action = filter.on_request(&mut ctx).await.expect("happy path");

    assert_eq!(
        stub.requests()[0].signals,
        vec!["complexity".to_owned()],
        "an explicitly configured signal is sent as the single entry"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn t_i7_a_mismatched_signal_surfaces_as_invalid_argument() {
    // What a real llm-d-sc does when `signal:` does not match its
    // LLM_D_SC_CLASSIFIER (verified in src/grpc/classify.rs).
    let stub = StubServer::start(StubBehaviour::Fail(
        tonic::Code::InvalidArgument,
        "unsupported signal 'sensitivity'; this instance serves 'complexity'".to_owned(),
    ))
    .await
    .expect("stub must bind");
    let filter = filter(&stub.endpoint(), "timeout_ms: 5000\nsignal: sensitivity\n");

    let harness = ContextHarness::post(CHAT_BODY);
    let mut ctx = harness.context();
    let action = filter
        .on_request(&mut ctx)
        .await
        .expect("misconfiguration is not a panic");

    assert!(matches!(action, FilterAction::Continue));
    assert_eq!(ctx.get_metadata("llm_d_sc.status"), Some("INVALID_ARGUMENT"));
    assert_eq!(ctx.cluster_name(), Some("general"));
}
