//! T-U6 — the vendored proto is byte-identical to llm-d-sc's.
//!
//! Vendoring is what keeps this crate buildable standalone (constraint C1:
//! llm-d-sc is frozen and never patched). This test is the thing that stops the
//! vendored copy from silently drifting.

use std::path::PathBuf;

/// Where the upstream proto lives, honouring an explicit override.
fn upstream_proto() -> Option<PathBuf> {
    let root = std::env::var_os("LLM_D_SC_GENESIS_DIR")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join("llm-d-sc-genesis")))?;
    let path = root.join("proto/classify.proto");
    path.is_file().then_some(path)
}

#[test]
fn t_u6_vendored_proto_matches_upstream_byte_for_byte() {
    let vendored_path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("proto/classify.proto");
    let vendored = std::fs::read(&vendored_path).expect("the vendored proto must exist");

    let Some(upstream_path) = upstream_proto() else {
        eprintln!(
            "T-U6 SKIPPED: llm-d-sc-genesis checkout not found. \
             Set LLM_D_SC_GENESIS_DIR to the repository root to run this assertion."
        );
        return;
    };

    let upstream = std::fs::read(&upstream_path).expect("upstream proto is readable");
    assert_eq!(
        vendored,
        upstream,
        "vendored {} has drifted from {}; re-copy it (llm-d-sc is frozen, so the vendored copy follows it)",
        vendored_path.display(),
        upstream_path.display()
    );
}

#[test]
fn t_u6_generated_bindings_match_the_frozen_wire_contract() {
    use llm_d_sc_praxis_filter::pb::{ClassificationStatus, ClassifyRequest, ClassifyResponse, RankedSignal};

    // The enum values are load-bearing: the decision table branches on them.
    assert_eq!(ClassificationStatus::Unspecified as i32, 0);
    assert_eq!(ClassificationStatus::Ok as i32, 1);
    assert_eq!(ClassificationStatus::Abstain as i32, 2);
    assert_eq!(ClassificationStatus::Unavailable as i32, 3);

    // ADR-0001 / AC-010: a route is unrepresentable on the wire. This
    // constructor would not compile if the response grew a route field.
    let response = ClassifyResponse {
        request_id: String::new(),
        classifier_id: String::new(),
        model_revision: String::new(),
        tokenizer_revision: String::new(),
        taxonomy_revision: String::new(),
        status: ClassificationStatus::Ok as i32,
        ranked: vec![RankedSignal {
            label: "SIMPLE".to_owned(),
            score: 1.0,
        }],
    };
    assert_eq!(response.ranked.len(), 1);

    let request = ClassifyRequest {
        request_id: String::new(),
        session_id: String::new(),
        context: String::new(),
        signals: Vec::new(),
    };
    assert!(request.signals.is_empty());
}
