//! DittoBench validator wire contract (HTTP).
//!
//! The harness-facing types — [`RunRequest`], [`ToolExecRequest`],
//! [`ToolExecResponse`], [`ObservedToolCall`], [`RunResponse`] (plus
//! `SeedRequest`/`SeedResponse` in `seed.rs`) — are byte-compatible with the Go
//! validator's wire contract (dittobench-api's `pkg/protocol/protocol.go`). The
//! validator on Bittensor subnet 118 (SN118) POSTs a [`RunRequest`] to the
//! miner's `/run` endpoint per case and expects a [`RunResponse`]. Datasets
//! ([`Dataset`]) and score shapes ([`CaseScore`], [`ScoreReport`]) here are a
//! **partial local subset** produced for offline practice — they mirror the Go
//! field names but are not the full validator report.
//!
//! Field naming: the Go side uses `snake_case` json tags throughout, so every
//! struct here uses `#[serde(rename_all = "snake_case")]` (which is also the
//! Rust default field casing, but we keep it explicit for safety).

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// An expected tool in a dataset case (Go: `ToolSpec`).
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct ToolSpec {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub required_args: Option<BTreeMap<String, String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub forbidden_args: Option<Vec<String>>,
}

/// One tool-calling benchmark case (Go: `ToolCase`).
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct ToolCase {
    pub id: String,
    pub category: String,
    pub prompt: String,
    pub expected_tools: Vec<ToolSpec>,
    pub max_tool_calls: i32,
    pub allow_extra_tools: bool,
    pub expected_behavior: String,
}

/// One seed memory pair for a memory case (local practice extension).
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct SeedMemory {
    pub prompt: String,
    pub response: String,
    pub days_ago: i64,
}

/// A memory-recall benchmark case (local practice extension). The harness is
/// seeded with `seed_memories` and then asked `question`; the answer should
/// surface `expected_answer`.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct MemoryCase {
    pub id: String,
    pub question: String,
    pub expected_answer: String,
    pub seed_memories: Vec<SeedMemory>,
}

/// A (fresh, seeded) benchmark dataset (Go: `Dataset`, extended with
/// `memory_cases`).
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct Dataset {
    pub seed: i64,
    pub generated_at: String,
    pub tool_cases: Vec<ToolCase>,
    #[serde(default)]
    pub memory_cases: Vec<MemoryCase>,
}

/// A tool schema sent to the harness for a case (Go: `ToolDefinition`).
/// Named `ToolDefWire` here to avoid confusion with the harness's own
/// `ditto_harness::types::ToolDefinition`.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct ToolDefWire {
    pub name: String,
    pub description: String,
    /// JSON schema for the tool input.
    #[serde(default, skip_serializing_if = "Value::is_null")]
    pub parameters: Value,
}

/// What the validator POSTs to the harness `/run` endpoint per case
/// (Go: `RunRequest`). Distinct from `ditto_harness::chat::RunRequest`.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct RunRequest {
    pub case_id: String,
    pub system_prompt: String,
    pub user_input: String,
    #[serde(default)]
    pub tools: Vec<ToolDefWire>,
    /// Additive v7+ execution contract selector.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bench_version: Option<u32>,
    /// Optional observed-execution URL served by the validator. When present,
    /// a harness should EXECUTE each non-memory
    /// catalog tool call by POSTing a [`ToolExecRequest`] here and using the
    /// returned [`ToolExecResponse::result`], instead of stubbing it locally. The
    /// validator then observes the real trajectory (rather than trusting
    /// self-report) and can score whether the answer incorporates returned
    /// content. Absent ⇒ pre-observed-execution behavior (stub tools locally).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_endpoint: Option<String>,
    /// Optional (observed execution): the memory graph this case must be answered from
    /// (multi-graph isolation). Mirrors the `user_id` the haystack was seeded
    /// under; answer only from this user's memory, never leak another user's
    /// facts. Absent ⇒ the default single-user graph.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub user_id: Option<String>,
}

/// One mock tool-execution call a harness POSTs to the validator-served
/// [`RunRequest::tool_endpoint`] (Go: `ToolExecRequest`). The validator returns a
/// deterministic, seed-derived [`ToolExecResponse`] and records the call as the
/// authoritative observed trajectory.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct ToolExecRequest {
    pub case_id: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub user_id: String,
    pub name: String,
    #[serde(default, skip_serializing_if = "Value::is_null")]
    pub args: Value,
    #[serde(default)]
    pub hop: i32,
}

/// The mock result the validator returns for a [`ToolExecRequest`] (Go:
/// `ToolExecResponse`). `result` is the tool output to reason over; `error` is
/// set (with `result` empty) for a tool the endpoint does not serve (a memory
/// tool) — treat it like a real tool error.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct ToolExecResponse {
    #[serde(default)]
    pub result: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub error: String,
}

/// A tool call the harness made (Go: `ObservedToolCall`).
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct ObservedToolCall {
    pub name: String,
    #[serde(default, skip_serializing_if = "Value::is_null")]
    pub args: Value,
    #[serde(default)]
    pub hop: i32,
}

