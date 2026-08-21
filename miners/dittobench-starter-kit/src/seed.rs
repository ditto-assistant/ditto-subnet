//! Self-contained LongMemEval seed user — a fixed dummy user whose memories
//! have been run through subject sync (subjects pre-generated + organized),
//! bulk-loaded into the local Turso vector DB and ready for retrieval. This is
//! the "fresh dummy user to experiment with" the kit ships.
//!
//! The fixtures under `fixtures/seed-user/` are a coherent, type-balanced slice
//! of the LongMemEval `dittobench_lme_fixture` (see `scripts/build-seed-user.py`):
//! conversation pairs, the subjects those pairs link to, and the subject↔pair
//! graph. The original production subject EMBEDDINGS are intentionally dropped;
//! we recompute embeddings at load time with the kit's embedder so pairs,
//! subjects, and queries share one vector space.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use async_trait::async_trait;
use chrono::{DateTime, Utc};
use ditto_harness::memory::{SaveMemoryRequest, Store, SubjectInput};
use ditto_harness::types::{
    EmbedRequest, EmbedResponse, Embedder, Error as HarnessError, Result as HarnessResult,
};
use serde::{Deserialize, Serialize};
use tokio::sync::{Mutex, RwLock};

use crate::baseline::USER_ID;

const PAIRS_JSON: &str = include_str!("../fixtures/seed-user/pairs.json");
const SUBJECTS_JSON: &str = include_str!("../fixtures/seed-user/subjects.json");
const LINKS_JSON: &str = include_str!("../fixtures/seed-user/subject_links.json");
const MEMORY_CASES_JSON: &str = include_str!("../fixtures/seed-user/memory_cases.json");
// The validator broker and Platform route both admit at most 256 inputs. Keep
// the starter harness on that reviewed boundary even though direct Perplexity
// currently accepts a larger batch.
const SEED_EMBED_BATCH_SIZE: usize = 256;
// Perplexity caps a request at 120k combined tokens. Since a token cannot
// contain less than one UTF-8 byte, 96 KiB of JSON-encoded input strings stays
// below that boundary and the broker/Platform 1 MiB body limits with ample room
// for the envelope. A single larger input is still sent alone because splitting
// it would change the embedding.
const SEED_EMBED_BATCH_JSON_BYTES: usize = 96 << 10;

tokio::task_local! {
    /// Prevents unrelated `/run` tasks from reading the temporary seed cache
    /// while a seed request is in flight on the shared Store.
    static SEED_CACHE_ACTIVE: bool;
}

/// Short-lived read-through cache used only while one `/seed` wave is loaded.
///
/// `Store::save_memory` embeds one pair and then its subjects. Preloading those
/// exact strings lets the provider do the expensive work in bounded batches
/// while retaining the ordinary Store write/upsert path. The cache is cleared
/// before the seed response returns, so later benchmark queries remain real,
/// metered embedding requests and one run cannot retain another wave's text.
pub struct SeedBatchEmbedder {
    inner: Arc<dyn Embedder>,
    cache: RwLock<HashMap<String, Vec<f32>>>,
    seed_lock: Mutex<()>,
}

impl SeedBatchEmbedder {
    pub fn new(inner: Arc<dyn Embedder>) -> Self {
        Self {
            inner,
            cache: RwLock::new(HashMap::new()),
            seed_lock: Mutex::new(()),
        }
    }

    async fn prefetch(&self, texts: &[String]) -> HarnessResult<()> {
        // A failed provider call must not leave vectors from an earlier wave
        // eligible for read-through.
        self.clear().await;
        let mut seen = HashSet::new();
        let unique: Vec<String> = texts
            .iter()
            .filter(|text| !text.trim().is_empty())
            .filter(|text| seen.insert((*text).clone()))
            .cloned()
            .collect();
        let mut loaded = HashMap::with_capacity(unique.len());
        for chunk in seed_embedding_batches(unique)? {
            let response = self
                .inner
                .embed(EmbedRequest {
                    texts: chunk.clone(),
                })
                .await?;
            if response.embeddings.len() != chunk.len() {
                return Err(HarnessError::Embedding(format!(
                    "seed embedding batch returned {} vectors for {} inputs",
                    response.embeddings.len(),
                    chunk.len()
                )));
            }
            for (text, embedding) in chunk.into_iter().zip(response.embeddings) {
                loaded.insert(text, embedding);
            }
        }
        *self.cache.write().await = loaded;
        Ok(())
    }

