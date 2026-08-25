//! The `llm_d_sc` [`HttpFilter`] implementation (SPEC §4.3 – §4.7).

use std::{
    collections::HashMap,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

use async_trait::async_trait;
use http::{HeaderName, HeaderValue, header::InvalidHeaderValue};
use praxis_filter::{
    BodyAccess, BodyMode, FilterAction, FilterError, HttpFilter, HttpFilterContext, Rejection, parse_filter_config,
};

use crate::{
    client::{ClassifyChannel, ClassifyFailure},
    config::{FILTER_NAME, FailureAction, LlmDScConfig},
    metrics,
    pb::{ClassificationStatus, ClassifyRequest, ClassifyResponse, RankedSignal},
    prompt::{extract_prompt, truncate_chars},
};

// -----------------------------------------------------------------------------
// Constants
// -----------------------------------------------------------------------------

/// Prefix of every provenance header this filter owns.
///
/// Client-supplied headers under this prefix are stripped on *every* path so a
/// caller can never forge provenance for a downstream policy filter. This
/// mirrors `endpoint_selector`'s trust posture.
pub const HEADER_PREFIX: &str = "x-llm-d-sc-";

/// `x-llm-d-sc-label`.
static HEADER_LABEL: HeaderName = HeaderName::from_static("x-llm-d-sc-label");
/// `x-llm-d-sc-score`.
static HEADER_SCORE: HeaderName = HeaderName::from_static("x-llm-d-sc-score");
/// `x-llm-d-sc-classifier`.
static HEADER_CLASSIFIER: HeaderName = HeaderName::from_static("x-llm-d-sc-classifier");
/// `x-llm-d-sc-taxonomy-revision`.
static HEADER_TAXONOMY: HeaderName = HeaderName::from_static("x-llm-d-sc-taxonomy-revision");
/// `x-llm-d-sc-status`.
static HEADER_STATUS: HeaderName = HeaderName::from_static("x-llm-d-sc-status");
/// `x-llm-d-sc-latency-us`.
///
/// The classify RPC wall clock, in microseconds. This is the only component of
/// the end-to-end latency decomposition that an outside observer cannot derive:
/// a benchmark client can measure total time and the upstream can report its
/// own, but the classify hop is invisible from both ends without this. Emitting
/// it makes `total = praxis + classify + upstream` a *measured* identity rather
/// than one inferred by subtraction.
static HEADER_LATENCY_US: HeaderName = HeaderName::from_static("x-llm-d-sc-latency-us");

/// The `llm_d_sc.status` values (SPEC §4.6).
pub mod status {
    /// A mapped cluster was selected from a ranked label.
    pub const OK: &str = "OK";
    /// The top label is not in `routes` — forward-compatible with taxonomy growth.
    pub const UNMAPPED_LABEL: &str = "UNMAPPED_LABEL";
    /// The top score was below `min_score`.
    pub const LOW_CONFIDENCE: &str = "LOW_CONFIDENCE";
    /// The response carried no ranked signals.
    pub const NO_SIGNAL: &str = "NO_SIGNAL";
    /// llm-d-sc abstained.
    pub const ABSTAIN: &str = "ABSTAIN";
    /// llm-d-sc reported `UNAVAILABLE` (or an unrecognised status).
    pub const UNAVAILABLE: &str = "UNAVAILABLE";
    /// The bounded inference queue was full.
    pub const RESOURCE_EXHAUSTED: &str = "RESOURCE_EXHAUSTED";
    /// The configured `signal:` is not the one this instance serves.
    pub const INVALID_ARGUMENT: &str = "INVALID_ARGUMENT";
    /// Any other gRPC error.
    pub const ERROR: &str = "ERROR";
    /// The local `timeout_ms` budget elapsed.
    pub const TIMEOUT: &str = "TIMEOUT";
    /// No prompt could be extracted; the RPC was skipped entirely.
    pub const SKIPPED_NO_PROMPT: &str = "SKIPPED_NO_PROMPT";
}

// -----------------------------------------------------------------------------
// Decision
// -----------------------------------------------------------------------------

/// The revision fingerprint carried by a classify response.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Provenance {
    /// Which classifier answered (e.g. `complexity`).
    pub classifier_id: String,
    /// Model weights revision.
    pub model_revision: String,
    /// Tokenizer revision.
    pub tokenizer_revision: String,
    /// Taxonomy revision.
    pub taxonomy_revision: String,
}

