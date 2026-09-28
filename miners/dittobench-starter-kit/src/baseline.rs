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
//! ======================= BENCH V13 HONEST ARCHITECTURE =====================
//! Bench v13 grades the prose and adds relay-observed gates (see `v13.rs`
//! and PROTOCOL.md "Bench v13"). The kit stays inside every gate by
//! construction, and each rule below is the line a rewrite would cross:
//!
//!  * The model's value is served as the model wrote it. The `answer` slot is
//!    only ever a verbatim substring of `final_text` (`v13::answer_slot_from_prose`);
//!    the host never rescales (`/100`), maps a direction word, reformats a
//!    number, or composes a slot. (`slot_not_in_prose`,
//!    `served_text_not_model_emitted`.) The slot is OFF unless
//!    `DITTOBENCH_ANSWER_SLOT` is set (the `--gates` rehearsal sets it): the
//!    wire stays at bench 9, so a default-on slot would change live v12
//!    grading (an authoritative slot has no prose fallback).
//!  * The graded value is never written into a harness-authored span. The
//!    system prompt carries a values-free policy (`v13::HARNESS_POLICY_PROMPT`);
//!    retrieved memory is injected by the harness library as `/seed`-derived
//!    context, which the causal gate exempts. (`answer_in_prompt`.)
//!  * The whole catalog is offered on every turn, including the deciding one.
//!    The documented preloading example (`DITTOBENCH_PRELOAD_TOP_K`) trims by
//!    the PUBLISHED embedding and always retains its top-3, which is the
//!    safe harbor. Restraint is the model's choice: a model-emitted call is
//!    always executed, never swallowed. (`restraint_without_offer`,
//!    `expected_tool_not_offered`, `swallowed_model_call`.)
//!  * Clarifying questions and declines come from the model, name the missing
//!    detail, and cite what memory search found. The stock harness leaves the
//!    optional `abstain` field absent; prose is never converted into a wire flag.
//!  * Runtime-described options (`set_accent_color`, `set_chat_font`) are
//!    solved list-then-act: the model calls `discover_capabilities`, reads the
//!    served inventory, and passes one listed spelling; a near-miss is decided
//!    by the qualifier the user used. The mock's "unknown option" error is fed
//!    back to the model to recover, never patched on the host.
//!
//! `scripts/local-rehearsal.py --gates` replays the public rules against a
//! local run and prints per-case notes before you upload.
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
use crate::v13;

// This is a starter-harness safety boundary, not a benchmark scoring limit.
// Outcome-driven agents may legitimately use more than fifteen tool calls;
// parallel calls can also share one model turn. Miners remain free to tune or
// remove this local turn bound, subject to the validator's case deadline.
const DEFAULT_MAX_AGENT_TURNS: usize = 24;

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
    "save_memory",
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
    ///
    /// The returned `Store` is process-wide and shared across overlapping
    /// `/run`. Turso connections are not re-entrant (`concurrent use
    /// forbidden`); `ditto-harness` `Db` opens one connection per overlapping
    /// op. See `tests/store_concurrency.rs`.
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
            protocol::supports_bench_version(req.bench_version),
            "unsupported benchmark version {}",
            req.bench_version,
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
        //
        // Bench v13 catalog-present gate: the model must be OFFERED the catalog
        // on the deciding turn for restraint or a tool choice to be its own.
        // The default offers everything. `DITTOBENCH_PRELOAD_TOP_K` is the
        // documented semantic-preloading example: it trims by the published
        // embedding and always keeps the safe-harbor top-3 (`v13::preload_catalog`).
        let wire_tools: Vec<protocol::ToolDefWire> = req
            .tools
            .iter()
            .filter(|d| {
                !(self.include_memory_tools && MEMORY_TOOL_NAMES.contains(&d.name.as_str()))
            })
            .cloned()
            .collect();
        let offered =
            v13::preload_catalog(&req.user_input, &wire_tools, v13::preload_top_k_from_env());
        let mut tools_offered: Vec<String> = offered.iter().map(|d| d.name.clone()).collect();
        if self.include_memory_tools {
            tools_offered.extend(MEMORY_TOOL_NAMES.iter().map(|name| name.to_string()));
        }
        let host_tools: Vec<Arc<dyn Tool>> = offered
            .iter()
            .map(|d| Arc::new(WireTool::from_wire(d, exec_ctx.clone())) as Arc<dyn Tool>)
            .collect();

        // The system prompt the model runs on: the wire prompt first, then the
        // values-free v13 answering policy (answer in the requested unit, ask
        // by naming the missing detail, list-then-act, grounded declines), and
        // the `Answer:` line request only when the slot is enabled.
        // EXTENSION POINT: keep it values-free — a graded value written here is
        // a harness-authored span and the v13 causal gate zeroes it.
        let answer_slot = v13::answer_slot_enabled();
        let system_prompt = v13::compose_system_prompt(&req.system_prompt, answer_slot);

        let case_model = match req
            .inference_base_url
            .as_ref()
            .filter(|url| !url.trim().is_empty())
        {
            Some(base_url) => Self::build_model(&ModelProvider::Platform {
                base_url: base_url.clone(),
                model: self.model_name.clone(),
            })?,
            None => Arc::clone(&self.model),
        };
        let harness = Harness::new(Options {
            model: case_model,
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
                        system_prompt,
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

        // Local gate diagnostics: when the rehearsal asks for it, record what
        // the model was offered and what it emitted so `--gates` can replay the
        // v13 rules. Never set on-chain; the validator's relay holds its own
        // record.
        if let Ok(path) = std::env::var(v13::COMPLETION_LOG_ENV) {
            if !path.trim().is_empty() {
                let entry = v13::completion_log_entry(
                    &req,
                    &user_id,
                    tools_offered,
                    &result.result.messages,
                );
                if let Err(err) = v13::append_completion_log(std::path::Path::new(&path), &entry) {
                    eprintln!("completion log append failed for {}: {err}", req.case_id);
                }
            }
        }

        let final_text = result.result.text;
        Ok(protocol::RunResponse {
            // An optional scorer field may be set only by the model itself.
            // A decline in final_text is not authority to synthesize abstain.
            abstain: None,
            // The slot is the model's own trailing `Answer:` line, copied
            // verbatim, or absent; absent always while `DITTOBENCH_ANSWER_SLOT`
            // is unset (see `v13::ANSWER_SLOT_ENV`). Bench v13 grades the prose
            // and uses the slot as a tie-break; a slot the prose does not carry,
            // or one the model never emitted (a `/100` rescale, a direction map,
            // a reformatted number), is what `slot_not_in_prose` /
            // `served_text_not_model_emitted` charge. EXTENSION POINT: keep any
            // extractor a verbatim copy.
            answer: v13::answer_slot(&final_text, answer_slot),
            final_text,
            tool_calls,
            prompt_tokens,
            output_tokens,
            latency_ms,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reserves_every_real_memory_tool_name() {
        assert_eq!(
            MEMORY_TOOL_NAMES,
            &[
                "save_memory",
                "search_memories",
                "fetch_memories",
                "search_subjects",
                "search_memories_in_subjects",
            ]
        );
    }

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
}
