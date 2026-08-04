//! The BASELINE HARNESS — this is what miners optimize.
//!
//! It wires together the four pieces of a Ditto agent:
//!   1. a local Turso `Store` (embedded SQLite-family DB with native vectors),
//!   2. an `Embedder` (Ollama `embeddinggemma` by default, 768 dims),
//!   3. a chat `Model` (OpenRouter or local Ollama/vLLM),
//!   4. a `chat::Harness` that prepares memory context, exposes memory tools,
//!      runs the agent loop, and (optionally) saves the turn.
//!
//! `run()` translates a wire `protocol::RunRequest` into a harness run and maps
//! the `RunResult` back to a `protocol::RunResponse`.
//!
//! ============================ EXTENSION POINTS ============================
//! Miners improve their score by editing THIS file. On-chain scoring locks the
//! model to `openai/gpt-oss-20b` through the platform inference relay and
//! FORCES it, so the model is not a tuning lever on-chain. The
//! real levers are retrieval quality, memory grounding, and tool-selection /
//! argument accuracy:
//!
//!  * RETRIEVAL / MEMORY — `PrepareRequest` fields `use_composite`,
//!    `long_term_limit`, `short_term_limit`, `candidate_pool_size`, `variant`.
//!    Better recall = better memory-case answers. You can also plug a learned
//!    `WeightPredictor` into `StoreOptions::predictor`.
//!
//!  * TOOLS — `Options::tools`. The baseline ships memory tools only
//!    (`include_memory_tools: true`). Add host `Tool` implementations to give
//!    the agent real capabilities (web search, image gen, ...). Note: the
//!    validator scores tool *selection*, so even stub tools that record intent
//!    are fine for tool-calling cases.
//!
//!  * SYSTEM PROMPT — `PrepareRequest::system_prompt` in `run()`. The wire
//!    request supplies one, but you can prepend/augment it (tool-use policy,
//!    abstention rules, formatting) to nudge correct tool selection.
//!
//!  * MODEL CHOICE — `Baseline::build_model`. Only affects LOCAL practice: swap
//!    the model id, point at a local Ollama model (free, private), or a vLLM
//!    endpoint. On-chain the validator overrides this with the locked
//!    `openai/gpt-oss-20b`, so it is not a scored lever; use it to rehearse against
//!    the reference weights locally.
//!
//! =========================================================================

use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::Arc;
use std::time::Instant;

use anyhow::Context;
use async_trait::async_trait;
use ditto_harness::agent::NoopHandler;
use ditto_harness::chat::{Harness, Options, PrepareRequest, RunRequest as ChatRunRequest};
use ditto_harness::db::Db;
use ditto_harness::memory::{CompositeSearchRequest, SaveMemoryRequest, Store, StoreOptions};
use ditto_harness::models::{
    ChatModelConfig, ModelParams, OllamaEmbedder, DEFAULT_OLLAMA_BASE_URL,
};
use ditto_harness::retrieval::{MlpPredictor, Reranker, Variant, WeightPredictor};
use ditto_harness::types::{
    ChatMessage, Content, Embedder, Model, Result as HarnessResult, Tool, ToolDefinition,
};
use serde_json::{json, Value};

use crate::protocol;

// This is a starter-harness safety boundary, not a benchmark scoring limit.
// Outcome-driven agents may legitimately use more than fifteen tool calls;
// parallel calls can also share one model turn. Miners remain free to tune or
// remove this local turn bound, subject to the validator's case deadline.
const DEFAULT_MAX_AGENT_TURNS: usize = 24;