    async fn clear(&self) {
        self.cache.write().await.clear();
    }
}

fn seed_embedding_batches(unique: Vec<String>) -> HarnessResult<Vec<Vec<String>>> {
    let mut batches: Vec<Vec<String>> = Vec::new();
    let mut batch: Vec<String> = Vec::new();
    let mut batch_json_bytes = 2; // Array brackets.
    for text in unique {
        let text_json_bytes = serde_json::to_vec(&text)
            .map_err(|error| HarnessError::Embedding(format!("encode seed input: {error}")))?
            .len()
            + usize::from(!batch.is_empty()); // Array comma.
        if !batch.is_empty()
            && (batch.len() == SEED_EMBED_BATCH_SIZE
                || batch_json_bytes + text_json_bytes > SEED_EMBED_BATCH_JSON_BYTES)
        {
            batches.push(std::mem::take(&mut batch));
            batch_json_bytes = 2;
        }
        batch_json_bytes += text_json_bytes;
        batch.push(text);
    }
    if !batch.is_empty() {
        batches.push(batch);
    }
    Ok(batches)
}

#[async_trait]
impl Embedder for SeedBatchEmbedder {
    async fn embed(&self, req: EmbedRequest) -> HarnessResult<EmbedResponse> {
        if !SEED_CACHE_ACTIVE
            .try_with(|active| *active)
            .unwrap_or(false)
        {
            return self.inner.embed(req).await;
        }
        let cache = self.cache.read().await;
        let cached: Option<Vec<Vec<f32>>> = req
            .texts
            .iter()
            .map(|text| cache.get(text).cloned())
            .collect();
        drop(cache);
        if let Some(embeddings) = cached {
            return Ok(EmbedResponse {
                embeddings,
                ..EmbedResponse::default()
            });
        }
        self.inner.embed(req).await
    }
}

#[derive(Deserialize)]
pub struct Pair {
    pub pair_id: String,
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub timestamp: String,
    pub prompt: String,
    pub response: String,
}

#[derive(Deserialize)]
pub struct Subject {
    pub id: String,
    pub subject_text: String,
    #[serde(default)]
    pub description_text: String,
}

#[derive(Deserialize)]
pub struct Link {
    pub subject_id: String,
    pub pair_id: String,
}

/// A LongMemEval memory question for the practice run.
#[derive(Deserialize, Clone)]
pub struct MemoryCase {
    pub question_id: String,
    #[serde(default)]
    pub question_type: String,
    pub query: String,
    /// Expected answer — LongMemEval stores some as numbers, so keep it as a
    /// raw JSON value; use [`MemoryCase::answer_text`] for the plain-text
    /// answer used by the deterministic grader.
    #[serde(default)]
    pub answer: serde_json::Value,
    #[serde(default)]
    pub answer_pair_ids: Vec<String>,
}

impl MemoryCase {
    /// The expected answer as a plain string (numbers rendered without quotes).
    pub fn answer_text(&self) -> String {
        match &self.answer {
            serde_json::Value::String(s) => s.clone(),
            serde_json::Value::Null => String::new(),
            other => other.to_string(),
        }
    }
}

/// The bundled memory questions (real LongMemEval Q/A over the seed user).
pub fn memory_cases() -> Vec<MemoryCase> {
    serde_json::from_str(MEMORY_CASES_JSON).expect("parse bundled memory_cases.json")
}

/// Outcome of loading the seed user.
pub struct SeedStats {
    pub pairs: usize,
    pub subjects: usize,
    pub links: usize,
}

/// Loads the bundled seed user into `store` under [`USER_ID`]. Each pair is
/// saved (embedding `prompt\nresponse`) with its linked subjects (each embedded
/// + linked) — the same `save_memory` path production uses to build the subject
///   graph. Idempotent: upserts on `(user, pair_id)` and `(user, kg, subject_text)`,
///   so re-running refreshes rather than duplicates.
pub async fn load_seed_user(store: &Store) -> anyhow::Result<SeedStats> {
    let pairs: Vec<Pair> = serde_json::from_str(PAIRS_JSON)?;
    let subjects: Vec<Subject> = serde_json::from_str(SUBJECTS_JSON)?;
    let links: Vec<Link> = serde_json::from_str(LINKS_JSON)?;
    load_haystack(store, USER_ID, &pairs, &subjects, &links, true).await
}

