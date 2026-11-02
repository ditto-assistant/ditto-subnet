//! Shadow-only reference **router** harness for `DittoBench` (contract v1).
//!
//! This is the reference router published as the router starter kit's default
//! configuration. It advertises the router contract v1 surface, faithfully
//! passes the four provider wires through to the validator relay, accepts
//! task memory seeds, and ships one deterministic example lever (archetype
//! rerouting, off by default) to demonstrate the competition's spirit.
//!
//! Router contract v1 is a **shadow-only** dimension: permanently
//! `weight_eligible=false`, it does not change the active Tool + Memory score
//! or subnet weights. See `docs/router-compression-competition-v1.md`.
//!
//! Not implemented here (separate epic issues; referenced only): the
//! `RouterContractVersion` registry and the `packages/dittobench-router-contract`
//! shared conformance vectors.

pub mod protocol;
pub mod relay;
pub mod reroute;
pub mod seed;
pub mod server;

pub use relay::Relay;
pub use reroute::RerouteConfig;
pub use server::{router, RouterService};
