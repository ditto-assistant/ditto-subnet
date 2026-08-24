//! Luna-compatible Chat Completions models.

use std::collections::VecDeque;
use std::fmt;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use ditto_harness::{
    ChatChunk, ChatMessage, ContentType, Cost, CostedUsage, Error, Model, ToolCall, ToolDefinition,
    Usage,
};
use reqwest::Url;
use serde_json::{json, Value};

use crate::protocol::{LUNA_MODEL, LUNA_REASONING_EFFORT};

const OPENROUTER_BASE_URL: &str = "https://openrouter.ai/api/v1";
const OPENROUTER_PROVIDER_ROUTE: &str = "azure/eu";
const MAX_MODEL_RESPONSE_BYTES: usize = 8 * 1024 * 1024;
const MAX_TOOL_CALLS_PER_RESPONSE: usize = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum EndpointKind {
    TicketBroker,
    DirectOpenRouter,
}

/// A strict Luna Chat Completions adapter. Direct `OpenRouter` access is
/// constructible only with an explicit practice permission flag.
pub struct LunaChatModel {
    client: reqwest::Client,
    endpoint: Url,
    bearer: String,
    kind: EndpointKind,
    max_completion_tokens: u64,
}

impl fmt::Debug for LunaChatModel {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("LunaChatModel")
            .field("endpoint", &self.endpoint)
            .field("bearer", &"[REDACTED]")
            .field("kind", &self.kind)
            .field("max_completion_tokens", &self.max_completion_tokens)
            .finish_non_exhaustive()
    }
}

impl LunaChatModel {
    /// Builds a model for a ticket-scoped Platform broker.
    ///
    /// # Errors
    ///
    /// Returns an error when the broker URL or HTTP client configuration is
    /// invalid.
    pub fn ticket_broker(base_url: &str, max_completion_tokens: u64) -> Result<Self, Error> {
        Self::new(
            base_url,
            "ticket".to_string(),
            EndpointKind::TicketBroker,
            max_completion_tokens,
        )
    }

    /// Builds an explicitly permitted direct local-practice model.
    ///
    /// # Errors
    ///
    /// Returns an error when permission, the API key, or client configuration
    /// is invalid.
    pub fn direct_openrouter(
        api_key: String,
        explicit_practice_permission: bool,
        max_completion_tokens: u64,
    ) -> Result<Self, Error> {
        if !explicit_practice_permission {
            return Err(Error::InvalidArgument(
                "direct OpenRouter requires explicit local-practice permission".to_string(),
            ));
        }
        if api_key.trim().is_empty() {
            return Err(Error::InvalidArgument(
                "OPENROUTER_API_KEY is required for direct local practice".to_string(),
            ));
        }
        Self::new(
            OPENROUTER_BASE_URL,
            api_key,
            EndpointKind::DirectOpenRouter,
            max_completion_tokens,
        )
    }

