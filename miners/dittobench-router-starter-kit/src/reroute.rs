//! The reference **archetype rerouting** lever.
//!
//! This is the one clearly-commented, deterministic example lever this kit
//! ships to demonstrate the competition's spirit. It is **off by default**;
//! the default router is a byte-faithful pass-through (see `server.rs`).
//!
//! ## What it does
//!
//! On the Anthropic Messages route only, it detects a *cheap-shaped* request
//! — the small, tool-free, short-`max_tokens` "aside" the harness sends for
//! title/probe generation — and rewrites **only** the top-level `model`
//! field to a cheaper catalog route id before forwarding. Every other byte
//! of the request is left identical (a surgical value splice, not a JSON
//! re-serialization), which is what keeps the provider prefix cache hitting
//! and satisfies the determinism and prefix-stability gates.
//!
//! ## Ledger-kind mapping (from the design doc's observable transform ledger)
//!
//! The validator diffs the ingress-tap body against the upstream body and
//! classifies each difference deterministically. This lever produces exactly
//! one classification:
//!
//! | This lever | Ledger kind |
//! |---|---|
//! | rewrites the request `model`                              | `route` |
//!
//! Other levers a miner could add here, and the kinds they would produce
//! (left as future work; see the README "Extending" section):
//!
//! | Future lever | Ledger kind |
//! |---|---|
//! | condense a tool `description` (name + schema unchanged)   | `tool_digest` |
//! | compact a completed-turn `tool_result`                    | `result_digest` / `context_compaction` |
//! | attach verbatim seeded-memory content                     | `memory_placement` |
//! | move/add a `cache_control` breakpoint                     | `cache_marks` |
//! | a side call with no tap counterpart (title/compaction)    | `side_call` |
//!
//! ## Hard constraints this lever respects
//!
//! - **Determinism / prefix stability**: the rewrite is a pure function of the
//!   request bytes and static config, applied identically on every re-send.
//! - **No novel tokens / no contamination**: it never adds text upstream; it
//!   only substitutes one catalog identifier for another.
//! - **Catalog-only models**: the target must be a route id in the frozen task
//!   catalog. The relay rejects anything else as `route_not_in_catalog`; this
//!   kit does not ship a hardcoded model so a miner cannot accidentally name a
//!   route outside a given task's catalog.
//! - **No canary leakage**: seeded memory (including the canary) is never read
//!   by this lever.

use serde_json::Value;

/// An aside request produces at most this many output tokens. Title/probe
/// prompts request a tiny completion; a real coding turn asks for far more.
const MAX_ASIDE_OUTPUT_TOKENS: u64 = 512;

/// An aside request carries a small prompt. Measured in characters of the
/// user message content, deterministically, without tokenizing.
const MAX_ASIDE_INPUT_CHARS: usize = 4_096;

/// An aside request carries little or no system prompt.
const MAX_ASIDE_SYSTEM_CHARS: usize = 4_096;

/// Configuration for the archetype rerouting lever.
#[derive(Debug, Clone, Default)]
pub struct RerouteConfig {
    enabled: bool,
    aside_model: Option<String>,
}

impl RerouteConfig {
    /// Builds a config. The lever only ever fires when it is both enabled and
    /// given a non-empty target route id.
    #[must_use]
    pub fn new(enabled: bool, aside_model: Option<String>) -> Self {
        let aside_model = aside_model.filter(|model| !model.trim().is_empty());
        Self {
            enabled,
            aside_model,
        }
    }

    /// Whether the lever can fire at all (enabled and configured with a target).
    #[must_use]
    pub fn is_active(&self) -> bool {
        self.enabled && self.aside_model.is_some()
    }

    /// Applies the lever to an Anthropic Messages request body.
    ///
    /// Returns `Some(new_bytes)` when the request is a cheap aside and the
    /// `model` was rewritten to the configured cheaper route (ledger kind
    /// `route`); returns `None` when the request must be forwarded byte-for-
    /// byte unchanged (the default and safe path).
    #[must_use]
    pub fn reroute_messages(&self, raw: &[u8]) -> Option<Vec<u8>> {
        let target = self.aside_model.as_deref()?;
        if !self.enabled {
            return None;
        }
        let value: Value = serde_json::from_slice(raw).ok()?;
        if !is_cheap_aside(&value) {
            return None;
        }
        let current = value.get("model").and_then(Value::as_str)?;
        if current == target {
            return None;
        }
        splice_model(raw, current, target)
    }
}

