//! The `/router/*` control surface and the four provider pass-through routes.
//!
//! The default router is a **faithful pass-through**: it forwards each provider
//! request to the validator relay byte-for-byte, streams the response back
//! unchanged (SSE included), returns the provider `usage` untouched, and echoes
//! the ingress `X-Dittobench-Step` tap header onto the upstream call. The one
//! optional transform is the archetype rerouting lever in [`crate::reroute`],
//! which is off by default.

use axum::body::{Body, Bytes};
use axum::extract::{DefaultBodyLimit, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};

use crate::protocol::{RouterHealthResponse, RouterSeedRequest, RouterSeedResponse};
use crate::relay::Relay;
use crate::reroute::RerouteConfig;
use crate::seed::SeedStore;

/// Anthropic Messages route (the one route the reference lever may rewrite).
pub const ROUTE_MESSAGES: &str = "/v1/messages";
/// Anthropic token-counting route (`count_tokens: true` in the advertisement).
pub const ROUTE_COUNT_TOKENS: &str = "/v1/messages/count_tokens";
/// `OpenAI` Chat Completions route.
pub const ROUTE_CHAT_COMPLETIONS: &str = "/v1/chat/completions";
/// `OpenAI` Responses route.
pub const ROUTE_RESPONSES: &str = "/v1/responses";

/// The ingress tap header the router echoes onto every upstream call so the
/// relay can line each upstream body up with its tap counterpart.
const STEP_HEADER: &str = "x-dittobench-step";

/// Generous cap for a provider request body: a real agent turn can carry a
/// large context window. The relay enforces the authoritative budget.
const MAX_BODY_BYTES: usize = 64 * 1024 * 1024;

/// The shared, cloneable router service state.
#[derive(Clone)]
pub struct RouterService {
    relay: Option<Relay>,
    reroute: RerouteConfig,
    seeds: SeedStore,
}

impl RouterService {
    /// Builds the service. `relay` is `None` in local health-only practice,
    /// in which case the provider routes return `503`.
    #[must_use]
    pub fn new(relay: Option<Relay>, reroute: RerouteConfig) -> Self {
        Self {
            relay,
            reroute,
            seeds: SeedStore::new(),
        }
    }
}

/// Builds the axum router exposing the router control surface and the four
/// provider wires.
pub fn router(service: RouterService) -> Router {
    Router::new()
        .route("/router/health", get(health))
        .route("/router/seed", post(seed))
        .route(ROUTE_MESSAGES, post(messages))
        .route(ROUTE_COUNT_TOKENS, post(count_tokens))
        .route(ROUTE_CHAT_COMPLETIONS, post(chat_completions))
        .route(ROUTE_RESPONSES, post(responses))
        .layer(DefaultBodyLimit::max(MAX_BODY_BYTES))
        .with_state(service)
}

async fn health() -> Json<RouterHealthResponse> {
    Json(RouterHealthResponse::default())
}

async fn seed(
    State(service): State<RouterService>,
    Json(request): Json<RouterSeedRequest>,
) -> Json<RouterSeedResponse> {
    let memory_count = service.seeds.replace(request.memories);
    Json(RouterSeedResponse {
        router_contract_version: request.router_contract_version,
        memory_count,
    })
}

async fn messages(
    State(service): State<RouterService>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    // The one place the reference lever can fire. When it returns None (the
    // default, and whenever the request is not a cheap aside) the original
    // bytes are forwarded unchanged.
    let rewritten = service.reroute.reroute_messages(&body);
    forward(&service, ROUTE_MESSAGES, rewritten, &headers, body).await
}

async fn count_tokens(
    State(service): State<RouterService>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    forward(&service, ROUTE_COUNT_TOKENS, None, &headers, body).await
}

async fn chat_completions(
    State(service): State<RouterService>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    forward(&service, ROUTE_CHAT_COMPLETIONS, None, &headers, body).await
}

async fn responses(
    State(service): State<RouterService>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    forward(&service, ROUTE_RESPONSES, None, &headers, body).await
}

