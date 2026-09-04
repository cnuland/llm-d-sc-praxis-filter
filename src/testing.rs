//! Test-only scaffolding: an [`HttpFilterContext`] builder and an in-process
//! stub `classify.Classify` server.
//!
//! Compiled only under the `test-server` feature, which this crate's own
//! dev-dependency turns on for `cargo test` and nothing else turns on for a
//! shipped build.

use std::{
    collections::HashMap,
    net::SocketAddr,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

use praxis_core::{id::IdGenerator, time::SystemTimeSource};
use praxis_filter::{BodyMode, HttpFilterContext, Request, RequestExtensions, SubRequestResponseMode};

use crate::pb::{
    ClassifyRequest, ClassifyResponse,
    classify_server::{Classify, ClassifyServer},
};

// -----------------------------------------------------------------------------
// Filter Context
// -----------------------------------------------------------------------------

/// Owns the borrowed pieces an [`HttpFilterContext`] needs.
pub struct ContextHarness {
    /// The request the context borrows.
    pub request: Request,
    /// Deterministic id source.
    pub id_generator: IdGenerator,
    /// Monotonic clock source.
    pub time_source: SystemTimeSource,
    /// The pre-read request body, as the protocol layer would supply it.
    pub body: Option<bytes::Bytes>,
}

impl ContextHarness {
    /// A `POST /v1/chat/completions` harness carrying `body` as the pre-read
    /// request body.
    #[must_use]
    pub fn post(body: impl Into<bytes::Bytes>) -> Self {
        Self {
            request: Request {
                method: http::Method::POST,
                uri: http::Uri::from_static("/v1/chat/completions"),
                headers: http::HeaderMap::new(),
            },
            id_generator: IdGenerator::with_seed(0),
            time_source: SystemTimeSource,
            body: Some(body.into()),
        }
    }

    /// A harness with no pre-read body at all.
    #[must_use]
    pub fn empty() -> Self {
        let mut harness = Self::post(bytes::Bytes::new());
        harness.body = None;
        harness
    }

    /// Add a request header.
    ///
    /// # Panics
    ///
    /// Panics if `name` or `value` is not a legal header.
    #[must_use]
    pub fn with_header(mut self, name: &'static str, value: &str) -> Self {
        let name: http::HeaderName = name.parse().expect("test header name");
        let value: http::HeaderValue = value.parse().expect("test header value");
        let _previous = self.request.headers.insert(name, value);
        self
    }

    /// Build the filter context.
    #[must_use]
    pub fn context(&self) -> HttpFilterContext<'_> {
        HttpFilterContext {
            buffered_request_body: self.body.clone(),
            body_done_indices: Vec::new(),
            branch_iterations: HashMap::new(),
            client_addr: None,
            cluster: None,
            current_filter_id: None,
            downstream_tls: false,
            extensions: RequestExtensions::default(),
            executed_filter_indices: Vec::new(),
            extra_request_headers: Vec::new(),
            request_headers_to_remove: Vec::new(),
            request_headers_to_set: Vec::new(),
            filter_metadata: HashMap::new(),
            pre_read_mutations: Vec::new(),
            structured_metadata: HashMap::new(),
            filter_results: HashMap::new(),
            filter_state: HashMap::new(),
            health_registry: None,
            id_generator: &self.id_generator,
            kv_stores: None,
            metrics_route: None,
            peer_identity: None,
            session_stores: None,
            subrequest_client: None,
            subrequest_response_mode: SubRequestResponseMode::Buffered,
            request: &self.request,
            request_body_bytes: 0,
            request_body_mode: BodyMode::Stream,
            request_start: Instant::now(),
            response_body_bytes: 0,
            response_body_mode: BodyMode::Stream,
            response_header: None,
            response_headers_modified: false,
            attempted_endpoints: Vec::new(),
            retry_policy: None,
            route_retry_policy: None,
            cluster_retry_state: None,
            cluster_retry_state_released: false,
            endpoint_reselector: None,
            pinned_endpoint_address: None,
            rewritten_path: None,
            selected_endpoint_index: None,
            time_source: &self.time_source,
            upstream: None,
        }
    }
}

// -----------------------------------------------------------------------------
// Stub Server
// -----------------------------------------------------------------------------

/// What the stub server should do with a classify call.
#[derive(Clone, Debug)]
pub enum StubBehaviour {
    /// Answer with this response.
    Respond(Box<ClassifyResponse>),

    /// Sleep this long, then answer.
    SlowRespond(Duration, Box<ClassifyResponse>),