/// Batched variant used by the reference harness. The public unbatched helper
/// above stays available to custom stores that do not use [`SeedBatchEmbedder`].
pub async fn load_seed_user_batched(
    store: &Store,
    embedder: &SeedBatchEmbedder,
) -> anyhow::Result<SeedStats> {
    let pairs: Vec<Pair> = serde_json::from_str(PAIRS_JSON)?;
    let subjects: Vec<Subject> = serde_json::from_str(SUBJECTS_JSON)?;
    let links: Vec<Link> = serde_json::from_str(LINKS_JSON)?;
    load_haystack_batched(store, embedder, USER_ID, &pairs, &subjects, &links, true).await
}

fn memory_embedding_text(pair: &Pair) -> String {
    // Keep byte-identical with ditto-harness Store::save_memory, whose summary
    // is empty on this seed path.
    format!("{}\n{}\n", pair.prompt, pair.response)
        .trim()
        .to_string()
}

fn subject_embedding_text(subject: &Subject) -> String {
    format!("{}\n{}", subject.subject_text, subject.description_text)
        .trim()
        .to_string()
}

fn seed_embedding_inputs(pairs: &[Pair], subjects: &[Subject], links: &[Link]) -> Vec<String> {
    let pair_ids: HashSet<&str> = pairs.iter().map(|pair| pair.pair_id.as_str()).collect();
    let linked_subject_ids: HashSet<&str> = links
        .iter()
        .filter(|link| pair_ids.contains(link.pair_id.as_str()))
        .map(|link| link.subject_id.as_str())
        .collect();
    pairs
        .iter()
        .map(memory_embedding_text)
        .chain(
            subjects
                .iter()
                .filter(|subject| linked_subject_ids.contains(subject.id.as_str()))
                .map(subject_embedding_text),
        )
        .collect()
}

async fn load_haystack_batched(
    store: &Store,
    embedder: &SeedBatchEmbedder,
    user_id: &str,
    pairs: &[Pair],
    subjects: &[Subject],
    links: &[Link],
    log_progress: bool,
) -> anyhow::Result<SeedStats> {
    // The wrapper is shared by the HTTP server. Keep each seed wave's cache
    // lifecycle atomic even if a client submits overlapping `/seed` calls.
    let _seed_guard = embedder.seed_lock.lock().await;
    embedder
        .prefetch(&seed_embedding_inputs(pairs, subjects, links))
        .await
        .map_err(|error| anyhow::anyhow!("prefetch seed embeddings: {error}"))?;
    let result = SEED_CACHE_ACTIVE
        .scope(
            true,
            load_haystack(store, user_id, pairs, subjects, links, log_progress),
        )
        .await;
    embedder.clear().await;
    result
}

/// Shared loader used by both the bundled seed user and the `/seed` endpoint.
/// Saves each pair via `save_memory` (embedding `prompt\nresponse`) with the
/// subjects linked to it, so embeddings + the subject graph are rebuilt. The
/// save path upserts on `(user, pair_id)` and `(user, kg, subject_text)`, so
/// re-seeding a haystack refreshes rather than duplicates (idempotent).
async fn load_haystack(
    store: &Store,
    user_id: &str,
    pairs: &[Pair],
    subjects: &[Subject],
    links: &[Link],
    log_progress: bool,
) -> anyhow::Result<SeedStats> {
    let subj_by_id: HashMap<&str, &Subject> = subjects.iter().map(|s| (s.id.as_str(), s)).collect();
    let mut subs_by_pair: HashMap<&str, Vec<&Subject>> = HashMap::new();
    for l in links {
        if let Some(s) = subj_by_id.get(l.subject_id.as_str()) {
            subs_by_pair.entry(l.pair_id.as_str()).or_default().push(s);
        }
    }

    let total = pairs.len();
    for (i, p) in pairs.iter().enumerate() {
        let timestamp: Option<DateTime<Utc>> = DateTime::parse_from_rfc3339(&p.timestamp)
            .ok()
            .map(|t| t.with_timezone(&Utc));
        let subjects_in: Vec<SubjectInput> = subs_by_pair
            .get(p.pair_id.as_str())
            .map(|v| {
                v.iter()
                    .map(|s| SubjectInput {
                        text: s.subject_text.clone(),
                        description: s.description_text.clone(),
                        key: false,
                    })
                    .collect()
            })
            .unwrap_or_default();

        store
            .save_memory(SaveMemoryRequest {
                user_id: user_id.to_string(),
                id: p.pair_id.clone(),
                session_id: p.session_id.clone(),
                prompt: p.prompt.clone(),
                response: p.response.clone(),
                source: "seed".to_string(),
                timestamp,
                subjects: subjects_in,
                ..Default::default()
            })
            .await
            .map_err(|e| anyhow::anyhow!("save_memory {}: {e}", p.pair_id))?;

        if log_progress && ((i + 1) % 50 == 0 || i + 1 == total) {
            eprintln!("  seeded {}/{} pairs", i + 1, total);
        }
    }

    Ok(SeedStats {
        pairs: total,
        subjects: subjects.len(),
        links: links.len(),
    })
}