    fn new(
        base_url: &str,
        bearer: String,
        kind: EndpointKind,
        max_completion_tokens: u64,
    ) -> Result<Self, Error> {
        let mut endpoint = Url::parse(base_url)
            .map_err(|error| Error::InvalidArgument(format!("invalid inference URL: {error}")))?;
        if !matches!(endpoint.scheme(), "http" | "https")
            || !endpoint.username().is_empty()
            || endpoint.password().is_some()
            || endpoint.query().is_some()
            || endpoint.fragment().is_some()
        {
            return Err(Error::InvalidArgument(
                "inference URL must be an http(s) URL without credentials, query, or fragment"
                    .to_string(),
            ));
        }
        let path = format!("{}/chat/completions", endpoint.path().trim_end_matches('/'));
        endpoint.set_path(&path);
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_mins(5))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(Error::Http)?;
        Ok(Self {
            client,
            endpoint,
            bearer,
            kind,
            max_completion_tokens: max_completion_tokens.clamp(1, 32_768),
        })
    }

    fn request_body(
        &self,
        messages: &[ChatMessage],
        tools: &[ToolDefinition],
    ) -> Result<Value, Error> {
        let messages = messages
            .iter()
            .map(openai_message)
            .collect::<Result<Vec<_>, _>>()?;
        let tools: Vec<Value> = tools
            .iter()
            .map(|tool| {
                json!({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": if tool.input_schema.is_null() {
                            json!({"type": "object", "properties": {}, "additionalProperties": false})
                        } else {
                            tool.input_schema.clone()
                        }
                    }
                })
            })
            .collect();
        let mut body = json!({
            "model": LUNA_MODEL,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "reasoning": {"effort": LUNA_REASONING_EFFORT},
            "max_completion_tokens": self.max_completion_tokens
        });
        if self.kind == EndpointKind::DirectOpenRouter {
            body["provider"] = json!({
                "order": [OPENROUTER_PROVIDER_ROUTE],
                "allow_fallbacks": false,
                "require_parameters": true,
                "data_collection": "deny",
                "zdr": true
            });
        } else {
            body["parallel_tool_calls"] = Value::Bool(false);
        }
        Ok(body)
    }

    fn validate_response_identity(&self, response: &ChatCompletionResponse) -> Result<(), Error> {
        if !response.model.is_empty() && response.model != LUNA_MODEL {
            return Err(Error::Model(format!(
                "chat completion model mismatch: expected {LUNA_MODEL:?}, received {:?}",
                response.model
            )));
        }
        if self.kind == EndpointKind::DirectOpenRouter
            && response.provider.as_deref() != Some("Azure")
        {
            return Err(Error::Model(format!(
                "OpenRouter provider mismatch: expected Azure, received {:?}",
                response.provider
            )));
        }
        if self.kind == EndpointKind::DirectOpenRouter && response.id.is_empty() {
            return Err(Error::Model(
                "OpenRouter response omitted its generation identity".to_string(),
            ));
        }
        if response.usage.prompt < 0 || response.usage.completion < 0 || response.usage.total < 0 {
            return Err(Error::Model(
                "chat completion reported a negative token count".to_string(),
            ));
        }
        let expected_total = response
            .usage
            .prompt
            .checked_add(response.usage.completion)
            .ok_or_else(|| Error::Model("chat completion token total overflowed".to_string()))?;
        if response.usage.total != expected_total {
            return Err(Error::Model(format!(
                "chat completion token total mismatch: expected {expected_total}, received {}",
                response.usage.total
            )));
        }
        if let Some(cost) = response.usage.cost {
            if !cost.is_finite() || cost < 0.0 {
                return Err(Error::Model(
                    "chat completion reported an invalid USD cost".to_string(),
                ));
            }
        } else if self.kind == EndpointKind::DirectOpenRouter {
            return Err(Error::Model(
                "OpenRouter response omitted its reported USD cost".to_string(),
            ));
        }
        Ok(())
    }

    #[cfg(test)]
    fn direct_openrouter_for_test(
        base_url: &str,
        max_completion_tokens: u64,
    ) -> Result<Self, Error> {
        Self::new(
            base_url,
            "test-secret".to_string(),
            EndpointKind::DirectOpenRouter,
            max_completion_tokens,
        )
    }
}

#[async_trait]
impl Model for LunaChatModel {
    async fn next(
        &self,
        messages: &[ChatMessage],
        tools: &[ToolDefinition],
    ) -> ditto_harness::Result<ChatChunk> {
        let body = self.request_body(messages, tools)?;
        let response = self
            .client
            .post(self.endpoint.clone())
            .bearer_auth(&self.bearer)
            .header("HTTP-Referer", "https://heyditto.ai")
            .header("X-OpenRouter-Title", "DittoBench Coding")
            .json(&body)
            .send()
            .await?;
        let status = response.status();
        if let Some(length) = response.content_length() {
            if length > MAX_MODEL_RESPONSE_BYTES as u64 {
                return Err(Error::Model("model response exceeded 8 MiB".to_string()));
            }
        }
        let bytes = response.bytes().await?;
        if bytes.len() > MAX_MODEL_RESPONSE_BYTES {
            return Err(Error::Model("model response exceeded 8 MiB".to_string()));
        }
        if !status.is_success() {
            let detail = String::from_utf8_lossy(&bytes);
            return Err(Error::Model(format!(
                "chat completion returned HTTP {status}: {}",
                detail.chars().take(512).collect::<String>()
            )));
        }
        let value: Value = serde_json::from_slice(&bytes)?;
        if let Some(error) = value.get("error").filter(|error| !error.is_null()) {
            let detail = error
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("OpenRouter returned an error envelope");
            return Err(Error::Model(format!(
                "chat completion error envelope: {}",
                detail.chars().take(512).collect::<String>()
            )));
        }
        let parsed: ChatCompletionResponse = serde_json::from_value(value)
            .map_err(|error| Error::Model(format!("invalid chat completion envelope: {error}")))?;
        self.validate_response_identity(&parsed)?;
        let cost = response_cost(self.kind, &parsed);
        let metadata = response_metadata(self.kind, &parsed);
        let choice = parsed
            .choices
            .into_iter()
            .next()
            .ok_or_else(|| Error::Model("chat completion returned no choices".to_string()))?;
        if choice.message.tool_calls.len() > MAX_TOOL_CALLS_PER_RESPONSE {
            return Err(Error::Model(format!(
                "chat completion returned {} tool calls; maximum is {MAX_TOOL_CALLS_PER_RESPONSE}",
                choice.message.tool_calls.len()
            )));
        }
        let mut tool_calls = parse_tool_calls(choice.message.tool_calls)?;
        let tool_call = tool_calls.pop_front();
        Ok(ChatChunk {
            text: choice.message.content.unwrap_or_default(),
            tool_call,
            cost: Some(cost),
            metadata: Some(metadata),
        })
    }
}