impl From<&ClassifyResponse> for Provenance {
    fn from(response: &ClassifyResponse) -> Self {
        Self {
            classifier_id: response.classifier_id.clone(),
            model_revision: response.model_revision.clone(),
            tokenizer_revision: response.tokenizer_revision.clone(),
            taxonomy_revision: response.taxonomy_revision.clone(),
        }
    }
}

/// What the decision table says to do with the request.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Route {
    /// Send the request to this cluster.
    Cluster(String),

    /// Answer the client with `status_on_reject`.
    Reject,
}

/// The complete outcome of one classification, before any context mutation.
///
/// [`LlmDScFilter::decide`] produces this from a `Result<ClassifyResponse,
/// ClassifyFailure>` and nothing else, so every row of SPEC §4.6 is reachable
/// in a unit test without a socket.
#[derive(Clone, Debug, PartialEq)]
pub struct Decision {
    /// Cluster selection, or rejection.
    pub route: Route,

    /// The `llm_d_sc.status` value.
    pub status: &'static str,

    /// The winning ranked label, when there was one.
    pub label: Option<String>,

    /// The winning ranked score, when there was one.
    pub score: Option<f32>,

    /// Revision fingerprint, when a response came back.
    pub provenance: Option<Provenance>,
}

impl Decision {
    /// A decision that carries nothing but a status and a fallback route.
    fn fallback(status: &'static str, action: FailureAction, default_cluster: &str) -> Self {
        Self {
            route: match action {
                FailureAction::DefaultCluster => Route::Cluster(default_cluster.to_owned()),
                FailureAction::Reject => Route::Reject,
            },
            status,
            label: None,
            score: None,
            provenance: None,
        }
    }
}

// -----------------------------------------------------------------------------
// Filter
// -----------------------------------------------------------------------------

/// Routes an OpenAI-shaped request to a Praxis cluster using llm-d-sc.
#[derive(Debug)]
pub struct LlmDScFilter {
    /// Validated configuration.
    config: LlmDScConfig,

    /// Label -> cluster lookup, precomputed from `config.routes`.
    routes: HashMap<String, String>,

    /// Every cluster this filter may select, including `default_cluster`.
    clusters: Vec<String>,

    /// Persistent, lazily-connected classify channel.
    channel: ClassifyChannel,

    /// `config.timeout_ms` as a [`Duration`].
    timeout: Duration,

    /// Fallback source of correlation ids when no `x-request-id` is present.
    ///
    /// A counter rather than a UUID: llm-d-sc only echoes this value back, and
    /// a random-id dependency would be new third-party surface for nothing.
    request_counter: AtomicU64,
}

impl LlmDScFilter {
    /// Build the filter from its YAML config.
    ///
    /// Validation happens here, so a bad config fails proxy startup rather than
    /// a request. The gRPC channel is built lazily and performs no I/O.
    ///
    /// # Errors
    ///
    /// Returns a [`FilterError`] on a malformed or invalid configuration.
    pub fn from_config(config: &serde_yaml::Value) -> Result<Box<dyn HttpFilter>, FilterError> {
        let config: LlmDScConfig = parse_filter_config(FILTER_NAME, config)?;
        config.validate()?;
        Ok(Box::new(Self::new(config)?))
    }

    /// Build the filter from an already-deserialized config.
    ///
    /// # Errors
    ///
    /// Returns a [`FilterError`] if the config is invalid or the endpoint
    /// cannot be turned into a URI.
    pub fn new(config: LlmDScConfig) -> Result<Self, FilterError> {
        config.validate()?;
        let channel = ClassifyChannel::connect_lazy(&config)?;
        Ok(Self {
            routes: config.route_map(),
            clusters: config.all_clusters(),
            timeout: Duration::from_millis(config.timeout_ms),
            channel,
            config,
            request_counter: AtomicU64::new(0),
        })
    }

