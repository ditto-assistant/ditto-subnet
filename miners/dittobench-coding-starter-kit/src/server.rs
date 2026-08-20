//! `/coding/*` HTTP service and model factories.

use std::sync::Arc;

use axum::extract::{DefaultBodyLimit, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use ditto_harness::{ChatChunk, Model};

use crate::agent::{AgentError, CodingAgent};
use crate::memory::{MemoryError, MemoryRegistry};
use crate::model::{LunaChatModel, ScriptedLunaModel};
use crate::protocol::{
    CodingFinalReport, CodingHealthResponse, CodingRunRequest, CodingRunResponse,
    CodingSeedRequest, CodingSeedResponse,
};
use crate::workspace_client::WorkspaceClient;

#[derive(Debug, thiserror::Error)]
pub enum ServiceError {
    #[error("invalid request: {0}")]
    Invalid(String),
    #[error("case conflict: {0}")]
    Conflict(String),
    #[error("case state unavailable: {0}")]
    State(String),
    #[error("model setup: {0}")]
    Model(String),
    #[error("agent run: {0}")]
    Agent(String),
    #[error("service capacity exhausted: {0}")]
    Capacity(String),
}

impl From<MemoryError> for ServiceError {
    fn from(error: MemoryError) -> Self {
        match error {
            MemoryError::Invalid(message) => Self::Invalid(message),
            MemoryError::Conflict { .. } | MemoryError::AlreadyClaimed => {
                Self::Conflict(error.to_string())
            }
            MemoryError::NotSeeded | MemoryError::Store(_) => Self::State(error.to_string()),
            MemoryError::Capacity => Self::Capacity(error.to_string()),
        }
    }
}

impl From<AgentError> for ServiceError {
    fn from(error: AgentError) -> Self {
        Self::Agent(error.to_string())
    }
}

pub trait ModelFactory: Send + Sync {
    /// Creates one case-scoped model.
    ///
    /// # Errors
    ///
    /// Returns an error when the configured provider cannot satisfy the run
    /// contract.
    fn create(&self, request: &CodingRunRequest) -> Result<Arc<dyn Model>, ServiceError>;
}

#[derive(Debug, Clone, Copy, Default)]
pub struct TicketBrokerModelFactory;

impl ModelFactory for TicketBrokerModelFactory {
    fn create(&self, request: &CodingRunRequest) -> Result<Arc<dyn Model>, ServiceError> {
        let model = LunaChatModel::ticket_broker(
            &request.inference_base_url,
            request.budgets.model_output_tokens.min(32_768),
        )
        .map_err(|error| ServiceError::Model(error.to_string()))?;
        Ok(Arc::new(model))
    }
}

pub struct DirectOpenRouterModelFactory {
    api_key: String,
}

impl DirectOpenRouterModelFactory {
    /// Creates a direct local-practice factory behind an explicit permission.
    ///
    /// # Errors
    ///
    /// Returns an error when permission or the API key is missing.
    pub fn new(api_key: String, explicit_practice_permission: bool) -> Result<Self, ServiceError> {
        if !explicit_practice_permission {
            return Err(ServiceError::Invalid(
                "direct OpenRouter is permitted only by an explicit local-practice flag"
                    .to_string(),
            ));
        }
        if api_key.trim().is_empty() {
            return Err(ServiceError::Invalid(
                "OPENROUTER_API_KEY is required".to_string(),
            ));
        }
        Ok(Self { api_key })
    }
}

impl ModelFactory for DirectOpenRouterModelFactory {
    fn create(&self, request: &CodingRunRequest) -> Result<Arc<dyn Model>, ServiceError> {
        let model = LunaChatModel::direct_openrouter(
            self.api_key.clone(),
            true,
            request.budgets.model_output_tokens.min(32_768),
        )
        .map_err(|error| ServiceError::Model(error.to_string()))?;
        Ok(Arc::new(model))
    }
}

pub struct ScriptedModelFactory {
    script: Vec<ChatChunk>,
}

impl ScriptedModelFactory {
    /// Creates a deterministic local-practice model factory.
    ///
    /// # Errors
    ///
    /// Returns an error when permission is absent or the script is empty.
    pub fn new(
        script: Vec<ChatChunk>,
        explicit_practice_permission: bool,
    ) -> Result<Self, ServiceError> {
        if !explicit_practice_permission {
            return Err(ServiceError::Invalid(
                "scripted model is permitted only by an explicit local-practice flag".to_string(),
            ));
        }
        if script.is_empty() {
            return Err(ServiceError::Invalid(
                "scripted model requires at least one response".to_string(),
            ));
        }
        Ok(Self { script })
    }
}

impl ModelFactory for ScriptedModelFactory {
    fn create(&self, _request: &CodingRunRequest) -> Result<Arc<dyn Model>, ServiceError> {
        Ok(Arc::new(ScriptedLunaModel::new(self.script.clone())))
    }
}

#[derive(Clone)]
pub struct CodingService {
    memory: MemoryRegistry,
    models: Arc<dyn ModelFactory>,
}

impl CodingService {
    #[must_use]
    pub fn new(models: Arc<dyn ModelFactory>) -> Self {
        Self {
            memory: MemoryRegistry::default(),
            models,
        }
    }

    /// Installs one task-scoped memory bundle.
    ///
    /// # Errors
    ///
    /// Returns an error when validation, identity, capacity, or storage fails.
    pub async fn seed(
        &self,
        request: CodingSeedRequest,
    ) -> Result<CodingSeedResponse, ServiceError> {
        self.memory.seed(request).await.map_err(Into::into)
    }

    /// Runs one coding case and destroys its scoped memory state afterward.
    ///
    /// # Errors
    ///
    /// Returns an error when validation, retrieval, model setup, workspace
    /// transport, or agent execution fails.
    pub async fn run(&self, request: CodingRunRequest) -> Result<CodingRunResponse, ServiceError> {
        request.validate().map_err(ServiceError::Invalid)?;
        let query = format!(
            "{}\n{}\n{}",
            request.issue.title,
            request.issue.description,
            request.issue.constraints.join("\n")
        );
        let claim = self
            .memory
            .retrieve(
                &request.ticket_id,
                &request.case_id,
                &request.profile_capability_id,
                &query,
                6,
            )
            .await?;
        let claim_id = claim.claim_id();
        let result = async {
            let model = self.models.create(&request)?;
            let workspace = WorkspaceClient::new(
                request.workspace_capability_url.clone(),
                request.case_id.clone(),
                request.profile_capability_id.clone(),
            )
            .map_err(|error| ServiceError::Invalid(error.to_string()))?;
            let agent = CodingAgent::new(model, workspace.tools());
            let outcome = agent.run(&request, &claim.memories).await?;
            Ok(CodingRunResponse {
                case_id: request.case_id.clone(),
                final_report: CodingFinalReport {
                    summary: outcome.final_text,
                    remaining_risks: Vec::new(),
                },
            })
        }
        .await;
        let _ = self
            .memory
            .finish_claim(&request.ticket_id, &request.case_id, claim_id)
            .await;
        result
    }
}

pub fn router(service: CodingService) -> Router {
    Router::new()
        .route("/coding/health", get(health))
        .route("/coding/seed", post(seed))
        .route("/coding/run", post(run))
        .layer(DefaultBodyLimit::max(4 * 1024 * 1024))
        .with_state(service)
}

async fn health() -> Json<CodingHealthResponse> {
    Json(CodingHealthResponse::default())
}

async fn seed(
    State(service): State<CodingService>,
    Json(request): Json<CodingSeedRequest>,
) -> Result<Json<CodingSeedResponse>, ApiError> {
    service.seed(request).await.map(Json).map_err(ApiError)
}

async fn run(
    State(service): State<CodingService>,
    Json(request): Json<CodingRunRequest>,
) -> Result<Json<CodingRunResponse>, ApiError> {
    service.run(request).await.map(Json).map_err(ApiError)
}

struct ApiError(ServiceError);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let status = match self.0 {
            ServiceError::Invalid(_) => StatusCode::BAD_REQUEST,
            ServiceError::Conflict(_) | ServiceError::State(_) => StatusCode::CONFLICT,
            ServiceError::Capacity(_) => StatusCode::TOO_MANY_REQUESTS,
            ServiceError::Model(_) | ServiceError::Agent(_) => StatusCode::BAD_GATEWAY,
        };
        (
            status,
            Json(serde_json::json!({"error": self.0.to_string()})),
        )
            .into_response()
    }
}