fn parse_tool_calls(calls: Vec<OpenAiToolCall>) -> Result<VecDeque<ToolCall>, Error> {
    calls
        .into_iter()
        .map(|call| {
            let args = serde_json::from_str(&call.function.arguments).map_err(|error| {
                Error::Model(format!("tool arguments are not valid JSON: {error}"))
            })?;
            Ok(ToolCall {
                id: call.id,
                name: call.function.name,
                args,
            })
        })
        .collect()
}

fn response_cost(kind: EndpointKind, response: &ChatCompletionResponse) -> CostedUsage {
    let provider = match (kind, response.provider.as_deref()) {
        (EndpointKind::TicketBroker, _) => "platform".to_string(),
        (EndpointKind::DirectOpenRouter, Some(provider)) => format!("openrouter:{provider}"),
        (EndpointKind::DirectOpenRouter, None) => "openrouter:unknown".to_string(),
    };
    CostedUsage {
        usage: Usage {
            provider,
            model: if response.model.is_empty() {
                LUNA_MODEL.to_string()
            } else {
                response.model.clone()
            },
            input_tokens: response.usage.prompt,
            output_tokens: response.usage.completion,
            total_tokens: response.usage.total,
        },
        cost: response
            .usage
            .cost
            .map_or_else(Cost::default, |amount| Cost {
                currency: "USD".to_string(),
                amount,
            }),
    }
}

fn response_metadata(
    kind: EndpointKind,
    response: &ChatCompletionResponse,
) -> serde_json::Map<String, Value> {
    let mut metadata = serde_json::Map::new();
    if !response.id.is_empty() {
        metadata.insert(
            "generation_id".to_string(),
            Value::String(response.id.clone()),
        );
    }
    if let Some(provider) = &response.provider {
        metadata.insert(
            "response_provider".to_string(),
            Value::String(provider.clone()),
        );
    }
    metadata.insert(
        "requested_model".to_string(),
        Value::String(LUNA_MODEL.to_string()),
    );
    metadata.insert(
        "effective_model".to_string(),
        Value::String(if response.model.is_empty() {
            LUNA_MODEL.to_string()
        } else {
            response.model.clone()
        }),
    );
    metadata.insert(
        "model_identity_source".to_string(),
        Value::String(
            if response.model.is_empty() {
                "request_binding"
            } else {
                "response"
            }
            .to_string(),
        ),
    );
    metadata.insert(
        "transport".to_string(),
        Value::String(
            match kind {
                EndpointKind::TicketBroker => "ticket_broker",
                EndpointKind::DirectOpenRouter => "direct_openrouter",
            }
            .to_string(),
        ),
    );
    if let Some(cost) = response.usage.cost.and_then(serde_json::Number::from_f64) {
        metadata.insert("reported_cost_usd".to_string(), Value::Number(cost));
    }
    metadata
}

fn openai_message(message: &ChatMessage) -> Result<Value, Error> {
    let text = message
        .content
        .iter()
        .filter(|content| {
            matches!(
                content.content_type,
                None | Some(ContentType::Text | ContentType::Markdown)
            )
        })
        .map(|content| content.content.as_str())
        .filter(|content| !content.is_empty())
        .collect::<Vec<_>>()
        .join("\n");
    match message.role.as_str() {
        "system" | "user" => Ok(json!({"role": message.role, "content": text})),
        "assistant" => {
            let calls: Vec<Value> = message
                .tool_calls
                .iter()
                .map(|call| {
                    json!({
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.args.to_string()
                        }
                    })
                })
                .collect();
            Ok(json!({
                "role": "assistant",
                "content": if text.is_empty() { Value::Null } else { Value::String(text) },
                "tool_calls": calls
            }))
        }
        "tool" => {
            let result = message
                .content
                .iter()
                .find_map(|content| content.tool_call_response.as_ref())
                .map_or_else(
                    || Value::String(text),
                    |response| {
                        if response.error.is_empty() {
                            response.output.clone()
                        } else {
                            json!({"error": response.error})
                        }
                    },
                );
            Ok(json!({
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": result.to_string()
            }))
        }
        other => Err(Error::InvalidArgument(format!(
            "unsupported chat role {other:?}"
        ))),
    }
}

#[derive(Debug, serde::Deserialize)]
struct ChatCompletionResponse {
    #[serde(default)]
    id: String,
    #[serde(default)]
    model: String,
    #[serde(default)]
    provider: Option<String>,
    choices: Vec<ChatChoice>,
    usage: ChatUsage,
}

#[derive(Debug, serde::Deserialize)]
struct ChatChoice {
    message: ChatCompletionMessage,
}

