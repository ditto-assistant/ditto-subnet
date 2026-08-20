//! Typed client for the validator-owned coding workspace capability.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use ditto_harness::{Error, Tool, ToolDefinition};
use serde_json::{json, Value};
use tokio::sync::Mutex;

use crate::protocol::{
    is_lower_sha256, WorkspaceToolRequest, WorkspaceToolResponse, CODING_CONTRACT_VERSION,
};

const TOOL_NAMES: &[(&str, &str)] = &[
    ("repo_list_tree", "repo.list_tree"),
    ("repo_search", "repo.search"),
    ("repo_read_file", "repo.read_file"),
    ("repo_read_range", "repo.read_range"),
    ("repo_apply_patch", "repo.apply_patch"),
    ("repo_create_file", "repo.create_file"),
    ("repo_delete_file", "repo.delete_file"),
    ("tests_run", "tests.run"),
    ("build_run", "build.run"),
    ("git_status", "git.status"),
    ("git_diff", "git.diff"),
];

struct WorkspaceContext {
    client: reqwest::Client,
    endpoint: String,
    case_id: String,
    profile_capability_id: String,
    sequence: Mutex<WorkspaceSequence>,
}

#[derive(Debug, Default)]
struct WorkspaceSequence {
    next_call: u64,
    last_sequence: u64,
    poisoned: bool,
}

#[derive(Clone)]
pub struct WorkspaceClient {
    context: Arc<WorkspaceContext>,
}

impl WorkspaceClient {
    /// Creates a client for one opaque workspace capability.
    ///
    /// # Errors
    ///
    /// Returns an error when the capability URL or HTTP client configuration
    /// is invalid.
    pub fn new(
        endpoint: String,
        case_id: String,
        profile_capability_id: String,
    ) -> Result<Self, Error> {
        let url = reqwest::Url::parse(&endpoint)
            .map_err(|error| Error::InvalidArgument(format!("invalid workspace URL: {error}")))?;
        if !matches!(url.scheme(), "http" | "https")
            || !url.username().is_empty()
            || url.password().is_some()
            || url.fragment().is_some()
        {
            return Err(Error::InvalidArgument(
                "workspace capability must be an http(s) URL without credentials or fragment"
                    .to_string(),
            ));
        }
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_mins(5))
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Self {
            context: Arc::new(WorkspaceContext {
                client,
                endpoint,
                case_id,
                profile_capability_id,
                sequence: Mutex::new(WorkspaceSequence::default()),
            }),
        })
    }

    #[must_use]
    pub fn tools(&self) -> Vec<Arc<dyn Tool>> {
        tool_definitions()
            .into_iter()
            .map(|definition| {
                let runner_name = runner_name_for(&definition.name)
                    .unwrap_or(definition.name.as_str())
                    .to_string();
                Arc::new(RemoteWorkspaceTool {
                    definition,
                    runner_name,
                    context: Arc::clone(&self.context),
                }) as Arc<dyn Tool>
            })
            .collect()
    }
}

struct RemoteWorkspaceTool {
    definition: ToolDefinition,
    runner_name: String,
    context: Arc<WorkspaceContext>,
}

#[async_trait]
impl Tool for RemoteWorkspaceTool {
    fn definition(&self) -> ToolDefinition {
        self.definition.clone()
    }

