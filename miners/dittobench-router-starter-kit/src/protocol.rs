//! Harness-facing `DittoBench` router contract v1 wire types.
//!
//! Router contract v1 is a **shadow-only** competition dimension. It is
//! permanently `weight_eligible=false` and does not alter the active
//! `DittoBench` Tool + Memory score or subnet weights. See
//! `docs/router-compression-competition-v1.md` for the authoritative
//! interface spec (epic:
//! <https://github.com/ditto-assistant/ditto-subnet/issues/1664>).

use serde::{Deserialize, Serialize};

/// The single router contract version this reference kit advertises.
pub const ROUTER_CONTRACT_VERSION: u32 = 1;

/// The provider wires the router serves, exactly as advertised at
/// `GET /router/health`.
pub const ADVERTISED_WIRES: [&str; 3] = ["anthropic_messages", "openai_chat", "openai_responses"];

/// The `GET /router/health` advertisement.
///
/// The validator binds these known fields to the exact screened image digest;
/// unknown fields are ignored. A `404` here means "no router project" (no
/// penalty, no router score); a malformed advertisement yields no router
/// attestation.
///
/// The serialized shape is exactly:
/// ```json
/// {
///   "status": "ok",
///   "supported_router_contract_versions": [1],
///   "wires": ["anthropic_messages", "openai_chat", "openai_responses"],
///   "count_tokens": true
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RouterHealthResponse {
    pub status: String,
    pub supported_router_contract_versions: Vec<u32>,
    pub wires: Vec<String>,
    pub count_tokens: bool,
}

impl Default for RouterHealthResponse {
    fn default() -> Self {
        Self {
            status: "ok".to_string(),
            supported_router_contract_versions: vec![ROUTER_CONTRACT_VERSION],
            wires: ADVERTISED_WIRES
                .iter()
                .map(|wire| (*wire).to_string())
                .collect(),
            count_tokens: true,
        }
    }
}

/// One validator-seeded memory record, reusing the `coding_memory_v1` field
/// shape delivered to the coding kit. The router demo stores these verbatim;
/// only the fields the reference kit needs are named, and unknown fields are
/// ignored for forward compatibility.
///
/// A production router would use these for the `memory_placement` lever
/// (attaching a relevant record's verbatim content upstream). This reference
/// kit only stores them; see the README "Extending" section.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VisibleMemoryRecord {
    pub memory_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scope: Option<String>,
    #[serde(default, rename = "type", skip_serializing_if = "Option::is_none")]
    pub memory_type: Option<String>,
    pub content: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub confidence_micros: Option<u64>,
}

/// The `POST /router/seed` request. The validator supplies the task's memory
/// records (may be empty) plus one canary record. The contamination gate
/// requires the canary never appear upstream, so this reference kit never
/// forwards seeded content on its own.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct RouterSeedRequest {
    #[serde(default = "default_contract_version")]
    pub router_contract_version: u32,
    #[serde(default)]
    pub memories: Vec<VisibleMemoryRecord>,
}

fn default_contract_version() -> u32 {
    ROUTER_CONTRACT_VERSION
}

/// The `POST /router/seed` response: how many records the router stored.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RouterSeedResponse {
    pub router_contract_version: u32,
    pub memory_count: usize,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn health_advertisement_is_the_exact_contract_shape() {
        let advertised = serde_json::to_value(RouterHealthResponse::default()).unwrap();
        assert_eq!(
            advertised,
            serde_json::json!({
                "status": "ok",
                "supported_router_contract_versions": [1],
                "wires": ["anthropic_messages", "openai_chat", "openai_responses"],
                "count_tokens": true
            })
        );
    }

    #[test]
    fn health_advertisement_serializes_fields_in_contract_order() {
        // The screener reads the raw bytes; keep field order stable.
        let text = serde_json::to_string(&RouterHealthResponse::default()).unwrap();
        assert_eq!(
            text,
            "{\"status\":\"ok\",\
             \"supported_router_contract_versions\":[1],\
             \"wires\":[\"anthropic_messages\",\"openai_chat\",\"openai_responses\"],\
             \"count_tokens\":true}"
        );
    }

    #[test]
    fn seed_request_accepts_empty_and_unknown_fields() {
        let empty: RouterSeedRequest = serde_json::from_str("{}").unwrap();
        assert_eq!(empty.router_contract_version, ROUTER_CONTRACT_VERSION);
        assert!(empty.memories.is_empty());

        let seeded: RouterSeedRequest = serde_json::from_str(
            r#"{"router_contract_version":1,"unknown_future_field":true,"memories":[
                {"memory_id":"m1","scope":"profile","type":"user_workflow",
                 "content":"Prefer minimal diffs.","confidence_micros":900000,
                 "extra":"ignored"}
            ]}"#,
        )
        .unwrap();
        assert_eq!(seeded.memories.len(), 1);
        assert_eq!(seeded.memories[0].memory_id, "m1");
        assert_eq!(seeded.memories[0].content, "Prefer minimal diffs.");
    }
}