    /// The validated configuration.
    #[must_use]
    pub fn config(&self) -> &LlmDScConfig {
        &self.config
    }

    /// The `label` value for `llm_d_sc_route_total`, bounded by config.
    ///
    /// The ranked label is chosen by llm-d-sc, not by us. Emitting it verbatim
    /// would let an upstream service mint unbounded Prometheus series simply by
    /// growing its taxonomy. Only labels the operator actually routes on are
    /// emitted; anything else collapses to
    /// [`metrics::UNMAPPED_LABEL_SENTINEL`], and a decision with no label at
    /// all (the skip and failure paths) is attributed to its status.
    #[must_use]
    pub fn metric_label(&self, decision: &Decision) -> String {
        match &decision.label {
            Some(label) if self.routes.contains_key(label) => label.clone(),
            Some(_) => metrics::UNMAPPED_LABEL_SENTINEL.to_owned(),
            None => decision.status.to_owned(),
        }
    }

    /// Map a classify outcome onto a routing decision (SPEC §4.6).
    ///
    /// Pure: no context, no clock, no network. This is the seam the decision
    /// table tests drive.
    #[must_use]
    pub fn decide(&self, outcome: Result<ClassifyResponse, ClassifyFailure>) -> Decision {
        let default_cluster = self.config.default_cluster.as_str();
        let response = match outcome {
            Ok(response) => response,
            Err(ClassifyFailure::Timeout) => {
                return Decision::fallback(status::TIMEOUT, self.config.on_unavailable, default_cluster);
            },
            Err(ClassifyFailure::Status(grpc)) => {
                let (label, action) = match grpc.code() {
                    tonic::Code::ResourceExhausted => (status::RESOURCE_EXHAUSTED, self.config.on_resource_exhausted),
                    tonic::Code::InvalidArgument => {
                        tracing::warn!(
                            message = grpc.message(),
                            "llm-d-sc rejected the configured signal; check that `signal:` matches LLM_D_SC_CLASSIFIER"
                        );
                        (status::INVALID_ARGUMENT, self.config.on_unavailable)
                    },
                    _ => (status::ERROR, self.config.on_unavailable),
                };
                return Decision::fallback(label, action, default_cluster);
            },
        };

        let provenance = Provenance::from(&response);
        let wire_status = ClassificationStatus::try_from(response.status);
        match wire_status {
            Ok(ClassificationStatus::Ok) => {},
            Ok(ClassificationStatus::Abstain) => {
                return Decision {
                    provenance: Some(provenance),
                    ..Decision::fallback(status::ABSTAIN, FailureAction::DefaultCluster, default_cluster)
                };
            },
            // UNAVAILABLE, UNSPECIFIED, and anything a newer server might add
            // all mean "no usable answer", so they share the operator's
            // `on_unavailable` posture.
            Ok(ClassificationStatus::Unavailable | ClassificationStatus::Unspecified) | Err(_) => {
                return Decision {
                    provenance: Some(provenance),
                    ..Decision::fallback(status::UNAVAILABLE, self.config.on_unavailable, default_cluster)
                };
            },
        }

        // The wire contract does not promise ordering, so the winner is the
        // maximum by score, never `ranked[0]`.
        let Some(top) = top_signal(&response.ranked) else {
            return Decision {
                provenance: Some(provenance),
                ..Decision::fallback(status::NO_SIGNAL, FailureAction::DefaultCluster, default_cluster)
            };
        };

        let (route, decision_status) = if top.score < self.config.min_score {
            (Route::Cluster(default_cluster.to_owned()), status::LOW_CONFIDENCE)
        } else {
            match self.routes.get(&top.label) {
                Some(cluster) => (Route::Cluster(cluster.clone()), status::OK),
                None => (Route::Cluster(default_cluster.to_owned()), status::UNMAPPED_LABEL),
            }
        };

        Decision {
            route,
            status: decision_status,
            label: Some(top.label.clone()),
            score: Some(top.score),
            provenance: Some(provenance),
        }
    }