#[derive(Debug, serde::Deserialize)]
struct ChatCompletionMessage {
    #[serde(default)]
    content: Option<String>,
    #[serde(default)]
    tool_calls: Vec<OpenAiToolCall>,
}

#[derive(Debug, serde::Deserialize)]
struct OpenAiToolCall {
    id: String,
    function: OpenAiFunctionCall,
}

#[derive(Debug, serde::Deserialize)]
struct OpenAiFunctionCall {
    name: String,
    arguments: String,
}

#[derive(Debug, serde::Deserialize)]
struct ChatUsage {
    #[serde(rename = "prompt_tokens")]
    prompt: i64,
    #[serde(rename = "completion_tokens")]
    completion: i64,
    #[serde(rename = "total_tokens")]
    total: i64,
    #[serde(default)]
    cost: Option<f64>,
}

/// Deterministic model used to exercise the complete function-call loop with
/// no network. The script is public practice data, never scored behavior.
pub struct ScriptedLunaModel {
    chunks: Mutex<VecDeque<ChatChunk>>,
}

impl ScriptedLunaModel {
    #[must_use]
    pub fn new(chunks: Vec<ChatChunk>) -> Self {
        Self {
            chunks: Mutex::new(chunks.into()),
        }
    }

    /// Decodes a deterministic local-practice response sequence.
    ///
    /// # Errors
    ///
    /// Returns an error for malformed JSON or an empty script.
    pub fn from_json(bytes: &[u8]) -> Result<Self, Error> {
        let chunks: Vec<ChatChunk> = serde_json::from_slice(bytes)?;
        if chunks.is_empty() {
            return Err(Error::InvalidArgument(
                "scripted model requires at least one chunk".to_string(),
            ));
        }
        Ok(Self::new(chunks))
    }
}

#[async_trait]
impl Model for ScriptedLunaModel {
    async fn next(
        &self,
        _messages: &[ChatMessage],
        _tools: &[ToolDefinition],
    ) -> ditto_harness::Result<ChatChunk> {
        self.chunks
            .lock()
            .map_err(|_| Error::Model("scripted model lock poisoned".to_string()))?
            .pop_front()
            .ok_or_else(|| Error::Model("scripted model exhausted before final text".to_string()))
    }
}

#[must_use]
pub fn shared_script(chunks: Vec<ChatChunk>) -> Arc<dyn Model> {
    Arc::new(ScriptedLunaModel::new(chunks))
}

#[cfg(test)]
mod tests {
    use std::collections::{BTreeMap, HashMap};
    use std::fs;
    use std::path::Path;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

    use super::*;
    use axum::extract::State;
    use axum::http::{header, StatusCode};
    use axum::routing::post;
    use axum::{Json, Router};
    use ditto_harness::{Content, ContentType, ToolCallResponse};
    use serde::Deserialize;
    use sha2::{Digest, Sha256};

    use crate::agent::CODING_SYSTEM_PROMPT;
    use crate::workspace_client::tool_definitions;