    /// Fail with this gRPC code and message.
    Fail(tonic::Code, String),
}

/// The stub `classify.Classify` service.
#[derive(Clone, Debug)]
struct StubService {
    /// The scripted behaviour.
    behaviour: StubBehaviour,
    /// Every request the stub has received, in order.
    seen: Arc<Mutex<Vec<ClassifyRequest>>>,
}

#[tonic::async_trait]
impl Classify for StubService {
    async fn classify(
        &self,
        request: tonic::Request<ClassifyRequest>,
    ) -> Result<tonic::Response<ClassifyResponse>, tonic::Status> {
        let inner = request.into_inner();
        self.seen.lock().expect("stub request log poisoned").push(inner);
        match &self.behaviour {
            StubBehaviour::Respond(response) => Ok(tonic::Response::new((**response).clone())),
            StubBehaviour::SlowRespond(delay, response) => {
                tokio::time::sleep(*delay).await;
                Ok(tonic::Response::new((**response).clone()))
            },
            StubBehaviour::Fail(code, message) => Err(tonic::Status::new(*code, message.clone())),
        }
    }
}

/// A running in-process stub classify server.
#[derive(Debug)]
pub struct StubServer {
    /// The ephemeral address the stub bound to.
    addr: SocketAddr,
    /// Count of ACCEPTED TCP connections — the server-side evidence for T-I6.
    accepted: Arc<AtomicU64>,
    /// Every request the stub has received.
    seen: Arc<Mutex<Vec<ClassifyRequest>>>,
    /// Dropping this shuts the server task down.
    _shutdown: tokio::sync::oneshot::Sender<()>,
}

impl StubServer {
    /// Bind a stub on an ephemeral loopback port and start serving.
    ///
    /// Must be called from inside a Tokio runtime.
    ///
    /// # Errors
    ///
    /// Returns an I/O error if the listener cannot bind.
    pub async fn start(behaviour: StubBehaviour) -> std::io::Result<Self> {
        Self::start_on("127.0.0.1:0", behaviour).await
    }

    /// Bind a stub on a SPECIFIC address and start serving.
    ///
    /// Tests want an ephemeral port; a benchmark wants a fixed one, because the
    /// Praxis config that points at it is written ahead of time.
    ///
    /// # Errors
    ///
    /// Returns an I/O error if the listener cannot bind.
    pub async fn start_on(addr: &str, behaviour: StubBehaviour) -> std::io::Result<Self> {
        let listener = tokio::net::TcpListener::bind(addr).await?;
        let addr = listener.local_addr()?;

        let seen = Arc::new(Mutex::new(Vec::new()));
        let service = StubService {
            behaviour,
            seen: Arc::clone(&seen),
        };

        // Count every ACCEPTED TCP connection. A client that reuses one
        // persistent HTTP/2 channel across N calls produces exactly ONE accept;
        // a client that reconnects per call produces N. Measuring at the accept
        // boundary observes the property from OUTSIDE the client, so the client
        // cannot assert its own good behaviour. (Mirrors llm-d-sc's I-008.)
        let accepted = Arc::new(AtomicU64::new(0));
        let accept_counter = Arc::clone(&accepted);
        let incoming =
            tokio_stream::StreamExt::map(tokio_stream::wrappers::TcpListenerStream::new(listener), move |conn| {
                if conn.is_ok() {
                    let _previous = accept_counter.fetch_add(1, Ordering::SeqCst);
                }
                conn
            });

        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
        let serve = tonic::transport::Server::builder()
            .add_service(ClassifyServer::new(service))
            .serve_with_incoming_shutdown(incoming, async {
                let _received = shutdown_rx.await;
            });
        let _task = tokio::spawn(serve);

        Ok(Self {
            addr,
            accepted,
            seen,
            _shutdown: shutdown_tx,
        })
    }

    /// The `host:port` the stub is listening on.
    #[must_use]
    pub fn endpoint(&self) -> String {
        self.addr.to_string()
    }

    /// How many TCP connections the stub has accepted.
    #[must_use]
    pub fn accepted_connections(&self) -> u64 {
        self.accepted.load(Ordering::SeqCst)
    }

    /// Every classify request the stub has received, in order.
    ///
    /// # Panics
    ///
    /// Panics if the request log mutex is poisoned.
    #[must_use]
    pub fn requests(&self) -> Vec<ClassifyRequest> {
        self.seen.lock().expect("stub request log poisoned").clone()
    }
}
