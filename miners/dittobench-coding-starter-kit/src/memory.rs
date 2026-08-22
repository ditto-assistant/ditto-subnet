//! Task-scoped embedded memory storage and retrieval.

use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use ditto_harness::db::{Db, EMBEDDING_DIMS};
use ditto_harness::memory::{SaveMemoryRequest, SearchMemoriesRequest, Store, StoreOptions};
use ditto_harness::{EmbedRequest, EmbedResponse, Embedder};
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::sync::RwLock;

use crate::protocol::{CodingSeedRequest, CodingSeedResponse, VisibleMemoryRecord};

const MAX_ACTIVE_CASES: usize = 32;
const DEFAULT_SEED_TTL: Duration = Duration::from_mins(30);

#[derive(Debug, thiserror::Error)]
pub enum MemoryError {
    #[error("invalid memory seed: {0}")]
    Invalid(String),
    #[error("memory identity conflict for ticket {ticket_id:?}, case {case_id:?}")]
    Conflict { ticket_id: String, case_id: String },
    #[error("memory case was not seeded")]
    NotSeeded,
    #[error("memory case was already claimed for execution")]
    AlreadyClaimed,
    #[error("active memory case limit reached")]
    Capacity,
    #[error("memory store: {0}")]
    Store(String),
}

#[derive(Debug, Clone, PartialEq)]
pub struct RetrievedMemory {
    pub memory_id: String,
    pub content: String,
    pub metadata: Value,
    pub similarity: f64,
}

#[derive(Debug)]
pub struct MemoryClaim {
    pub memories: Vec<RetrievedMemory>,
    claim_id: u64,
}

impl MemoryClaim {
    #[must_use]
    pub fn claim_id(&self) -> u64 {
        self.claim_id
    }
}

#[derive(Debug, Clone, Hash, PartialEq, Eq)]
struct CaseKey {
    ticket_id: String,
    case_id: String,
}

struct CaseMemory {
    profile_capability_id: String,
    memory_bundle_sha256: String,
    store: Store,
    created_at: Instant,
    claim_id: AtomicU64,
}

struct RegistryInner {
    cases: RwLock<HashMap<CaseKey, Arc<CaseMemory>>>,
    next_claim_id: AtomicU64,
    seed_ttl: Duration,
}

#[derive(Clone)]
pub struct MemoryRegistry {
    inner: Arc<RegistryInner>,
}

impl Default for MemoryRegistry {
    fn default() -> Self {
        Self {
            inner: Arc::new(RegistryInner {
                cases: RwLock::new(HashMap::new()),
                next_claim_id: AtomicU64::new(1),
                seed_ttl: DEFAULT_SEED_TTL,
            }),
        }
    }
}