/// Deterministically classify high-confidence grounded declines from model prose.
///
/// The wire-level `abstain` field is the canonical signal. This deliberately
/// lives in the starter harness rather than the frozen validator grader. The
/// grammar covers common possession, knowledge, recall, retrieval, disclosure,
/// and record-absence constructions while rejecting responses that recover an
/// answer later in the same message.
fn inferred_abstain(final_text: &str) -> Option<bool> {
    const GROUNDED_DECLINES: &[&str] = &[
        // First-person possession / knowledge absence.
        "i don't have",
        "i do not have",
        "i haven't got",
        "i have not got",
        "i have no record",
        "i have no records",
        "i have no information",
        "i have no memory of",
        "i have no memory for",
        "i have no knowledge of",
        "i have no knowledge about",
        "i lack information about",
        "i lack information on",
        "i lack any record",
        "i have nothing on file",
        "i have nothing recorded",
        "i don't have enough information",
        "i do not have enough information",
        "i don't have enough context",
        "i do not have enough context",
        "i have no recollection of",
        "i don't know",
        "i do not know",
        // Recall / memory failure.
        "i don't recall",
        "i do not recall",
        "i can't recall",
        "i cannot recall",
        "i couldn't recall",
        "i could not recall",
        "i don't remember",
        "i do not remember",
        "i can't remember",
        "i cannot remember",
        "i couldn't remember",
        "i could not remember",
        // Retrieval failure.
        "i can't find",
        "i cannot find",
        "i couldn't find",
        "i could not find",
        "i was unable to find",
        "i'm unable to find",
        "i am unable to find",
        "i can't locate",
        "i cannot locate",
        "i couldn't locate",
        "i could not locate",
        "i can't retrieve",
        "i cannot retrieve",
        "i couldn't retrieve",
        "i could not retrieve",
        "i don't see any mention",
        "i do not see any mention",
        "i don't see any record",
        "i do not see any record",
        "i don't see any information",
        "i do not see any information",
        "i can't determine from",
        "i cannot determine from",
        "i couldn't determine from",
        "i could not determine from",
        "i'm unable to determine from",
        "i am unable to determine from",
        "i can't verify from",
        "i cannot verify from",
        "i see no mention",
        "i see no record",
        "i see no information",
        "i found no mention",
        "i found no record",
        "i found no information",
        "i can't answer based on",
        "i cannot answer based on",
        "i'm unable to answer based on",
        "i am unable to answer based on",
        // Explicit awareness absence, scoped to conversation evidence.
        "i'm not aware of any mention",
        "i am not aware of any mention",
        "i'm not aware of any record",
        "i am not aware of any record",
        "i'm not aware of any information",
        "i am not aware of any information",
        "i wasn't aware of any mention",
        "i was not aware of any mention",
        "i'm unaware of any mention",
        "i am unaware of any mention",
        "i'm unaware of any record",
        "i am unaware of any record",
        "i haven't been told",
        "i have not been told",
        "i wasn't told",
        "i was not told",
        "i wasn't given",
        "i was not given",
        // The user never disclosed the fact.
        "you haven't told",
        "you have not told",
        "you never told",
        "you didn't tell",
        "you did not tell",
        "you haven't shared",
        "you have not shared",
        "you never shared",
        "you didn't share",
        "you did not share",
        "you haven't mentioned",
        "you have not mentioned",
        "you never mentioned",
        "you didn't mention",
        "you did not mention",
        "you haven't provided",
        "you have not provided",
        "you never provided",
        "you didn't provide",
        "you did not provide",
        "you haven't given",
        "you have not given",
        "you never gave",
        "you didn't give",
        "you did not give",
        "you haven't stated",
        "you have not stated",
        "you never stated",
        "you didn't state",
        "you did not state",
        "you haven't specified",
        "you have not specified",
        "you never specified",
        "you didn't specify",
        "you did not specify",
        "you haven't indicated",
        "you have not indicated",
        "you never indicated",
        "you didn't indicate",
        "you did not indicate",
        "you haven't disclosed",
        "you have not disclosed",
        "you never disclosed",
        "you didn't disclose",
        "you did not disclose",
        "you haven't said",
        "you have not said",
        "you never said",
        "you didn't say",
        "you did not say",
        "we haven't discussed",
        "we have not discussed",
        "we never discussed",
        // Impersonal record / conversation absence.
        "there's no record",
        "there is no record",
        "there was no record",
        "there are no records",
        "there were no records",
        "there's no information",
        "there is no information",
        "there was no information",
        "there isn't enough information",
        "there is not enough information",
        "there wasn't enough information",
        "there was not enough information",
        "insufficient information in",
        "no record of",
        "no information about",
        "no information on",
        "no such information was provided",
        "no such information was shared",
        "no such information was mentioned",
        "no such detail was provided",
        "no such detail was shared",
        "no such detail was mentioned",
        "nothing in my memory",
        "nothing in our conversation",
        "nothing in the conversation",
        "nothing in our chat",
        "nothing in the chat",
        "not in my memory",
        "not in my records",
        "not in our conversation",
        "not in the conversation",
        "not in our chat",
        "not in the chat",
        "not in the conversation history",
        "not in our conversation history",
        "not on record",
        "that wasn't mentioned",
        "that was not mentioned",
        "that wasn't provided",
        "that was not provided",
        "that wasn't stated",
        "that was not stated",
        "it wasn't mentioned",
        "it was not mentioned",
        "it wasn't provided",
        "it was not provided",
        "it wasn't stated",
        "it was not stated",
        "our conversation doesn't contain",
        "our conversation does not contain",
        "the conversation doesn't contain",
        "the conversation does not contain",
        "our chat doesn't contain",
        "our chat does not contain",
        "the chat doesn't contain",
        "the chat does not contain",
        "the history doesn't include",
        "the history does not include",
        "this hasn't come up",
        "this has not come up",
        "that hasn't come up",
        "that has not come up",
        "it hasn't been mentioned",
        "it has not been mentioned",
        "that hasn't been mentioned",
        "that has not been mentioned",
        "that hasn't been discussed",
        "that has not been discussed",
    ];
    const ANSWER_RECOVERY: &[&str] = &[
        "but",
        "however",
        "actually",
        "yet",
        "nevertheless",
        "nonetheless",
        "although",
        "though",
        "except",
        "turns out",
        "i found",
        "i located",
        "i retrieved",
        "i remember now",
        "now i remember",
        "i do remember",
        "i can confirm",
        "i can tell you",
        "the answer is",
        "the value is",
        "value is",
        "answer is",
        "stored value is",
        "record shows",
        "record says",
        "history shows",
    ];
    const NON_GROUNDED_REFUSAL: &[&str] = &[
        "permission",
        "authorization",
        "authority",
        "access",
        "ability",
        "capability",
        "to disclose",
        "to delete",
        "to forget",
        "to remove",
        "to save",
        "to store",
    ];

    let normalized = normalize_decline_text(final_text);
    let padded = format!(" {normalized} ");
    let matched = GROUNDED_DECLINES
        .iter()
        .filter_map(|phrase| {
            let needle = format!(" {phrase} ");
            padded.find(&needle).map(|start| (start, needle.len()))
        })
        .min_by_key(|(start, _)| *start);
    let (start, length) = matched?;
    let tail = &padded[start + length..];
    let non_grounded_refusal = NON_GROUNDED_REFUSAL
        .iter()
        .any(|phrase| contains_decline_phrase(tail, phrase));
    let recovered = ANSWER_RECOVERY
        .iter()
        .any(|phrase| contains_decline_phrase(tail, phrase))
        || contains_subject_copula(tail, "your")
        || contains_subject_copula(tail, "it")
        || contains_decline_phrase(tail, "i have");

    (!non_grounded_refusal && !recovered).then_some(true)
}

