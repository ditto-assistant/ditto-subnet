//! DittoBench miner CLI.
//!
//! Subcommands:
//!   serve       — HTTP server exposing POST /run + POST /seed + GET /health (validator faces this)
//!   playground  — interactive web UI: chat with the prod-faithful agent over the seed user
//!   seed-user   — load the bundled LongMemEval seed user into the local Turso DB
//!   mem-eval    — evaluate memory RETRIEVAL (recall@k) over the seed user; no LLM calls
//!   evaluate    — score against a FIXED local benchmark (fixed-seed tools + bundled questions)
//!   practice    — generate a ROTATING dataset, run it through the baseline, print a report
//!   ollama-check — verify local GPT-OSS + embeddinggemma readiness
//!   submit      — package the repo into a submission tarball (upload happens
//!                 via the playground Submit tab / the ditto CLI — see `submit()`)

use std::collections::HashMap;
use std::sync::Arc;

use anyhow::Context;
use axum::extract::{DefaultBodyLimit, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use clap::{Parser, Subcommand};

use dittobench_starter_kit::baseline::{
    Baseline, DEFAULT_OLLAMA_CHAT_MODEL, DEFAULT_OLLAMA_EMBED_MODEL, USER_ID,
};
use dittobench_starter_kit::{datagen, eval, protocol, scorer};

#[derive(Parser)]
#[command(
    name = "dittobench-miner",
    about = "DittoBench (SN118) miner starter kit",
    version
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Run the HTTP harness server (POST /run, POST /seed, GET /health).
    Serve {
        #[arg(long, default_value_t = 8080)]
        port: u16,
    },
    /// Launch the interactive playground UI: chat with a 1:1 prod-Ditto agent
    /// over the seed user, with the tool catalog + live tool-call/memory trace.
    Playground {
        #[arg(long, default_value_t = 8088)]
        port: u16,
    },
    /// Load the bundled LongMemEval seed user (pairs + pre-synced subjects)
    /// into the local Turso vector DB, ready for retrieval. Idempotent.
    SeedUser,
    /// Evaluate memory RETRIEVAL over the seed user: run the bundled LongMemEval
    /// questions through the full production retrieval pipeline (MLP weights +
    /// composite V2 + cross-encoder rerank) and report recall@k. Run `seed-user`
    /// first. No LLM calls — this isolates retrieval quality.
    MemEval {
        /// Retrieve top-k memories per question.
        #[arg(long, default_value_t = 10)]
        k: usize,
        /// Limit the number of questions (0 = all bundled cases).
        #[arg(long, default_value_t = 0)]
        limit: usize,
    },
    /// Test a submission locally against a FIXED benchmark: the static seed
    /// user + the same bundled LongMemEval questions + a fixed-seed tool set,
    /// every run. Reproducible inputs so you can iterate on your score (vs the
    /// hosted validator, which rotates a fresh dataset). Run
    /// `seed-user` first.
    Evaluate {
        /// Number of (fixed-seed) tool cases.
        #[arg(long, default_value_t = 12)]
        tools: usize,
        /// Number of bundled memory questions to run (0 = all 50).
        #[arg(long, default_value_t = 12)]
        mem: usize,
        /// Fixed dataset seed for the tool cases (change only to vary the fixed set).
        #[arg(long, default_value_t = 7)]
        seed: i64,
    },
    /// Generate a ROTATING random dataset (anti-overfit), run it through the
    /// baseline, and score it — mirrors the hosted validator's fresh-dataset
    /// behavior. For a stable local iteration loop, use `evaluate` instead.
    Practice {
        /// Number of tool cases.
        #[arg(long, default_value_t = 20)]
        n: usize,
        /// Number of memory cases.
        #[arg(long, default_value_t = 5)]
        mem: usize,
        /// Seed for dataset generation (defaults to a random seed).
        #[arg(long)]
        seed: Option<i64>,
    },
    /// Verify that local Ollama has the exact chat and embedding models needed
    /// for local development, including a 768-dimension embed call.
    OllamaCheck,
    /// Package the repository into a submission tarball.
    Submit,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Load .env if present so miners can just paste their OPENROUTER_API_KEY.
    let _ = dotenvy::dotenv();
    let cli = Cli::parse();
    match cli.command {
        Command::Serve { port } => serve(port).await,
        Command::Playground { port } => dittobench_starter_kit::playground::serve(port).await,
        Command::SeedUser => seed_user().await,
        Command::MemEval { k, limit } => mem_eval(k, limit).await,
        Command::Evaluate { tools, mem, seed } => evaluate(tools, mem, seed).await,
        Command::Practice { n, mem, seed } => practice(n, mem, seed).await,
        Command::OllamaCheck => ollama_check().await,
        Command::Submit => submit(),
    }
}