impl MemoryRegistry {
    /// Verifies and installs one idempotent task-scoped memory bundle.
    ///
    /// # Errors
    ///
    /// Returns an error for invalid bytes, a digest/identity conflict, store
    /// failure, or exhausted active-case capacity.
    pub async fn seed(
        &self,
        request: CodingSeedRequest,
    ) -> Result<CodingSeedResponse, MemoryError> {
        request.validate().map_err(MemoryError::Invalid)?;
        self.purge_expired().await;
        let computed = memory_bundle_sha256(&request).map_err(MemoryError::Invalid)?;
        if computed != request.memory_bundle_sha256 {
            return Err(MemoryError::Invalid(format!(
                "memory bundle digest mismatch: declared {}, computed {computed}",
                request.memory_bundle_sha256
            )));
        }

        let key = CaseKey {
            ticket_id: request.ticket_id.clone(),
            case_id: request.case_id.clone(),
        };
        {
            let cases = self.inner.cases.read().await;
            if let Some(existing) = cases.get(&key) {
                if existing.profile_capability_id == request.profile_capability_id
                    && existing.memory_bundle_sha256 == request.memory_bundle_sha256
                {
                    return Ok(CodingSeedResponse {
                        case_id: request.case_id,
                        profile_capability_id: request.profile_capability_id,
                        memory_bundle_sha256: request.memory_bundle_sha256,
                        memory_count: request.memories.len(),
                        idempotent_replay: true,
                    });
                }
                return Err(MemoryError::Conflict {
                    ticket_id: request.ticket_id,
                    case_id: request.case_id,
                });
            }
            if cases.len() >= MAX_ACTIVE_CASES {
                return Err(MemoryError::Capacity);
            }
        }

        let db = Db::open_memory()
            .await
            .map_err(|error| MemoryError::Store(error.to_string()))?;
        let store = Store::new(StoreOptions {
            db: Arc::new(db),
            embedder: Arc::new(StableLexicalEmbedder),
            predictor: None,
            reranker: None,
        });
        for record in &request.memories {
            let metadata = serde_json::to_string(record)
                .map_err(|error| MemoryError::Store(error.to_string()))?;
            store
                .save_memory(SaveMemoryRequest {
                    user_id: request.profile_capability_id.clone(),
                    id: record.memory_id.clone(),
                    title: record.memory_type.clone(),
                    prompt: record.content.clone(),
                    source: "coding_seed_v1".to_string(),
                    source_context: metadata,
                    ..SaveMemoryRequest::default()
                })
                .await
                .map_err(|error| MemoryError::Store(error.to_string()))?;
        }
        let entry = Arc::new(CaseMemory {
            profile_capability_id: request.profile_capability_id.clone(),
            memory_bundle_sha256: request.memory_bundle_sha256.clone(),
            store,
            created_at: Instant::now(),
            claim_id: AtomicU64::new(0),
        });
        let mut cases = self.inner.cases.write().await;
        if let Some(existing) = cases.get(&key) {
            if existing.profile_capability_id == request.profile_capability_id
                && existing.memory_bundle_sha256 == request.memory_bundle_sha256
            {
                return Ok(CodingSeedResponse {
                    case_id: request.case_id,
                    profile_capability_id: request.profile_capability_id,
                    memory_bundle_sha256: request.memory_bundle_sha256,
                    memory_count: request.memories.len(),
                    idempotent_replay: true,
                });
            }
            return Err(MemoryError::Conflict {
                ticket_id: request.ticket_id,
                case_id: request.case_id,
            });
        }
        if cases.len() >= MAX_ACTIVE_CASES {
            return Err(MemoryError::Capacity);
        }
        cases.insert(key, entry);
        Ok(CodingSeedResponse {
            case_id: request.case_id,
            profile_capability_id: request.profile_capability_id,
            memory_bundle_sha256: request.memory_bundle_sha256,
            memory_count: request.memories.len(),
            idempotent_replay: false,
        })
    }

    /// Atomically claims and retrieves the exact seeded profile capability.
    ///
    /// # Errors
    ///
    /// Returns an error when the case is absent, the profile conflicts, or
    /// embedding/database retrieval fails.
    pub async fn retrieve(
        &self,
        ticket_id: &str,
        case_id: &str,
        profile_capability_id: &str,
        query: &str,
        limit: usize,
    ) -> Result<MemoryClaim, MemoryError> {
        self.purge_expired().await;
        let key = CaseKey {
            ticket_id: ticket_id.to_string(),
            case_id: case_id.to_string(),
        };
        let entry = self
            .inner
            .cases
            .read()
            .await
            .get(&key)
            .cloned()
            .ok_or(MemoryError::NotSeeded)?;
        if entry.profile_capability_id != profile_capability_id {
            return Err(MemoryError::Conflict {
                ticket_id: ticket_id.to_string(),
                case_id: case_id.to_string(),
            });
        }
        let claim_id = self.inner.next_claim_id.fetch_add(1, Ordering::SeqCst);
        if claim_id == 0 {
            return Err(MemoryError::Capacity);
        }
        entry
            .claim_id
            .compare_exchange(0, claim_id, Ordering::SeqCst, Ordering::SeqCst)
            .map_err(|_| MemoryError::AlreadyClaimed)?;
        let result = entry
            .store
            .search_memories(SearchMemoriesRequest {
                user_id: profile_capability_id.to_string(),
                queries: vec![query.to_string()],
                limit: limit.clamp(1, 16),
                min_similarity: 0.01,
                ..SearchMemoriesRequest::default()
            })
            .await
            .map_err(|error| MemoryError::Store(error.to_string()))
            .and_then(|memories| {
                memories
                    .into_iter()
                    .map(|memory| {
                        let metadata = serde_json::from_str(&memory.source_context)
                            .map_err(|error| MemoryError::Store(error.to_string()))?;
                        Ok(RetrievedMemory {
                            memory_id: memory.id,
                            content: memory.prompt,
                            metadata,
                            similarity: memory.similarity,
                        })
                    })
                    .collect()
            });
        match result {
            Ok(memories) => Ok(MemoryClaim { memories, claim_id }),
            Err(error) => {
                let _ = entry.claim_id.compare_exchange(
                    claim_id,
                    0,
                    Ordering::SeqCst,
                    Ordering::SeqCst,
                );
                Err(error)
            }
        }
    }

