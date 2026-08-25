//! A `classify.Classify` server that is deliberately, controllably SLOW.
//!
//! B-6's "classifier slow" case asserts that a classifier which overruns the
//! filter's deadline costs the request `timeout_ms` and not one millisecond
//! more. Proving that needs a server slower than the budget.
//!
//! The real classifier answers in ~14 ms, so the only way to provoke a timeout
//! with it is to set an absurdly small budget (1 ms) — at which point the
//! measurement is dominated by timer granularity and RPC teardown rather than
//! by the deadline, and the assertion is testing the runtime's clock instead of
//! the filter's contract. A stub that sleeps 500 ms against a 100 ms budget
//! puts the deadline an order of magnitude clear of that noise.
//!
//! Usage: slow-classifier <listen-addr> <delay-ms>
//!   cargo run --features test-server --example slow-classifier -- 127.0.0.1:50052 500

use std::time::Duration;

use llm_d_sc_praxis_filter::{
    pb::{ClassificationStatus, ClassifyResponse, RankedSignal},
    testing::{StubBehaviour, StubServer},
};

#[tokio::main]
async fn main() -> std::io::Result<()> {
    let mut args = std::env::args().skip(1);
    let addr = args.next().unwrap_or_else(|| "127.0.0.1:50052".to_owned());
    let delay_ms: u64 = args
        .next()
        .unwrap_or_else(|| "500".to_owned())
        .parse()
        .expect("delay-ms must be a number");

    // A response that WOULD be perfectly usable — the point is that it arrives
    // too late, not that it is malformed. If the filter ever reported this
    // label, the deadline was not enforced.
    let response = ClassifyResponse {
        request_id: String::new(),
        classifier_id: "slow-stub".to_owned(),
        model_revision: "slow-stub".to_owned(),
        tokenizer_revision: "slow-stub".to_owned(),
        taxonomy_revision: "slow-stub".to_owned(),
        status: ClassificationStatus::Ok as i32,
        ranked: vec![RankedSignal {
            label: "SIMPLE".to_owned(),
            score: 1.0,
        }],
    };

    let server = StubServer::start_on(
        &addr,
        StubBehaviour::SlowRespond(Duration::from_millis(delay_ms), Box::new(response)),
    )
    .await?;

    eprintln!(
        "slow-classifier: listening on {}, sleeping {delay_ms} ms before every answer",
        server.endpoint()
    );

    std::future::pending::<()>().await;
    Ok(())
}