    async fn execute(&self, arguments: Value) -> ditto_harness::Result<Value> {
        if !arguments.is_object() {
            return Err(Error::Tool(
                "workspace tool arguments must be an object".to_string(),
            ));
        }
        let mut sequence = self.context.sequence.lock().await;
        if sequence.poisoned {
            return Err(Error::Tool(
                "workspace session is unavailable after an ambiguous transport failure".to_string(),
            ));
        }
        sequence.next_call = sequence.next_call.saturating_add(1);
        let ordinal = sequence.next_call;
        let call_id = format!("workspace-call-{ordinal}");
        let request = WorkspaceToolRequest {
            coding_contract_version: CODING_CONTRACT_VERSION,
            case_id: self.context.case_id.clone(),
            profile_capability_id: self.context.profile_capability_id.clone(),
            call_id: call_id.clone(),
            name: self.runner_name.clone(),
            arguments,
        };
        let mut attempt = 0_u8;
        let (status, bytes) = loop {
            let result = async {
                let response = self
                    .context
                    .client
                    .post(&self.context.endpoint)
                    .json(&request)
                    .send()
                    .await?;
                let status = response.status();
                let bytes = response.bytes().await?;
                Ok::<_, reqwest::Error>((status, bytes))
            }
            .await;
            match result {
                Ok(response) => break response,
                Err(_) if attempt == 0 => attempt = 1,
                Err(error) => {
                    sequence.poisoned = true;
                    return Err(error.into());
                }
            }
        };
        if bytes.len() > 2 * 1024 * 1024 {
            sequence.poisoned = true;
            return Err(Error::Tool("workspace response exceeded 2 MiB".to_string()));
        }
        if !status.is_success() {
            sequence.poisoned = true;
            return Err(Error::Tool(format!(
                "workspace endpoint returned HTTP {status}"
            )));
        }
        let response: WorkspaceToolResponse = serde_json::from_slice(&bytes).inspect_err(|_| {
            sequence.poisoned = true;
        })?;
        if response.call_id != call_id {
            sequence.poisoned = true;
            return Err(Error::Tool("workspace call_id mismatch".to_string()));
        }
        if !is_lower_sha256(&response.event_sha256) {
            sequence.poisoned = true;
            return Err(Error::Tool(
                "workspace response has invalid event_sha256".to_string(),
            ));
        }
        let expected = sequence.last_sequence.saturating_add(1);
        if response.sequence != expected {
            sequence.poisoned = true;
            return Err(Error::Tool(format!(
                "workspace sequence mismatch: expected {}, received {}",
                expected, response.sequence
            )));
        }
        sequence.last_sequence = response.sequence;
        if response.ok && response.error.is_some() {
            sequence.poisoned = true;
            return Err(Error::Tool(
                "successful workspace response carried an error".to_string(),
            ));
        }
        if !response.ok {
            return Err(Error::Tool(response.error.map_or_else(
                || "workspace operation failed".to_string(),
                |error| format!("{}: {}", error.code, error.message),
            )));
        }
        Ok(response.result)
    }
}