    /// Build the `ClassifyRequest` for a prompt (SPEC §4.5 step 3).
    fn build_request(&self, request_id: String, prompt: &str) -> ClassifyRequest {
        ClassifyRequest {
            request_id,
            // v0.1 has no session concept in the filter.
            session_id: String::new(),
            context: truncate_chars(prompt, self.config.max_prompt_chars).to_owned(),
            // An empty list is always accepted; a non-matching entry is an
            // INVALID_ARGUMENT from llm-d-sc (SPEC §2.1).
            signals: self.config.signal.clone().map_or_else(Vec::new, |s| vec![s]),
        }
    }

    /// Prefer the proxy's `x-request-id` so a classification correlates with
    /// the access log; otherwise derive one from a per-filter counter.
    fn request_id(&self, ctx: &HttpFilterContext<'_>) -> String {
        match ctx.request_id() {
            Some(id) if !id.is_empty() => id.to_owned(),
            _ => format!("llm-d-sc-{}", self.request_counter.fetch_add(1, Ordering::Relaxed)),
        }
    }

    /// Remove every client-supplied `x-llm-d-sc-*` header (SPEC §4.5 step 1).
    fn strip_client_provenance(ctx: &mut HttpFilterContext<'_>) {
        let forged: Vec<HeaderName> = ctx
            .request
            .headers
            .keys()
            .filter(|name| name.as_str().starts_with(HEADER_PREFIX))
            .cloned()
            .collect();
        for name in forged {
            ctx.request_headers_to_remove.push(name);
        }
    }

    /// Write metadata, headers and metrics, and turn the decision into an action.
    fn apply(&self, ctx: &mut HttpFilterContext<'_>, decision: &Decision, elapsed: Option<Duration>) -> FilterAction {
        ctx.set_metadata("llm_d_sc.status", decision.status);
        if let Some(label) = &decision.label {
            ctx.set_metadata("llm_d_sc.label", label.clone());
        }
        if let Some(score) = decision.score {
            ctx.set_metadata("llm_d_sc.score", format!("{score:.4}"));
        }
        if let Some(provenance) = &decision.provenance {
            ctx.set_metadata("llm_d_sc.classifier_id", provenance.classifier_id.clone());
            ctx.set_metadata("llm_d_sc.model_revision", provenance.model_revision.clone());
            ctx.set_metadata("llm_d_sc.tokenizer_revision", provenance.tokenizer_revision.clone());
            ctx.set_metadata("llm_d_sc.taxonomy_revision", provenance.taxonomy_revision.clone());
        }
        if let Some(elapsed) = elapsed {
            let micros = u64::try_from(elapsed.as_micros()).unwrap_or(u64::MAX);
            ctx.set_metadata("llm_d_sc.latency_us", micros.to_string());
        }

        metrics::record_classify(decision.status, elapsed.map(|e| e.as_secs_f64()));

        let action = match &decision.route {
            Route::Cluster(cluster) => {
                ctx.set_metadata("llm_d_sc.cluster", cluster.clone());
                ctx.cluster = Some(Arc::from(cluster.as_str()));
                metrics::record_route(self.metric_label(decision), cluster.clone());
                FilterAction::Continue
            },
            Route::Reject => {
                tracing::warn!(
                    status = decision.status,
                    "llm_d_sc rejecting request: classification unavailable and the configured posture is `reject`"
                );
                FilterAction::Reject(Rejection::status(self.config.status_on_reject))
            },
        };

        if self.config.emit_headers {
            Self::emit_headers(ctx, decision, elapsed);
        }

        action
    }

