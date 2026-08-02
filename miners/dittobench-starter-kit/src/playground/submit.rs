//! Submit-to-dittobench-api proxy: the playground backend forwards submissions
//! to the hosted validator so the browser never has to (avoids CORS).

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};

use super::AppState;

/// Config for proxying submissions to dittobench-api (resolved from env at
/// startup). The playground backend makes the outbound call so the browser
/// never has to (avoids CORS).
///
/// By default this targets the official hosted practice validator so
/// miners can score against a fresh anti-cheat dataset. Pointing
/// `DITTOBENCH_API_URL` at a localhost api is internal dev only.
#[derive(Clone)]
pub(crate) struct SubmitConfig {
    /// Base URL of dittobench-api, e.g. `http://localhost:8000`.
    pub(super) api_url: String,
    /// Git URL of this crate that the validator clones + builds.
    pub(super) git_url: String,
    /// Git ref of this crate to build.
    pub(super) git_ref: String,
    /// Repository-relative Docker context for a monorepo-hosted crate.
    pub(super) git_subdir: String,
    /// URL of an already-running harness (`serve`) for the fast local path: the
    /// validator skips the Docker build and runs generate→seed→run→score
    /// directly against it. Reachable from the api's host (use
    /// `http://host.docker.internal:8080` if the api runs in a container).
    pub(super) harness_url: String,
}

impl SubmitConfig {
    pub(super) fn from_env() -> Self {
        SubmitConfig {
            api_url: std::env::var("DITTOBENCH_API_URL").unwrap_or_else(|_| {
                // Official hosted practice validator. Override with
                // DITTOBENCH_API_URL=http://localhost:8000 for internal dev.
                "https://dittobench-api-22790208601.us-central1.run.app".to_string()
            }),
            git_url: std::env::var("DITTOBENCH_CRATE_GIT")
                .unwrap_or_else(|_| "https://github.com/ditto-assistant/ditto-subnet".to_string()),
            git_ref: std::env::var("DITTOBENCH_CRATE_REF").unwrap_or_else(|_| "main".to_string()),
            git_subdir: std::env::var("DITTOBENCH_CRATE_SUBDIR")
                .unwrap_or_else(|_| "miners/dittobench-starter-kit".to_string()),
            // Default matches `serve`'s default port (8080).
            harness_url: std::env::var("DITTOBENCH_HARNESS_URL")
                .unwrap_or_else(|_| "http://localhost:8080".to_string()),
        }
    }
}

#[derive(Deserialize)]
pub(super) struct SubmitReq {
    /// "small" | "medium" | "full", passed straight through to the api.
    #[serde(default)]
    run_size: String,
    /// "local" (default) runs against an already-running harness (skips the
    /// Docker build for fast iteration); "crate" has the validator clone + build
    /// this crate in Docker (the real SN118 flow).
    #[serde(default)]
    target: String,
}

/// `POST /api/submit`: forward a submission to `<DITTOBENCH_API_URL>/v1/submit`
/// and return the api's `{run_id, poll}` to the browser. The backend makes the
/// call so the browser avoids CORS. The `target` selects the local running
/// harness (fast) or a full Docker crate build.
pub(super) async fn submit_start_handler(
    State(state): State<AppState>,
    Json(req): Json<SubmitReq>,
) -> impl IntoResponse {
    let run_size = match req.run_size.as_str() {
        "small" | "medium" | "full" => req.run_size.clone(),
        _ => "small".to_string(),
    };
    let url = format!("{}/v1/submit", state.submit.api_url.trim_end_matches('/'));
    // This public practice endpoint intentionally omits bench_version and
    // remains on its active legacy contract. Canonical v7 requires a
    // platform-issued ticket/session and is never inferred from UI copy or a
    // repository default. The playground never forwards provider credentials.
    let body = if req.target == "crate" {
        json!({
            "git_url": state.submit.git_url,
            "git_ref": state.submit.git_ref,
            "git_subdir": state.submit.git_subdir,
            "run_size": run_size,
        })
    } else {
        json!({
            "harness_url": state.submit.harness_url,
            "run_size": run_size,
        })
    };
    match state.http.post(&url).json(&body).send().await {
        Ok(resp) => relay_json(resp).await,
        Err(err) => (
            StatusCode::BAD_GATEWAY,
            Json(json!({"error": format!("submit to {url}: {err}")})),
        )
            .into_response(),
    }
}

/// `GET /api/submit/:id`: proxy `GET <DITTOBENCH_API_URL>/v1/runs/:id` and
/// return the run's JSON (status, stage, progress, partial cases, report).
pub(super) async fn submit_poll_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> impl IntoResponse {
    let url = format!(
        "{}/v1/runs/{}",
        state.submit.api_url.trim_end_matches('/'),
        id
    );
    match state.http.get(&url).send().await {
        Ok(resp) => relay_json(resp).await,
        Err(err) => (
            StatusCode::BAD_GATEWAY,
            Json(json!({"error": format!("poll {url}: {err}")})),
        )
            .into_response(),
    }
}

/// Relays an upstream response: preserves its status, parses the body as JSON
/// (falling back to wrapping raw text), and returns it to the browser.
async fn relay_json(resp: reqwest::Response) -> axum::response::Response {
    let status = StatusCode::from_u16(resp.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let text = resp.text().await.unwrap_or_default();
    match serde_json::from_str::<Value>(&text) {
        Ok(v) => (status, Json(v)).into_response(),
        Err(_) => (
            status,
            Json(json!({"error": "non-JSON upstream", "body": text})),
        )
            .into_response(),
    }
}