/// Returns the canonical coding workspace tool schemas.
///
/// # Panics
///
/// Panics only if the internal canonical order names a missing definition.
#[must_use]
#[allow(clippy::too_many_lines)]
pub fn tool_definitions() -> Vec<ToolDefinition> {
    let definitions: HashMap<&str, (&str, Value)> = HashMap::from([
        (
            "repo.list_tree",
            (
                "List a bounded repository subtree.",
                object_schema(
                    json!({
                        "path": {"type": "string", "maxLength": 256},
                        "depth": {"type": "integer", "minimum": 0, "maximum": 8}
                    }),
                    &["path", "depth"],
                ),
            ),
        ),
        (
            "repo.search",
            (
                "Search literal text within a bounded repository subtree.",
                object_schema(
                    json!({
                        "query": {"type": "string", "minLength": 1, "maxLength": 256},
                        "path": {"type": "string", "maxLength": 256},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 100}
                    }),
                    &["query", "path", "max_results"],
                ),
            ),
        ),
        (
            "repo.read_file",
            (
                "Read one bounded repository file.",
                object_schema(
                    json!({"path": {"type": "string", "minLength": 1, "maxLength": 256}}),
                    &["path"],
                ),
            ),
        ),
        (
            "repo.read_range",
            (
                "Read a bounded inclusive line range from one repository file.",
                object_schema(
                    json!({
                        "path": {"type": "string", "minLength": 1, "maxLength": 256},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1}
                    }),
                    &["path", "start_line", "end_line"],
                ),
            ),
        ),
        (
            "repo.apply_patch",
            (
                "Atomically replace exact text in one editable file at its expected digest.",
                object_schema(
                    json!({
                        "path": {"type": "string", "minLength": 1, "maxLength": 256},
                        "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "replacements": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 16,
                            "items": {
                                "type": "object",
                                "additionalProperties": false,
                                "required": ["old_text", "new_text"],
                                "properties": {
                                    "old_text": {"type": "string", "minLength": 1, "maxLength": 65536},
                                    "new_text": {"type": "string", "maxLength": 65536}
                                }
                            }
                        }
                    }),
                    &["path", "expected_sha256", "replacements"],
                ),
            ),
        ),
        (
            "repo.create_file",
            (
                "Create one manifest-authorized UTF-8 file; disabled by public practice policy.",
                object_schema(
                    json!({
                        "path": {"type": "string", "minLength": 1, "maxLength": 256},
                        "content": {"type": "string", "maxLength": 65536}
                    }),
                    &["path", "content"],
                ),
            ),
        ),
        (
            "repo.delete_file",
            (
                "Delete one manifest-authorized file at its expected digest; disabled by public practice policy.",
                object_schema(
                    json!({
                        "path": {"type": "string", "minLength": 1, "maxLength": 256},
                        "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}
                    }),
                    &["path", "expected_sha256"],
                ),
            ),
        ),
        (
            "tests.run",
            (
                "Run a task-manifest test command by opaque command ID.",
                object_schema(
                    json!({"command_id": {"type": "string", "minLength": 1, "maxLength": 80}}),
                    &["command_id"],
                ),
            ),
        ),
        (
            "build.run",
            (
                "Run a task-manifest build command by opaque command ID.",
                object_schema(
                    json!({"command_id": {"type": "string", "minLength": 1, "maxLength": 80}}),
                    &["command_id"],
                ),
            ),
        ),
        (
            "git.status",
            (
                "Return the validator-owned workspace change status.",
                object_schema(json!({}), &[]),
            ),
        ),
        (
            "git.diff",
            (
                "Return the bounded current workspace diff for review.",
                object_schema(json!({}), &[]),
            ),
        ),
    ]);
    TOOL_NAMES
        .iter()
        .map(|(model_name, runner_name)| {
            let (description, schema) = definitions
                .get(runner_name)
                .expect("canonical runner tool exists");
            ToolDefinition {
                name: (*model_name).to_string(),
                description: (*description).to_string(),
                input_schema: schema.clone(),
            }
        })
        .collect()
}

fn runner_name_for(model_name: &str) -> Option<&'static str> {
    TOOL_NAMES.iter().find_map(|(model, runner)| {
        if *model == model_name {
            Some(*runner)
        } else {
            None
        }
    })
}