async fn ollama_check() -> anyhow::Result<()> {
    let base_url = std::env::var("OLLAMA_BASE_URL")
        .unwrap_or_else(|_| ditto_harness::models::DEFAULT_OLLAMA_BASE_URL.to_string());
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()?;

    for model in [DEFAULT_OLLAMA_CHAT_MODEL, DEFAULT_OLLAMA_EMBED_MODEL] {
        let response = client
            .post(format!("{}/api/show", base_url.trim_end_matches('/')))
            .json(&serde_json::json!({"model": model}))
            .send()
            .await
            .with_context(|| format!("reach Ollama at {base_url}"))?;
        anyhow::ensure!(
            response.status().is_success(),
            "Ollama model {model} is not ready (HTTP {}); run `ollama pull {model}`",
            response.status()
        );
    }

    let response = client
        .post(format!("{}/api/embed", base_url.trim_end_matches('/')))
        .json(&serde_json::json!({
            "model": DEFAULT_OLLAMA_EMBED_MODEL,
            "input": "DittoBench Ollama readiness check"
        }))
        .send()
        .await
        .context("call Ollama embedding endpoint")?;
    let status = response.status();
    anyhow::ensure!(
        status.is_success(),
        "Ollama embedding check failed: HTTP {status}"
    );
    let payload: serde_json::Value = response.json().await.context("decode Ollama embedding")?;
    let dimensions = payload["embeddings"][0]
        .as_array()
        .map(Vec::len)
        .context("Ollama embedding response did not contain embeddings[0]")?;
    anyhow::ensure!(
        dimensions == 768,
        "{DEFAULT_OLLAMA_EMBED_MODEL} returned {dimensions} dimensions; expected 768"
    );

    println!(
        "Ollama ready: chat={DEFAULT_OLLAMA_CHAT_MODEL}, embeddings={DEFAULT_OLLAMA_EMBED_MODEL} (768 dimensions), endpoint={base_url}"
    );
    Ok(())
}

// --- serve ------------------------------------------------------------------

#[derive(Clone)]
struct AppState {
    baseline: Arc<Baseline>,
}

async fn serve(port: u16) -> anyhow::Result<()> {
    let baseline = Arc::new(Baseline::from_env().await?);
    let state = AppState { baseline };
    let app = Router::new()
        .route("/health", get(health))
        .route("/run", post(run_handler))
        .route("/seed", post(seed_handler))
        // The validator POSTs the whole seed haystack to /seed in one request;
        // at run_size=full that is hundreds of pairs over thousands of subjects
        // and exceeds axum's 2 MB default body limit (413 "length limit
        // exceeded"). The validator is the trusted scoring authority, so lift
        // the cap to a generous bound rather than the default.
        .layer(DefaultBodyLimit::max(256 * 1024 * 1024))
        .with_state(state);

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .with_context(|| format!("bind {addr}"))?;
    eprintln!("dittobench-miner serving on http://{addr} (POST /run, POST /seed, GET /health)");
    axum::serve(listener, app).await.context("axum serve")?;
    Ok(())
}

async fn health() -> impl IntoResponse {
    (StatusCode::OK, Json(serde_json::json!({"status": "ok"})))
}