    /// Removes a case only when `claim_id` still owns its execution claim.
    #[must_use]
    pub async fn finish_claim(&self, ticket_id: &str, case_id: &str, claim_id: u64) -> bool {
        let key = CaseKey {
            ticket_id: ticket_id.to_string(),
            case_id: case_id.to_string(),
        };
        let mut cases = self.inner.cases.write().await;
        let owns_claim = cases
            .get(&key)
            .is_some_and(|entry| entry.claim_id.load(Ordering::SeqCst) == claim_id);
        if owns_claim {
            cases.remove(&key);
        }
        owns_claim
    }

    async fn purge_expired(&self) {
        let ttl = self.inner.seed_ttl;
        self.inner.cases.write().await.retain(|_, entry| {
            entry.claim_id.load(Ordering::SeqCst) != 0 || entry.created_at.elapsed() < ttl
        });
    }

    #[cfg(test)]
    async fn active_count(&self) -> usize {
        self.inner.cases.read().await.len()
    }

    #[cfg(test)]
    fn with_seed_ttl(seed_ttl: Duration) -> Self {
        Self {
            inner: Arc::new(RegistryInner {
                cases: RwLock::new(HashMap::new()),
                next_claim_id: AtomicU64::new(1),
                seed_ttl,
            }),
        }
    }
}

/// Deterministic, dependency-free retrieval embedding for public practice.
/// It is deliberately a reference baseline, not a production quality model.
#[derive(Debug, Clone, Copy)]
pub struct StableLexicalEmbedder;

#[async_trait]
impl Embedder for StableLexicalEmbedder {
    async fn embed(&self, request: EmbedRequest) -> ditto_harness::Result<EmbedResponse> {
        Ok(EmbedResponse {
            embeddings: request
                .texts
                .iter()
                .map(|text| stable_lexical_embedding(text))
                .collect(),
            ..EmbedResponse::default()
        })
    }
}

#[must_use]
#[allow(clippy::cast_possible_truncation)]
pub fn stable_lexical_embedding(text: &str) -> Vec<f32> {
    let words: Vec<String> = text
        .split(|character: char| !character.is_alphanumeric())
        .filter(|word| !word.is_empty())
        .map(str::to_lowercase)
        .collect();
    let mut features = words.clone();
    features.extend(
        words
            .windows(2)
            .map(|pair| format!("{}:{}", pair[0], pair[1])),
    );
    let mut vector = vec![0.0_f32; EMBEDDING_DIMS];
    for feature in features {
        let digest = Sha256::digest(feature.as_bytes());
        let index = (u16::from_be_bytes([digest[0], digest[1]]) as usize) % EMBEDDING_DIMS;
        let sign = if digest[2] & 1 == 0 { 1.0 } else { -1.0 };
        vector[index] += sign;
    }
    let norm = vector
        .iter()
        .map(|value| f64::from(*value) * f64::from(*value))
        .sum::<f64>()
        .sqrt();
    if norm == 0.0 {
        vector[0] = 1.0;
    } else {
        for value in &mut vector {
            *value /= norm as f32;
        }
    }
    vector
}

/// Computes the canonical visible-memory artifact digest.
///
/// # Errors
///
/// Returns an error only if the typed projection cannot be serialized.
pub fn memory_bundle_sha256(request: &CodingSeedRequest) -> Result<String, String> {
    #[derive(Serialize)]
    struct Projection<'a> {
        memories: &'a [VisibleMemoryRecord],
    }
    let value = serde_json::to_value(Projection {
        memories: &request.memories,
    })
    .map_err(|error| error.to_string())?;
    let serialized =
        serde_json::to_string(&canonicalize(value)).map_err(|error| error.to_string())?;
    let mut bytes = serialized
        .replace('\u{2028}', "\\u2028")
        .replace('\u{2029}', "\\u2029")
        .into_bytes();
    bytes.push(b'\n');
    Ok(hex_sha256(&bytes))
}

fn canonicalize(value: Value) -> Value {
    match value {
        Value::Object(object) => Value::Object(
            object
                .into_iter()
                .map(|(key, value)| (key, canonicalize(value)))
                .collect::<BTreeMap<_, _>>()
                .into_iter()
                .collect(),
        ),
        Value::Array(values) => Value::Array(values.into_iter().map(canonicalize).collect()),
        other => other,
    }
}

