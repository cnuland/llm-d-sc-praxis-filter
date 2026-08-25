//! T-U1 (configuration parsing and validation) and T-U2 (`selected_clusters`).

use llm_d_sc_praxis_filter::{FailureAction, LlmDScConfig, LlmDScFilter};
use praxis_filter::HttpFilter;

/// A config that satisfies every validation rule.
fn valid_yaml() -> &'static str {
    r#"
endpoint: "127.0.0.1:50051"
default_cluster: general
routes:
  - label: SIMPLE
    cluster: small
  - label: MEDIUM
    cluster: small
  - label: COMPLEX
    cluster: large
  - label: REASONING
    cluster: large
"#
}

/// Parse a YAML snippet the way the Praxis registry would.
fn from_config(yaml: &str) -> Result<Box<dyn HttpFilter>, praxis_filter::FilterError> {
    let value: serde_yaml::Value = serde_yaml::from_str(yaml).expect("test YAML must be well-formed");
    LlmDScFilter::from_config(&value)
}

/// Parse straight into the config struct.
fn parse(yaml: &str) -> Result<LlmDScConfig, String> {
    let value: serde_yaml::Value = serde_yaml::from_str(yaml).expect("test YAML must be well-formed");
    let config: LlmDScConfig = praxis_filter::parse_filter_config("llm_d_sc", &value).map_err(|e| e.to_string())?;
    config.validate().map_err(|e| e.to_string())?;
    Ok(config)
}

// -----------------------------------------------------------------------------
// T-U1 — configuration
// -----------------------------------------------------------------------------

#[test]
fn t_u1_valid_config_parses_with_documented_defaults() {
    let config = parse(valid_yaml()).expect("valid config should parse");

    assert_eq!(config.endpoint, "127.0.0.1:50051", "endpoint should round-trip");
    assert_eq!(config.default_cluster, "general", "default_cluster should round-trip");
    assert_eq!(config.routes.len(), 4, "all four routes should parse");
    assert_eq!(config.timeout_ms, 100, "timeout_ms should default to 100");
    assert_eq!(
        config.connect_timeout_ms, 1000,
        "connect_timeout_ms should default to 1000"
    );
    assert_eq!(config.max_prompt_chars, 4096, "max_prompt_chars should default to 4096");
    assert_eq!(
        config.max_body_bytes,
        1024 * 1024,
        "max_body_bytes should default to 1 MiB"
    );
    assert_eq!(config.status_on_reject, 503, "status_on_reject should default to 503");
    assert!(config.emit_headers, "emit_headers should default to true");
    assert!(
        (config.min_score - 0.0).abs() < f32::EPSILON,
        "min_score should default to 0"
    );
    assert_eq!(
        config.on_unavailable,
        FailureAction::DefaultCluster,
        "on_unavailable should default to fail-open"
    );
    assert_eq!(
        config.on_resource_exhausted,
        FailureAction::DefaultCluster,
        "on_resource_exhausted should default to fail-open"
    );
    assert!(
        config.signal.is_none(),
        "signal should default to unset (empty signals)"
    );
}

#[test]
fn t_u1_explicit_values_override_defaults() {
    let config = parse(&format!(
        "{}\nsignal: complexity\ntimeout_ms: 250\nmin_score: 0.5\nmax_prompt_chars: 64\n\
         max_body_bytes: 2048\non_unavailable: reject\non_resource_exhausted: reject\n\
         status_on_reject: 429\nemit_headers: false\nconnect_timeout_ms: 2000\n",
        valid_yaml()
    ))
    .expect("explicit config should parse");

    assert_eq!(config.signal.as_deref(), Some("complexity"));
    assert_eq!(config.timeout_ms, 250);
    assert!((config.min_score - 0.5).abs() < f32::EPSILON);
    assert_eq!(config.max_prompt_chars, 64);
    assert_eq!(config.max_body_bytes, 2048);
    assert_eq!(config.on_unavailable, FailureAction::Reject);
    assert_eq!(config.on_resource_exhausted, FailureAction::Reject);
    assert_eq!(config.status_on_reject, 429);
    assert!(!config.emit_headers);
    assert_eq!(config.connect_timeout_ms, 2000);
}

#[test]
fn t_u1_unknown_field_is_rejected() {
    let err = parse(&format!("{}\nnot_a_real_field: 1\n", valid_yaml()))
        .expect_err("an unknown field must fail construction");
    assert!(
        err.contains("not_a_real_field"),
        "error should name the offending field, got: {err}"
    );
}

#[test]
fn t_u1_empty_routes_rejected() {
    let err = parse("endpoint: \"127.0.0.1:50051\"\ndefault_cluster: general\nroutes: []\n")
        .expect_err("empty routes must fail construction");
    assert!(err.contains("routes must not be empty"), "got: {err}");
}

#[test]
fn t_u1_duplicate_label_rejected() {
    let yaml = "endpoint: \"127.0.0.1:50051\"\ndefault_cluster: general\n\
                routes:\n  - {label: SIMPLE, cluster: small}\n  - {label: SIMPLE, cluster: large}\n";
    let err = parse(yaml).expect_err("a duplicate label must fail construction");
    assert!(err.contains("duplicate routes[].label 'SIMPLE'"), "got: {err}");
}

#[test]
fn t_u1_empty_cluster_name_rejected() {
    let yaml = "endpoint: \"127.0.0.1:50051\"\ndefault_cluster: general\n\
                routes:\n  - {label: SIMPLE, cluster: \"\"}\n";
    let err = parse(yaml).expect_err("an empty cluster name must fail construction");
    assert!(err.contains("routes[].cluster must not be empty"), "got: {err}");
}