fn normalize_decline_text(text: &str) -> String {
    let mut normalized = String::with_capacity(text.len());
    for character in text.chars() {
        match character {
            '\u{2018}' | '\u{2019}' | '\u{02bc}' => normalized.push('\''),
            c if c.is_alphanumeric() || c == '\'' => {
                normalized.extend(c.to_lowercase());
            }
            _ => normalized.push(' '),
        }
    }
    normalized.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn contains_decline_phrase(text: &str, phrase: &str) -> bool {
    let padded = format!(" {text} ");
    padded.contains(&format!(" {phrase} "))
}

fn contains_subject_copula(text: &str, subject: &str) -> bool {
    let words = text.split_whitespace().collect::<Vec<_>>();
    words.iter().enumerate().any(|(index, word)| {
        *word == subject
            && (index == 0 || !matches!(words[index - 1], "what" | "which" | "whether"))
            && words[index + 1..words.len().min(index + 7)]
                .iter()
                .any(|candidate| matches!(*candidate, "is" | "was" | "are" | "were"))
    })
}

/// Shared per-case context for executing catalog tools through the validator's
/// mock tool endpoint (observed execution). One is built per `/run` when
/// the validator advertises `tool_endpoint`, and Arc-cloned into every
/// [`WireTool`] of that case so they share one HTTP client and a monotonic `hop`
/// counter (the trajectory order the validator observes).
struct ToolExecCtx {
    client: reqwest::Client,
    endpoint: String,
    case_id: String,
    user_id: String,
    hop: AtomicI32,
}

const MAX_OBSERVED_TOOL_ATTEMPTS: usize = 2;

fn is_retryable_tool_error(error: &str) -> bool {
    let error = error.to_ascii_lowercase();
    error.contains("retry")
        || error.contains("transient")
        || error.contains("temporary")
        || error.contains("429")
        || error.contains("502")
        || error.contains("503")
        || error.contains("504")
}

fn is_retryable_tool_status(status: reqwest::StatusCode) -> bool {
    status == reqwest::StatusCode::TOO_MANY_REQUESTS
        || status == reqwest::StatusCode::BAD_GATEWAY
        || status == reqwest::StatusCode::SERVICE_UNAVAILABLE
        || status == reqwest::StatusCode::GATEWAY_TIMEOUT
}

/// A catalog tool built from a wire tool definition. It exposes the case's
/// catalog tool to the model — so the agent can *select* it, which is what the
/// validator scores. When a [`ToolExecCtx`] is attached (observed execution), `execute()`
/// runs the tool for real by POSTing to the validator's mock endpoint and
/// returning the served result, so (a) the validator observes the true
/// trajectory and (b) the model can incorporate the returned content
/// (result-usage). Without one it returns a benign placeholder so multi-turn
/// cases can still proceed.
struct WireTool {
    def: ToolDefinition,
    exec: Option<Arc<ToolExecCtx>>,
}

impl WireTool {
    fn from_wire(d: &protocol::ToolDefWire, exec: Option<Arc<ToolExecCtx>>) -> WireTool {
        WireTool {
            def: ToolDefinition {
                name: d.name.clone(),
                description: d.description.clone(),
                input_schema: d.parameters.clone(),
            },
            exec,
        }
    }
}

#[async_trait]
impl Tool for WireTool {
    fn definition(&self) -> ToolDefinition {
        self.def.clone()
    }

    async fn execute(&self, args: Value) -> HarnessResult<Value> {
        // Observed execution: execute for real through the validator's mock endpoint.
        if let Some(ctx) = &self.exec {
            for attempt in 0..MAX_OBSERVED_TOOL_ATTEMPTS {
                let hop = ctx.hop.fetch_add(1, Ordering::SeqCst);
                let body = protocol::ToolExecRequest {
                    case_id: ctx.case_id.clone(),
                    user_id: ctx.user_id.clone(),
                    name: self.def.name.clone(),
                    args: args.clone(),
                    hop,
                };
                match ctx.client.post(&ctx.endpoint).json(&body).send().await {
                    Ok(resp) => {
                        let status = resp.status();
                        if !status.is_success() {
                            let response_body = resp.text().await.unwrap_or_default();
                            if attempt + 1 < MAX_OBSERVED_TOOL_ATTEMPTS
                                && is_retryable_tool_status(status)
                            {
                                continue;
                            }
                            return Ok(json!({
                                "error": format!(
                                    "tool endpoint returned {status}: {response_body}"
                                )
                            }));
                        }
                        match resp.json::<protocol::ToolExecResponse>().await {
                            Ok(r) if !r.result.is_empty() => {
                                return Ok(json!({ "result": r.result }));
                            }
                            Ok(r) if !r.error.is_empty() => {
                                if attempt + 1 < MAX_OBSERVED_TOOL_ATTEMPTS
                                    && is_retryable_tool_error(&r.error)
                                {
                                    continue;
                                }
                                return Ok(json!({ "error": r.error }));
                            }
                            Ok(_) => {
                                return Ok(json!({
                                    "error": format!(
                                        "tool endpoint returned an empty result for {}",
                                        self.def.name
                                    )
                                }));
                            }
                            Err(err) => {
                                return Ok(
                                    json!({ "error": format!("decode tool result: {err}") }),
                                );
                            }
                        }
                    }
                    Err(err) => {
                        return Ok(json!({ "error": format!("tool endpoint unreachable: {err}") }));
                    }
                }
            }
            return Ok(json!({ "error": "tool endpoint retry budget exhausted" }));
        }
        Ok(json!({
            "status": "ok",
            "note": "stub result from the practice harness; provide tool_endpoint (observed execution) or a real Tool to execute",
        }))
    }
}

#[cfg(test)]
mod tool_exec_tests {
    use super::*;
    use axum::extract::State;
    use axum::http::StatusCode;
    use axum::routing::post;
    use axum::{Json, Router};
    use std::sync::Mutex;

    async fn transient_json_then_success(
        State(calls): State<Arc<Mutex<Vec<protocol::ToolExecRequest>>>>,
        Json(call): Json<protocol::ToolExecRequest>,
    ) -> Json<protocol::ToolExecResponse> {
        let attempt = {
            let mut calls = calls.lock().expect("lock calls");
            calls.push(call);
            calls.len()
        };
        if attempt == 1 {
            return Json(protocol::ToolExecResponse {
                error: "transient upstream error (503); retry".to_string(),
                ..Default::default()
            });
        }
        Json(protocol::ToolExecResponse {
            result: "Top result: the Veltrix index reached 4,218 points.".to_string(),
            ..Default::default()
        })
    }

    async fn transient_status_then_success(
        State(calls): State<Arc<Mutex<Vec<protocol::ToolExecRequest>>>>,
        Json(call): Json<protocol::ToolExecRequest>,
    ) -> (StatusCode, Json<protocol::ToolExecResponse>) {
        let attempt = {
            let mut calls = calls.lock().expect("lock calls");
            calls.push(call);
            calls.len()
        };
        if attempt == 1 {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(protocol::ToolExecResponse::default()),
            );
        }
        (
            StatusCode::OK,
            Json(protocol::ToolExecResponse {
                result: "Top result: the Veltrix index reached 4,218 points.".to_string(),
                ..Default::default()
            }),
        )
    }

    async fn serve(app: Router) -> (String, tokio::task::JoinHandle<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind test server");
        let address = listener.local_addr().expect("test address");
        let task = tokio::spawn(async move {
            axum::serve(listener, app).await.expect("serve test app");
        });
        (format!("http://{address}/tool"), task)
    }

    fn exec_context(endpoint: String) -> Arc<ToolExecCtx> {
        Arc::new(ToolExecCtx {
            client: reqwest::Client::new(),
            endpoint,
            case_id: "case-123".to_string(),
            user_id: "scored-user".to_string(),
            hop: AtomicI32::new(0),
        })
    }

    fn wire_tool(exec: Arc<ToolExecCtx>) -> WireTool {
        WireTool {
            def: ToolDefinition {
                name: "search_web".to_string(),
                description: String::new(),
                input_schema: json!({"type": "object"}),
            },
            exec: Some(exec),
        }
    }

    async fn assert_transient_recovery(
        app: Router,
        calls: Arc<Mutex<Vec<protocol::ToolExecRequest>>>,
    ) {
        let (endpoint, task) = serve(app).await;
        let result = wire_tool(exec_context(endpoint))
            .execute(json!({"queries": ["Veltrix index"]}))
            .await
            .expect("execute tool");
        task.abort();

        assert_eq!(
            result["result"],
            "Top result: the Veltrix index reached 4,218 points."
        );
        let calls = calls.lock().expect("lock calls");
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].hop, 0);
        assert_eq!(calls[1].hop, 1);
        assert_eq!(calls[0].args, calls[1].args);
    }

    #[tokio::test]
    async fn retries_a_transient_tool_error_once() {
        let calls = Arc::new(Mutex::new(Vec::new()));
        let app = Router::new()
            .route("/tool", post(transient_json_then_success))
            .with_state(Arc::clone(&calls));
        assert_transient_recovery(app, calls).await;
    }

    #[tokio::test]
    async fn retries_a_transient_http_status_once() {
        let calls = Arc::new(Mutex::new(Vec::new()));
        let app = Router::new()
            .route("/tool", post(transient_status_then_success))
            .with_state(Arc::clone(&calls));
        assert_transient_recovery(app, calls).await;
    }
}