async fn run_handler(
    State(state): State<AppState>,
    Json(req): Json<protocol::RunRequest>,
) -> impl IntoResponse {
    match state.baseline.run(req).await {
        Ok(resp) => (StatusCode::OK, Json(resp)).into_response(),
        Err(err) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": err.to_string()})),
        )
            .into_response(),
    }
}

/// `POST /seed` — install a FRESH memory haystack into the harness Turso store
/// before the validator asks memory questions. Loads the provided pairs +
/// subjects + links via the same `save_memory` path as the bundled seed user
/// (idempotent upserts). Returns the counts that were loaded.
async fn seed_handler(
    State(state): State<AppState>,
    Json(req): Json<dittobench_starter_kit::seed::SeedRequest>,
) -> impl IntoResponse {
    match dittobench_starter_kit::seed::seed_from_request(state.baseline.store(), req).await {
        Ok(resp) => (StatusCode::OK, Json(resp)).into_response(),
        Err(err) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": err.to_string()})),
        )
            .into_response(),
    }
}

// --- seed-user --------------------------------------------------------------

async fn seed_user() -> anyhow::Result<()> {
    let baseline = Baseline::from_env().await?;
    eprintln!(
        "loading bundled LongMemEval seed user into the vector DB (embeds pairs + subjects)..."
    );
    let stats = dittobench_starter_kit::seed::load_seed_user(baseline.store()).await?;
    println!(
        "seeded user {USER_ID:?}: {} pairs, {} subjects, {} subject links — ready for retrieval",
        stats.pairs, stats.subjects, stats.links
    );
    Ok(())
}

// --- mem-eval ---------------------------------------------------------------

async fn mem_eval(k: usize, limit: usize) -> anyhow::Result<()> {
    use std::collections::BTreeMap;

    let mut cases = dittobench_starter_kit::seed::memory_cases();
    if limit > 0 && cases.len() > limit {
        cases.truncate(limit);
    }
    anyhow::ensure!(!cases.is_empty(), "no bundled memory cases");

    let baseline = Baseline::from_env().await?;
    eprintln!(
        "evaluating retrieval recall@{k} over {} LongMemEval questions (full pipeline: MLP + composite V2 + cross-encoder rerank)...",
        cases.len()
    );

    let mut hits = 0usize; // at least one answer pair retrieved
    let mut recall_sum = 0.0f64; // fraction of answer pairs retrieved
                                 // per question-type aggregates: (hit_count, recall_sum, n)
    let mut by_type: BTreeMap<String, (usize, f64, usize)> = BTreeMap::new();

    for (i, c) in cases.iter().enumerate() {
        let retrieved = match baseline.retrieve(&c.query, k).await {
            Ok(r) => r,
            Err(err) => {
                eprintln!("  case {} retrieve failed: {err}", c.question_id);
                continue;
            }
        };
        let want: std::collections::HashSet<&str> =
            c.answer_pair_ids.iter().map(String::as_str).collect();
        let got: std::collections::HashSet<&str> = retrieved.iter().map(String::as_str).collect();
        let found = want.iter().filter(|p| got.contains(*p)).count();
        let recall = if want.is_empty() {
            0.0
        } else {
            found as f64 / want.len() as f64
        };
        let hit = found > 0;
        if hit {
            hits += 1;
        }
        recall_sum += recall;
        let e = by_type
            .entry(c.question_type.clone())
            .or_insert((0, 0.0, 0));
        e.0 += hit as usize;
        e.1 += recall;
        e.2 += 1;
        if (i + 1) % 10 == 0 || i + 1 == cases.len() {
            eprintln!("  {}/{} questions", i + 1, cases.len());
        }
    }

    let n = cases.len() as f64;
    println!("\n=== DittoBench memory retrieval report (recall@{k}) ===");
    println!("questions:   {}", cases.len());
    println!(
        "hit@{k}:      {:.3}   (>=1 answer pair retrieved)",
        hits as f64 / n
    );
    println!(
        "recall@{k}:   {:.3}   (mean fraction of answer pairs retrieved)",
        recall_sum / n
    );
    println!("\nby question type:");
    for (t, (h, r, cnt)) in &by_type {
        println!(
            "  {:<28} hit {:.3}  recall {:.3}  (n={})",
            t,
            *h as f64 / *cnt as f64,
            r / *cnt as f64,
            cnt
        );
    }
    Ok(())
}

