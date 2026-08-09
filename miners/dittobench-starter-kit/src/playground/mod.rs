//! Interactive playground — talk to a **production-faithful Ditto agent** over
//! the seeded dummy user, with **fake-but-plausible tool responses** so you can
//! exercise tool calling without real integrations.
//!
//! It serves a single-file web UI (`playground.html`) backed by:
//!   * `GET  /api/health`  — liveness
//!   * `GET  /api/tools`   — the full tool catalog (names, descriptions, schemas)
//!   * `POST /api/chat`    — one multi-turn chat turn
//!
//! Fidelity to prod Ditto (v2 chat): the production system prompt + persona +
//! tool-use policy ([`prompt`]), the real tool catalog, REAL memory tools +
//! composite-V2 retrieval + cross-encoder rerank over the seed user, and the
//! agentic tool loop. The chat model is whatever the baseline is configured
//! with (`DITTOBENCH_MODEL`; set it to [`PROD_DEFAULT_MODEL`] to mirror prod).
//! Action tools (search_web, create_image, agent jobs, settings, ...) return
//! canned results ([`tools`]) so the loop runs end to end.
//!
//! Modules: [`tools`] (fake action tools), [`prompt`] (the prod system prompt),
//! [`submit`] (the hosted-validator submit proxy); the HTTP server and
//! the live local scoring glue live here.

mod prompt;
mod submit;
mod tools;

pub use prompt::PROD_DEFAULT_MODEL;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::{Html, IntoResponse};
use axum::routing::{get, post};
use axum::{Json, Router};
use ditto_harness::agent::NoopHandler;
use ditto_harness::chat::{Harness, Options, PrepareRequest, RunRequest as ChatRunRequest};
use ditto_harness::retrieval::Variant;
use ditto_harness::types::{ChatMessage, Content, Tool, ToolDefinition};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::baseline::{Baseline, USER_ID};

// ---------------------------------------------------------------------------
// Chat turn
// ---------------------------------------------------------------------------

/// One observed (fake) tool call.
#[derive(Clone, Serialize)]
pub struct ToolEvent {
    pub name: String,
    pub args: Value,
    pub result: Value,
}

/// A retrieved memory hit shown in the UI.
#[derive(Clone, Serialize)]
pub struct MemHit {
    pub pair_id: String,
    pub preview: String,
    pub score: f64,
}

/// The result of one chat turn.
#[derive(Clone, Serialize)]
pub struct PlaygroundTurn {
    pub reply: String,
    pub tools: Vec<ToolEvent>,
    pub memories: Vec<MemHit>,
}