/// Deterministic, shape-based detector for a cheap "aside" Anthropic Messages
/// request (the title/probe archetype the design doc reroutes to a flash
/// model). It keys on request *shape*, never on harness wording, so it keeps
/// working against the hidden partition's different system prompts.
#[must_use]
pub fn is_cheap_aside(body: &Value) -> bool {
    // No tools: a tool-bearing turn is a real agent step, never an aside.
    let no_tools = match body.get("tools") {
        None => true,
        Some(Value::Array(tools)) => tools.is_empty(),
        Some(_) => false,
    };
    if !no_tools || body.get("tool_choice").is_some() {
        return false;
    }

    // Small requested output.
    let Some(max_tokens) = body.get("max_tokens").and_then(Value::as_u64) else {
        return false;
    };
    if max_tokens == 0 || max_tokens > MAX_ASIDE_OUTPUT_TOKENS {
        return false;
    }

    // A single short user turn.
    let Some(messages) = body.get("messages").and_then(Value::as_array) else {
        return false;
    };
    if messages.len() != 1 {
        return false;
    }
    if messages[0].get("role").and_then(Value::as_str) != Some("user") {
        return false;
    }
    if content_text_len(&messages[0]) > MAX_ASIDE_INPUT_CHARS {
        return false;
    }

    // Little or no system prompt.
    let system_len = body.get("system").map_or(0, value_text_len);
    system_len <= MAX_ASIDE_SYSTEM_CHARS
}

/// Total length of the textual content of one Anthropic message, whether the
/// content is a string or an array of blocks.
fn content_text_len(message: &Value) -> usize {
    message.get("content").map_or(0, value_text_len)
}

/// Deterministic character count of any Anthropic content value (a string, or
/// an array of blocks whose `text` fields are summed).
fn value_text_len(value: &Value) -> usize {
    match value {
        Value::String(text) => text.chars().count(),
        Value::Array(blocks) => blocks
            .iter()
            .map(|block| {
                block
                    .get("text")
                    .and_then(Value::as_str)
                    .map_or(0, |t| t.chars().count())
            })
            .sum(),
        _ => 0,
    }
}