// --- evaluate (fixed local submission test) ---------------------------------

async fn evaluate(n_tools: usize, n_mem: usize, seed: i64) -> anyhow::Result<()> {
    let baseline = Baseline::from_env().await?;

    // Guard: the static seed user must be loaded (memory questions query it).
    // A retrieval ERROR (e.g. the embedding backend is down) is not the same
    // as an empty store — surface it instead of telling the user to re-seed.
    let probe = baseline
        .retrieve("hello", 1)
        .await
        .context("memory retrieval probe failed (is the retrieval stack up?)")?;
    if probe.is_empty() {
        anyhow::bail!("seed user not loaded — run `dittobench-miner seed-user` first");
    }

    // FIXED inputs: a fixed-seed tool set + the same bundled LongMemEval
    // questions over the static seed user, every run.
    let mut ds = datagen::generate(seed, n_tools, 0);
    let mut cases = dittobench_starter_kit::seed::memory_cases();
    if n_mem > 0 && cases.len() > n_mem {
        cases.truncate(n_mem);
    }
    ds.memory_cases = cases
        .iter()
        .map(|c| protocol::MemoryCase {
            id: c.question_id.clone(),
            question: c.query.clone(),
            expected_answer: c.answer_text(),
            seed_memories: Vec::new(),
        })
        .collect();

    eprintln!(
        "evaluate (FIXED): {} tool cases (seed={}) + {} bundled LongMemEval questions over the static seed user",
        ds.tool_cases.len(),
        seed,
        ds.memory_cases.len()
    );

    let qtype_by_id: HashMap<String, String> = cases
        .iter()
        .map(|c| (c.question_id.clone(), c.question_type.clone()))
        .collect();

    // Shared eval loop: deterministic judge-free grading (matches on-chain).
    let results = eval::run_suite(&baseline, &ds, &qtype_by_id, |o| {
        if o.error {
            eprintln!("{} case {} failed: {}", o.kind, o.case_id, o.detail);
        }
    })
    .await;

    let report = scorer::score(
        &format!("evaluate-seed{seed}"),
        &ds,
        &results.tool_resps,
        &results.mem_results,
    );
    print_report(&report, &ds);
    eprintln!(
        "\n(inputs are fixed; the model is still stochastic, so scores vary slightly run-to-run.\n the hosted validator rotates a fresh dataset per submission.)"
    );
    Ok(())
}

// --- practice ---------------------------------------------------------------

async fn practice(n: usize, mem: usize, seed: Option<i64>) -> anyhow::Result<()> {
    let seed = seed.unwrap_or_else(|| {
        use rand::Rng;
        rand::thread_rng().gen::<i64>().abs()
    });
    let ds = datagen::generate(seed, n, mem);
    eprintln!(
        "generated dataset seed={} ({} tool cases, {} memory cases)",
        seed,
        ds.tool_cases.len(),
        ds.memory_cases.len()
    );

    let baseline = Baseline::from_env().await?;

    // Auto-seed memory cases so retrieval has something to find. Idempotent.
    if !ds.memory_cases.is_empty() {
        eprintln!("seeding memory-case fixtures...");
        for mc in &ds.memory_cases {
            for (j, sm) in mc.seed_memories.iter().enumerate() {
                let id = format!("{}-seed-{}", mc.id, j);
                baseline
                    .seed_memory(&id, &sm.prompt, &sm.response, sm.days_ago)
                    .await?;
            }
        }
    }

    // Shared eval loop (datagen memory cases carry no LongMemEval question
    // type, so the qtype map is empty).
    let results = eval::run_suite(&baseline, &ds, &HashMap::new(), |o| {
        if o.error {
            eprintln!("{} case {} failed: {}", o.kind, o.case_id, o.detail);
        }
    })
    .await;

    let report = scorer::score(
        &format!("practice-{seed}"),
        &ds,
        &results.tool_resps,
        &results.mem_results,
    );
    print_report(&report, &ds);
    Ok(())
}