/// Runs one production-faithful chat turn: builds a harness with fake action
/// tools + the real memory tools + the seed-user store, the prod system prompt,
/// and the prior history, then returns the reply, the fake tool trace, and the
/// retrieved memories.
pub async fn playground_turn(
    baseline: &Baseline,
    history: &[(String, String)],
    user_input: &str,
) -> anyhow::Result<PlaygroundTurn> {
    // Fake action tools = full catalog minus the real memory tools.
    let host_tools: Vec<Arc<dyn Tool>> = crate::catalog::catalog()
        .into_iter()
        .filter(|d| !crate::baseline::MEMORY_TOOL_NAMES.contains(&d.name.as_str()))
        .map(|d| {
            Arc::new(tools::FakeTool {
                def: ToolDefinition {
                    name: d.name,
                    description: d.description,
                    input_schema: d.parameters,
                },
            }) as Arc<dyn Tool>
        })
        .collect();

    let harness = Harness::new(Options {
        model: baseline.model_arc(),
        memory: Some(Arc::clone(baseline.store())),
        tools: host_tools,
        include_memory_tools: true,
    });

    // Build the message history (prior turns + the new user message).
    let mut messages: Vec<ChatMessage> = history
        .iter()
        .map(|(role, content)| ChatMessage {
            role: role.clone(),
            content: vec![Content::text(content.clone())],
            ..ChatMessage::default()
        })
        .collect();
    messages.push(ChatMessage {
        role: "user".to_string(),
        content: vec![Content::text(user_input.to_string())],
        ..ChatMessage::default()
    });

    let result = harness
        .run(
            ChatRunRequest {
                prepare: PrepareRequest {
                    user_id: USER_ID.to_string(),
                    user_input: user_input.to_string(),
                    system_prompt: prompt::system_prompt(baseline.model_name()),
                    messages,
                    use_composite: true,
                    variant: Variant::V2,
                    candidate_pool_size: 50,
                    ..PrepareRequest::default()
                },
                // Match the scored starter's room for composed trajectories.
                // This bounds the stock UI loop, not the benchmark score.
                max_turns: 24,
                save_memory: false,
                ..ChatRunRequest::default()
            },
            &NoopHandler,
        )
        .await
        .map_err(|e| anyhow::anyhow!("playground run: {e}"))?;

    // Recover ALL tool calls from the transcript — both the fake action tools
    // and the REAL memory tools (search_memories, fetch_memories, ...). Tool
    // results live in `Content.tool_call_response` keyed by the call id.
    let mut results: std::collections::HashMap<String, Value> = std::collections::HashMap::new();
    for m in &result.result.messages {
        for c in &m.content {
            if let Some(tr) = &c.tool_call_response {
                let r = if !tr.error.is_empty() {
                    json!({ "error": tr.error })
                } else {
                    tr.output.clone()
                };
                results.insert(tr.id.clone(), r);
            }
        }
    }
    let mut tools: Vec<ToolEvent> = Vec::new();
    for m in &result.result.messages {
        for tc in &m.tool_calls {
            tools.push(ToolEvent {
                name: tc.name.clone(),
                args: tc.args.clone(),
                result: results.get(&tc.id).cloned().unwrap_or(Value::Null),
            });
        }
    }

    let memories = baseline
        .retrieve_previews(user_input, 6)
        .await
        .unwrap_or_default()
        .into_iter()
        .map(|(pair_id, preview, score)| MemHit {
            pair_id,
            preview,
            score,
        })
        .collect();

    Ok(PlaygroundTurn {
        reply: result.result.text,
        tools,
        memories,
    })
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------

type ChatHistory = HashMap<String, Vec<(String, String)>>;

#[derive(Clone)]
pub(crate) struct AppState {
    pub(crate) baseline: Arc<Baseline>,
    pub(crate) sessions: Arc<Mutex<ChatHistory>>,
    pub(crate) score_jobs: Arc<Mutex<HashMap<String, ScoreJob>>>,
    pub(crate) http: reqwest::Client,
    pub(crate) submit: submit::SubmitConfig,
}

#[derive(Deserialize)]
struct ChatReq {
    #[serde(default)]
    session_id: String,
    message: String,
}

#[derive(Serialize)]
struct ChatResp {
    reply: String,
    tools: Vec<ToolEvent>,
    memories: Vec<MemHit>,
}

const INDEX_HTML: &str = include_str!("../playground.html");

#[cfg(test)]
mod hosted_rehearsal_copy_tests {
    use super::INDEX_HTML;

    #[test]
    fn banner_explains_the_remote_tool_observation_boundary() {
        assert!(INDEX_HTML.contains("Hosted v8 rehearsal."));
        assert!(INDEX_HTML.contains("remote harness cannot reach"));
        assert!(INDEX_HTML.contains("scripts/local-rehearsal.py --run-size small"));
        assert!(INDEX_HTML.contains("neither rehearsal is submission certification"));
    }
}

/// Runs the playground server.
pub async fn serve(port: u16) -> anyhow::Result<()> {
    use anyhow::Context;
    let baseline = Arc::new(Baseline::from_env().await?);
    let submit = submit::SubmitConfig::from_env();
    eprintln!(
        "submit proxy -> {} (local harness {}, crate {}@{}:{})",
        submit.api_url, submit.harness_url, submit.git_url, submit.git_ref, submit.git_subdir
    );
    let state = AppState {
        baseline,
        sessions: Arc::new(Mutex::new(HashMap::new())),
        score_jobs: Arc::new(Mutex::new(HashMap::new())),
        http: reqwest::Client::new(),
        submit,
    };
    let app = Router::new()
        .route("/", get(|| async { Html(INDEX_HTML) }))
        .route(
            "/api/health",
            get(|| async { Json(json!({"status":"ok"})) }),
        )
        .route("/api/tools", get(tools_handler))
        .route("/api/chat", post(chat_handler))
        .route("/api/score", post(score_start_handler))
        .route("/api/score/:id", get(score_poll_handler))
        .route("/api/submit", post(submit::submit_start_handler))
        .route("/api/submit/:id", get(submit::submit_poll_handler))
        .with_state(state);

    let addr = format!("127.0.0.1:{port}");
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .with_context(|| format!("bind {addr}"))?;
    eprintln!("playground UI on http://{addr}  (open it in your browser)");
    axum::serve(listener, app).await.context("axum serve")?;
    Ok(())
}

/// Returns the full tool catalog (name, description, schema) for the UI.
async fn tools_handler() -> impl IntoResponse {
    let tools: Vec<Value> = crate::catalog::catalog()
        .into_iter()
        .map(|d| {
            let memory = crate::baseline::MEMORY_TOOL_NAMES.contains(&d.name.as_str());
            json!({
                "name": d.name,
                "description": d.description,
                "parameters": d.parameters,
                "kind": if memory { "memory (real, queries the seed user)" } else { "action (fake response in playground)" },
            })
        })
        .collect();
    Json(json!({ "tools": tools }))
}

async fn chat_handler(
    State(state): State<AppState>,
    Json(req): Json<ChatReq>,
) -> impl IntoResponse {
    let session_id = if req.session_id.is_empty() {
        "default".to_string()
    } else {
        req.session_id.clone()
    };
    let history = {
        let sessions = state.sessions.lock().expect("sessions lock");
        sessions.get(&session_id).cloned().unwrap_or_default()
    };

    match playground_turn(&state.baseline, &history, &req.message).await {
        Ok(turn) => {
            let mut sessions = state.sessions.lock().expect("sessions lock");
            let h = sessions.entry(session_id).or_default();
            h.push(("user".to_string(), req.message.clone()));
            h.push(("assistant".to_string(), turn.reply.clone()));
            (
                StatusCode::OK,
                Json(ChatResp {
                    reply: turn.reply,
                    tools: turn.tools,
                    memories: turn.memories,
                }),
            )
                .into_response()
        }
        Err(err) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": err.to_string()})),
        )
            .into_response(),
    }
}

