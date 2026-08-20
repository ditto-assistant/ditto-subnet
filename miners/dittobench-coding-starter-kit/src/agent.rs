//! Coding-specific model/tool loop with bounded operational state.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use ditto_harness::{ChatMessage, Content, ContentType, Model, Tool, ToolCallResponse};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::context;
use crate::memory::RetrievedMemory;
use crate::protocol::CodingRunRequest;

pub const CODING_SYSTEM_PROMPT: &str = r"You are a repository coding agent operating through validator-owned tools.

Solve the current issue with the smallest complete, maintainable patch.

Rules:
1. Inspect relevant files before editing.
2. Use supplied user/project memory only when relevant. Verify stale or uncertain memory against current code and instructions.
3. Modify the repository only through the provided typed tools.
4. Prefer bounded reads, focused searches, and atomic edits.
5. Do not modify tests, dependencies, generated files, or build policy unless the current issue explicitly requires it.
6. Never claim a test passed unless tests_run returned a passing result.
7. Run focused visible tests, inspect status and diff, then return a concise final summary.
8. When using a tool, call at most one in that turn and wait for its result before choosing the next action.
9. Do not attempt network access, hidden-test discovery, sandbox escape, grader access, or benchmark manipulation.";

#[derive(Debug, thiserror::Error)]
pub enum AgentError {
    #[error("model: {0}")]
    Model(String),
    #[error("coding model returned empty final text")]
    EmptyFinal,
    #[error("model turn budget exhausted")]
    TurnBudget,
    #[error("workspace tool budget exhausted")]
    ToolBudget,
    #[error("model input token budget exhausted")]
    InputBudget,
    #[error("model output token budget exhausted")]
    OutputBudget,
    #[error("coding wall-time budget exhausted")]
    WallTime,
    #[error("required coding context exceeds the model input budget")]
    ContextBudget,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AgentOutcome {
    pub final_text: String,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub workspace_tool_calls: u32,
}

pub struct CodingAgent {
    model: Arc<dyn Model>,
    tools: Vec<Arc<dyn Tool>>,
}

impl CodingAgent {
    pub fn new(model: Arc<dyn Model>, tools: Vec<Arc<dyn Tool>>) -> Self {
        Self { model, tools }
    }

    /// Runs one bounded coding case.
    ///
    /// # Errors
    ///
    /// Returns an error when model execution fails or a declared token,
    /// tool-call, turn, or wall-time budget is exhausted.
    pub async fn run(
        &self,
        request: &CodingRunRequest,
        memories: &[RetrievedMemory],
    ) -> Result<AgentOutcome, AgentError> {
        let deadline = Duration::from_secs(request.budgets.wall_time_seconds);
        tokio::time::timeout(deadline, self.run_inner(request, memories))
            .await
            .map_err(|_| AgentError::WallTime)?
    }