    const MINER_INFERENCE_VECTOR: &str = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../packages/dittobench-coding-contract/testdata/coding_inference_miner_v1.json"
    ));

    #[derive(Debug, Deserialize)]
    struct MinerInferenceVector {
        schema: String,
        coding_contract_version: u32,
        weight_eligible: bool,
        system_prompt: Value,
        tool_schema: Value,
        turns: Vec<MinerInferenceTurn>,
        expected: MinerInferenceExpected,
    }

    #[derive(Debug, Deserialize)]
    struct MinerInferenceTurn {
        sequence: u32,
        messages: Vec<Value>,
        max_completion_tokens: u64,
        response: Value,
    }

    #[derive(Debug, Deserialize)]
    struct MinerInferenceExpected {
        #[serde(rename = "prompt_sha256")]
        prompt_digest: String,
        #[serde(rename = "tool_schema_sha256")]
        tool_schema_digest: String,
        #[serde(rename = "request_sha256")]
        request_digests: Vec<String>,
        #[serde(rename = "response_sha256")]
        response_digests: Vec<String>,
    }

    fn miner_inference_vector() -> MinerInferenceVector {
        serde_json::from_str(MINER_INFERENCE_VECTOR).expect("miner inference vector is valid")
    }

    fn canonicalize_vector(value: Value) -> Value {
        match value {
            Value::Object(object) => Value::Object(
                object
                    .into_iter()
                    .map(|(key, value)| (key, canonicalize_vector(value)))
                    .collect::<BTreeMap<_, _>>()
                    .into_iter()
                    .collect(),
            ),
            Value::Array(values) => {
                Value::Array(values.into_iter().map(canonicalize_vector).collect())
            }
            other => other,
        }
    }

    fn vector_digest(value: &Value) -> String {
        let serialized = serde_json::to_string(&canonicalize_vector(value.clone()))
            .expect("vector projection serializes");
        let mut bytes = serialized
            .replace('\u{2028}', "\\u2028")
            .replace('\u{2029}', "\\u2029")
            .into_bytes();
        bytes.push(b'\n');
        let digest = Sha256::digest(bytes);
        let mut output = String::with_capacity(64);
        for byte in digest {
            use std::fmt::Write as _;
            let _ = write!(output, "{byte:02x}");
        }
        output
    }

    fn vector_messages(values: &[Value]) -> Vec<ChatMessage> {
        let mut call_names = HashMap::new();
        for value in values {
            let Some(calls) = value.get("tool_calls").and_then(Value::as_array) else {
                continue;
            };
            for call in calls {
                let id = call["id"].as_str().expect("tool call id");
                let name = call["function"]["name"].as_str().expect("tool call name");
                call_names.insert(id.to_string(), name.to_string());
            }
        }

        values
            .iter()
            .map(|value| {
                let role = value["role"].as_str().expect("message role").to_string();
                let content = value
                    .get("content")
                    .and_then(Value::as_str)
                    .map(|text| vec![Content::text(text)])
                    .unwrap_or_default();
                let tool_calls = value
                    .get("tool_calls")
                    .and_then(Value::as_array)
                    .map(|calls| {
                        calls
                            .iter()
                            .map(|call| ToolCall {
                                id: call["id"].as_str().expect("tool call id").to_string(),
                                name: call["function"]["name"]
                                    .as_str()
                                    .expect("tool call name")
                                    .to_string(),
                                args: serde_json::from_str(
                                    call["function"]["arguments"]
                                        .as_str()
                                        .expect("tool call arguments"),
                                )
                                .expect("tool call arguments are JSON"),
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                let tool_call_id = value
                    .get("tool_call_id")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                let content = if role == "tool" {
                    let output: Value = serde_json::from_str(
                        value["content"].as_str().expect("tool result content"),
                    )
                    .expect("tool result content is JSON");
                    vec![Content {
                        content_type: Some(ContentType::ToolResult),
                        tool_call_response: Some(ToolCallResponse {
                            id: tool_call_id.clone(),
                            name: call_names
                                .get(&tool_call_id)
                                .expect("tool result references a prior call")
                                .clone(),
                            output,
                            error: String::new(),
                        }),
                        ..Content::default()
                    }]
                } else {
                    content
                };
                ChatMessage {
                    role,
                    content,
                    tool_calls,
                    tool_call_id,
                }
            })
            .collect()
    }

    fn expected_ticket_request(vector: &MinerInferenceVector, turn: &MinerInferenceTurn) -> Value {
        json!({
            "model": LUNA_MODEL,
            "messages": turn.messages,
            "tools": vector.tool_schema["tools"],
            "tool_choice": "auto",
            "reasoning": {"effort": LUNA_REASONING_EFFORT},
            "max_completion_tokens": turn.max_completion_tokens,
            "parallel_tool_calls": false
        })
    }

    #[derive(Clone)]
    struct CompletionState {
        response: Value,
        request: Arc<Mutex<Option<Value>>>,
        requests: Arc<AtomicUsize>,
    }

    async fn completion(
        State(state): State<CompletionState>,
        Json(request): Json<Value>,
    ) -> Json<Value> {
        state.requests.fetch_add(1, Ordering::SeqCst);
        *state.request.lock().unwrap() = Some(request);
        Json(state.response)
    }

    async fn direct_mock(
        response: Value,
    ) -> (
        LunaChatModel,
        Arc<Mutex<Option<Value>>>,
        Arc<AtomicUsize>,
        tokio::task::JoinHandle<()>,
    ) {
        let request = Arc::new(Mutex::new(None));
        let requests = Arc::new(AtomicUsize::new(0));
        let app = Router::new()
            .route("/v1/chat/completions", post(completion))
            .with_state(CompletionState {
                response,
                request: Arc::clone(&request),
                requests: Arc::clone(&requests),
            });
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let model = LunaChatModel::direct_openrouter_for_test(&format!("http://{address}/v1"), 100)
            .unwrap();
        (model, request, requests, server)
    }

    #[derive(Clone)]
    struct VectorCompletionState {
        responses: Arc<Mutex<VecDeque<Value>>>,
        requests: Arc<Mutex<Vec<Value>>>,
    }

    async fn vector_completion(
        State(state): State<VectorCompletionState>,
        Json(request): Json<Value>,
    ) -> Json<Value> {
        state.requests.lock().unwrap().push(request);
        Json(
            state
                .responses
                .lock()
                .unwrap()
                .pop_front()
                .expect("vector response is available"),
        )
    }

    async fn ticket_vector_mock(
        responses: Vec<Value>,
        max_completion_tokens: u64,
    ) -> (
        LunaChatModel,
        Arc<Mutex<Vec<Value>>>,
        tokio::task::JoinHandle<()>,
    ) {
        let requests = Arc::new(Mutex::new(Vec::new()));
        let app = Router::new()
            .route("/v1/chat/completions", post(vector_completion))
            .with_state(VectorCompletionState {
                responses: Arc::new(Mutex::new(responses.into())),
                requests: Arc::clone(&requests),
            });
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let model =
            LunaChatModel::ticket_broker(&format!("http://{address}/v1"), max_completion_tokens)
                .unwrap();
        (model, requests, server)
    }

    #[test]
    fn shared_miner_vector_matches_system_tools_and_ticket_requests() {
        let vector = miner_inference_vector();
        assert_eq!(vector.schema, "dittobench-coding-inference-miner-vector-v1");
        assert_eq!(vector.coding_contract_version, 1);
        assert!(!vector.weight_eligible);
        assert_eq!(
            vector.system_prompt,
            json!({
                "schema": "dittobench-coding-system-prompt-v1",
                "content": CODING_SYSTEM_PROMPT
            })
        );
        assert_eq!(
            vector_digest(&vector.system_prompt),
            vector.expected.prompt_digest
        );

        let tools = tool_definitions();
        let max_completion_tokens = vector.turns[0].max_completion_tokens;
        let model =
            LunaChatModel::ticket_broker("http://127.0.0.1:1/v1", max_completion_tokens).unwrap();
        let mut rebuilt = Vec::new();
        for (index, turn) in vector.turns.iter().enumerate() {
            assert_eq!(turn.sequence as usize, index + 1);
            assert_eq!(turn.max_completion_tokens, max_completion_tokens);
            let request = model
                .request_body(&vector_messages(&turn.messages), &tools)
                .unwrap();
            assert_eq!(request, expected_ticket_request(&vector, turn));
            assert_eq!(
                vector_digest(&request),
                vector.expected.request_digests[index]
            );
            assert_eq!(
                vector_digest(&turn.response),
                vector.expected.response_digests[index]
            );
            rebuilt.push(request);
        }

        let actual_tool_schema = json!({
            "schema": "dittobench-coding-model-tools-v1",
            "tools": rebuilt[0]["tools"]
        });
        assert_eq!(actual_tool_schema, vector.tool_schema);
        assert_eq!(
            vector_digest(&actual_tool_schema),
            vector.expected.tool_schema_digest
        );
    }

    #[tokio::test]
    async fn shared_miner_vector_responses_yield_tool_call_then_final_text() {
        let vector = miner_inference_vector();
        let responses = vector
            .turns
            .iter()
            .map(|turn| turn.response.clone())
            .collect();
        let max_completion_tokens = vector.turns[0].max_completion_tokens;
        let (model, captured, server) = ticket_vector_mock(responses, max_completion_tokens).await;
        let tools = tool_definitions();
        let first = model
            .next(&vector_messages(&vector.turns[0].messages), &tools)
            .await
            .unwrap();
        let first_call = first
            .tool_call
            .expect("first response contains a tool call");
        assert_eq!(first.text, "");
        assert_eq!(first_call.id, "call-read-parser");
        assert_eq!(first_call.name, "repo_read_file");
        assert_eq!(first_call.args, json!({"path": "src/parser.py"}));

        let second = model
            .next(&vector_messages(&vector.turns[1].messages), &tools)
            .await
            .unwrap();
        assert!(second.tool_call.is_none());
        assert_eq!(second.text, "Applied the parser repair.");

        let captured = captured.lock().unwrap();
        assert_eq!(captured.len(), vector.turns.len());
        for (index, request) in captured.iter().enumerate() {
            assert_eq!(
                request,
                &expected_ticket_request(&vector, &vector.turns[index])
            );
            assert_eq!(
                vector_digest(request),
                vector.expected.request_digests[index]
            );
        }
        server.abort();
    }

    fn visit_rust_sources(path: &Path, inspect: &mut impl FnMut(&Path, &str)) {
        for entry in fs::read_dir(path).expect("Rust source directory is readable") {
            let entry = entry.expect("Rust source entry is readable");
            let path = entry.path();
            if path.is_dir() {
                visit_rust_sources(&path, inspect);
            } else if path.extension().and_then(|value| value.to_str()) == Some("rs") {
                let body = fs::read_to_string(&path).expect("Rust source is UTF-8");
                inspect(&path, &body);
            }
        }
    }

    #[test]
    fn rust_source_never_includes_validator_only_inference_policy_vector() {
        let validator_filename = ["coding_inference_", "policy_v1.json"].concat();
        let locked_filename = ["coding_inference_", "policy_locked_v1.json"].concat();
        let validator_schema = ["dittobench-coding-inference-", "policy-vector-v1"].concat();
        let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
        visit_rust_sources(&source, &mut |path, body| {
            assert!(
                !body.contains(&validator_filename),
                "{} references the validator-only policy filename",
                path.display()
            );
            assert!(
                !body.contains(&locked_filename),
                "{} references the validator-only locked policy filename",
                path.display()
            );
            assert!(
                !body.contains(&validator_schema),
                "{} references the validator-only policy schema",
                path.display()
            );
        });

        let vector: Value = serde_json::from_str(MINER_INFERENCE_VECTOR).unwrap();
        let keys: Vec<&str> = vector
            .as_object()
            .expect("miner vector root is an object")
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            keys,
            vec![
                "coding_contract_version",
                "expected",
                "schema",
                "system_prompt",
                "tool_schema",
                "turns",
                "weight_eligible"
            ]
        );
    }

    fn response(model: &str, usage: Option<Value>) -> Value {
        let mut value = json!({
            "id": "gen-azure-123",
            "model": model,
            "provider": "Azure",
            "choices": [{"message": {"content": "done", "tool_calls": []}}]
        });
        if let Some(usage) = usage {
            value["usage"] = usage;
        }
        value
    }

    #[test]
    fn direct_openrouter_is_explicit_and_has_strict_routing() {
        assert!(LunaChatModel::direct_openrouter("secret".to_string(), false, 100).is_err());
        let model = LunaChatModel::direct_openrouter("secret".to_string(), true, 100).unwrap();
        let body = model
            .request_body(
                &[ChatMessage {
                    role: "user".to_string(),
                    content: vec![Content::text("fix it")],
                    ..ChatMessage::default()
                }],
                &[],
            )
            .unwrap();
        assert_eq!(body["model"], LUNA_MODEL);
        assert_eq!(body["reasoning"]["effort"], "medium");
        assert!(body.get("parallel_tool_calls").is_none());
        assert_eq!(
            body["provider"]["order"],
            json!([OPENROUTER_PROVIDER_ROUTE])
        );
        assert_eq!(body["provider"]["allow_fallbacks"], false);
        assert_eq!(body["provider"]["require_parameters"], true);
        assert_eq!(body["provider"]["data_collection"], "deny");
        assert_eq!(body["provider"]["zdr"], true);
        assert!(!body.to_string().contains("secret"));
        assert!(!format!("{model:?}").contains("secret"));
    }

    #[test]
    fn broker_does_not_forward_openrouter_routing_policy() {
        let model = LunaChatModel::ticket_broker("http://127.0.0.1:1234/v1", 100).unwrap();
        let body = model
            .request_body(
                &[ChatMessage {
                    role: "user".to_string(),
                    content: vec![Content::text("fix it")],
                    ..ChatMessage::default()
                }],
                &[],
            )
            .unwrap();
        assert!(body.get("provider").is_none());
        assert_eq!(body["parallel_tool_calls"], false);
    }

    #[tokio::test]
    async fn direct_response_records_validated_generation_provider_usage_and_cost() {
        let (model, request, requests, server) = direct_mock(response(
            LUNA_MODEL,
            Some(json!({
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
                "cost": 0.000_042
            })),
        ))
        .await;
        let chunk = model
            .next(
                &[ChatMessage {
                    role: "user".to_string(),
                    content: vec![Content::text("fix")],
                    ..ChatMessage::default()
                }],
                &[],
            )
            .await
            .unwrap();
        let cost = chunk.cost.unwrap();
        assert_eq!(cost.usage.model, LUNA_MODEL);
        assert_eq!(cost.usage.provider, "openrouter:Azure");
        assert_eq!(cost.usage.input_tokens, 11);
        assert_eq!(cost.usage.output_tokens, 7);
        assert_eq!(cost.usage.total_tokens, 18);
        assert_eq!(cost.cost.currency, "USD");
        assert!((cost.cost.amount - 0.000_042).abs() < f64::EPSILON);
        let metadata = chunk.metadata.unwrap();
        assert_eq!(metadata["generation_id"], "gen-azure-123");
        assert_eq!(metadata["requested_model"], LUNA_MODEL);
        assert_eq!(metadata["effective_model"], LUNA_MODEL);
        assert_eq!(metadata["model_identity_source"], "response");
        assert_eq!(metadata["response_provider"], "Azure");
        assert_eq!(metadata["transport"], "direct_openrouter");
        assert_eq!(metadata["reported_cost_usd"], 0.000_042);
        let request = request.lock().unwrap().clone().unwrap();
        assert_eq!(request["model"], LUNA_MODEL);
        assert_eq!(request["provider"]["order"], json!(["azure/eu"]));
        assert_eq!(requests.load(Ordering::SeqCst), 1);
        server.abort();
    }

    #[tokio::test]
    async fn direct_response_uses_pinned_request_when_model_field_is_omitted() {
        let mut payload = response(
            LUNA_MODEL,
            Some(json!({
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
                "cost": 0.000_001
            })),
        );
        payload.as_object_mut().unwrap().remove("model");
        let (model, _, _, server) = direct_mock(payload).await;
        let chunk = model
            .next(
                &[ChatMessage {
                    role: "user".to_string(),
                    content: vec![Content::text("fix")],
                    ..ChatMessage::default()
                }],
                &[],
            )
            .await
            .unwrap();
        assert_eq!(chunk.cost.unwrap().usage.model, LUNA_MODEL);
        let metadata = chunk.metadata.unwrap();
        assert_eq!(metadata["effective_model"], LUNA_MODEL);
        assert_eq!(metadata["model_identity_source"], "request_binding");
        server.abort();
    }

    fn tool_call(index: usize) -> Value {
        json!({
            "id": format!("call-{index}"),
            "type": "function",
            "function": {
                "name": "repo_read_file",
                "arguments": format!(r#"{{"path":"file-{index}"}}"#)
            }
        })
    }

    #[tokio::test]
    async fn multiple_tool_calls_fail_closed_without_execution() {
        let mut payload = response(
            LUNA_MODEL,
            Some(json!({
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cost": 0.000_03
            })),
        );
        payload["choices"][0]["message"]["tool_calls"] =
            Value::Array((0..2).map(tool_call).collect());
        let (model, _request, requests, server) = direct_mock(payload).await;
        let messages = [ChatMessage {
            role: "user".to_string(),
            content: vec![Content::text("fix")],
            ..ChatMessage::default()
        }];

        let result = model.next(&messages, &[]).await;
        assert!(matches!(result, Err(Error::Model(message)) if message.contains("maximum is 1")));
        assert_eq!(requests.load(Ordering::SeqCst), 1);
        server.abort();
    }

    #[tokio::test]
    async fn successful_http_error_envelope_fails_with_provider_reason() {
        let (model, _, _, server) = direct_mock(json!({
            "error": {"message": "temporarily rate-limited upstream", "code": 429}
        }))
        .await;
        let result = model
            .next(
                &[ChatMessage {
                    role: "user".to_string(),
                    content: vec![Content::text("fix")],
                    ..ChatMessage::default()
                }],
                &[],
            )
            .await;
        assert!(matches!(
            result,
            Err(Error::Model(message)) if message.contains("temporarily rate-limited upstream")
        ));
        server.abort();
    }

    #[test]
    fn direct_response_rejects_model_mismatch_and_missing_usage() {
        let model = LunaChatModel::direct_openrouter("secret".to_string(), true, 100).unwrap();
        let mismatch: ChatCompletionResponse = serde_json::from_value(response(
            "openai/another-model",
            Some(json!({
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost": 0.1
            })),
        ))
        .unwrap();
        assert!(matches!(
            model.validate_response_identity(&mismatch),
            Err(Error::Model(message)) if message.contains("model mismatch")
        ));
        assert!(
            serde_json::from_value::<ChatCompletionResponse>(response(LUNA_MODEL, None)).is_err()
        );
    }

    #[test]
    fn direct_response_rejects_bad_tokens_and_missing_cost() {
        let model = LunaChatModel::direct_openrouter("secret".to_string(), true, 100).unwrap();
        for usage in [
            json!({"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0, "cost": 0.1}),
            json!({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 4, "cost": 0.1}),
            json!({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}),
            json!({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3, "cost": -0.1}),
        ] {
            let parsed: ChatCompletionResponse =
                serde_json::from_value(response(LUNA_MODEL, Some(usage))).unwrap();
            assert!(model.validate_response_identity(&parsed).is_err());
        }
    }

    async fn redirect() -> (StatusCode, [(header::HeaderName, &'static str); 1]) {
        (
            StatusCode::TEMPORARY_REDIRECT,
            [(header::LOCATION, "/followed")],
        )
    }

    async fn followed(State(called): State<Arc<AtomicBool>>) -> Json<Value> {
        called.store(true, Ordering::SeqCst);
        Json(json!({
            "model": LUNA_MODEL,
            "choices": [{"message": {"content": "followed", "tool_calls": []}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        }))
    }

    #[tokio::test]
    async fn inference_client_refuses_redirects() {
        let called = Arc::new(AtomicBool::new(false));
        let app = Router::new()
            .route("/v1/chat/completions", post(redirect))
            .route("/followed", post(followed))
            .with_state(Arc::clone(&called));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let model = LunaChatModel::ticket_broker(&format!("http://{address}/v1"), 100).unwrap();
        let result = model
            .next(
                &[ChatMessage {
                    role: "user".to_string(),
                    content: vec![Content::text("fix")],
                    ..ChatMessage::default()
                }],
                &[],
            )
            .await;
        assert!(matches!(result, Err(Error::Model(message)) if message.contains("307")));
        assert!(!called.load(Ordering::SeqCst));
        server.abort();
    }
}