/// Forwards one provider request to the relay and streams the response back.
///
/// `rewritten` carries the lever's output when it fired; otherwise the original
/// `body` bytes are sent verbatim. The response body is streamed chunk-for-
/// chunk, so streaming SSE and the trailing provider `usage` pass through
/// unchanged.
async fn forward(
    service: &RouterService,
    route: &str,
    rewritten: Option<Vec<u8>>,
    headers: &HeaderMap,
    body: Bytes,
) -> Response {
    let Some(relay) = service.relay.as_ref() else {
        return json_error(
            StatusCode::SERVICE_UNAVAILABLE,
            "relay upstream is not configured; set DITTOBENCH_RELAY_BASE_URL and DITTOBENCH_RELAY_TICKET",
        );
    };
    let url = match relay.url_for(route) {
        Ok(url) => url,
        Err(error) => return json_error(StatusCode::INTERNAL_SERVER_ERROR, &error.to_string()),
    };

    let outgoing = rewritten.unwrap_or_else(|| body.to_vec());

    let mut request = relay.client().post(url).bearer_auth(relay.ticket());
    // Forward the content framing and the ingress tap header verbatim; the
    // relay bearer is our ticket, never a provider credential.
    for name in [
        axum::http::header::CONTENT_TYPE.as_str(),
        axum::http::header::ACCEPT.as_str(),
        STEP_HEADER,
    ] {
        if let Some(value) = headers.get(name) {
            request = request.header(name, value.as_bytes());
        }
    }

    match request.body(outgoing).send().await {
        Ok(response) => stream_response(response),
        Err(error) => json_error(
            StatusCode::BAD_GATEWAY,
            &format!("relay request failed: {error}"),
        ),
    }
}

/// Streams an upstream relay response back to the harness unchanged.
fn stream_response(response: reqwest::Response) -> Response {
    let status =
        StatusCode::from_u16(response.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let mut builder = Response::builder().status(status);
    for (name, value) in response.headers() {
        if is_hop_by_hop(name.as_str()) {
            continue;
        }
        if let Ok(header_value) = HeaderValue::from_bytes(value.as_bytes()) {
            builder = builder.header(name.as_str(), header_value);
        }
    }
    match builder.body(Body::from_stream(response.bytes_stream())) {
        Ok(response) => response,
        Err(_) => json_error(StatusCode::BAD_GATEWAY, "failed to build relay response"),
    }
}

/// Hop-by-hop and framing headers that must not be copied onto a re-streamed
/// response (axum re-frames the streamed body itself).
fn is_hop_by_hop(name: &str) -> bool {
    matches!(
        name.to_ascii_lowercase().as_str(),
        "connection"
            | "keep-alive"
            | "proxy-authenticate"
            | "proxy-authorization"
            | "te"
            | "trailer"
            | "transfer-encoding"
            | "upgrade"
            | "content-length"
    )
}

fn json_error(status: StatusCode, message: &str) -> Response {
    (status, Json(serde_json::json!({ "error": message }))).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use axum::http::Request;
    use tower::ServiceExt;

    fn service_without_relay() -> RouterService {
        RouterService::new(None, RerouteConfig::default())
    }

    #[tokio::test]
    async fn health_returns_the_exact_advertisement() {
        let app = router(service_without_relay());
        let response = app
            .oneshot(Request::get("/router/health").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), 4096).await.unwrap();
        let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(
            value,
            serde_json::json!({
                "status": "ok",
                "supported_router_contract_versions": [1],
                "wires": ["anthropic_messages", "openai_chat", "openai_responses"],
                "count_tokens": true
            })
        );
    }

    #[tokio::test]
    async fn seed_accepts_records_and_reports_the_count() {
        let app = router(service_without_relay());
        let response = app
            .oneshot(
                Request::post("/router/seed")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        r#"{"router_contract_version":1,"memories":[
                            {"memory_id":"m1","content":"a"},
                            {"memory_id":"m2","content":"b"}
                        ]}"#,
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), 4096).await.unwrap();
        let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(
            value,
            serde_json::json!({"router_contract_version":1,"memory_count":2})
        );
    }

    #[tokio::test]
    async fn provider_route_without_relay_returns_service_unavailable() {
        let app = router(service_without_relay());
        let response = app
            .oneshot(
                Request::post(ROUTE_MESSAGES)
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"model":"m","max_tokens":8,"messages":[]}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    }
}