// ---------------------------------------------------------------------------
// `/seed` endpoint wire contract — a fresh memory haystack pushed by the
// validator before it asks memory questions.
// ---------------------------------------------------------------------------

/// Request body for the harness `POST /seed` route (snake_case). The validator
/// sends a fresh haystack: conversation pairs, the subjects, and the
/// subject↔pair links. `user_id` defaults to the kit [`USER_ID`].
///
/// DittoBench v8 seeding modes:
/// - **Prepared** — pairs + subjects + links.
/// - **Raw pairs** — pairs only (`subjects: []`, `links: []`): the validator seeds
///   raw conversation pairs and expects YOUR harness to build its own subject
///   index. `seed_from_request` runs the same `save_memory` path either way, so
///   a harness that constructs subjects from pairs answers raw-pair questions a
///   prepared-subjects-only harness cannot — this is where miners can win.
/// - **Staged waves** — `/seed` is called repeatedly, each carrying the
///   next chunk with an incremented `wave`, interleaved with `/run`. Seeding is
///   an idempotent upsert, so accepting `wave` and merging is all that's needed.
#[derive(Deserialize)]
pub struct SeedRequest {
    #[serde(default)]
    pub user_id: Option<String>,
    /// 0-based staged-seeding wave. Advisory: seeding upserts, so a
    /// harness can ignore this and simply merge each call. `i32` to match the
    /// wire protocol's other counters (e.g. `ObservedToolCall::hop`).
    #[serde(default)]
    pub wave: i32,
    #[serde(default)]
    pub pairs: Vec<Pair>,
    #[serde(default)]
    pub subjects: Vec<Subject>,
    #[serde(default)]
    pub links: Vec<Link>,
}

/// Response body for `POST /seed`.
#[derive(Serialize)]
pub struct SeedResponse {
    pub pairs: usize,
    pub subjects: usize,
    pub links: usize,
}

/// Loads a validator-provided haystack into `store`. Reuses the same
/// `save_memory` path as [`load_seed_user`] (per pair, with its linked
/// subjects), so embeddings + the subject graph are built and the operation is
/// idempotent (upserts). The validator calls this to install a FRESH haystack
/// before asking memory questions.
pub async fn seed_from_request(store: &Store, req: SeedRequest) -> anyhow::Result<SeedResponse> {
    let user_id = req
        .user_id
        .as_deref()
        .filter(|s| !s.is_empty())
        .unwrap_or(USER_ID);
    let stats = load_haystack(store, user_id, &req.pairs, &req.subjects, &req.links, false).await?;
    Ok(SeedResponse {
        pairs: stats.pairs,
        subjects: stats.subjects,
        links: stats.links,
    })
}