#[allow(clippy::needless_pass_by_value)]
fn object_schema(properties: Value, required: &[&str]) -> Value {
    json!({
        "type": "object",
        "additionalProperties": false,
        "properties": properties,
        "required": required
    })
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

    use axum::extract::State;
    use axum::http::{header, StatusCode};
    use axum::routing::post;
    use axum::{Json, Router};

    use super::*;

    #[derive(Clone, Default)]
    struct SerialState {
        active: Arc<AtomicUsize>,
        max_active: Arc<AtomicUsize>,
        sequence: Arc<AtomicUsize>,
    }

    async fn serial_response(
        State(state): State<SerialState>,
        Json(request): Json<WorkspaceToolRequest>,
    ) -> Json<WorkspaceToolResponse> {
        let active = state.active.fetch_add(1, Ordering::SeqCst) + 1;
        state.max_active.fetch_max(active, Ordering::SeqCst);
        tokio::time::sleep(Duration::from_millis(25)).await;
        state.active.fetch_sub(1, Ordering::SeqCst);
        let sequence = state.sequence.fetch_add(1, Ordering::SeqCst) + 1;
        Json(WorkspaceToolResponse {
            call_id: request.call_id,
            sequence: u64::try_from(sequence).unwrap(),
            ok: true,
            result: json!({"sequence": sequence}),
            error: None,
            event_sha256: "c".repeat(64),
        })
    }

    #[test]
    fn canonical_tools_are_narrow_and_ordered() {
        let tools = tool_definitions();
        assert_eq!(tools.len(), 11);
        assert_eq!(tools[0].name, "repo_list_tree");
        assert_eq!(tools[10].name, "git_diff");
        assert!(tools
            .iter()
            .all(|tool| tool.input_schema["additionalProperties"] == false));
        assert!(tools.iter().all(|tool| !tool.name.contains("shell")));
        for tool in &tools {
            assert!(
                (1..=64).contains(&tool.name.len())
                    && tool
                        .name
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-')),
                "model-facing function {:?} violates ^[A-Za-z0-9_-]{{1,64}}$",
                tool.name
            );
        }
        assert_eq!(runner_name_for("repo_read_file"), Some("repo.read_file"));
        for (model_name, runner_name) in TOOL_NAMES {
            assert_eq!(runner_name_for(model_name), Some(*runner_name));
        }
    }

    async fn redirect() -> (StatusCode, [(header::HeaderName, &'static str); 1]) {
        (
            StatusCode::TEMPORARY_REDIRECT,
            [(header::LOCATION, "/escaped")],
        )
    }

    async fn escaped(State(called): State<Arc<AtomicBool>>) -> Json<WorkspaceToolResponse> {
        called.store(true, Ordering::SeqCst);
        Json(WorkspaceToolResponse {
            call_id: "workspace-call-1".to_string(),
            sequence: 1,
            ok: true,
            result: json!({}),
            error: None,
            event_sha256: "a".repeat(64),
        })
    }

    #[tokio::test]
    async fn workspace_capability_client_refuses_redirects() {
        let called = Arc::new(AtomicBool::new(false));
        let app = Router::new()
            .route("/tool", post(redirect))
            .route("/escaped", post(escaped))
            .with_state(Arc::clone(&called));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let client = WorkspaceClient::new(
            format!("http://{address}/tool"),
            "case".to_string(),
            "profile".to_string(),
        )
        .unwrap();
        let result = client.tools()[0]
            .execute(json!({"path": "", "depth": 1}))
            .await;
        assert!(matches!(result, Err(Error::Tool(message)) if message.contains("307")));
        assert!(!called.load(Ordering::SeqCst));
        server.abort();
    }

    #[tokio::test]
    async fn transport_loss_retries_the_same_idempotency_key() {
        use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            for attempt in 0..2 {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut request = vec![0_u8; 16 * 1024];
                let _ = stream.read(&mut request).await.unwrap();
                if attempt == 0 {
                    drop(stream);
                    continue;
                }
                let body = serde_json::to_vec(&WorkspaceToolResponse {
                    call_id: "workspace-call-1".to_string(),
                    sequence: 1,
                    ok: true,
                    result: json!({"replayed": true}),
                    error: None,
                    event_sha256: "b".repeat(64),
                })
                .unwrap();
                let headers = format!(
                    "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
                    body.len()
                );
                stream.write_all(headers.as_bytes()).await.unwrap();
                stream.write_all(&body).await.unwrap();
                stream.shutdown().await.unwrap();
            }
        });
        let client = WorkspaceClient::new(
            format!("http://{address}/tool"),
            "case".to_string(),
            "profile".to_string(),
        )
        .unwrap();
        let result = client.tools()[0]
            .execute(json!({"path": ".", "depth": 1}))
            .await
            .unwrap();
        assert_eq!(result["replayed"], true);
        server.await.unwrap();
    }

    #[tokio::test]
    async fn concurrent_workspace_calls_are_serialized() {
        let state = SerialState::default();
        let max_active = Arc::clone(&state.max_active);
        let app = Router::new()
            .route("/tool", post(serial_response))
            .with_state(state);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let client = WorkspaceClient::new(
            format!("http://{address}/tool"),
            "case".to_string(),
            "profile".to_string(),
        )
        .unwrap();
        let tools = client.tools();
        let first = Arc::clone(&tools[0]);
        let second = Arc::clone(&tools[1]);
        let (first_result, second_result) = tokio::join!(
            first.execute(json!({"path": ".", "depth": 1})),
            second.execute(json!({"query": "x", "path": ".", "max_results": 1}))
        );
        assert!(first_result.is_ok());
        assert!(second_result.is_ok());
        assert_eq!(max_active.load(Ordering::SeqCst), 1);
        server.abort();
    }
}
