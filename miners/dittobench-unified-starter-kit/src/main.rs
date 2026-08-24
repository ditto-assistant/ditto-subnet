//! One listener for the active normal `DittoBench` harness and the additive,
//! shadow-only coding harness.

use std::path::Path;
use std::sync::Arc;

use anyhow::{Context, Result};
use axum::extract::{DefaultBodyLimit, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use clap::{Parser, Subcommand};
use dittobench_coding_starter_kit::server::TicketBrokerModelFactory;
use dittobench_coding_starter_kit::{router as coding_router, CodingService, ModelFactory};
use dittobench_starter_kit::baseline::Baseline;
use dittobench_starter_kit::{protocol, seed};

const MAX_TARBALL_BYTES: u64 = 20 * 1024 * 1024;
const ARCHIVE_NAME: &str = "dittobench-submission.tgz";
const TEMP_ARCHIVE_NAME: &str = ".dittobench-submission.tar";
const SOURCE_CRATES: [&str; 3] = [
    "dittobench-unified-starter-kit",
    "dittobench-starter-kit",
    "dittobench-coding-starter-kit",
];
const TAR_EXCLUDES: [&str; 12] = [
    "target", "*/target", ".git", "*/.git", "*.tgz", "*.tar", "*.db", "*.db-*", ".env", ".env.*",
    "*/.env", "*/.env.*",
];

#[derive(Debug, Parser)]
#[command(
    name = "dittobench-unified-miner",
    version,
    about = "Unified normal and shadow-coding DittoBench reference miner"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Run normal and additive coding routes on one port.
    Serve {
        #[arg(long, default_value_t = 8080)]
        port: u16,
    },
    /// Package the three source crates into one uploadable Docker build context.
    Submit,
}

#[derive(Clone)]
struct NormalState {
    baseline: Arc<Baseline>,
}

#[tokio::main]
async fn main() -> Result<()> {
    let _ = dotenvy::dotenv();
    match Cli::parse().command {
        Command::Serve { port } => serve(port).await,
        Command::Submit => submit(&std::env::current_dir().context("read current directory")?),
    }
}

async fn serve(port: u16) -> Result<()> {
    let normal = NormalState {
        baseline: Arc::new(Baseline::from_env().await?),
    };
    let models: Arc<dyn ModelFactory> = Arc::new(TicketBrokerModelFactory);
    let coding = CodingService::new(models);
    let app = normal_router(normal).merge(coding_router(coding));

    let address = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&address)
        .await
        .with_context(|| format!("bind {address}"))?;
    eprintln!(
        "dittobench unified miner listening on {address} \
         (normal: /health /seed /run; coding: /coding/health /coding/seed /coding/run)"
    );
    axum::serve(listener, app)
        .await
        .context("serve unified harness")
}

fn normal_router(state: NormalState) -> Router {
    Router::new()
        .route("/health", get(normal_health))
        .route("/run", post(normal_run))
        .route("/seed", post(normal_seed))
        // The normal validator can seed a large, trusted haystack. The coding
        // router carries its own bounded request layer, so this does not widen
        // the coding request body cap.
        .layer(DefaultBodyLimit::max(256 * 1024 * 1024))
        .with_state(state)
}

async fn normal_health() -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(serde_json::json!({
            "status": "ok",
            "capabilities": ["case_scoped_inference_v1"]
        })),
    )
}

async fn normal_run(
    State(state): State<NormalState>,
    Json(request): Json<protocol::RunRequest>,
) -> impl IntoResponse {
    match state.baseline.run(request).await {
        Ok(response) => (StatusCode::OK, Json(response)).into_response(),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": error.to_string()})),
        )
            .into_response(),
    }
}

async fn normal_seed(
    State(state): State<NormalState>,
    Json(request): Json<seed::SeedRequest>,
) -> impl IntoResponse {
    match seed::seed_from_request(state.baseline.store(), request).await {
        Ok(response) => (StatusCode::OK, Json(response)).into_response(),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": error.to_string()})),
        )
            .into_response(),
    }
}

