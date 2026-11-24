//! The memory tools the kit serves to the model (`include_memory_tools`) come
//! from `ditto_harness::memory::memory_tools`, and their `definition()` is the
//! schema the model sees. The wire's own memory-tool definitions are dropped.
//!
//! `search_memories` must not advertise `sessionId`. That field is an
//! exact-match filter, not an "all sessions" sentinel. A corpus seeded with one
//! session id per conversation (LongMemEval) admits no rows for any value the
//! model invents (`"default"`, `"main"`), and the run then reports an empty
//! memory instead of an error (ditto-subnet#3206).
//!
//! These tests need neither Ollama nor a chat model.

use std::sync::Arc;

use async_trait::async_trait;
use ditto_harness::db::{Db, EMBEDDING_DIMS};
use ditto_harness::memory::{memory_tools, Store, StoreOptions};
use ditto_harness::{EmbedRequest, EmbedResponse, Embedder};
use dittobench_starter_kit::baseline::MEMORY_TOOL_NAMES;
use serde_json::{json, Value};

/// Every text embeds to the same unit vector, so retrieval is decided by the
/// SQL filters alone.
struct ConstantEmbedder;

#[async_trait]
impl Embedder for ConstantEmbedder {
    async fn embed(&self, req: EmbedRequest) -> ditto_harness::Result<EmbedResponse> {
        let mut unit = vec![0f32; EMBEDDING_DIMS];
        unit[0] = 1.0;
        Ok(EmbedResponse {
            embeddings: req.texts.iter().map(|_| unit.clone()).collect(),
            ..EmbedResponse::default()
        })
    }
}

async fn store() -> Arc<Store> {
    let db = Db::open_memory().await.expect("open in-memory turso db");
    Arc::new(Store::new(StoreOptions {
        db: Arc::new(db),
        embedder: Arc::new(ConstantEmbedder),
        predictor: None,
        reranker: None,
    }))
}

#[tokio::test]
async fn served_search_memories_does_not_advertise_session_id() {
    let tools = memory_tools(store().await, "kit-user", "");
    let search = tools
        .iter()
        .map(|t| t.definition())
        .find(|d| d.name == "search_memories")
        .expect("harness serves search_memories");
    assert!(
        MEMORY_TOOL_NAMES.contains(&search.name.as_str()),
        "the kit offers search_memories as a real memory tool"
    );
    let props = search.input_schema["properties"]
        .as_object()
        .expect("properties object");
    assert!(
        !props.contains_key("sessionId"),
        "search_memories must not advertise sessionId: {}",
        search.input_schema
    );
    assert!(props.contains_key("queries"), "{}", search.input_schema);
}

/// A memory seeded under a per-conversation session id, as LongMemEval loads
/// one, is found by every argument shape the advertised schema allows.
#[tokio::test]
async fn advertised_search_finds_a_per_conversation_memory() {
    let tools = memory_tools(store().await, "kit-user", "");
    let tool = |name: &str| {
        tools
            .iter()
            .find(|t| t.definition().name == name)
            .unwrap_or_else(|| panic!("harness serves {name}"))
    };
    tool("save_memory")
        .execute(json!({
            "prompt": "How long is my commute?",
            "response": "45 minutes each way",
            "summary": "Commute length",
            "sessionId": "answer_40a90d51"
        }))
        .await
        .expect("save memory");

    let search = tool("search_memories");
    let advertised: Vec<String> = search.definition().input_schema["properties"]
        .as_object()
        .expect("properties object")
        .keys()
        .cloned()
        .collect();
    let mut args = json!({"queries": ["commute"]});
    if advertised.iter().any(|k| k == "topK") {
        args["topK"] = json!(5);
    }
    for key in &advertised {
        assert!(
            matches!(key.as_str(), "queries" | "topK"),
            "unexpected model-facing search_memories field {key:?}"
        );
    }
    let out: Value = search.execute(args).await.expect("search memories");
    let memories = out["memories"].as_array().expect("memories array");
    assert_eq!(memories.len(), 1, "advertised-shape search: {out}");
}