// ---------------------------------------------------------------------------
// Scoring (live local evaluation)
// ---------------------------------------------------------------------------

/// One scored case, streamed as the run progresses.
#[derive(Clone, Serialize)]
pub struct ScoredCase {
    pub kind: String, // "tool" | "memory"
    pub case_id: String,
    pub category: String,
    pub prompt: String,
    pub score: f64,
    pub latency_ms: i64,
    pub detail: String,
}

/// A scoring job's live state (polled by the UI).
#[derive(Clone, Serialize, Default)]
pub struct ScoreJob {
    pub status: String, // "running" | "done" | "error"
    pub done: usize,
    pub total: usize,
    pub cases: Vec<ScoredCase>,
    pub composite: f64,
    pub tool_mean: f64,
    pub memory_mean: f64,
    pub median_ms: i64,
    pub error: String,
}

#[derive(Deserialize)]
struct ScoreStartReq {
    #[serde(default)]
    tools: usize,
    #[serde(default)]
    mem: usize,
}

/// Fixed dataset seed for the local scoring demo (same cases every run).
const SCORE_SEED: i64 = 7;

/// Starts a scoring run in the background and returns its job id to poll.
async fn score_start_handler(
    State(state): State<AppState>,
    Json(req): Json<ScoreStartReq>,
) -> impl IntoResponse {
    let n_tools = if req.tools == 0 { 6 } else { req.tools.min(20) };
    let n_mem = if req.mem == 0 { 6 } else { req.mem.min(20) };
    let job_id = uuid::Uuid::new_v4().to_string();
    {
        let mut jobs = state.score_jobs.lock().expect("score_jobs lock");
        jobs.insert(
            job_id.clone(),
            ScoreJob {
                status: "running".to_string(),
                ..ScoreJob::default()
            },
        );
    }
    let baseline = Arc::clone(&state.baseline);
    let jobs = Arc::clone(&state.score_jobs);
    let id = job_id.clone();
    tokio::spawn(async move { run_scoring(baseline, jobs, id, n_tools, n_mem).await });
    Json(json!({ "job_id": job_id }))
}