/// What the harness returns for a case (Go: `RunResponse`).
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct RunResponse {
    pub final_text: String,
    pub tool_calls: Vec<ObservedToolCall>,
    pub prompt_tokens: i64,
    pub output_tokens: i64,
    pub latency_ms: i64,
    /// Optional short answer slot: the bare value `final_text` asserts (a
    /// name, number, or comma-separated list). The validator's deterministic
    /// grader matches this slot when present and falls back to `final_text`
    /// containment, so populating it removes prose-phrasing risk. EXTENSION
    /// POINT: extract it from your agent's output.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub answer: Option<String>,
    /// Optional grounded-decline flag: set true when the asked fact is not in
    /// memory. Correct on needle-absent cases; abstaining on an answerable
    /// case scores 0.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub abstain: Option<bool>,
}

/// The score for one case (Go: `CaseScore`).
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct CaseScore {
    pub case_id: String,
    pub category: String,
    /// 0..1.
    pub tool_score: f64,
    /// 0..1 result-usage credit for a result-usage tool case (observed execution): whether
    /// the answer incorporated the value the executed tool returned. Omitted (0)
    /// for non-result-usage cases.
    #[serde(default, skip_serializing_if = "is_zero")]
    pub result_usage: f64,
    pub latency_ms: i64,
    pub called: Vec<String>,
    pub expected: Vec<String>,
    #[serde(default)]
    pub notes: Vec<String>,
}

fn is_zero(x: &f64) -> bool {
    *x == 0.0
}

/// The full result of scoring a run (Go: `ScoreReport`, extended with
/// `memory_mean`).
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct ScoreReport {
    pub run_id: String,
    pub generated_at: String,
    /// 0..1 weighted composite.
    pub composite: f64,
    /// 0..1 mean tool score.
    pub tool_mean: f64,
    /// 0..1 fraction of memory cases answered correctly.
    pub memory_mean: f64,
    pub median_ms: i64,
    pub n: i32,
    pub per_case: Vec<CaseScore>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_request_uses_snake_case_keys() {
        let req = RunRequest {
            case_id: "c1".into(),
            system_prompt: "be helpful".into(),
            user_input: "hi".into(),
            tools: vec![ToolDefWire {
                name: "search_web".into(),
                description: "d".into(),
                parameters: serde_json::json!({"type": "object"}),
            }],
            ..Default::default()
        };
        let v = serde_json::to_value(&req).expect("serialize");
        let obj = v.as_object().expect("object");
        for key in ["case_id", "system_prompt", "user_input", "tools"] {
            assert!(obj.contains_key(key), "missing key {key}");
        }
        // The optional observed-execution fields are omitted when absent (byte-compatible
        // with an old validator that never sends them).
        assert!(!obj.contains_key("tool_endpoint"));
        assert!(!obj.contains_key("user_id"));
        assert!(!obj.contains_key("bench_version"));
    }

    #[test]
    fn phase_c_run_request_accepts_optional_fields() {
        // An observed-execution RunRequest from the validator deserializes cleanly.
        let json = r#"{
            "case_id": "web_result_usage-1-0",
            "system_prompt": "be helpful",
            "user_input": "figure on the Veltrix index?",
            "tools": [],
            "bench_version": 8,
            "tool_endpoint": "http://host.docker.internal:49222/tool",
            "user_id": "colleague"
        }"#;
        let req: RunRequest = serde_json::from_str(json).expect("deserialize");
        assert_eq!(
            req.tool_endpoint.as_deref(),
            Some("http://host.docker.internal:49222/tool")
        );
        assert_eq!(req.user_id.as_deref(), Some("colleague"));
        assert_eq!(req.bench_version, Some(8));
    }

    #[test]
    fn tool_exec_round_trips() {
        let req = ToolExecRequest {
            case_id: "c1".into(),
            user_id: "miner".into(),
            name: "search_web".into(),
            args: serde_json::json!({"query": "x"}),
            hop: 0,
        };
        let back: ToolExecRequest =
            serde_json::from_str(&serde_json::to_string(&req).expect("ser")).expect("de");
        assert_eq!(req, back);
        // A served result and an error variant both parse.
        let ok: ToolExecResponse =
            serde_json::from_str(r#"{"result":"the Veltrix index reached 3,418 points"}"#)
                .expect("de");
        assert_eq!(ok.error, "");
        let err: ToolExecResponse = serde_json::from_str(
            r#"{"error":"tool not available via this endpoint: search_memories"}"#,
        )
        .expect("de");
        assert_eq!(err.result, "");
    }

    #[test]
    fn run_response_round_trips() {
        let resp = RunResponse {
            final_text: "answer".into(),
            tool_calls: vec![ObservedToolCall {
                name: "search_web".into(),
                args: serde_json::json!({"query": "x"}),
                hop: 0,
            }],
            prompt_tokens: 10,
            output_tokens: 5,
            latency_ms: 42,
            answer: Some("answer".into()),
            abstain: None,
        };
        let json = serde_json::to_string(&resp).expect("serialize");
        let back: RunResponse = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(resp, back);
        // Absent slots deserialize as None (additive-optional wire fields).
        let legacy: RunResponse =
            serde_json::from_str(r#"{"final_text":"x","tool_calls":[],"prompt_tokens":0,"output_tokens":0,"latency_ms":0}"#)
                .expect("legacy deserialize");
        assert_eq!(legacy.answer, None);
        assert_eq!(legacy.abstain, None);
    }
}