/// Default local DB path (overridable via `DITTOBENCH_DB`).
pub const DEFAULT_DB_PATH: &str = "./dittobench.db";
/// The benchmark v8 scored model and local OpenRouter default.
pub const DEFAULT_OPENROUTER_MODEL: &str = "openai/gpt-oss-20b";
/// Ollama's canonical local chat model tag.
pub const DEFAULT_OLLAMA_CHAT_MODEL: &str = "gpt-oss:20b";
/// Ollama's canonical 768-dimensional embedder tag.
pub const DEFAULT_OLLAMA_EMBED_MODEL: &str = "embeddinggemma";
/// Fixed user id for the single-tenant miner DB.
pub const USER_ID: &str = "miner";

/// Catalog tools the harness already serves as REAL memory tools when
/// `include_memory_tools` is true. We must NOT also register stub copies, or
/// the model sees a duplicate function declaration (strict providers like
/// Gemini reject that with a 400). The real tools represent these names.
pub const MEMORY_TOOL_NAMES: &[&str] = &[
    "search_memories",
    "fetch_memories",
    "search_subjects",
    "search_memories_in_subjects",
];

/// How the chat model is provisioned.
#[derive(Debug, Clone)]
pub enum ModelProvider {
    /// OpenRouter; reads `OPENROUTER_API_KEY` from the environment.
    OpenRouter { model: String },
    /// Ticket-scoped platform relay used by canonical benchmark v8 runs.
    Platform { base_url: String, model: String },
    /// Local Ollama server.
    Ollama { base_url: String, model: String },
}

impl ModelProvider {
    /// The configured chat model id (whichever provider serves it).
    pub fn model_id(&self) -> &str {
        match self {
            ModelProvider::OpenRouter { model } => model,
            ModelProvider::Platform { model, .. } => model,
            ModelProvider::Ollama { model, .. } => model,
        }
    }