/// Returns the current state of a scoring job.
async fn score_poll_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> impl IntoResponse {
    let jobs = state.score_jobs.lock().expect("score_jobs lock");
    match jobs.get(&id) {
        Some(job) => (StatusCode::OK, Json(job.clone())).into_response(),
        None => (StatusCode::NOT_FOUND, Json(json!({"error":"unknown job"}))).into_response(),
    }
}

fn push_case(jobs: &Arc<Mutex<HashMap<String, ScoreJob>>>, id: &str, case: ScoredCase) {
    let mut j = jobs.lock().expect("score_jobs lock");
    if let Some(job) = j.get_mut(id) {
        job.cases.push(case);
        job.done += 1;
    }
}

/// Runs the FIXED local evaluation (static seed user + same questions + a
/// fixed-seed tool set) through the shared eval loop ([`crate::eval`]),
/// streaming per-case scores into the job, then the scorer's aggregate.
/// Mirrors the `evaluate` CLI command.
async fn run_scoring(
    baseline: Arc<Baseline>,
    jobs: Arc<Mutex<HashMap<String, ScoreJob>>>,
    job_id: String,
    n_tools: usize,
    n_mem: usize,
) {
    let mut ds = crate::datagen::generate(SCORE_SEED, n_tools, 0);
    let mut mem_cases = crate::seed::memory_cases();
    if n_mem > 0 && mem_cases.len() > n_mem {
        mem_cases.truncate(n_mem);
    }
    // The same bundled LongMemEval questions over the seed user, in the
    // dataset shape the shared eval loop + scorer consume.
    ds.memory_cases = mem_cases
        .iter()
        .map(|c| crate::protocol::MemoryCase {
            id: c.question_id.clone(),
            question: c.query.clone(),
            expected_answer: c.answer_text(),
            seed_memories: Vec::new(),
        })
        .collect();
    {
        let mut j = jobs.lock().expect("score_jobs lock");
        if let Some(job) = j.get_mut(&job_id) {
            job.total = ds.tool_cases.len() + ds.memory_cases.len();
        }
    }
    let qtype_by_id: HashMap<String, String> = mem_cases
        .iter()
        .map(|c| (c.question_id.clone(), c.question_type.clone()))
        .collect();

    let results = crate::eval::run_suite(&baseline, &ds, &qtype_by_id, |o| {
        push_case(
            &jobs,
            &job_id,
            ScoredCase {
                kind: o.kind.to_string(),
                case_id: o.case_id.to_string(),
                category: o.category.to_string(),
                prompt: o.prompt.to_string(),
                score: o.score,
                latency_ms: o.latency_ms,
                detail: o.detail,
            },
        );
    })
    .await;

    // Aggregate exactly like the scorer (v2 composite weights, shared median).
    let report = crate::scorer::score("playground", &ds, &results.tool_resps, &results.mem_results);

    let mut j = jobs.lock().expect("score_jobs lock");
    if let Some(job) = j.get_mut(&job_id) {
        job.status = "done".to_string();
        job.tool_mean = report.tool_mean;
        job.memory_mean = report.memory_mean;
        job.composite = report.composite;
        job.median_ms = report.median_ms;
    }
}