/// Surgically replaces the value of the first top-level `"model"` key, leaving
/// every other byte of the request identical. Returns `None` if the field
/// cannot be located unambiguously or the found value does not equal `old`
/// (in which case the caller forwards the original bytes unchanged).
///
/// A full parse-and-re-serialize would reorder keys and normalize whitespace,
/// breaking the byte-for-byte prefix stability the cache depends on; this
/// splice touches only the model token.
#[must_use]
pub fn splice_model(raw: &[u8], old: &str, new: &str) -> Option<Vec<u8>> {
    let text = std::str::from_utf8(raw).ok()?;
    let bytes = text.as_bytes();

    let key_pos = text.find("\"model\"")?;
    let after_key = key_pos + "\"model\"".len();
    let colon_rel = text[after_key..].find(':')?;
    let mut idx = after_key + colon_rel + 1;

    while idx < bytes.len() && bytes[idx].is_ascii_whitespace() {
        idx += 1;
    }
    if idx >= bytes.len() || bytes[idx] != b'"' {
        return None;
    }
    let value_start = idx;

    let mut cursor = idx + 1;
    while cursor < bytes.len() {
        match bytes[cursor] {
            b'\\' => cursor += 2,
            b'"' => break,
            _ => cursor += 1,
        }
    }
    if cursor >= bytes.len() {
        return None;
    }
    let value_end = cursor + 1;

    // Confirm the enclosed JSON string decodes to exactly the current model.
    let decoded: String = serde_json::from_str(&text[value_start..value_end]).ok()?;
    if decoded != old {
        return None;
    }

    let replacement = serde_json::to_string(new).ok()?;
    let mut out = Vec::with_capacity(raw.len() - (value_end - value_start) + replacement.len());
    out.extend_from_slice(&raw[..value_start]);
    out.extend_from_slice(replacement.as_bytes());
    out.extend_from_slice(&raw[value_end..]);
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn aside_body() -> String {
        r#"{"model":"claude-sonnet-4-5","max_tokens":32,"messages":[{"role":"user","content":"Write a 5-word title."}]}"#.to_string()
    }

    #[test]
    fn detects_small_tool_free_aside() {
        let body: Value = serde_json::from_str(&aside_body()).unwrap();
        assert!(is_cheap_aside(&body));
    }

    #[test]
    fn rejects_requests_with_tools() {
        let body: Value = serde_json::from_str(
            r#"{"model":"m","max_tokens":16,"tools":[{"name":"repo_read"}],"messages":[{"role":"user","content":"hi"}]}"#,
        )
        .unwrap();
        assert!(!is_cheap_aside(&body));
    }

    #[test]
    fn rejects_large_output_requests() {
        let body: Value = serde_json::from_str(
            r#"{"model":"m","max_tokens":8192,"messages":[{"role":"user","content":"hi"}]}"#,
        )
        .unwrap();
        assert!(!is_cheap_aside(&body));
    }

    #[test]
    fn rejects_multi_turn_conversations() {
        let body: Value = serde_json::from_str(
            r#"{"model":"m","max_tokens":16,"messages":[{"role":"user","content":"a"},{"role":"assistant","content":"b"}]}"#,
        )
        .unwrap();
        assert!(!is_cheap_aside(&body));
    }

    #[test]
    fn disabled_lever_never_rewrites() {
        let config = RerouteConfig::new(false, Some("glm-5.3-flash".to_string()));
        assert!(!config.is_active());
        assert!(config.reroute_messages(aside_body().as_bytes()).is_none());
    }

    #[test]
    fn unconfigured_target_never_rewrites() {
        let config = RerouteConfig::new(true, None);
        assert!(!config.is_active());
        assert!(config.reroute_messages(aside_body().as_bytes()).is_none());
    }

    #[test]
    fn active_lever_rewrites_only_the_model_and_nothing_else() {
        let config = RerouteConfig::new(true, Some("glm-5.3-flash".to_string()));
        assert!(config.is_active());
        let original = aside_body();
        let rewritten = config.reroute_messages(original.as_bytes()).unwrap();
        let rewritten_text = String::from_utf8(rewritten).unwrap();

        // Only the model token changed.
        assert_eq!(
            rewritten_text,
            original.replace("\"claude-sonnet-4-5\"", "\"glm-5.3-flash\"")
        );
        // And the rest of the JSON is byte-identical either side of the token.
        let value: Value = serde_json::from_str(&rewritten_text).unwrap();
        assert_eq!(value["model"], "glm-5.3-flash");
        assert_eq!(value["max_tokens"], 32);
    }

    #[test]
    fn non_aside_request_is_forwarded_unchanged() {
        let config = RerouteConfig::new(true, Some("glm-5.3-flash".to_string()));
        let heavy = r#"{"model":"claude-sonnet-4-5","max_tokens":8192,"tools":[{"name":"repo_read"}],"messages":[{"role":"user","content":"Fix the bug."}]}"#;
        assert!(config.reroute_messages(heavy.as_bytes()).is_none());
    }

    #[test]
    fn splice_leaves_surrounding_bytes_identical_with_whitespace() {
        let raw = b"{ \"model\" : \"old-route\" , \"max_tokens\": 8 }";
        let out = splice_model(raw, "old-route", "new-route").unwrap();
        assert_eq!(
            String::from_utf8(out).unwrap(),
            "{ \"model\" : \"new-route\" , \"max_tokens\": 8 }"
        );
    }

    #[test]
    fn splice_refuses_when_current_value_mismatches() {
        let raw = b"{\"model\":\"actual\"}";
        assert!(splice_model(raw, "expected", "new").is_none());
    }
}
