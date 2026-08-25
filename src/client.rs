//! The llm-d-sc gRPC client (SPEC §4.8).
//!
//! One lazily-connected [`Channel`] is shared by every request. `connect_lazy`
//! means no blocking I/O and no connect attempt up front, so the proxy starts
//! even when llm-d-sc is down and tonic reconnects transparently — which is
//! also what makes N calls share ONE TCP connection.
//!
//! DEVIATION from SPEC §4.8, which says the channel is built "once at filter
//! construction": `Endpoint::connect_lazy` spawns the channel's background task
//! through `tokio::spawn`, so it panics outside a Tokio runtime — and Praxis
//! builds its pipelines synchronously in `server::run_server_with_registry`,
//! before any runtime exists. The [`Endpoint`] (and therefore all URI and
//! timeout validation) is still built in `from_config`; only the `tokio::spawn`
//! is deferred to the first request, behind a [`OnceLock`] so exactly one
//! channel is ever created. Every property the spec wanted from
//! construction-time building is preserved: no blocking I/O at startup, no
//! connect attempt before the first request, and one connection for N calls.

use std::{sync::OnceLock, time::Duration};

use praxis_filter::FilterError;
use tonic::transport::{Channel, Endpoint};

use crate::{
    config::{FILTER_NAME, LlmDScConfig},
    pb::{ClassifyRequest, ClassifyResponse, classify_client::ClassifyClient},
};

/// HTTP/2 keep-alive ping interval for the classify channel.
const KEEP_ALIVE_INTERVAL: Duration = Duration::from_secs(30);

// -----------------------------------------------------------------------------
// Failure
// -----------------------------------------------------------------------------

/// Why a classify call did not return a response.
///
/// Kept separate from [`tonic::Status`] so the local timeout — which has no
/// gRPC status of its own — is a first-class outcome in the decision table.
#[derive(Debug)]
pub enum ClassifyFailure {
    /// The local `timeout_ms` budget elapsed before the RPC completed.
    Timeout,

    /// The RPC completed with a gRPC error status.
    Status(Box<tonic::Status>),
}

impl ClassifyFailure {
    /// Wrap a gRPC status.
    #[must_use]
    pub fn status(status: tonic::Status) -> Self {
        Self::Status(Box::new(status))
    }
}

// -----------------------------------------------------------------------------
// Client
// -----------------------------------------------------------------------------

/// A persistent, lazily-connected classify channel.
#[derive(Debug)]
pub struct ClassifyChannel {
    /// Fully configured endpoint; built and validated at filter construction.
    endpoint: Endpoint,

    /// The shared HTTP/2 channel, materialized on first use inside a runtime.
    /// Cloning it is a cheap handle, not a connect.
    channel: OnceLock<Channel>,
}

impl ClassifyChannel {
    /// Build the channel from config without touching the network.
    ///
    /// # Errors
    ///
    /// Returns a [`FilterError`] if `endpoint` cannot be turned into a URI.
    pub fn connect_lazy(config: &LlmDScConfig) -> Result<Self, FilterError> {
        let uri = format!("http://{}", config.endpoint);
        let endpoint = Endpoint::from_shared(uri)
            .map_err(|e| -> FilterError { format!("{FILTER_NAME}: invalid endpoint: {e}").into() })?
            .connect_timeout(Duration::from_millis(config.connect_timeout_ms))
            .tcp_nodelay(true)
            .http2_keep_alive_interval(KEEP_ALIVE_INTERVAL)
            .keep_alive_while_idle(true);

        Ok(Self {
            endpoint,
            channel: OnceLock::new(),
        })
    }

    /// The shared channel, creating it on first use.
    ///
    /// [`OnceLock::get_or_init`] runs the initializer at most once even under
    /// concurrent first requests, so there is exactly one channel per filter.
    ///
    /// # Panics
    ///
    /// Panics if called outside a Tokio runtime. Every caller is inside
    /// `on_request`, which the proxy only ever runs on a runtime worker.
    fn channel(&self) -> Channel {
        self.channel
            .get_or_init(|| self.endpoint.clone().connect_lazy())
            .clone()
    }

    /// Issue one classify RPC under a total `timeout` budget.
    ///
    /// # Errors
    ///
    /// Returns [`ClassifyFailure::Timeout`] when the budget elapses, otherwise
    /// [`ClassifyFailure::Status`] with the gRPC status.
    pub async fn classify(
        &self,
        request: ClassifyRequest,
        timeout: Duration,
    ) -> Result<ClassifyResponse, ClassifyFailure> {
        // A `Channel` clone is a handle over the same multiplexed HTTP/2
        // connection, so this does not open a socket per request.
        let mut client = ClassifyClient::new(self.channel());
        match tokio::time::timeout(timeout, client.classify(request)).await {
            Err(_elapsed) => Err(ClassifyFailure::Timeout),
            Ok(Err(status)) => Err(ClassifyFailure::status(status)),
            Ok(Ok(response)) => Ok(response.into_inner()),
        }
    }
}
