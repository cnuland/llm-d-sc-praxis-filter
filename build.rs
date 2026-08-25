//! Generates the `classify.Classify` gRPC bindings from the vendored proto.
//!
//! SPEC 4.8: the shipped filter is a *client* only. The server stubs are
//! generated exclusively under the `test-server` feature, which the crate's own
//! dev-dependency turns on for `cargo test`.

fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("cargo:rerun-if-changed=proto/classify.proto");
    let build_server = std::env::var_os("CARGO_FEATURE_TEST_SERVER").is_some();
    tonic_prost_build::configure()
        .build_client(true)
        .build_server(build_server)
        .compile_protos(&["proto/classify.proto"], &["proto"])?;
    Ok(())
}