#[test]
fn t_u1_empty_default_cluster_rejected() {
    let yaml = "endpoint: \"127.0.0.1:50051\"\ndefault_cluster: \"\"\n\
                routes:\n  - {label: SIMPLE, cluster: small}\n";
    let err = parse(yaml).expect_err("an empty default_cluster must fail construction");
    assert!(err.contains("default_cluster must not be empty"), "got: {err}");
}

#[test]
fn t_u1_bad_status_code_rejected() {
    let err = parse(&format!("{}\nstatus_on_reject: 999\n", valid_yaml()))
        .expect_err("a status outside 100..=599 must fail construction");
    assert!(err.contains("status_on_reject must be in 100..=599"), "got: {err}");
}

#[test]
fn t_u1_zero_timeout_rejected() {
    let err = parse(&format!("{}\ntimeout_ms: 0\n", valid_yaml())).expect_err("a zero timeout must fail construction");
    assert!(err.contains("timeout_ms must be greater than zero"), "got: {err}");
}

#[test]
fn t_u1_zero_connect_timeout_rejected() {
    let err = parse(&format!("{}\nconnect_timeout_ms: 0\n", valid_yaml()))
        .expect_err("a zero connect timeout must fail construction");
    assert!(
        err.contains("connect_timeout_ms must be greater than zero"),
        "got: {err}"
    );
}

#[test]
fn t_u1_zero_max_prompt_chars_rejected() {
    let err = parse(&format!("{}\nmax_prompt_chars: 0\n", valid_yaml()))
        .expect_err("a zero prompt ceiling must fail construction");
    assert!(err.contains("max_prompt_chars must be greater than zero"), "got: {err}");
}

#[test]
fn t_u1_max_body_bytes_above_absolute_ceiling_rejected() {
    let err = parse(&format!("{}\nmax_body_bytes: 67108865\n", valid_yaml()))
        .expect_err("a body ceiling above 64 MiB must fail construction");
    assert!(err.contains("exceeds the Praxis absolute maximum"), "got: {err}");
}

#[test]
fn t_u1_empty_endpoint_rejected() {
    let yaml = "endpoint: \"\"\ndefault_cluster: general\nroutes:\n  - {label: SIMPLE, cluster: small}\n";
    let err = parse(yaml).expect_err("an empty endpoint must fail construction");
    assert!(err.contains("endpoint must not be empty"), "got: {err}");
}

#[test]
fn t_u1_endpoint_with_scheme_rejected() {
    let yaml = "endpoint: \"http://127.0.0.1:50051\"\ndefault_cluster: general\n\
                routes:\n  - {label: SIMPLE, cluster: small}\n";
    let err = parse(yaml).expect_err("a URL endpoint must fail construction");
    assert!(err.contains("host:port authority"), "got: {err}");
}

#[test]
fn t_u1_from_config_builds_a_filter_without_touching_the_network() {
    // 127.0.0.1:1 has nothing listening. `connect_lazy` means construction
    // still succeeds, which is what lets the proxy start before llm-d-sc does.
    let yaml = "endpoint: \"127.0.0.1:1\"\ndefault_cluster: general\nroutes:\n  - {label: SIMPLE, cluster: small}\n";
    let filter = from_config(yaml).expect("construction must not require a reachable classifier");
    assert_eq!(filter.name(), "llm_d_sc", "registry name should be llm_d_sc");
}

#[test]
fn t_u1_structural_filter_entry_keys_are_ignored() {
    // `parse_filter_config` strips the FilterEntry wrapper keys before
    // deserialization, so `deny_unknown_fields` must not choke on them.
    let yaml = format!("filter: llm_d_sc\nname: classify\n{}", valid_yaml());
    let filter = from_config(&yaml).expect("structural keys should be stripped, not rejected");
    assert_eq!(filter.name(), "llm_d_sc");
}

// -----------------------------------------------------------------------------
// T-U2 — selected_clusters
// -----------------------------------------------------------------------------

#[test]
fn t_u2_selected_clusters_include_default_and_are_deduped() {
    let filter = from_config(valid_yaml()).expect("valid config");
    let mut clusters = filter.selected_clusters();
    clusters.sort();

    assert_eq!(
        clusters,
        vec!["general".to_owned(), "large".to_owned(), "small".to_owned()],
        "selected_clusters must be the mapped clusters plus default_cluster, deduped"
    );
    assert!(
        filter.selects_cluster(),
        "selects_cluster must be true or check_lb_without_cluster_selector rejects the chain"
    );
}

#[test]
fn t_u2_default_cluster_also_used_as_a_route_target_appears_once() {
    let yaml = "endpoint: \"127.0.0.1:50051\"\ndefault_cluster: general\n\
                routes:\n  - {label: SIMPLE, cluster: general}\n  - {label: COMPLEX, cluster: large}\n";
    let filter = from_config(yaml).expect("valid config");
    let clusters = filter.selected_clusters();

    assert_eq!(clusters.iter().filter(|c| *c == "general").count(), 1, "deduped");
    assert_eq!(clusters.len(), 2, "should be exactly general and large");
}

// -----------------------------------------------------------------------------
// Body capability declarations (SPEC 4.3)
// -----------------------------------------------------------------------------

#[test]
fn body_access_is_read_only_so_stream_buffer_is_honoured() {
    let filter = from_config(&format!("{}\nmax_body_bytes: 4096\n", valid_yaml())).expect("valid config");

    assert_eq!(
        filter.request_body_access(),
        praxis_filter::BodyAccess::ReadOnly,
        "compute_body_capabilities ignores request_body_mode entirely when access is None"
    );
    assert_eq!(
        filter.request_body_mode(),
        praxis_filter::BodyMode::StreamBuffer { max_bytes: Some(4096) },
        "StreamBuffer is what triggers the whole-body pre-read before routing"
    );
}