    /// Set the provenance headers the upstream sees.
    fn emit_headers(ctx: &mut HttpFilterContext<'_>, decision: &Decision, elapsed: Option<Duration>) {
        set_header(ctx, &HEADER_STATUS, decision.status);
        if let Some(elapsed) = elapsed {
            let micros = u64::try_from(elapsed.as_micros()).unwrap_or(u64::MAX);
            set_header(ctx, &HEADER_LATENCY_US, &micros.to_string());
        }
        if let Some(label) = &decision.label {
            set_header(ctx, &HEADER_LABEL, label);
        }
        if let Some(score) = decision.score {
            set_header(ctx, &HEADER_SCORE, &format!("{score:.4}"));
        }
        if let Some(provenance) = &decision.provenance {
            set_header(ctx, &HEADER_CLASSIFIER, &provenance.classifier_id);
            set_header(ctx, &HEADER_TAXONOMY, &provenance.taxonomy_revision);
        }
    }
}

/// The ranked signal with the highest score, if any.
///
/// `max_by` with [`f32::total_cmp`] rather than `ranked[0]`: the wire contract
/// does not promise ordering.
fn top_signal(ranked: &[RankedSignal]) -> Option<&RankedSignal> {
    ranked.iter().max_by(|a, b| a.score.total_cmp(&b.score))
}

/// Queue an upstream header, skipping values HTTP cannot carry.
fn set_header(ctx: &mut HttpFilterContext<'_>, name: &HeaderName, value: &str) {
    if value.is_empty() {
        return;
    }
    match HeaderValue::from_str(value) {
        Ok(value) => ctx.request_headers_to_set.push((name.clone(), value)),
        Err(e) => log_bad_header(name, &e),
    }
}

/// A classifier-supplied value that is not a legal header value.
fn log_bad_header(name: &HeaderName, error: &InvalidHeaderValue) {
    tracing::warn!(header = %name, error = %error, "llm_d_sc provenance value is not a valid header value");
}

// -----------------------------------------------------------------------------
// HttpFilter
// -----------------------------------------------------------------------------

#[async_trait]
impl HttpFilter for LlmDScFilter {
    fn name(&self) -> &'static str {
        FILTER_NAME
    }

    fn selects_cluster(&self) -> bool {
        true
    }

    fn selected_clusters(&self) -> Vec<String> {
        self.clusters.clone()
    }

    /// `ReadOnly` is load-bearing: `compute_body_capabilities` ignores
    /// [`HttpFilter::request_body_mode`] entirely when access is
    /// [`BodyAccess::None`], and `ctx.buffered_request_body` would be `None`.
    fn request_body_access(&self) -> BodyAccess {
        BodyAccess::ReadOnly
    }

    fn request_body_mode(&self) -> BodyMode {
        BodyMode::StreamBuffer {
            max_bytes: Some(self.config.max_body_bytes),
        }
    }

    /// The body is pre-read whole before the header phase, so all the work
    /// happens in [`HttpFilter::on_request`]; chunks are of no interest.
    async fn on_request_body(
        &self,
        _ctx: &mut HttpFilterContext<'_>,
        _body: &mut Option<bytes::Bytes>,
        _end_of_stream: bool,
    ) -> Result<FilterAction, FilterError> {
        Ok(FilterAction::BodyDone)
    }

    async fn on_request(&self, ctx: &mut HttpFilterContext<'_>) -> Result<FilterAction, FilterError> {
        // Step 1: always, even when everything else is skipped.
        Self::strip_client_provenance(ctx);

        // Step 2: no prompt is never a 4xx — a classifier filter must not
        // become a request validator.
        let prompt = ctx.buffered_request_body.as_ref().and_then(|body| extract_prompt(body));
        let Some(prompt) = prompt else {
            let decision = Decision::fallback(
                status::SKIPPED_NO_PROMPT,
                FailureAction::DefaultCluster,
                &self.config.default_cluster,
            );
            return Ok(self.apply(ctx, &decision, None));
        };

        // Steps 3-4.
        let request = self.build_request(self.request_id(ctx), &prompt);
        let started = Instant::now();
        let outcome = self.channel.classify(request, self.timeout).await;
        let elapsed = started.elapsed();

        // Steps 5-8.
        let decision = self.decide(outcome);
        Ok(self.apply(ctx, &decision, Some(elapsed)))
    }
}
