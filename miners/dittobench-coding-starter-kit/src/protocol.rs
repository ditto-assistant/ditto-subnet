//! Harness-facing `DittoBench` coding contract v1.

use serde::{Deserialize, Serialize};

pub const CODING_CONTRACT_VERSION: u32 = 1;
pub const LUNA_MODEL: &str = "openai/gpt-5.6-luna";
pub const LUNA_REASONING_EFFORT: &str = "medium";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CodingHealthResponse {
    pub status: String,
    pub supported_coding_contract_versions: Vec<u32>,
    pub capabilities: Vec<String>,
}

impl Default for CodingHealthResponse {
    fn default() -> Self {
        Self {
            status: "ok".to_string(),
            supported_coding_contract_versions: vec![CODING_CONTRACT_VERSION],
            capabilities: vec![
                "scoped_memory_seed_v1".to_string(),
                "coding_runner_tools_v1".to_string(),
                "case_scoped_inference_v1".to_string(),
            ],
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct VisibleMemoryRecord {
    pub memory_id: String,
    #[serde(default, alias = "repository_id")]
    pub repository_capability_id: Option<String>,
    #[serde(default)]
    pub fact_group_id: Option<String>,
    #[serde(default)]
    pub scope: String,
    #[serde(default, rename = "type")]
    pub memory_type: String,
    pub content: String,
    #[serde(default, alias = "valid_from_revision")]
    pub valid_from_epoch: Option<String>,
    #[serde(default, alias = "valid_until_revision")]
    pub valid_until_epoch: Option<String>,
    #[serde(default)]
    pub supersedes: Vec<String>,
    #[serde(default)]
    pub confidence_micros: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CodingSeedRequest {
    pub coding_contract_version: u32,
    pub ticket_id: String,
    pub case_id: String,
    pub profile_capability_id: String,
    pub memory_bundle_sha256: String,
    #[serde(default)]
    pub memories: Vec<VisibleMemoryRecord>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CodingSeedResponse {
    pub case_id: String,
    pub profile_capability_id: String,
    pub memory_bundle_sha256: String,
    pub memory_count: usize,
    pub idempotent_replay: bool,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct CodingIssue {
    #[serde(default)]
    pub title: String,
    pub description: String,
    #[serde(default)]
    pub constraints: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CodingBudgets {
    pub model_input_tokens: u64,
    pub model_output_tokens: u64,
    pub workspace_tool_calls: u32,
    pub wall_time_seconds: u64,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct CodingRuntimePolicy {
    #[serde(default)]
    pub editable_paths: Vec<String>,
    #[serde(default)]
    pub test_command_ids: Vec<String>,
    #[serde(default)]
    pub build_command_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CodingRunRequest {
    pub coding_contract_version: u32,
    pub ticket_id: String,
    pub case_id: String,
    pub profile_capability_id: String,
    pub visible_bundle_sha256: String,
    pub issue: CodingIssue,
    pub repository_epoch: String,
    pub runtime_policy: CodingRuntimePolicy,
    pub workspace_capability_url: String,
    pub inference_base_url: String,
    pub budgets: CodingBudgets,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct CodingFinalReport {
    pub summary: String,
    #[serde(default)]
    pub remaining_risks: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CodingRunResponse {
    pub case_id: String,
    pub final_report: CodingFinalReport,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct WorkspaceToolRequest {
    pub coding_contract_version: u32,
    pub case_id: String,
    pub profile_capability_id: String,
    pub call_id: String,
    pub name: String,
    pub arguments: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct WorkspaceToolResponse {
    pub call_id: String,
    pub sequence: u64,
    pub ok: bool,
    #[serde(default)]
    pub result: serde_json::Value,
    #[serde(default)]
    pub error: Option<WorkspaceToolError>,
    pub event_sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct WorkspaceToolError {
    pub code: String,
    pub message: String,
}

#[must_use]
pub fn is_lower_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// Validates one bounded opaque identifier.
///
/// # Errors
///
/// Returns an error when the value is empty, oversized, or contains a control
/// character.
pub fn validate_identifier(label: &str, value: &str) -> Result<(), String> {
    if value.is_empty() || value.len() > 256 {
        return Err(format!("{label} must contain 1..=256 bytes"));
    }
    if value.chars().any(char::is_control) {
        return Err(format!("{label} contains a control character"));
    }
    Ok(())
}

impl CodingSeedRequest {
    /// Validates all known coding seed fields and bounds.
    ///
    /// # Errors
    ///
    /// Returns an error for an unsupported contract, malformed identity,
    /// invalid digest, duplicate memory, or oversized memory bundle.
    pub fn validate(&self) -> Result<(), String> {
        if self.coding_contract_version != CODING_CONTRACT_VERSION {
            return Err(format!(
                "unsupported coding_contract_version {}",
                self.coding_contract_version
            ));
        }
        validate_identifier("ticket_id", &self.ticket_id)?;
        validate_identifier("case_id", &self.case_id)?;
        validate_identifier("profile_capability_id", &self.profile_capability_id)?;
        if !is_lower_sha256(&self.memory_bundle_sha256) {
            return Err("memory_bundle_sha256 must be lowercase SHA-256".to_string());
        }
        if self.memories.len() > 128 {
            return Err("memory bundle exceeds 128 records".to_string());
        }
        let mut ids = std::collections::HashSet::new();
        for memory in &self.memories {
            validate_identifier("memory_id", &memory.memory_id)?;
            if !ids.insert(memory.memory_id.as_str()) {
                return Err(format!("duplicate memory_id {:?}", memory.memory_id));
            }
            if memory.content.is_empty() || memory.content.len() > 16 * 1024 {
                return Err(format!(
                    "memory {:?} content must contain 1..=16384 bytes",
                    memory.memory_id
                ));
            }
            if memory
                .confidence_micros
                .is_some_and(|value| value > 1_000_000)
            {
                return Err(format!(
                    "memory {:?} confidence_micros exceeds 1000000",
                    memory.memory_id
                ));
            }
        }
        Ok(())
    }
}

impl CodingRunRequest {
    /// Validates all known coding run fields and budgets.
    ///
    /// # Errors
    ///
    /// Returns an error for an unsupported contract, malformed identity,
    /// invalid digest, oversized issue, invalid URL length, or unsafe budget.
    pub fn validate(&self) -> Result<(), String> {
        if self.coding_contract_version != CODING_CONTRACT_VERSION {
            return Err(format!(
                "unsupported coding_contract_version {}",
                self.coding_contract_version
            ));
        }
        validate_identifier("ticket_id", &self.ticket_id)?;
        validate_identifier("case_id", &self.case_id)?;
        validate_identifier("profile_capability_id", &self.profile_capability_id)?;
        validate_identifier("repository_epoch", &self.repository_epoch)?;
        if !is_lower_sha256(&self.visible_bundle_sha256) {
            return Err("visible_bundle_sha256 must be lowercase SHA-256".to_string());
        }
        if self.issue.description.is_empty() || self.issue.description.len() > 64 * 1024 {
            return Err("issue description must contain 1..=65536 bytes".to_string());
        }
        if self.issue.title.len() > 1024
            || self.issue.constraints.len() > 64
            || self
                .issue
                .constraints
                .iter()
                .any(|constraint| constraint.len() > 4096)
        {
            return Err("issue title or constraints exceed contract bounds".to_string());
        }
        validate_runtime_policy(&self.runtime_policy)?;
        if self.workspace_capability_url.len() > 4096 || self.inference_base_url.len() > 4096 {
            return Err("capability URL exceeds 4096 bytes".to_string());
        }
        if self.budgets.model_input_tokens == 0
            || self.budgets.model_output_tokens == 0
            || self.budgets.workspace_tool_calls == 0
            || self.budgets.wall_time_seconds == 0
        {
            return Err("all coding budgets must be positive".to_string());
        }
        if self.budgets.workspace_tool_calls > 1_000
            || self.budgets.wall_time_seconds > 7_200
            || self.budgets.model_input_tokens > 2_000_000
            || self.budgets.model_output_tokens > 250_000
        {
            return Err("coding budget exceeds contract safety cap".to_string());
        }
        Ok(())
    }
}

fn validate_runtime_policy(policy: &CodingRuntimePolicy) -> Result<(), String> {
    if policy.editable_paths.len() > 64
        || policy.test_command_ids.len() > 64
        || policy.build_command_ids.len() > 64
    {
        return Err("runtime policy exceeds 64 entries per collection".to_string());
    }
    let mut paths = std::collections::HashSet::new();
    for path in &policy.editable_paths {
        if path.is_empty()
            || path.len() > 256
            || path.starts_with('/')
            || path.contains('\\')
            || path
                .split('/')
                .any(|segment| segment.is_empty() || matches!(segment, "." | ".." | ".git"))
            || path.chars().any(char::is_control)
        {
            return Err(format!("runtime policy contains unsafe path {path:?}"));
        }
        if !paths.insert(path) {
            return Err(format!("runtime policy repeats editable path {path:?}"));
        }
    }
    for (label, values) in [
        ("test_command_ids", &policy.test_command_ids),
        ("build_command_ids", &policy.build_command_ids),
    ] {
        let mut seen = std::collections::HashSet::new();
        for value in values {
            if value.is_empty()
                || value.len() > 80
                || value.chars().any(char::is_control)
                || !seen.insert(value)
            {
                return Err(format!("runtime policy contains invalid {label} entry"));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn workspace_success_accepts_null_error() {
        let response: WorkspaceToolResponse = serde_json::from_value(serde_json::json!({
            "call_id": "call-1",
            "sequence": 1,
            "ok": true,
            "result": {"content": "source"},
            "error": null,
            "event_sha256": "a".repeat(64)
        }))
        .unwrap();
        assert!(response.error.is_none());
        assert_eq!(response.result["content"], "source");
    }

    #[test]
    fn workspace_failure_accepts_typed_error() {
        let response: WorkspaceToolResponse = serde_json::from_value(serde_json::json!({
            "call_id": "call-2",
            "sequence": 2,
            "ok": false,
            "result": null,
            "error": {"code": "stale_digest", "message": "file changed"},
            "event_sha256": "b".repeat(64)
        }))
        .unwrap();
        assert_eq!(response.error.unwrap().code, "stale_digest");
    }

    #[test]
    fn runtime_policy_is_required_validated_and_forward_compatible() {
        let mut value = serde_json::json!({
            "coding_contract_version": 1,
            "ticket_id": "ticket",
            "case_id": "case",
            "profile_capability_id": "profile",
            "visible_bundle_sha256": "a".repeat(64),
            "issue": {"description": "fix it"},
            "repository_epoch": "repository-v2",
            "workspace_capability_url": "http://runner.invalid/tool",
            "inference_base_url": "http://broker.invalid/v1",
            "budgets": {
                "model_input_tokens": 100,
                "model_output_tokens": 100,
                "workspace_tool_calls": 10,
                "wall_time_seconds": 10
            },
            "future_field": "ignored"
        });
        assert!(serde_json::from_value::<CodingRunRequest>(value.clone()).is_err());
        value["runtime_policy"] = serde_json::json!({
            "editable_paths": [],
            "test_command_ids": [],
            "build_command_ids": []
        });
        let mut request: CodingRunRequest = serde_json::from_value(value).unwrap();
        assert!(request.validate().is_ok());
        request.runtime_policy.editable_paths = vec!["../hidden".to_string()];
        assert!(request.validate().is_err());
    }
}
