//! Coding-specific model/tool loop with bounded operational state.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use ditto_harness::{ChatMessage, Content, ContentType, Model, Tool, ToolCallResponse};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::context;
use crate::memory::RetrievedMemory;
use crate::protocol::CodingRunRequest;

const MAX_LIVE_TOOL_OBSERVATION_BYTES: usize = 32 * 1024;

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
    /// Returns an error only for unrecoverable model failure before any
    /// authoritative workspace activity. Budget exhaustion returns a bounded
    /// advisory report so the validator can still freeze and grade the patch.
    pub async fn run(
        &self,
        request: &CodingRunRequest,
        memories: &[RetrievedMemory],
    ) -> Result<AgentOutcome, AgentError> {
        let deadline = Duration::from_secs(request.budgets.wall_time_seconds);
        let meters = Arc::new(RunMeters::default());
        if let Ok(result) = tokio::time::timeout(
            deadline,
            self.run_inner(request, memories, Arc::clone(&meters)),
        )
        .await
        {
            result
        } else {
            let (input_tokens, output_tokens, workspace_tool_calls) = meters.snapshot();
            Ok(degraded_outcome(
                &AgentError::WallTime,
                input_tokens,
                output_tokens,
                workspace_tool_calls,
            ))
        }
    }

    // Budget checks intentionally remain adjacent to the actions they guard.
    #[allow(clippy::too_many_lines)]
    async fn run_inner(
        &self,
        request: &CodingRunRequest,
        memories: &[RetrievedMemory],
        meters: Arc<RunMeters>,
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
            .unwrap_or(1_000)
            .saturating_add(16)
            .min(1_016);
        let context_bytes = usize::try_from(request.budgets.model_input_tokens)
            .unwrap_or(2_000_000)
            .saturating_mul(4)
            .min(8_000_000);
        let mut input_tokens = 0_u64;
        let mut output_tokens = 0_u64;
        let mut workspace_tool_calls = 0_u32;
        let mut last_call: Option<(String, Value)> = None;
        let mut repeated_calls = 0_u8;

        for turn in 0..max_turns {
            if !context::enforce_budget(&mut messages, context_bytes) {
                return Ok(degraded_outcome(
                    &AgentError::ContextBudget,
                    input_tokens,
                    output_tokens,
                    workspace_tool_calls,
                ));
            }
            debug_assert!(context::message_bytes(&messages) <= context_bytes);
            let chunk = match self.model.next(&messages, &definitions).await {
                Ok(chunk) => chunk,
                Err(error) if workspace_tool_calls > 0 => {
                    return Ok(degraded_outcome(
                        &AgentError::Model(error.to_string()),
                        input_tokens,
                        output_tokens,
                        workspace_tool_calls,
                    ));
                }
                Err(error) => return Err(AgentError::Model(error.to_string())),
            };
            if let Some(cost) = &chunk.cost {
                input_tokens = input_tokens.saturating_add(
                    u64::try_from(cost.usage.input_tokens.max(0)).unwrap_or(u64::MAX),
                );
                output_tokens = output_tokens.saturating_add(
                    u64::try_from(cost.usage.output_tokens.max(0)).unwrap_or(u64::MAX),
                );
                meters.store(input_tokens, output_tokens, workspace_tool_calls);
            }
            if input_tokens > request.budgets.model_input_tokens {
                return Ok(degraded_outcome(
                    &AgentError::InputBudget,
                    input_tokens,
                    output_tokens,
                    workspace_tool_calls,
                ));
            }
            if output_tokens > request.budgets.model_output_tokens {
                return Ok(degraded_outcome(
                    &AgentError::OutputBudget,
                    input_tokens,
                    output_tokens,
                    workspace_tool_calls,
                ));
            }

            let Some(mut call) = chunk.tool_call else {
                let final_text = bounded_text(chunk.text.trim(), 2_000);
                if final_text.is_empty() {
                    return Ok(degraded_outcome(
                        &AgentError::EmptyFinal,
                        input_tokens,
                        output_tokens,
                        workspace_tool_calls,
                    ));
                }
                return Ok(AgentOutcome {
                    final_text,
                    input_tokens,
                    output_tokens,
                    workspace_tool_calls,
                });
            };
            if workspace_tool_calls >= request.budgets.workspace_tool_calls {
                return Ok(degraded_outcome(
                    &AgentError::ToolBudget,
                    input_tokens,
                    output_tokens,
                    workspace_tool_calls,
                ));
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
                let error =
                    AgentError::Model(format!("repeated identical tool call {:?}", call.name));
                if workspace_tool_calls > 0 {
                    return Ok(degraded_outcome(
                        &error,
                        input_tokens,
                        output_tokens,
                        workspace_tool_calls,
                    ));
                }
                return Err(error);
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
                        output: bounded_json_value(output, MAX_LIVE_TOOL_OBSERVATION_BYTES),
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
            workspace_tool_calls = workspace_tool_calls.saturating_add(1);
            meters.store(input_tokens, output_tokens, workspace_tool_calls);
        }
        Ok(degraded_outcome(
            &AgentError::TurnBudget,
            input_tokens,
            output_tokens,
            workspace_tool_calls,
        ))
    }
}

#[derive(Default)]
struct RunMeters {
    input_tokens: AtomicU64,
    output_tokens: AtomicU64,
    workspace_tool_calls: AtomicU32,
}

impl RunMeters {
    fn store(&self, input_tokens: u64, output_tokens: u64, workspace_tool_calls: u32) {
        self.input_tokens.store(input_tokens, Ordering::Relaxed);
        self.output_tokens.store(output_tokens, Ordering::Relaxed);
        self.workspace_tool_calls
            .store(workspace_tool_calls, Ordering::Relaxed);
    }