fn submit(root: &Path) -> Result<()> {
    let root = root
        .canonicalize()
        .with_context(|| format!("resolve submission directory {}", root.display()))?;
    let miners = root
        .parent()
        .context("unified starter must have a miners parent")?;
    anyhow::ensure!(
        root.file_name().and_then(|name| name.to_str()) == Some(SOURCE_CRATES[0]),
        "run submit from the {} directory",
        SOURCE_CRATES[0]
    );
    for source in SOURCE_CRATES {
        anyhow::ensure!(
            miners.join(source).join("Cargo.toml").is_file(),
            "required source crate {source} is missing next to {}",
            root.display()
        );
    }

    let archive = root.join(ARCHIVE_NAME);
    let temporary = root.join(TEMP_ARCHIVE_NAME);
    let _ = std::fs::remove_file(&archive);
    let _ = std::fs::remove_file(&temporary);

    let root_entries = ["Dockerfile", ".dockerignore"];
    run_tar(&root, &temporary, false, &root_entries, &[])?;
    run_tar(miners, &temporary, true, &SOURCE_CRATES, &TAR_EXCLUDES)?;
    let status = std::process::Command::new("gzip")
        .args(["-n", "-f"])
        .arg(&temporary)
        .status()
        .context("run gzip for submission archive")?;
    anyhow::ensure!(status.success(), "gzip failed while packaging submission");
    std::fs::rename(temporary.with_extension("tar.gz"), &archive)
        .with_context(|| format!("install {}", archive.display()))?;

    let size = std::fs::metadata(&archive)
        .with_context(|| format!("stat {}", archive.display()))?
        .len();
    anyhow::ensure!(
        size <= MAX_TARBALL_BYTES,
        "submission archive is {size} bytes, over the {MAX_TARBALL_BYTES}-byte (20 MiB) limit"
    );
    println!("packaged unified build context -> {}", archive.display());
    println!("excluded local state: {}", TAR_EXCLUDES.join(", "));
    println!(
        "next: cd ../.. && uv run ditto verify --path {}",
        archive.display()
    );
    Ok(())
}

fn run_tar(
    directory: &Path,
    archive: &Path,
    append: bool,
    entries: &[&str],
    excludes: &[&str],
) -> Result<()> {
    let mut command = std::process::Command::new("tar");
    command.arg(if append { "-rf" } else { "-cf" }).arg(archive);
    command.arg("-C").arg(directory);
    for exclude in excludes {
        command.arg(format!("--exclude={exclude}"));
    }
    command.args(entries);
    let status = command
        .status()
        .with_context(|| format!("run tar in {}", directory.display()))?;
    anyhow::ensure!(status.success(), "tar failed while packaging submission");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::{to_bytes, Body};
    use axum::http::Request;
    use tower::ServiceExt;

    #[tokio::test]
    async fn normal_and_coding_health_routes_are_additive() {
        let normal = Router::new().route("/health", get(normal_health));
        let coding = CodingService::new(Arc::new(TicketBrokerModelFactory));
        let app = normal.merge(coding_router(coding));

        let normal_response = app
            .clone()
            .oneshot(
                Request::get("/health")
                    .body(Body::empty())
                    .expect("normal request"),
            )
            .await
            .expect("normal response");
        assert_eq!(normal_response.status(), StatusCode::OK);
        let normal_body = to_bytes(normal_response.into_body(), 1024)
            .await
            .expect("normal body");
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&normal_body).expect("normal JSON"),
            serde_json::json!({
                "status": "ok",
                "capabilities": ["case_scoped_inference_v1"]
            })
        );

        let coding_response = app
            .oneshot(
                Request::get("/coding/health")
                    .body(Body::empty())
                    .expect("coding request"),
            )
            .await
            .expect("coding response");
        assert_eq!(coding_response.status(), StatusCode::OK);
        let coding_body = to_bytes(coding_response.into_body(), 1024)
            .await
            .expect("coding body");
        let coding =
            serde_json::from_slice::<serde_json::Value>(&coding_body).expect("coding JSON");
        assert_eq!(coding["status"], "ok");
        assert_eq!(
            coding["supported_coding_contract_versions"],
            serde_json::json!([1])
        );
        assert_eq!(
            coding["capabilities"],
            serde_json::json!([
                "scoped_memory_seed_v1",
                "coding_runner_tools_v1",
                "case_scoped_inference_v1"
            ])
        );
    }

    #[test]
    fn packaging_keeps_a_root_dockerfile_and_sibling_source_crates() {
        assert_eq!(SOURCE_CRATES[0], "dittobench-unified-starter-kit");
        assert!(TAR_EXCLUDES.contains(&"*/target"));
        assert!(TAR_EXCLUDES.contains(&".env.*"));
        let dockerfile = include_str!("../Dockerfile");
        for source in SOURCE_CRATES {
            assert!(dockerfile.contains(source));
        }
    }
}