#[cfg(test)]
mod tests {
    use async_trait::async_trait;
    use ditto_harness::{ChatMessage, ToolDefinition};
    use tokio::sync::Notify;

    use super::*;
    use crate::memory::memory_bundle_sha256;
    use crate::protocol::{
        CodingBudgets, CodingIssue, CodingRuntimePolicy, VisibleMemoryRecord,
        CODING_CONTRACT_VERSION,
    };

    #[test]
    fn practice_factories_require_explicit_permission() {
        assert!(ScriptedModelFactory::new(vec![ChatChunk::default()], false).is_err());
        assert!(DirectOpenRouterModelFactory::new("secret".to_string(), false).is_err());
    }

    struct BlockingFactory {
        started: Arc<Notify>,
        release: Arc<Notify>,
    }

    impl ModelFactory for BlockingFactory {
        fn create(&self, _request: &CodingRunRequest) -> Result<Arc<dyn Model>, ServiceError> {
            Ok(Arc::new(BlockingModel {
                started: Arc::clone(&self.started),
                release: Arc::clone(&self.release),
            }))
        }
    }

    struct BlockingModel {
        started: Arc<Notify>,
        release: Arc<Notify>,
    }

    #[async_trait]
    impl Model for BlockingModel {
        async fn next(
            &self,
            _messages: &[ChatMessage],
            _tools: &[ToolDefinition],
        ) -> ditto_harness::Result<ChatChunk> {
            self.started.notify_one();
            self.release.notified().await;
            Ok(ChatChunk {
                text: "finished".to_string(),
                ..ChatChunk::default()
            })
        }
    }

