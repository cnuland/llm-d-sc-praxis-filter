//! YAML configuration for the `llm_d_sc` filter (SPEC §4.2).
//!
//! Every struct is `deny_unknown_fields` and every rule is checked at
//! construction: a bad config fails proxy startup, never a request.

use std::collections::{HashMap, HashSet};

use praxis_filter::FilterError;
use serde::Deserialize;

/// Praxis' absolute ceiling on buffered request bodies (64 MiB).
pub const ABSOLUTE_MAX_BODY_BYTES: usize = 64 * 1024 * 1024;

/// The filter's registry name, used as the prefix of every error message.
pub const FILTER_NAME: &str = "llm_d_sc";

// -----------------------------------------------------------------------------
// Config
// -----------------------------------------------------------------------------

/// One ranked-label -> cluster mapping.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RouteEntry {
    /// A ranked label produced by the llm-d-sc taxonomy (e.g. `COMPLEX`).
    pub label: String,

    /// The Praxis cluster this label routes to.
    pub cluster: String,
}

/// What to do when classification could not produce a usable answer.
///
/// The default is fail-open: a classifier outage degrades routing quality, it
/// does not take the gateway down.
#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum FailureAction {
    /// Fall back to `default_cluster` and keep serving the request.
    #[default]
    DefaultCluster,

    /// Answer the client with `status_on_reject`.
    Reject,
}

/// Parsed `llm_d_sc` filter configuration.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LlmDScConfig {
    /// `host:port` of the llm-d-sc instance (h2c, no TLS in v0.1).
    pub endpoint: String,

    /// When set, sent as the single entry of `ClassifyRequest.signals`.
    ///
    /// llm-d-sc rejects any signal that is not the one signal that instance
    /// serves, so the safe default is to send nothing at all (SPEC §2.1).
    #[serde(default)]
    pub signal: Option<String>,

    /// Fallback cluster for every non-decision path.
    pub default_cluster: String,

    /// Ranked label -> cluster mappings. Non-empty, labels unique.
    pub routes: Vec<RouteEntry>,

    /// Total budget for the classify RPC, in milliseconds.
    #[serde(default = "default_timeout_ms")]
    pub timeout_ms: u64,

    /// Top score below this routes to `default_cluster`.
    #[serde(default)]
    pub min_score: f32,

    /// Prompt is truncated to this many characters before the RPC.
    #[serde(default = "default_max_prompt_chars")]
    pub max_prompt_chars: usize,

    /// `StreamBuffer` ceiling; a larger body gets a 413 from the protocol layer.
    #[serde(default = "default_max_body_bytes")]
    pub max_body_bytes: usize,

    /// Behaviour on `UNAVAILABLE`, `INVALID_ARGUMENT`, timeout and other errors.
    #[serde(default)]
    pub on_unavailable: FailureAction,

    /// Behaviour on gRPC `RESOURCE_EXHAUSTED`.
    #[serde(default)]
    pub on_resource_exhausted: FailureAction,

    /// Status used by both `reject` modes.
    #[serde(default = "default_status_on_reject")]
    pub status_on_reject: u16,

    /// Emit `x-llm-d-sc-*` provenance headers to upstream.
    #[serde(default = "default_true")]
    pub emit_headers: bool,

    /// TCP connect timeout for the gRPC channel, in milliseconds.
    #[serde(default = "default_connect_timeout_ms")]
    pub connect_timeout_ms: u64,
}

/// Default classify budget (ms).
fn default_timeout_ms() -> u64 {
    100
}

/// Default prompt truncation ceiling (characters).
fn default_max_prompt_chars() -> usize {
    4096
}

/// Default buffered-body ceiling (1 MiB).
fn default_max_body_bytes() -> usize {
    1024 * 1024
}

/// Default rejection status.
fn default_status_on_reject() -> u16 {
    503
}

/// Default connect timeout (ms).
fn default_connect_timeout_ms() -> u64 {
    1000
}