    fn from_provider_with(provider: &str, env: impl Fn(&str) -> Option<String>) -> ModelProvider {
        match provider {
            "platform" => ModelProvider::Platform {
                base_url: env("DITTOBENCH_INFERENCE_BASE_URL")
                    .expect("DITTOBENCH_INFERENCE_BASE_URL is required for platform inference"),
                model: env("DITTOBENCH_MODEL")
                    .unwrap_or_else(|| DEFAULT_OPENROUTER_MODEL.to_string()),
            },
            // Canonical validators historically selected this generic
            // OpenAI-compatible adapter name. Keep it as a URL-only alias of
            // the ticket-scoped platform broker so old and current v8 images
            // share one runtime contract. It does not select Chutes or read a
            // provider credential.
            "chutes" => ModelProvider::Platform {
                base_url: env("DITTOBENCH_INFERENCE_BASE_URL")
                    .or_else(|| env("CHUTES_BASE_URL"))
                    .expect("an injected ticket broker URL is required for platform inference"),
                model: env("DITTOBENCH_MODEL")
                    .unwrap_or_else(|| DEFAULT_OPENROUTER_MODEL.to_string()),
            },
            "ollama" => ModelProvider::Ollama {
                base_url: env("OLLAMA_BASE_URL")
                    .unwrap_or_else(|| DEFAULT_OLLAMA_BASE_URL.to_string()),
                model: env("DITTOBENCH_MODEL")
                    .unwrap_or_else(|| DEFAULT_OLLAMA_CHAT_MODEL.to_string()),
            },
            _ => ModelProvider::OpenRouter {
                // EXTENSION POINT: change this default model. It sets only LOCAL
                // practice runs and defaults to the on-chain scored model.
                // Benchmark v8 scoring locks inference to GPT-OSS-20B through
                // the platform relay and overrides whatever a submission sets.
                model: env("DITTOBENCH_MODEL")
                    .unwrap_or_else(|| DEFAULT_OPENROUTER_MODEL.to_string()),
            },
        }
    }

    /// Resolves the provider from environment variables. Defaults to OpenRouter
    /// with a fast tool-capable model; falls back to Ollama if
    /// `DITTOBENCH_PROVIDER=ollama`.
    pub fn from_env() -> ModelProvider {
        let provider = std::env::var("DITTOBENCH_PROVIDER")
            .unwrap_or_else(|_| "openrouter".to_string())
            .to_lowercase();
        Self::from_provider_with(&provider, |name| std::env::var(name).ok())
    }
}

/// The optimizable baseline agent.
///
/// The harness is rebuilt per `run()` so each case's tool catalog (sent on the
/// wire) is exposed to the model; the model and store are shared (cheap `Arc`
/// clones).
pub struct Baseline {
    model: Arc<dyn Model>,
    model_name: String,
    store: Arc<Store>,
    include_memory_tools: bool,
    /// Shared outbound HTTP client (observed-execution tool-endpoint calls). One client
    /// per Baseline so connections are pooled across cases.
    http: reqwest::Client,
}

impl Baseline {
    /// Builds the baseline from environment configuration:
    ///   - `DITTOBENCH_DB` (db path, default `./dittobench.db`)
    ///   - `DITTOBENCH_PROVIDER` (`openrouter` [default] | `ollama`; the
    ///     validator reserves `platform` for ticket-scoped scoring)
    ///   - `DITTOBENCH_MODEL` (model id)
    ///   - `OPENROUTER_API_KEY` (required for OpenRouter)
    ///   - `OLLAMA_BASE_URL` (embedder + ollama chat base url)
    pub async fn from_env() -> anyhow::Result<Baseline> {
        let db_path =
            std::env::var("DITTOBENCH_DB").unwrap_or_else(|_| DEFAULT_DB_PATH.to_string());
        let store = Self::open_store(&db_path).await?;
        let provider = ModelProvider::from_env();
        let model = Self::build_model(&provider)?;
        Ok(Baseline {
            model,
            model_name: provider.model_id().to_string(),
            store,
            include_memory_tools: true,
            http: reqwest::Client::new(),
        })
    }

    /// Opens (creating if needed) the local Turso store with the Ollama
    /// embedder, the production weight-predictor MLP, and the production
    /// cross-encoder reranker — mirroring the production retrieval stack 1:1.
    pub async fn open_store(db_path: &str) -> anyhow::Result<Arc<Store>> {
        let db = Db::open(db_path)
            .await
            .with_context(|| format!("open turso db {db_path}"))?;
        let embedder: Arc<dyn Embedder> = Arc::new(Self::build_embedder());
        Ok(Arc::new(Store::new(StoreOptions {
            db: Arc::new(db),
            embedder,
            predictor: Some(Self::build_predictor()?),
            reranker: Some(Self::build_reranker()?),
        })))
    }

    /// The weight-predictor MLP (production `model.bin`, shipped in the kit).
    /// Predicts the 7 composite fusion weights + scale from the query embedding
    /// + 17 aux features. EXTENSION POINT: retrain and swap the weights.
    pub fn build_predictor() -> anyhow::Result<Arc<dyn WeightPredictor>> {
        const MLP_BYTES: &[u8] = include_bytes!("../fixtures/models/mlp-weights.bin");
        let mlp = MlpPredictor::load_from_reader(MLP_BYTES)
            .map_err(|e| anyhow::anyhow!("load MLP weights: {e}"))?;
        Ok(Arc::new(mlp))
    }

    /// The cross-encoder reranker (production TinyBERT-L2 INT8 `model.onnx` +
    /// BERT vocab, shipped in the kit). Reranks the composite pool via RRF.
    /// EXTENSION POINT: swap the ONNX model / fusion weights.
    pub fn build_reranker() -> anyhow::Result<Arc<dyn Reranker>> {
        const ONNX_BYTES: &[u8] = include_bytes!("../fixtures/models/cross-encoder.onnx");
        const VOCAB_TXT: &str = include_str!("../fixtures/models/cross-encoder-vocab.txt");
        let ce = crate::reranker::CrossEncoderReranker::from_bytes(ONNX_BYTES, VOCAB_TXT)?;
        Ok(Arc::new(ce))
    }

    /// The embedder (Ollama `embeddinggemma`, 768 dims). EXTENSION POINT: swap
    /// for another embedder implementing `ditto_harness::types::Embedder`.
    pub fn build_embedder() -> OllamaEmbedder {
        let base_url = std::env::var("OLLAMA_BASE_URL")
            .unwrap_or_else(|_| DEFAULT_OLLAMA_BASE_URL.to_string());
        OllamaEmbedder::new(base_url)
    }