    fn snapshot(&self) -> (u64, u64, u32) {
        (
            self.input_tokens.load(Ordering::Relaxed),
            self.output_tokens.load(Ordering::Relaxed),
            self.workspace_tool_calls.load(Ordering::Relaxed),
        )
    }
}

fn degraded_outcome(
    reason: &AgentError,
    input_tokens: u64,
    output_tokens: u64,
    workspace_tool_calls: u32,
) -> AgentOutcome {
    AgentOutcome {
        final_text: bounded_text(
            &format!(
                concat!(
                    "Coding session ended without a final model summary: {}. ",
                    "The validator-owned workspace may contain a partial or complete patch; ",
                    "freeze it and rely on independent grading."
                ),
                reason
            ),
            2_000,
        ),
        input_tokens,
        output_tokens,
        workspace_tool_calls,
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
    use ditto_harness::{ChatChunk, Cost, CostedUsage, ToolCall, ToolDefinition, Usage};

    use super::*;
    use crate::model::shared_script;
    use crate::protocol::{
        CodingBudgets, CodingIssue, CodingRuntimePolicy, CODING_CONTRACT_VERSION,
    };

    struct RecordingTool {
        calls: Mutex<Vec<Value>>,
    }

    struct NeverModel;

    #[async_trait]
    impl Model for NeverModel {
        async fn next(
            &self,
            _messages: &[ChatMessage],
            _tools: &[ToolDefinition],
        ) -> ditto_harness::Result<ChatChunk> {
            std::future::pending().await
        }
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
        let bounded = bounded_json_value(
            json!({"content": "x".repeat(1_000_000)}),
            MAX_LIVE_TOOL_OBSERVATION_BYTES,
        );
        assert!(serde_json::to_vec(&bounded).unwrap().len() <= MAX_LIVE_TOOL_OBSERVATION_BYTES);
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
        let outcome = agent.run(&req, &[]).await.unwrap();
        assert_eq!(outcome.workspace_tool_calls, 2);
        assert!(outcome.final_text.contains("repeated identical tool call"));
    }

    #[tokio::test]
    async fn output_budget_after_workspace_activity_returns_degraded_report() {
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
                text: "completed patch summary".to_string(),
                cost: Some(CostedUsage {
                    usage: Usage {
                        provider: "test".to_string(),
                        model: "test".to_string(),
                        input_tokens: 1,
                        output_tokens: 1_001,
                        total_tokens: 1_002,
                    },
                    cost: Cost::default(),
                }),
                ..ChatChunk::default()
            },
        ]);
        let tool = Arc::new(RecordingTool {
            calls: Mutex::new(Vec::new()),
        });
        let agent = CodingAgent::new(model, vec![tool as Arc<dyn Tool>]);
        let outcome = agent.run(&request(), &[]).await.unwrap();
        assert_eq!(outcome.workspace_tool_calls, 1);
        assert!(outcome
            .final_text
            .contains("model output token budget exhausted"));
        assert!(outcome.final_text.contains("freeze it"));
    }

    #[tokio::test]
    async fn tool_budget_returns_degraded_report_instead_of_error() {
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
                tool_call: Some(ToolCall {
                    id: "call-2".to_string(),
                    name: "repo_read_file".to_string(),
                    args: json!({"path":"other.py"}),
                }),
                ..ChatChunk::default()
            },
        ]);
        let tool = Arc::new(RecordingTool {
            calls: Mutex::new(Vec::new()),
        });
        let mut request = request();
        request.budgets.workspace_tool_calls = 1;
        let agent = CodingAgent::new(model, vec![tool as Arc<dyn Tool>]);
        let outcome = agent.run(&request, &[]).await.unwrap();
        assert_eq!(outcome.workspace_tool_calls, 1);
        assert!(outcome
            .final_text
            .contains("workspace tool budget exhausted"));
    }

    #[tokio::test]
    async fn wall_time_returns_degraded_report_instead_of_error() {
        let mut request = request();
        request.budgets.wall_time_seconds = 1;
        let agent = CodingAgent::new(Arc::new(NeverModel), Vec::new());
        let outcome = agent.run(&request, &[]).await.unwrap();
        assert!(outcome.final_text.contains("wall-time budget exhausted"));
        assert!(outcome.final_text.contains("freeze it"));
        assert_eq!(outcome.workspace_tool_calls, 0);
    }

    struct OneToolThenHang {
        remaining: std::sync::atomic::AtomicU8,
    }

    #[async_trait]
    impl Model for OneToolThenHang {
        async fn next(
            &self,
            _messages: &[ChatMessage],
            _tools: &[ToolDefinition],
        ) -> ditto_harness::Result<ChatChunk> {
            if self.remaining.swap(0, Ordering::Relaxed) == 0 {
                std::future::pending::<()>().await;
            }
            Ok(ChatChunk {
                tool_call: Some(ToolCall {
                    id: "call".to_string(),
                    name: "repo_read_file".to_string(),
                    args: json!({"path": "app.py"}),
                }),
                ..ChatChunk::default()
            })
        }
    }

    #[tokio::test]
    async fn wall_time_preserves_workspace_activity_counts() {
        let mut request = request();
        request.budgets.wall_time_seconds = 1;
        let tool = Arc::new(RecordingTool {
            calls: Mutex::new(Vec::new()),
        });
        let agent = CodingAgent::new(
            Arc::new(OneToolThenHang {
                remaining: std::sync::atomic::AtomicU8::new(1),
            }),
            vec![tool as Arc<dyn Tool>],
        );
        let outcome = agent.run(&request, &[]).await.unwrap();
        assert!(outcome.final_text.contains("wall-time budget exhausted"));
        assert_eq!(outcome.workspace_tool_calls, 1);
    }
}