/// Loads one validator seed wave after pre-embedding its exact Store inputs in
/// provider-sized batches. Provider usage remains on this ticket; only the
/// number of HTTP round trips changes.
pub async fn seed_from_request_batched(
    store: &Store,
    embedder: &SeedBatchEmbedder,
    req: SeedRequest,
) -> anyhow::Result<SeedResponse> {
    let user_id = req
        .user_id
        .as_deref()
        .filter(|s| !s.is_empty())
        .unwrap_or(USER_ID);
    let stats = load_haystack_batched(
        store,
        embedder,
        user_id,
        &req.pairs,
        &req.subjects,
        &req.links,
        false,
    )
    .await?;
    Ok(SeedResponse {
        pairs: stats.pairs,
        subjects: stats.subjects,
        links: stats.links,
    })
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex as StdMutex;

    use ditto_harness::db::Db;
    use ditto_harness::memory::StoreOptions;

    use super::*;

    #[derive(Default)]
    struct RecordingEmbedder {
        calls: StdMutex<Vec<Vec<String>>>,
    }

    #[async_trait]
    impl Embedder for RecordingEmbedder {
        async fn embed(&self, req: EmbedRequest) -> HarnessResult<EmbedResponse> {
            self.calls
                .lock()
                .expect("lock calls")
                .push(req.texts.clone());
            Ok(EmbedResponse {
                embeddings: req
                    .texts
                    .iter()
                    .map(|text| vec![text.len() as f32; 768])
                    .collect(),
                ..EmbedResponse::default()
            })
        }
    }

    #[tokio::test]
    async fn seed_embedder_prefetches_bounded_unique_batches_and_clears() {
        let inner = Arc::new(RecordingEmbedder::default());
        let embedder = SeedBatchEmbedder::new(inner.clone());
        let mut texts: Vec<String> = (0..258).map(|index| format!("text-{index}")).collect();
        texts.push("text-0".to_string());

        embedder.prefetch(&texts).await.expect("prefetch");
        let calls = inner.calls.lock().expect("lock calls").clone();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].len(), SEED_EMBED_BATCH_SIZE);
        assert_eq!(calls[1].len(), 2);

        let uncached = embedder
            .embed(EmbedRequest {
                texts: vec!["text-257".to_string(), "text-0".to_string()],
            })
            .await
            .expect("embedding outside seed scope");
        assert_eq!(uncached.embeddings[0], vec![8.0; 768]);
        assert_eq!(uncached.embeddings[1], vec![6.0; 768]);
        assert_eq!(inner.calls.lock().expect("lock calls").len(), 3);

        let cached = SEED_CACHE_ACTIVE
            .scope(
                true,
                embedder.embed(EmbedRequest {
                    texts: vec!["text-257".to_string(), "text-0".to_string()],
                }),
            )
            .await
            .expect("cached embeddings");
        assert_eq!(cached.embeddings[0], vec![8.0; 768]);
        assert_eq!(cached.embeddings[1], vec![6.0; 768]);
        assert_eq!(inner.calls.lock().expect("lock calls").len(), 3);

        embedder.clear().await;
        SEED_CACHE_ACTIVE
            .scope(
                true,
                embedder.embed(EmbedRequest {
                    texts: vec!["text-0".to_string()],
                }),
            )
            .await
            .expect("uncached embedding");
        assert_eq!(inner.calls.lock().expect("lock calls").len(), 4);
    }

    #[tokio::test]
    async fn seed_embedder_batches_below_combined_payload_limit() {
        let inner = Arc::new(RecordingEmbedder::default());
        let embedder = SeedBatchEmbedder::new(inner.clone());

        embedder
            .prefetch(&["x".repeat(60 << 10), "y".repeat(60 << 10)])
            .await
            .expect("prefetch long seed inputs");

        let calls = inner.calls.lock().expect("lock calls");
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].len(), 1);
        assert_eq!(calls[1].len(), 1);
    }

    #[tokio::test]
    async fn batched_seed_uses_prefetched_vectors_through_real_store() {
        let inner = Arc::new(RecordingEmbedder::default());
        let embedder = Arc::new(SeedBatchEmbedder::new(inner.clone()));
        let store = Store::new(StoreOptions {
            db: Arc::new(Db::open_memory().await.expect("open memory db")),
            embedder: embedder.clone(),
            predictor: None,
            reranker: None,
        });
        let pairs = vec![
            Pair {
                pair_id: "pair-1".to_string(),
                session_id: "session-1".to_string(),
                timestamp: "2026-01-01T00:00:00Z".to_string(),
                prompt: "first prompt".to_string(),
                response: "first response".to_string(),
            },
            Pair {
                pair_id: "pair-2".to_string(),
                session_id: "session-1".to_string(),
                timestamp: "2026-01-01T00:01:00Z".to_string(),
                prompt: "second prompt".to_string(),
                response: "second response".to_string(),
            },
        ];
        let subjects = vec![Subject {
            id: "subject-1".to_string(),
            subject_text: "shared subject".to_string(),
            description_text: "description".to_string(),
        }];
        let links = vec![
            Link {
                subject_id: "subject-1".to_string(),
                pair_id: "pair-1".to_string(),
            },
            Link {
                subject_id: "subject-1".to_string(),
                pair_id: "pair-2".to_string(),
            },
        ];

        let stats = load_haystack_batched(
            &store,
            &embedder,
            "seed-user",
            &pairs,
            &subjects,
            &links,
            false,
        )
        .await
        .expect("load batched seed");

        assert_eq!(stats.pairs, 2);
        assert_eq!(stats.subjects, 1);
        assert_eq!(stats.links, 2);
        let calls = inner.calls.lock().expect("lock calls");
        assert_eq!(calls.len(), 1);
        assert_eq!(
            calls[0],
            vec![
                "first prompt\nfirst response",
                "second prompt\nsecond response",
                "shared subject\ndescription",
            ]
        );
    }

    #[test]
    fn seed_prefetch_inputs_match_store_text_and_skip_unlinked_subjects() {
        let pairs = vec![Pair {
            pair_id: "pair-1".to_string(),
            session_id: String::new(),
            timestamp: String::new(),
            prompt: " prompt ".to_string(),
            response: " response ".to_string(),
        }];
        let subjects = vec![
            Subject {
                id: "linked".to_string(),
                subject_text: " subject ".to_string(),
                description_text: " description ".to_string(),
            },
            Subject {
                id: "unused".to_string(),
                subject_text: "must not embed".to_string(),
                description_text: String::new(),
            },
        ];
        let links = vec![
            Link {
                subject_id: "linked".to_string(),
                pair_id: "pair-1".to_string(),
            },
            Link {
                subject_id: "unused".to_string(),
                pair_id: "missing-pair".to_string(),
            },
        ];

        assert_eq!(
            seed_embedding_inputs(&pairs, &subjects, &links),
            vec!["prompt \n response", "subject \n description"]
        );
    }

    #[test]
    fn v9_seed_accepts_uuid_capabilities_without_wave() {
        let req: SeedRequest = serde_json::from_str(
            r#"{
                "user_id":"8ec86f06-e794-4d1c-a920-97d3fcf5ce8b",
                "pairs":[{
                    "pair_id":"f493ee76-36e6-49da-b842-03378db9d35c",
                    "session_id":"4d86aa61-8bde-444e-88a0-6e4346ee8fb2",
                    "timestamp":"2026-01-01T00:00:00Z",
                    "prompt":"hello",
                    "response":"world"
                }],
                "subjects":[],
                "links":[]
            }"#,
        )
        .expect("deserialize v9 seed");
        assert_eq!(req.wave, 0);
        assert_eq!(
            req.user_id.as_deref(),
            Some("8ec86f06-e794-4d1c-a920-97d3fcf5ce8b")
        );
        assert_eq!(req.pairs[0].pair_id, "f493ee76-36e6-49da-b842-03378db9d35c");
        assert_eq!(
            req.pairs[0].session_id,
            "4d86aa61-8bde-444e-88a0-6e4346ee8fb2"
        );
    }

    #[test]
    fn v9_seed_accepts_explicit_empty_collections() {
        let req: SeedRequest = serde_json::from_str(
            r#"{"user_id":"8ec86f06-e794-4d1c-a920-97d3fcf5ce8b","pairs":[],"subjects":[],"links":[]}"#,
        )
        .expect("deserialize empty v9 seed");
        assert!(req.pairs.is_empty());
        assert!(req.subjects.is_empty());
        assert!(req.links.is_empty());
    }

    #[test]
    fn legacy_wave_remains_additive_compatible() {
        let req: SeedRequest = serde_json::from_str(r#"{"user_id":"miner","wave":3}"#)
            .expect("deserialize legacy seed");
        assert_eq!(req.wave, 3);
        assert!(req.pairs.is_empty());
    }
}