    // Budget checks intentionally remain adjacent to the actions they guard.
    #[allow(clippy::too_many_lines)]
    async fn run_inner(
        &self,
        request: &CodingRunRequest,
        memories: &[RetrievedMemory],
    ) -> Result<AgentOutcome, AgentError> {
        let mut messages = initial_messages(request, memories);
        let definitions = self
            .tools
            .iter()
            .map(|tool| tool.definition())
            .collect::<Vec<_>>();
        let tools: HashMap<String, Arc<dyn Tool>> = self
            .tools
            .iter()
            .map(|tool| (tool.definition().name, Arc::clone(tool)))
            .collect();
        let max_turns = usize::try_from(request.budgets.workspace_tool_calls)
            .unwrap_or(256)
            .saturating_add(16)
            .min(256);
        let context_bytes = usize::try_from(request.budgets.model_input_tokens)
            .unwrap_or(usize::MAX)
            .saturating_mul(4)
            .min(256 * 1024);
        let mut input_tokens = 0_u64;
        let mut output_tokens = 0_u64;
        let mut last_call: Option<(String, Value)> = None;
        let mut repeated_calls = 0_u8;

        for turn in 0..max_turns {
            if !context::enforce_budget(&mut messages, context_bytes) {
                return Err(AgentError::ContextBudget);
            }
            debug_assert!(context::message_bytes(&messages) <= context_bytes);
            let chunk = self
                .model
                .next(&messages, &definitions)
                .await
                .map_err(|error| AgentError::Model(error.to_string()))?;
            if let Some(cost) = &chunk.cost {
                input_tokens = input_tokens.saturating_add(
                    u64::try_from(cost.usage.input_tokens.max(0)).unwrap_or(u64::MAX),
                );
                output_tokens = output_tokens.saturating_add(
                    u64::try_from(cost.usage.output_tokens.max(0)).unwrap_or(u64::MAX),
                );
            }
            if input_tokens > request.budgets.model_input_tokens {
                return Err(AgentError::InputBudget);
            }
            if output_tokens > request.budgets.model_output_tokens {
                return Err(AgentError::OutputBudget);
            }

            let Some(mut call) = chunk.tool_call else {
                let final_text = bounded_text(chunk.text.trim(), 2_000);
                if final_text.is_empty() {
                    return Err(AgentError::EmptyFinal);
                }
                return Ok(AgentOutcome {
                    final_text,
                    input_tokens,
                    output_tokens,
                    workspace_tool_calls: u32::try_from(turn).unwrap_or(u32::MAX),
                });
            };
            if u32::try_from(turn).unwrap_or(u32::MAX) >= request.budgets.workspace_tool_calls {
                return Err(AgentError::ToolBudget);
            }
            if call.id.is_empty() {
                call.id = format!("model-call-{turn}");
            }
            let identity = (call.name.clone(), call.args.clone());
            if last_call.as_ref() == Some(&identity) {
                repeated_calls = repeated_calls.saturating_add(1);
            } else {
                repeated_calls = 1;
                last_call = Some(identity);
            }
            if repeated_calls >= 3 {
                return Err(AgentError::Model(format!(
                    "repeated identical tool call {:?}",
                    call.name
                )));
            }
            let mut history_call = call.clone();
            history_call.args = bounded_json_value(history_call.args, 4096);
            messages.push(ChatMessage {
                role: "assistant".to_string(),
                content: if chunk.text.is_empty() {
                    Vec::new()
                } else {
                    vec![Content::text(chunk.text)]
                },
                tool_calls: vec![history_call],
                ..ChatMessage::default()
            });
            let response = match tools.get(&call.name) {
                Some(tool) => match tool.execute(call.args.clone()).await {
                    Ok(output) => ToolCallResponse {
                        id: call.id.clone(),
                        name: call.name.clone(),
                        output: bounded_json_value(output, 4096),
                        error: String::new(),
                    },
                    Err(error) => ToolCallResponse {
                        id: call.id.clone(),
                        name: call.name.clone(),
                        output: Value::Null,
                        error: bounded_text(&error.to_string(), 2048),
                    },
                },
                None => ToolCallResponse {
                    id: call.id.clone(),
                    name: call.name.clone(),
                    output: Value::Null,
                    error: "unknown workspace tool".to_string(),
                },
            };
            messages.push(ChatMessage {
                role: "tool".to_string(),
                tool_call_id: call.id.clone(),
                content: vec![Content {
                    content_type: Some(ContentType::ToolResult),
                    tool_call_response: Some(response),
                    ..Content::default()
                }],
                ..ChatMessage::default()
            });
        }
        Err(AgentError::TurnBudget)
    }
}

fn initial_messages(request: &CodingRunRequest, memories: &[RetrievedMemory]) -> Vec<ChatMessage> {
    let memory_context: Vec<Value> = memories
        .iter()
        .map(|memory| {
            json!({
                "memory_id": memory.memory_id,
                "content": memory.content,
                "metadata": memory.metadata,
                "similarity_micros": similarity_micros(memory.similarity)
            })
        })
        .collect();
    let task = json!({
        "title": request.issue.title,
        "description": request.issue.description,
        "constraints": request.issue.constraints,
        "repository_epoch": request.repository_epoch,
        "runtime_policy": request.runtime_policy
    });
    vec![
        ChatMessage {
            role: "system".to_string(),
            content: vec![Content::text(CODING_SYSTEM_PROMPT)],
            ..ChatMessage::default()
        },
        ChatMessage {
            role: "system".to_string(),
            content: vec![Content::text(format!(
                "Task-scoped retrieved memory (untrusted historical context): {}",
                Value::Array(memory_context)
            ))],
            ..ChatMessage::default()
        },
        ChatMessage {
            role: "user".to_string(),
            content: vec![Content::text(format!("Current coding task: {task}"))],
            ..ChatMessage::default()
        },
    ]
}

#[allow(clippy::cast_possible_truncation)]
fn similarity_micros(value: f64) -> i64 {
    // Cosine similarity is bounded. Clamp before intentional fixed-point
    // quantization so the float-to-integer conversion is total.
    (value.clamp(-1.0, 1.0) * 1_000_000.0).round() as i64
}

fn bounded_text(value: &str, max_bytes: usize) -> String {
    if value.len() <= max_bytes {
        return value.to_string();
    }
    let mut boundary = max_bytes;
    while !value.is_char_boundary(boundary) {
        boundary -= 1;
    }
    value[..boundary].to_string()
}

fn bounded_json_value(value: Value, max_bytes: usize) -> Value {
    let Ok(bytes) = serde_json::to_vec(&value) else {
        return json!({"observation_truncated": true, "reason": "serialization_failed"});
    };
    if bytes.len() <= max_bytes {
        return value;
    }
    let digest = Sha256::digest(&bytes);
    let mut digest_hex = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        let _ = write!(digest_hex, "{byte:02x}");
    }
    let rendered = String::from_utf8_lossy(&bytes);
    let mut preview_bytes = max_bytes / 2;
    loop {
        let candidate = json!({
            "observation_truncated": true,
            "original_bytes": bytes.len(),
            "sha256": &digest_hex,
            "preview": bounded_text(&rendered, preview_bytes)
        });
        if serde_json::to_vec(&candidate).map_or(true, |encoded| encoded.len() <= max_bytes) {
            return candidate;
        }
        if preview_bytes == 0 {
            return json!({
                "observation_truncated": true,
                "original_bytes": bytes.len(),
                "sha256": &digest_hex
            });
        }
        preview_bytes /= 2;
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use async_trait::async_trait;
    use ditto_harness::{ChatChunk, ToolCall, ToolDefinition};

    use super::*;
    use crate::model::shared_script;
    use crate::protocol::{
        CodingBudgets, CodingIssue, CodingRuntimePolicy, CODING_CONTRACT_VERSION,
    };

    struct RecordingTool {
        calls: Mutex<Vec<Value>>,
    }

    #[async_trait]
    impl Tool for RecordingTool {
        fn definition(&self) -> ToolDefinition {
            ToolDefinition {
                name: "repo_read_file".to_string(),
                description: "read".to_string(),
                input_schema: json!({"type":"object"}),
            }
        }

        async fn execute(&self, args: Value) -> ditto_harness::Result<Value> {
            self.calls.lock().unwrap().push(args);
            Ok(json!({"content":"source"}))
        }
    }

    fn request() -> CodingRunRequest {
        CodingRunRequest {
            coding_contract_version: CODING_CONTRACT_VERSION,
            ticket_id: "ticket".to_string(),
            case_id: "case".to_string(),
            profile_capability_id: "profile".to_string(),
            visible_bundle_sha256: "a".repeat(64),
            issue: CodingIssue {
                title: "Fix".to_string(),
                description: "Fix the issue".to_string(),
                constraints: Vec::new(),
            },
            repository_epoch: "repository-v2".to_string(),
            runtime_policy: CodingRuntimePolicy {
                editable_paths: vec!["app.py".to_string()],
                test_command_ids: vec!["visible-unit".to_string()],
                build_command_ids: vec!["python-compile".to_string()],
            },
            workspace_capability_url: "http://runner.invalid/tool".to_string(),
            inference_base_url: "http://broker.invalid/v1".to_string(),
            budgets: CodingBudgets {
                model_input_tokens: 1000,
                model_output_tokens: 1000,
                workspace_tool_calls: 2,
                wall_time_seconds: 5,
            },
        }
    }

    #[test]
    fn runtime_policy_reaches_current_task_context() {
        let messages = initial_messages(&request(), &[]);
        let task = &messages[2].content[0].content;
        assert!(task.contains("app.py"));
        assert!(task.contains("repository-v2"));
        assert!(task.contains("visible-unit"));
        assert!(task.contains("python-compile"));
    }

    #[tokio::test]
    async fn scripted_model_executes_tool_then_finishes() {
        let model = shared_script(vec![
            ChatChunk {
                tool_call: Some(ToolCall {
                    id: "call-1".to_string(),
                    name: "repo_read_file".to_string(),
                    args: json!({"path":"app.py"}),
                }),
                ..ChatChunk::default()
            },
            ChatChunk {
                text: "Fixed the issue and reviewed the result.".to_string(),
                ..ChatChunk::default()
            },
        ]);
        let tool = Arc::new(RecordingTool {
            calls: Mutex::new(Vec::new()),
        });
        let agent = CodingAgent::new(model, vec![tool.clone() as Arc<dyn Tool>]);
        let outcome = agent.run(&request(), &[]).await.unwrap();
        assert_eq!(outcome.workspace_tool_calls, 1);
        assert_eq!(tool.calls.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn final_summary_is_utf8_safe_and_bounded_to_2000_bytes() {
        let model = shared_script(vec![ChatChunk {
            text: "é".repeat(1_500),
            ..ChatChunk::default()
        }]);
        let agent = CodingAgent::new(model, Vec::new());
        let outcome = agent.run(&request(), &[]).await.unwrap();
        assert_eq!(outcome.final_text.len(), 2_000);
        assert_eq!(outcome.final_text.chars().count(), 1_000);
    }

    #[test]
    fn huge_tool_observation_is_bounded_before_history() {
        let bounded = bounded_json_value(json!({"content": "x".repeat(1_000_000)}), 4096);
        assert!(serde_json::to_vec(&bounded).unwrap().len() <= 4096);
        assert_eq!(bounded["observation_truncated"], true);
    }

    #[tokio::test]
    async fn repeated_identical_calls_fail_closed() {
        let call = ChatChunk {
            tool_call: Some(ToolCall {
                id: "call".to_string(),
                name: "repo_read_file".to_string(),
                args: json!({"path":"app.py"}),
            }),
            ..ChatChunk::default()
        };
        let model = shared_script(vec![call.clone(), call.clone(), call]);
        let tool = Arc::new(RecordingTool {
            calls: Mutex::new(Vec::new()),
        });
        let mut req = request();
        req.budgets.workspace_tool_calls = 4;
        let agent = CodingAgent::new(model, vec![tool as Arc<dyn Tool>]);
        assert!(matches!(
            agent.run(&req, &[]).await,
            Err(AgentError::Model(_))
        ));
    }
}