    fn seed_request() -> CodingSeedRequest {
        let mut request = CodingSeedRequest {
            coding_contract_version: CODING_CONTRACT_VERSION,
            ticket_id: "ticket".to_string(),
            case_id: "case".to_string(),
            profile_capability_id: "profile".to_string(),
            memory_bundle_sha256: String::new(),
            memories: vec![VisibleMemoryRecord {
                memory_id: "memory".to_string(),
                repository_capability_id: None,
                fact_group_id: None,
                scope: "profile".to_string(),
                memory_type: "user_workflow".to_string(),
                content: "Prefer a minimal patch.".to_string(),
                valid_from_epoch: None,
                valid_until_epoch: None,
                supersedes: Vec::new(),
                confidence_micros: Some(900_000),
            }],
        };
        request.memory_bundle_sha256 = memory_bundle_sha256(&request).unwrap();
        request
    }

    fn run_request() -> CodingRunRequest {
        CodingRunRequest {
            coding_contract_version: CODING_CONTRACT_VERSION,
            ticket_id: "ticket".to_string(),
            case_id: "case".to_string(),
            profile_capability_id: "profile".to_string(),
            visible_bundle_sha256: "a".repeat(64),
            issue: CodingIssue {
                title: "Fix".to_string(),
                description: "Fix the issue.".to_string(),
                constraints: Vec::new(),
            },
            repository_epoch: "repository-v2".to_string(),
            runtime_policy: CodingRuntimePolicy::default(),
            workspace_capability_url: "http://127.0.0.1:9/tool".to_string(),
            inference_base_url: "http://127.0.0.1:9/v1".to_string(),
            budgets: CodingBudgets {
                model_input_tokens: 10_000,
                model_output_tokens: 1_000,
                workspace_tool_calls: 4,
                wall_time_seconds: 10,
            },
        }
    }

    #[tokio::test]
    async fn rejected_duplicate_run_cannot_remove_claim_owner_state() {
        let started = Arc::new(Notify::new());
        let release = Arc::new(Notify::new());
        let service = CodingService::new(Arc::new(BlockingFactory {
            started: Arc::clone(&started),
            release: Arc::clone(&release),
        }));
        let seed = seed_request();
        assert!(!service.seed(seed.clone()).await.unwrap().idempotent_replay);

        let started_wait = started.notified();
        let first_service = service.clone();
        let first = tokio::spawn(async move { first_service.run(run_request()).await });
        started_wait.await;

        assert!(matches!(
            service.run(run_request()).await,
            Err(ServiceError::Conflict(_))
        ));
        assert!(service.seed(seed).await.unwrap().idempotent_replay);

        release.notify_one();
        assert!(first.await.unwrap().is_ok());
    }
}
