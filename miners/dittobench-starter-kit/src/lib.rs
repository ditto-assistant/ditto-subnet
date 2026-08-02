//! DittoBench miner starter kit.
//!
//! Modules:
//! - [`protocol`]: the validator HTTP wire contract.
//! - [`catalog`]: the Ditto tool catalog.
//! - [`datagen`]: deterministic-per-seed dataset generation.
//! - [`eval`]: the shared tool+memory evaluation loop (CLI + playground).
//! - [`grade`]: deterministic judge-free memory grading (no LLM).
//! - [`scorer`]: turns harness responses into a score report.
//! - [`baseline`]: the optimizable agent (this is what you tune).
//! - [`reranker`]: ONNX cross-encoder reranker (production retrieval stage).
//! - [`seed`]: the bundled LongMemEval seed user (memory retrieval practice).
//! - [`playground`]: the interactive web playground (fake tools + submit flow).

pub mod baseline;
pub mod catalog;
pub mod datagen;
pub mod eval;
pub mod grade;
pub mod playground;
pub mod protocol;
pub mod reranker;
pub mod scorer;
pub mod seed;
