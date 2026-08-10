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

fn supports_git_subdir(capabilities: &Value) -> bool {
    capabilities
        .get("features")
        .and_then(Value::as_array)
        .is_some_and(|features| features.iter().any(|feature| feature == "git_subdir"))
}

async fn require_git_subdir_capability(state: &AppState) -> Result<(), String> {
    let url = format!(
        "{}/v1/capabilities",
        state.submit.api_url.trim_end_matches('/')
    );
    let response = state
        .http
        .get(&url)
        .send()
        .await
        .map_err(|err| format!("check hosted API capabilities at {url}: {err}"))?;
    if !response.status().is_success() {
        return Err(format!(
            "hosted API capability check returned {}; wait for the API cutover or set DITTOBENCH_API_URL to a compatible deployment",
            response.status()
        ));
    }
    let capabilities: Value = response
        .json()
        .await
        .map_err(|err| format!("decode hosted API capabilities: {err}"))?;
    if !supports_git_subdir(&capabilities) {
        return Err(
            "hosted API does not advertise git_subdir; refusing to build the monorepo root"
                .to_string(),
        );
    }
    Ok(())
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
    if req.target == "crate" {
        if let Err(error) = require_git_subdir_capability(&state).await {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(json!({"error": error})),
            )
                .into_response();
        }
    }
    // Practice explicitly selects the active v9 dataset and scoring contract.
    // The submitted harness still uses its own local model configuration; the
    // canonical scored path supplies ticket-bound inference separately.
    let body = if req.target == "crate" {
        json!({
            "git_url": state.submit.git_url,
            "git_ref": state.submit.git_ref,
            "git_subdir": state.submit.git_subdir,
            "run_size": run_size,
            "bench_version": crate::protocol::ACTIVE_BENCH_VERSION,
        })
    } else {
        json!({
            "harness_url": state.submit.harness_url,
            "run_size": run_size,
            "bench_version": crate::protocol::ACTIVE_BENCH_VERSION,
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

#[cfg(test)]
mod tests {
    use super::supports_git_subdir;
    use serde_json::json;

    #[test]
    fn git_subdir_requires_an_explicit_live_capability() {
        assert!(supports_git_subdir(&json!({"features": ["git_subdir"]})));
        assert!(!supports_git_subdir(&json!({"features": []})));
        assert!(!supports_git_subdir(&json!({
            "supported_bench_versions": [8, 9]
        })));
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
