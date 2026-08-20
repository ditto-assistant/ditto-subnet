//! Shadow-only reference coding harness for `DittoBench`.
//!
//! The miner owns memory retrieval and the coding loop. It never owns the
//! repository workspace, patch identity, grader, or authoritative tool trace.

pub mod agent;
pub mod context;
pub mod memory;
pub mod model;
pub mod protocol;
pub mod server;
pub mod workspace_client;

pub use server::{router, CodingService, ModelFactory};