fn hex_sha256(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut output = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        let _ = write!(output, "{byte:02x}");
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::CODING_CONTRACT_VERSION;

    fn seed_request(profile: &str, content: &str) -> CodingSeedRequest {
        let mut request = CodingSeedRequest {
            coding_contract_version: CODING_CONTRACT_VERSION,
            ticket_id: "ticket-1".to_string(),
            case_id: "case-1".to_string(),
            profile_capability_id: profile.to_string(),
            memory_bundle_sha256: "0".repeat(64),
            memories: vec![VisibleMemoryRecord {
                memory_id: "memory-1".to_string(),
                repository_capability_id: Some("repo-1".to_string()),
                fact_group_id: None,
                scope: "module".to_string(),
                memory_type: "previous_bug_fix".to_string(),
                content: content.to_string(),
                valid_from_epoch: Some("repo-v2".to_string()),
                valid_until_epoch: None,
                supersedes: Vec::new(),
                confidence_micros: 900_000,
            }],
        };
        request.memory_bundle_sha256 = memory_bundle_sha256(&request).unwrap();
        request
    }

    #[test]
    fn lexical_embedding_is_stable_and_768_dimensional() {
        let first = stable_lexical_embedding("Opaque identifiers remain strings");
        let second = stable_lexical_embedding("Opaque identifiers remain strings");
        assert_eq!(first.len(), EMBEDDING_DIMS);
        assert_eq!(first, second);
    }

    #[test]
    fn memory_bundle_digest_matches_canonical_memories_artifact() {
        let request = seed_request("profile-1", "Opaque identifiers remain strings");
        assert_eq!(
            memory_bundle_sha256(&request).unwrap(),
            "9e687d90e477b3318369c312091894d94c50bbae60de3d10be2fff57f8daa4f9"
        );
        let mut rebound = request;
        rebound.ticket_id = "another-ticket".to_string();
        rebound.case_id = "another-case".to_string();
        rebound.profile_capability_id = "another-profile".to_string();
        assert_eq!(
            memory_bundle_sha256(&rebound).unwrap(),
            "9e687d90e477b3318369c312091894d94c50bbae60de3d10be2fff57f8daa4f9"
        );
    }

    #[test]
    fn public_practice_digest_matches_python_projection() {
        const MEMORIES: &str = include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../research/dittobench-coding-datagen/practice/v1/agent/memories.jsonl"
        ));
        let memories = MEMORIES
            .lines()
            .map(|line| serde_json::from_str::<Value>(line).unwrap())
            .filter(|record| record["owner_user_id"] == "P01")
            .map(|record| {
                let repository = record["repository_id"].as_str().map(str::to_string);
                VisibleMemoryRecord {
                    memory_id: record["memory_id"].as_str().unwrap().to_string(),
                    repository_capability_id: repository.clone(),
                    fact_group_id: None,
                    scope: if repository.is_some() {
                        "repository".to_string()
                    } else {
                        "profile".to_string()
                    },
                    memory_type: if repository.is_some() {
                        "project_experience".to_string()
                    } else {
                        "user_workflow".to_string()
                    },
                    content: record["content"].as_str().unwrap().to_string(),
                    valid_from_epoch: record["valid_from_revision"].as_str().map(str::to_string),
                    valid_until_epoch: record["valid_until_revision"].as_str().map(str::to_string),
                    supersedes: serde_json::from_value(record["supersedes"].clone()).unwrap(),
                    confidence_micros: 900_000,
                }
            })
            .collect::<Vec<_>>();
        assert_eq!(memories.len(), 6);
        let request = CodingSeedRequest {
            coding_contract_version: CODING_CONTRACT_VERSION,
            ticket_id: "identity-is-not-part-of-the-artifact".to_string(),
            case_id: "PRACTICE-LEDGER-001".to_string(),
            profile_capability_id: "P01".to_string(),
            memory_bundle_sha256: String::new(),
            memories,
        };
        assert_eq!(
            memory_bundle_sha256(&request).unwrap(),
            "c8753c1183bc4a05d7e26e268e1670438a111b2ac6419a6bbbec0491a8df6a37"
        );
    }

    #[test]
    fn shared_memory_vectors_match_python_canonical_bytes() {
        const VECTOR: &str = include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../packages/dittobench-coding-contract/testdata/coding_memory_v1.json"
        ));
        let vector: Value = serde_json::from_str(VECTOR).unwrap();
        let memory: VisibleMemoryRecord = serde_json::from_value(vector["memory"].clone()).unwrap();
        let mut request = CodingSeedRequest {
            coding_contract_version: CODING_CONTRACT_VERSION,
            ticket_id: "vector-ticket".to_string(),
            case_id: "vector-case".to_string(),
            profile_capability_id: "vector-profile".to_string(),
            memory_bundle_sha256: String::new(),
            memories: vec![memory],
        };
        assert_eq!(
            memory_bundle_sha256(&request).unwrap(),
            vector["digests"]["ascii"].as_str().unwrap()
        );
        request.memories[0].content = vector["unicode_content"].as_str().unwrap().to_string();
        assert_eq!(
            memory_bundle_sha256(&request).unwrap(),
            vector["digests"]["unicode"].as_str().unwrap()
        );
    }

    #[test]
    fn shared_contract_seed_and_unicode_digest_match_python_and_go() {
        const CONTRACT: &str = include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../packages/dittobench-coding-contract/testdata/coding_contract_v1.json"
        ));
        let contract: Value = serde_json::from_str(CONTRACT).unwrap();
        let seed: CodingSeedRequest =
            serde_json::from_value(contract["seed_request"].clone()).unwrap();
        seed.validate().unwrap();
        assert_eq!(
            memory_bundle_sha256(&seed).unwrap(),
            seed.memory_bundle_sha256
        );
        let run: crate::protocol::CodingRunRequest =
            serde_json::from_value(contract["run_request"].clone()).unwrap();
        run.validate().unwrap();

        let mut unicode_seed = seed;
        unicode_seed.memories[0].content =
            "Preserve café <tag> & separators \u{2028} and \u{2029}.".to_string();
        assert_eq!(
            memory_bundle_sha256(&unicode_seed).unwrap(),
            contract["digests"]["unicode_seed_memory"].as_str().unwrap()
        );
    }

    #[tokio::test]
    async fn seed_is_idempotent_and_profile_scoped() {
        let registry = MemoryRegistry::default();
        let request = seed_request("profile-1", "Opaque identifiers remain strings");
        assert!(
            !registry
                .seed(request.clone())
                .await
                .unwrap()
                .idempotent_replay
        );
        assert!(registry.seed(request).await.unwrap().idempotent_replay);
        let found = registry
            .retrieve(
                "ticket-1",
                "case-1",
                "profile-1",
                "preserve opaque identifier strings",
                4,
            )
            .await
            .unwrap();
        assert_eq!(found.memories[0].memory_id, "memory-1");
        assert!(matches!(
            registry
                .retrieve(
                    "ticket-1",
                    "case-1",
                    "profile-1",
                    "preserve opaque identifier strings",
                    4,
                )
                .await,
            Err(MemoryError::AlreadyClaimed)
        ));
        assert!(matches!(
            registry
                .retrieve("ticket-1", "case-1", "profile-2", "opaque", 4)
                .await,
            Err(MemoryError::Conflict { .. })
        ));
        assert!(
            registry
                .finish_claim("ticket-1", "case-1", found.claim_id())
                .await
        );
        assert_eq!(registry.active_count().await, 0);
    }

    #[tokio::test]
    async fn concurrent_duplicate_claim_cannot_remove_owner_state() {
        let registry = MemoryRegistry::default();
        registry
            .seed(seed_request("profile-1", "opaque identifiers"))
            .await
            .unwrap();
        let (left, right) = tokio::join!(
            registry.retrieve("ticket-1", "case-1", "profile-1", "opaque", 4),
            registry.retrieve("ticket-1", "case-1", "profile-1", "opaque", 4)
        );
        let (claim, rejected) = match (left, right) {
            (Ok(claim), rejected) | (rejected, Ok(claim)) => (claim, rejected),
            other => panic!("expected exactly one claim owner, got {other:?}"),
        };
        assert!(matches!(rejected, Err(MemoryError::AlreadyClaimed)));
        assert!(
            !registry
                .finish_claim("ticket-1", "case-1", claim.claim_id() + 1)
                .await
        );
        assert_eq!(registry.active_count().await, 1);
        assert!(
            registry
                .finish_claim("ticket-1", "case-1", claim.claim_id())
                .await
        );
        assert_eq!(registry.active_count().await, 0);
    }

    #[tokio::test]
    async fn seed_only_cases_expire_during_next_registry_operation() {
        let registry = MemoryRegistry::with_seed_ttl(Duration::ZERO);
        registry
            .seed(seed_request("profile-1", "first"))
            .await
            .unwrap();
        let mut second = seed_request("profile-2", "second");
        second.ticket_id = "ticket-2".to_string();
        second.case_id = "case-2".to_string();
        registry.seed(second).await.unwrap();
        assert_eq!(registry.active_count().await, 1);
    }

    #[tokio::test]
    async fn changed_seed_for_same_case_is_rejected() {
        let registry = MemoryRegistry::default();
        registry
            .seed(seed_request("profile-1", "first"))
            .await
            .unwrap();
        assert!(matches!(
            registry.seed(seed_request("profile-1", "second")).await,
            Err(MemoryError::Conflict { .. })
        ));
    }
}