fn print_report(report: &protocol::ScoreReport, ds: &protocol::Dataset) {
    println!("\n=== DittoBench practice report ({}) ===", report.run_id);
    println!("composite:   {:.3}", report.composite);
    println!("tool_mean:   {:.3}", report.tool_mean);
    println!("memory_mean: {:.3}", report.memory_mean);
    println!("median_ms:   {}", report.median_ms);
    println!("n:           {}", report.n);

    // Per-category tool means.
    let mut by_cat: HashMap<&str, (f64, usize)> = HashMap::new();
    for cs in &report.per_case {
        let e = by_cat.entry(cs.category.as_str()).or_insert((0.0, 0));
        e.0 += cs.tool_score;
        e.1 += 1;
    }
    println!("\nper-category mean score:");
    let mut cats: Vec<&str> = by_cat.keys().copied().collect();
    cats.sort_unstable();
    for cat in cats {
        let (sum, count) = by_cat[cat];
        println!("  {:<18} {:.3}  (n={})", cat, sum / count as f64, count);
    }

    // Slowest cases.
    let mut slow: Vec<&protocol::CaseScore> = report.per_case.iter().collect();
    slow.sort_by_key(|case| std::cmp::Reverse(case.latency_ms));
    println!("\nslowest cases:");
    for cs in slow.iter().take(3) {
        println!(
            "  {:<28} {} ms  score={:.2}",
            cs.case_id, cs.latency_ms, cs.tool_score
        );
    }

    let _ = ds; // dataset available for richer reporting if you extend this.
}

// --- submit -----------------------------------------------------------------

/// Submission tarball cap enforced by the platform (20 MiB). Checked locally so
/// a miner learns here instead of being rejected server-side after upload.
const MAX_TARBALL_BYTES: u64 = 20 * 1024 * 1024;

fn submit() -> anyhow::Result<()> {
    let out = "dittobench-submission.tgz";
    // Never package secrets or local state: `.env` / `.env.*` hold your
    // OPENROUTER_API_KEY, `*.db` is your local Turso DB and `*.db-*` its
    // WAL/SHM sidecars. The tarball is uploaded to the platform — keep them out.
    let excludes = [
        "target", ".git", "*.tgz", "*.db", "*.db-*", ".env", ".env.*",
    ];
    let mut cmd = std::process::Command::new("tar");
    for pat in excludes {
        cmd.arg(format!("--exclude={pat}"));
    }
    let status = cmd.args(["-czf", out, "."]).status().context("run tar")?;
    anyhow::ensure!(status.success(), "tar failed");

    // Enforce the platform's 20 MiB cap locally so a miner fails fast here
    // instead of being rejected server-side after upload.
    let size = std::fs::metadata(out)
        .with_context(|| format!("stat {out}"))?
        .len();
    anyhow::ensure!(
        size <= MAX_TARBALL_BYTES,
        "submission tarball {out} is {size} bytes, over the {MAX_TARBALL_BYTES}-byte (20 MiB) limit; \
         trim large files (models, fixtures, checkouts) before submitting"
    );

    println!("packaged repository -> {out}");
    println!("excluded (secrets + local state): {}", excludes.join(", "));
    println!();
    println!("next steps:");
    println!("  * Hosted practice (off-chain): run `dittobench-miner playground` and use");
    println!("    its Submit tab to score this harness against the hosted validator");
    println!("    (see README \"Hosted practice\").");
    println!("  * On-chain submission (SN118): register your miner hotkey, then upload the");
    println!("    tarball with the `ditto` miner CLI from the ditto-subnet repo — it pays the");
    println!("    eval fee on chain and uploads to the platform:");
    println!("      ditto upload --path {out} --name <name> --coldkey <ck> --hotkey <hk>");
    Ok(())
}