    /// Builds the chat model. EXTENSION POINT: model selection.
    pub fn build_model(provider: &ModelProvider) -> anyhow::Result<Arc<dyn Model>> {
        let config = match provider {
            ModelProvider::OpenRouter { model } => {
                let api_key = std::env::var("OPENROUTER_API_KEY").context(
                    "OPENROUTER_API_KEY is not set; export it or set DITTOBENCH_PROVIDER=ollama",
                )?;
                ChatModelConfig::openrouter(api_key, model.clone())
            }
            ModelProvider::Platform { base_url, model } => ChatModelConfig::OpenAiCompat {
                base_url: base_url.clone(),
                // The trusted local broker authorizes the sandbox execution
                // boundary, not this non-secret compatibility header value.
                api_key: "ticket".to_string(),
                model: model.clone(),
            },
            ModelProvider::Ollama { base_url, model } => {
                ChatModelConfig::ollama(base_url.clone(), model.clone())
            }
        };
        // Deterministic decoding: a frozen reference model must answer phrasing
        // twins identically (metamorphic gate) and be stable run-to-run. temp 0
        // removes sampling noise and a fixed seed gives run-to-run reproducibility
        // on providers that honor it (OpenRouter and local compatible servers), so
        // the noise floor collapses; `None` max_tokens keeps the provider default.
        config
            .build_with_params(ModelParams {
                temperature: Some(0.0),
                max_tokens: None,
                seed: Some(42),
            })
            .map_err(|err| anyhow::anyhow!("build chat model: {err}"))
    }

    /// Direct access to the underlying store (for seeding memory fixtures).
    pub fn store(&self) -> &Arc<Store> {
        &self.store
    }

    /// Shared handle to the chat model (for the playground to build its own
    /// harness with fake tools).
    pub fn model_arc(&self) -> Arc<dyn Model> {
        Arc::clone(&self.model)
    }

    /// The model id actually configured on this baseline (whatever provider
    /// serves it) — e.g. for filling the `{MODEL}` slot in a system prompt.
    pub fn model_name(&self) -> &str {
        &self.model_name
    }

    /// Retrieves the top-k memories for `query` through the full production
    /// pipeline (MLP weights + composite V2 + cross-encoder rerank) and returns
    /// `(pair_id, preview, composite_score)` for display.
    pub async fn retrieve_previews(
        &self,
        query: &str,
        k: usize,
    ) -> anyhow::Result<Vec<(String, String, f64)>> {
        let (memories, _meta) = self
            .store
            .search_composite_memories(CompositeSearchRequest {
                user_id: USER_ID.to_string(),
                query: query.to_string(),
                limit: k,
                // Match the scored `run()` path (pool 100) so what a miner
                // inspects via retrieve is what scoring actually sees.
                candidate_pool_size: 100,
                variant: Variant::V2,
                ..CompositeSearchRequest::default()
            })
            .await
            .map_err(|err| anyhow::anyhow!("retrieve previews: {err}"))?;
        Ok(memories
            .into_iter()
            .map(|m| {
                let text = match (m.prompt.trim().is_empty(), m.response.trim().is_empty()) {
                    (false, false) => format!("{} → {}", m.prompt.trim(), m.response.trim()),
                    (false, true) => m.prompt.trim().to_string(),
                    (true, false) => m.response.trim().to_string(),
                    (true, true) => String::new(),
                };
                let preview: String = text.chars().take(200).collect();
                (m.id, preview, m.composite_score)
            })
            .collect())
    }

    /// Runs the full production retrieval pipeline for `query` and returns the
    /// retrieved memory pair ids, best-first. Exercises the whole stack —
    /// MLP-predicted composite weights (V2, pool 100) + cross-encoder rerank —
    /// without an LLM call, so it isolates and measures retrieval quality.
    pub async fn retrieve(&self, query: &str, k: usize) -> anyhow::Result<Vec<String>> {
        let (memories, _meta) = self
            .store
            .search_composite_memories(CompositeSearchRequest {
                user_id: USER_ID.to_string(),
                query: query.to_string(),
                limit: k,
                // Match the scored `run()` path (pool 100) so what a miner
                // inspects via retrieve is what scoring actually sees.
                candidate_pool_size: 100,
                variant: Variant::V2,
                ..CompositeSearchRequest::default()
            })
            .await
            .map_err(|err| anyhow::anyhow!("retrieve: {err}"))?;
        Ok(memories.into_iter().map(|m| m.id).collect())
    }

    /// Seeds a memory pair into the store (embeds it). Idempotent when `id` is
    /// stable (the store upserts on `(user_id, firestore_pair_id)`).
    pub async fn seed_memory(
        &self,
        id: &str,
        prompt: &str,
        response: &str,
        days_ago: i64,
    ) -> anyhow::Result<()> {
        let timestamp = chrono::Utc::now() - chrono::Duration::days(days_ago);
        self.store
            .save_memory(SaveMemoryRequest {
                user_id: USER_ID.to_string(),
                id: id.to_string(),
                prompt: prompt.to_string(),
                response: response.to_string(),
                source: "seed".to_string(),
                timestamp: Some(timestamp),
                ..SaveMemoryRequest::default()
            })
            .await
            .map_err(|err| anyhow::anyhow!("seed memory: {err}"))?;
        Ok(())
    }