/// `emit_headers` defaults to on.
fn default_true() -> bool {
    true
}

impl LlmDScConfig {
    /// Validate every rule from SPEC §4.2.
    ///
    /// # Errors
    ///
    /// Returns a [`FilterError`] naming the offending field.
    pub fn validate(&self) -> Result<(), FilterError> {
        if self.endpoint.trim().is_empty() {
            return Err(err("endpoint must not be empty"));
        }
        // The endpoint is an authority, not a URL: reject anything with a
        // scheme or a path so a typo fails at startup instead of at the first
        // request.
        if self.endpoint.contains("://") || self.endpoint.contains('/') {
            return Err(err(format!(
                "endpoint must be a host:port authority, not a URL (got '{}')",
                self.endpoint
            )));
        }
        let authority = format!("http://{}", self.endpoint);
        if authority.parse::<http::Uri>().is_err() {
            return Err(err(format!(
                "endpoint '{}' is not a valid URI authority",
                self.endpoint
            )));
        }

        if self.default_cluster.trim().is_empty() {
            return Err(err("default_cluster must not be empty"));
        }

        if self.routes.is_empty() {
            return Err(err("routes must not be empty"));
        }
        let mut seen = HashSet::with_capacity(self.routes.len());
        for route in &self.routes {
            if route.label.trim().is_empty() {
                return Err(err("routes[].label must not be empty"));
            }
            if route.cluster.trim().is_empty() {
                return Err(err(format!(
                    "routes[].cluster must not be empty (label '{}')",
                    route.label
                )));
            }
            if !seen.insert(route.label.as_str()) {
                return Err(err(format!("duplicate routes[].label '{}'", route.label)));
            }
        }

        if let Some(signal) = &self.signal
            && signal.trim().is_empty()
        {
            return Err(err("signal, when set, must not be empty"));
        }

        if !(100..=599).contains(&self.status_on_reject) {
            return Err(err(format!(
                "status_on_reject must be in 100..=599 (got {})",
                self.status_on_reject
            )));
        }

        for (field, value) in [
            ("timeout_ms", self.timeout_ms),
            ("connect_timeout_ms", self.connect_timeout_ms),
        ] {
            if value == 0 {
                return Err(err(format!("{field} must be greater than zero")));
            }
        }
        for (field, value) in [
            ("max_prompt_chars", self.max_prompt_chars),
            ("max_body_bytes", self.max_body_bytes),
        ] {
            if value == 0 {
                return Err(err(format!("{field} must be greater than zero")));
            }
        }
        if self.max_body_bytes > ABSOLUTE_MAX_BODY_BYTES {
            return Err(err(format!(
                "max_body_bytes ({}) exceeds the Praxis absolute maximum ({ABSOLUTE_MAX_BODY_BYTES})",
                self.max_body_bytes
            )));
        }
        if !self.min_score.is_finite() {
            return Err(err("min_score must be a finite number"));
        }

        Ok(())
    }

    /// Label -> cluster lookup table.
    #[must_use]
    pub fn route_map(&self) -> HashMap<String, String> {
        self.routes
            .iter()
            .map(|r| (r.label.clone(), r.cluster.clone()))
            .collect()
    }

    /// Every cluster this filter may select, deduped, including
    /// `default_cluster` (SPEC §4.3 — required by `check_misaligned_clusters`).
    #[must_use]
    pub fn all_clusters(&self) -> Vec<String> {
        let mut out = Vec::with_capacity(self.routes.len() + 1);
        let mut seen = HashSet::new();
        for name in std::iter::once(&self.default_cluster).chain(self.routes.iter().map(|r| &r.cluster)) {
            if seen.insert(name.as_str()) {
                out.push(name.clone());
            }
        }
        out
    }
}

/// Build a filter error prefixed with the filter name.
fn err(msg: impl std::fmt::Display) -> FilterError {
    format!("{FILTER_NAME}: {msg}").into()
}