    /// Runs one wire request through the harness, measuring latency, and maps
    /// the result to a `protocol::RunResponse`.
    ///
    /// Tool calls are observed by scanning the assistant messages in the
    /// agent transcript (the harness records each tool call as an assistant
    /// message with `tool_calls`).
    pub async fn run(&self, req: protocol::RunRequest) -> anyhow::Result<protocol::RunResponse> {
        let started = Instant::now();

        anyhow::ensure!(
            req.bench_version == protocol::ACTIVE_BENCH_VERSION,
            "unsupported benchmark version {}; this harness serves v{} only",
            req.bench_version,
            protocol::ACTIVE_BENCH_VERSION,
        );

        // Observed execution: the case may be scoped to a specific memory graph (multi-graph
        // isolation) — answer from that user's memory, defaulting to the kit user.
        let user_id = req
            .user_id
            .clone()
            .filter(|u| !u.is_empty())
            .unwrap_or_else(|| USER_ID.to_string());

        // Observed execution: when the validator advertises a mock tool endpoint, execute
        // catalog tools through it (so the validator observes the trajectory and
        // the model can use returned content). One shared context per case.
        let exec_ctx = req.tool_endpoint.as_ref().map(|ep| {
            Arc::new(ToolExecCtx {
                client: self.http.clone(),
                endpoint: ep.clone(),
                case_id: req.case_id.clone(),
                user_id: user_id.clone(),
                hop: AtomicI32::new(0),
            })
        });

        // Expose this case's tool catalog to the model so it can SELECT the
        // right tool (what the validator scores). Built per-run because the
        // catalog arrives on the wire. Memory tools are dropped here when the
        // harness serves the real ones (avoids duplicate declarations).
        // EXTENSION POINT: see `WireTool`.
        let host_tools: Vec<Arc<dyn Tool>> = req
            .tools
            .iter()
            .filter(|d| {
                !(self.include_memory_tools && MEMORY_TOOL_NAMES.contains(&d.name.as_str()))
            })
            .map(|d| Arc::new(WireTool::from_wire(d, exec_ctx.clone())) as Arc<dyn Tool>)
            .collect();

        let harness = Harness::new(Options {
            model: Arc::clone(&self.model),
            memory: Some(Arc::clone(&self.store)),
            tools: host_tools,
            include_memory_tools: self.include_memory_tools,
        });

        let result = harness
            .run(
                ChatRunRequest {
                    prepare: PrepareRequest {
                        user_id: user_id.clone(),
                        // user_input drives memory retrieval (the query)...
                        user_input: req.user_input.clone(),
                        system_prompt: req.system_prompt.clone(),
                        // ...and is ALSO passed explicitly as the user turn:
                        // `normalize_messages` only seeds `user_input` as a
                        // message when there is no system prompt, so with a
                        // system prompt set we must supply the turn ourselves.
                        messages: vec![ChatMessage {
                            role: "user".to_string(),
                            content: vec![Content::text(req.user_input.clone())],
                            ..ChatMessage::default()
                        }],
                        // Production retrieval config: composite V2 (7 signals +
                        // scale), MLP-predicted weights + cross-encoder rerank are
                        // wired on the Store. long_term_limit sets how many ranked
                        // memories are injected into context; the default (8) is
                        // too shallow for a large haystack (a specific needle, e.g.
                        // the canary nonce, ranks past 8 among 100+ pairs and never
                        // reaches the model). A deeper pool + more injected context
                        // lifts recall. EXTENSION POINT: retrieval tuning.
                        use_composite: true,
                        variant: Variant::V2,
                        candidate_pool_size: 100,
                        long_term_limit: 24,
                        ..PrepareRequest::default()
                    },
                    // Keep enough room for composed work, retries, and useful
                    // exploration. Scoring is outcome-driven and does not cap
                    // a correct trajectory at fifteen calls.
                    max_turns: DEFAULT_MAX_AGENT_TURNS,
                    save_memory: false,
                    ..ChatRunRequest::default()
                },
                &NoopHandler,
            )
            .await
            .map_err(|err| anyhow::anyhow!("harness run: {err}"))?;

        let latency_ms = started.elapsed().as_millis() as i64;

        // Observe tool calls from the transcript.
        let mut tool_calls = Vec::new();
        let mut hop = 0i32;
        for msg in &result.result.messages {
            for tc in &msg.tool_calls {
                tool_calls.push(protocol::ObservedToolCall {
                    name: tc.name.clone(),
                    args: tc.args.clone(),
                    hop,
                });
                hop += 1;
            }
        }

        // Aggregate token usage from collected costs.
        let mut prompt_tokens = 0i64;
        let mut output_tokens = 0i64;
        for c in &result.result.costs {
            prompt_tokens += c.usage.input_tokens;
            output_tokens += c.usage.output_tokens;
        }

        let final_text = result.result.text;
        Ok(protocol::RunResponse {
            abstain: inferred_abstain(&final_text),
            final_text,
            tool_calls,
            prompt_tokens,
            output_tokens,
            latency_ms,
            // EXTENSION POINT: populate the answer slot with the bare value
            // your final_text asserts (and abstain when the fact is not in
            // memory). The validator grades the slot when present, prose
            // containment otherwise -- an explicit slot removes phrasing risk.
            answer: None,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ollama_provider_defaults_to_gpt_oss() {
        let values = std::collections::HashMap::from([(
            "OLLAMA_BASE_URL",
            "http://ollama.test:11434".to_string(),
        )]);

        let provider =
            ModelProvider::from_provider_with("ollama", |name| values.get(name).cloned());
        match provider {
            ModelProvider::Ollama { base_url, model } => {
                assert_eq!(base_url, "http://ollama.test:11434");
                assert_eq!(model, DEFAULT_OLLAMA_CHAT_MODEL);
            }
            other => panic!("expected local Ollama provider, got {other:?}"),
        }
    }

    #[test]
    fn chutes_selector_is_only_a_platform_broker_alias() {
        let values = std::collections::HashMap::from([
            (
                "CHUTES_BASE_URL",
                "http://host.docker.internal:11436/v1/inference".to_string(),
            ),
            ("CHUTES_API_KEY", "must-not-be-read".to_string()),
            ("DITTOBENCH_MODEL", DEFAULT_OPENROUTER_MODEL.to_string()),
        ]);

        let provider =
            ModelProvider::from_provider_with("chutes", |name| values.get(name).cloned());
        match &provider {
            ModelProvider::Platform { base_url, model } => {
                assert_eq!(base_url, "http://host.docker.internal:11436/v1/inference");
                assert_eq!(model, DEFAULT_OPENROUTER_MODEL);
            }
            other => panic!("expected platform broker alias, got {other:?}"),
        }

        Baseline::build_model(&provider).expect("build injected platform broker model");
    }

    #[test]
    fn grounded_decline_grammar_covers_natural_model_variants() {
        for text in [
            "It seems I couldn't find any information about that.",
            "I could not find that in our previous conversation.",
            "I'm not aware of any mention of that in our conversation.",
            "I don’t have your blood type in memory.",
            "I do not have a record of your blood type.",
            "I haven't got that detail saved.",
            "I have no information about your preferred airport.",
            "I have no memory of you naming a preferred airport.",
            "I have no knowledge about that preference.",
            "I lack information about your preferred airport.",
            "I lack any record of that preference.",
            "I have nothing on file for your blood type.",
            "I have nothing recorded about that preference.",
            "I don't have enough information to answer that.",
            "I do not have enough context to determine that.",
            "I have no recollection of you sharing that preference.",
            "I don't know your blood type.",
            "I do not know which airport you prefer.",
            "I don't recall you sharing that.",
            "I cannot recall that detail.",
            "I couldn’t recall any such preference.",
            "I don't remember your blood type.",
            "I cannot remember you mentioning it.",
            "I could not remember that from our chat.",
            "I can't find that detail in memory.",
            "I was unable to find a saved preference.",
            "I’m unable to find any previous mention.",
            "I can't locate a record for that.",
            "I could not locate it in our conversation.",
            "I cannot retrieve that information.",
            "I couldn't retrieve a saved answer.",
            "I don't see any mention of that preference.",
            "I do not see any record of your blood type.",
            "I cannot determine from our conversation which airport you prefer.",
            "I’m unable to determine from the chat what your blood type is.",
            "I can't verify from my memory that you provided that detail.",
            "I see no mention of a preferred airport.",
            "I found no record of your blood type.",
            "I cannot answer based on our conversation history.",
            "I am not aware of any record of that.",
            "I wasn't aware of any mention of it.",
            "I’m unaware of any record of that preference.",
            "I haven't been told your blood type.",
            "I was not given a preferred airport.",
            "You haven't told me your blood type.",
            "You did not tell me which airport you prefer.",
            "You never shared that preference with me.",
            "You haven't mentioned a blood type.",
            "You did not mention that in our chat.",
            "You never provided that detail.",
            "You haven't given me that information.",
            "You never gave me a preferred airport.",
            "You have not stated that preference.",
            "You haven't specified a preferred airport.",
            "You never indicated your blood type.",
            "You did not disclose that detail.",
            "You haven't said which airport you prefer.",
            "We have not discussed your blood type.",
            "There’s no record of your blood type here.",
            "There are no records containing that preference.",
            "There was no information about that in our conversation.",
            "There isn't enough information in our chat to answer that.",
            "Insufficient information in the conversation to determine that.",
            "No record of that appears in memory.",
            "No information on that preference is available.",
            "No such information was provided in our conversation.",
            "No such detail was mentioned in our chat.",
            "Nothing in my memory identifies your preferred airport.",
            "Nothing in our conversation states your blood type.",
            "That detail is not in my memory.",
            "It is not in our conversation history.",
            "That preference is not on record.",
            "That wasn't mentioned previously.",
            "It was not provided in the chat.",
            "That wasn’t stated anywhere in our conversation.",
            "Our conversation doesn't contain that information.",
            "The chat does not contain a preferred airport.",
            "The history doesn't include your blood type.",
            "This hasn't come up in our conversation.",
            "That has not come up in our chat.",
            "It hasn't been mentioned in the conversation.",
            "That has not been discussed before.",
            "  I   COULD   NOT   FIND   that detail.  ",
        ] {
            assert_eq!(inferred_abstain(text), Some(true), "{text}");
        }
    }

    #[test]
    fn grounded_decline_grammar_rejects_answers_and_recoveries() {
        for text in [
            "Your value is Lisbon.",
            "I found your blood type: AB negative.",
            "The record shows your preferred airport is DCA.",
            "I am not aware of any issue with your saved preference.",
            "I can't share private information.",
            "I don't have permission to disclose your blood type.",
            "You never told me to delete Lisbon from memory.",
            "I couldn't find it at first, but your value is Lisbon.",
            "I don't remember why; however, your value is Lisbon.",
            "I had no record initially. Actually, the answer is Lisbon.",
            "I could not locate it, yet I found the value: Lisbon.",
            "I don't know why, though your blood type is AB negative.",
            "I couldn't retrieve it. Turns out the value is Lisbon.",
            "I didn't remember before; now I remember: Lisbon.",
            "I had no information at first; I can confirm it is Lisbon.",
            "I don't have Lisbon; I have Porto.",
            "I couldn't find a mismatch. The record says Lisbon.",
            "I don't see any issue. The stored value is Lisbon.",
            "The conversation doesn't contain an error; the answer is Lisbon.",
            "No record of deletion exists; your value is Lisbon.",
            "You never specified that Lisbon was wrong; your value is Lisbon.",
            "I don't recall an error. Your preferred airport is DCA.",
            "I couldn't find it earlier. It is Lisbon.",
        ] {
            assert_eq!(inferred_abstain(text), None, "{text}");
        }
    }

    #[test]
    fn v8_emits_typed_abstention() {
        for text in [
            "I couldn't find any information about that.",
            "I'm not aware of any mention of that in our conversation.",
        ] {
            assert_eq!(inferred_abstain(text), Some(true), "v8: {text}");
        }
    }
}
