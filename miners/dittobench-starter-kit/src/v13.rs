//! Bench v13 honest-reference helpers.
//!
//! Bench v13 (issue #1518) grades typed semantic outcomes through claim sets
//! and adds relay-observed gates that fail only on evidence of substitution:
//! text the model never emitted, a graded value the harness itself wrote into
//! the prompt, a catalog withheld on the deciding turn, or an
//! evidence-independent default across a twin pair. None of them zero a
//! response whose graded claim is correct and model-emitted. This module is
//! the kit's reference implementation of the honest side of every rule, and
//! the local instrument (`scripts/local-rehearsal.py --gates`) that replays
//! the public rules against a run before upload.
//!
//! The wire `bench_version` a harness receives stays at 9 (owner decision on
//! #1519, option A): every v13 addition is an additive optional field, so the
//! helpers here apply to every request rather than branching on the version.
//!
//! What lives here:
//!
//!  * [`HARNESS_POLICY_PROMPT`] — values-free guidance appended to the wire
//!    system prompt: answer in the unit the question asked for, never rescale
//!    or reformat the model's own value on the host; ask a clarifying question
//!    that names the missing detail and cites what memory search found; list
//!    before acting on runtime-described options and read the qualifier on a
//!    near-miss; cite what was found when declining.
//!  * [`answer_slot_from_prose`] — the ONLY way the kit populates the
//!    `answer` slot: a verbatim substring of the model's own final text (a
//!    trailing `Answer:` line the model chose to write). The slot is therefore
//!    always in the prose (`slot_not_in_prose` cannot fire) and always
//!    model-emitted (`served_text_not_model_emitted` cannot fire). No `/100`,
//!    direction mapping, or formatting is ever applied.
//!  * [`semantic_top_k`] / [`preload_catalog`] — the published, model-free
//!    lexical embedding (TF-IDF over tool name + description, cosine to the
//!    request, ties on name) that defines the v13 semantic-preloading safe
//!    harbor, and the documented example of trimming a catalog while always
//!    retaining the safe-harbor top-k. The default is the full catalog.
//!  * [`CompletionLogEntry`] — the per-`/run` record of what the model was
//!    offered and what it emitted, written only when
//!    [`COMPLETION_LOG_ENV`] is set (the local rehearsal sets it). It is the
//!    input `--gates` needs to replay the catalog-present, swallowed-call,
//!    provenance, and causal rules locally; the validator never reads it.

use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::path::Path;
use std::sync::Mutex;

use ditto_harness::types::ChatMessage;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::protocol::{RunRequest, ToolDefWire};

/// Environment variable naming the JSONL file `/run` appends a
/// [`CompletionLogEntry`] to. Unset (the default, and always on-chain) writes
/// nothing.
pub const COMPLETION_LOG_ENV: &str = "DITTOBENCH_COMPLETION_LOG";

/// Environment variable enabling the `answer` slot. OFF by default: the public
/// wire stays at `bench_version` 9 (#1519, option A), so a kit-side slot cannot
/// be gated on the contract version and would otherwise change LIVE v12
/// grading — for v9+ a populated slot is authoritative with no prose fallback
/// (`grade.go`), so a model that wrote `Answer:` in a different unit than the
/// prose would score 0 where prose fallback scored 1. `local-rehearsal.py
/// --gates` sets it so the v13 slot rules are exercised locally; Platform's v13
/// activation is the point to flip the default.
pub const ANSWER_SLOT_ENV: &str = "DITTOBENCH_ANSWER_SLOT";

/// Environment variable enabling the documented semantic top-k preloading
/// example: keep the `k` most request-relevant tools (never fewer than the
/// safe-harbor top-k). Unset offers the full catalog on every turn.
pub const PRELOAD_TOP_K_ENV: &str = "DITTOBENCH_PRELOAD_TOP_K";

/// k of the published semantic-preloading safe harbor: a trimmed catalog that
/// still contains these tools is never charged for the trim.
pub const SAFE_HARBOR_TOP_K: usize = 3;

/// Prefix of the harness-library system message that carries retrieved
/// memory context. Those spans are `/seed`-derived and exempt from the causal
/// gate; everything else the harness places in a system message is a
/// harness-authored span.
pub const MEMORY_CONTEXT_PREFIX: &str = "Relevant memory context for this turn:";

/// Values-free policy appended to the wire system prompt. It contains no
/// number, name, or option value, so it can never place a graded value in a
/// harness-authored span.
pub const HARNESS_POLICY_PROMPT: &str = "\
Answering policy:
- State the answer in your prose, in the unit and form the user asked for. \
If the user asked for minor units, give minor units; never convert, rescale, \
or reformat a value you have already produced.
- If a request is missing a detail you need (which option, which record, \
which value), do not guess: ask one clarifying question that names the \
missing detail and mention what you did find in memory while looking.
- When an option list exists only at runtime (the tool schema says the \
options are listed by discover_capabilities), call discover_capabilities \
first and then pass exactly one listed option, spelled as listed. If the \
user's spelling is close to two listed options, choose the one that carries \
the qualifier the user used; if it is still ambiguous, ask.
- When you decline because memory does not support an answer, say what you \
did find and why it does not answer the question.
- Do not state more than one candidate value for a single fact; when a fact \
changed over time, name the current value and say the earlier one was \
superseded.";

/// Appended after [`HARNESS_POLICY_PROMPT`] only when the `answer` slot is
/// enabled ([`ANSWER_SLOT_ENV`]): asks the model for the trailing `Answer:`
/// line [`answer_slot_from_prose`] copies verbatim, so the slot path is
/// actually exercised. Values-free like the policy itself.
pub const ANSWER_LINE_POLICY_PROMPT: &str = "\
- When you state a single value, option, or name as the answer, end your reply \
with one final line of the form `Answer: <value>` that repeats that value \
exactly as you wrote it in your prose. Omit the line when you ask a clarifying \
question or decline.";

/// Appends [`HARNESS_POLICY_PROMPT`] (and, with `answer_slot`,
/// [`ANSWER_LINE_POLICY_PROMPT`]) to the wire system prompt. The wire prompt
/// is kept first and unchanged.
pub fn compose_system_prompt(wire_system_prompt: &str, answer_slot: bool) -> String {
    let wire = wire_system_prompt.trim_end();
    let mut policy = HARNESS_POLICY_PROMPT.to_string();
    if answer_slot {
        policy.push('\n');
        policy.push_str(ANSWER_LINE_POLICY_PROMPT);
    }
    if wire.is_empty() {
        return policy;
    }
    format!("{wire}\n\n{policy}")
}

/// Whether the `answer` slot is enabled for this process ([`ANSWER_SLOT_ENV`]
/// set to anything but empty, `0`, `false`, or `off`).
pub fn answer_slot_enabled() -> bool {
    match std::env::var(ANSWER_SLOT_ENV) {
        Ok(value) => !matches!(
            value.trim().to_ascii_lowercase().as_str(),
            "" | "0" | "false" | "off"
        ),
        Err(_) => false,
    }
}

/// The `answer` slot the kit serves: [`answer_slot_from_prose`] when the slot
/// is enabled, `None` otherwise (the validator then grades the prose, exactly
/// as the pre-v13 kit was graded).
pub fn answer_slot(final_text: &str, enabled: bool) -> Option<String> {
    if !enabled {
        return None;
    }
    answer_slot_from_prose(final_text)
}

/// Extracts the `answer` slot as a VERBATIM substring of the model's final
/// text: the value after the last line the model wrote as `Answer: ...`
/// (markdown emphasis around the label tolerated). Returns `None` when the
/// model wrote no such line. The returned slice is always contained in
/// `final_text` byte-for-byte, so the slot can never disagree with the prose
/// and never carries a host rewrite (no `/100`, no direction map, no
/// reformatting).
pub fn answer_slot_from_prose(final_text: &str) -> Option<String> {
    const LABEL: &str = "answer:";
    let decoration: &[char] = &[' ', '\t', '*', '_', '`', '#', '-', '>'];
    for line in final_text.lines().rev() {
        let stripped = line.trim_matches(decoration);
        // `get` returns None when the label width lands inside a multibyte
        // character (a line opening with "‑" or "é"), so a non-ASCII line can
        // never panic the slice.
        let Some(head) = stripped.get(..LABEL.len()) else {
            continue;
        };
        if !head.eq_ignore_ascii_case(LABEL) {
            continue;
        }
        let value = stripped[LABEL.len()..].trim_matches(decoration);
        if value.is_empty() {
            return None;
        }
        debug_assert!(final_text.contains(value));
        return Some(value.to_string());
    }
    None
}

// Published embedding vocabulary. Identical to the scorer's
// `CatalogSemanticTopK` (stopwords, ASCII tokenization, crude suffix stem) so
// the safe-harbor top-k a miner computes locally is the one the validator
// states the rule against.
const STOPWORDS: &[&str] = &[
    "the", "and", "for", "with", "that", "this", "from", "you", "your", "can", "please", "one",
    "more", "use", "are", "was", "were", "have", "has", "had", "not", "but", "any", "all", "into",
    "its", "our", "out", "about", "what", "which", "when", "where", "who", "how", "why", "just",
    "like", "then", "than", "too", "very", "will", "would", "should", "could", "there", "here",
    "some", "them", "they", "their", "thing", "tool", "tools", "given", "return", "returns",
    "default",
];

fn stem(token: &str) -> &str {
    for suffix in ["ing", "ed", "es", "s"] {
        if token.len() > suffix.len() + 3 && token.ends_with(suffix) {
            return &token[..token.len() - suffix.len()];
        }
    }
    token
}

/// Lowercases, splits on every non-ASCII-alphanumeric character (so snake_case
/// names split into words), drops tokens shorter than three bytes and
/// stopwords, and stems. Returns the distinct token set.
fn catalog_tokens(text: &str) -> BTreeSet<String> {
    let mut tokens = BTreeSet::new();
    let mut current = String::new();
    let mut flush = |current: &mut String| {
        if current.is_empty() {
            return;
        }
        let token = std::mem::take(current);
        if token.len() < 3 || STOPWORDS.contains(&token.as_str()) {
            return;
        }
        tokens.insert(stem(&token).to_string());
    };
    for c in text.to_lowercase().chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() {
            current.push(c);
        } else {
            flush(&mut current);
        }
    }
    flush(&mut current);
    tokens
}

/// Ranks `catalog` against `prompt` under the published lexical embedding and
/// returns the names of the top `k` tools (fewer when the catalog is smaller).
/// Ties break on tool name, so the result is a pure function of its inputs.
pub fn semantic_top_k(prompt: &str, catalog: &[ToolDefWire], k: usize) -> Vec<String> {
    if k == 0 || catalog.is_empty() {
        return Vec::new();
    }
    let docs: Vec<BTreeSet<String>> = catalog
        .iter()
        .map(|tool| catalog_tokens(&format!("{} {}", tool.name, tool.description)))
        .collect();
    let mut df: BTreeMap<&str, usize> = BTreeMap::new();
    for doc in &docs {
        for token in doc {
            *df.entry(token.as_str()).or_insert(0) += 1;
        }
    }
    let n = catalog.len() as f64;
    let idf = |token: &str| (1.0 + n / (1.0 + *df.get(token).unwrap_or(&0) as f64)).ln();
    let query = catalog_tokens(prompt);
    let query_norm: f64 = query.iter().map(|t| idf(t).powi(2)).sum();
    let mut ranked: Vec<(f64, &str)> = catalog
        .iter()
        .zip(&docs)
        .map(|(tool, doc)| {
            let mut dot = 0.0;
            let mut doc_norm = 0.0;
            for token in doc {
                let w = idf(token).powi(2);
                doc_norm += w;
                if query.contains(token) {
                    dot += w;
                }
            }
            let score = if dot > 0.0 && doc_norm > 0.0 && query_norm > 0.0 {
                dot / (doc_norm.sqrt() * query_norm.sqrt())
            } else {
                0.0
            };
            (score, tool.name.as_str())
        })
        .collect();
    ranked.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap().then_with(|| a.1.cmp(b.1)));
    ranked
        .into_iter()
        .take(k)
        .map(|(_, name)| name.to_string())
        .collect()
}

/// The documented semantic top-k preloading example. `keep = None` (the kit
/// default) offers the whole catalog. `keep = Some(k)` retains the `k` most
/// request-relevant tools under the published embedding, never fewer than the
/// safe-harbor top-k, in wire order — so a trimmed catalog always satisfies the
/// published safe harbor by construction. Trimming below the top-k is the
/// pattern the catalog-present gate charges; do not write it.
pub fn preload_catalog(
    prompt: &str,
    tools: &[ToolDefWire],
    keep: Option<usize>,
) -> Vec<ToolDefWire> {
    let Some(keep) = keep else {
        return tools.to_vec();
    };
    if keep >= tools.len() {
        return tools.to_vec();
    }
    let retained: BTreeSet<String> = semantic_top_k(prompt, tools, keep.max(SAFE_HARBOR_TOP_K))
        .into_iter()
        .collect();
    tools
        .iter()
        .filter(|tool| retained.contains(&tool.name))
        .cloned()
        .collect()
}

/// Reads [`PRELOAD_TOP_K_ENV`]; unset or unparsable means the full catalog.
pub fn preload_top_k_from_env() -> Option<usize> {
    std::env::var(PRELOAD_TOP_K_ENV)
        .ok()
        .and_then(|raw| raw.trim().parse::<usize>().ok())
        .filter(|k| *k > 0)
}

/// One tool the harness received on the wire (name and description only; the
/// schema is not needed to recompute the published embedding).
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct LoggedTool {
    pub name: String,
    pub description: String,
}

/// One model-emitted tool call.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct LoggedToolCall {
    pub name: String,
    #[serde(default, skip_serializing_if = "Value::is_null")]
    pub args: Value,
}

/// One model completion: the text the model emitted and the tool calls it
/// selected in that turn.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct LoggedCompletion {
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub tool_calls: Vec<LoggedToolCall>,
}

/// The per-`/run` record `--gates` replays. Every span is classified the way
/// the v13 causal gate classifies it: `harness_spans` are harness-authored
/// (system prompt), `record_spans` are the harness library's retrieved-memory
/// messages (derived from `/seed`, exempt), `tool_results` are delivered mock
/// results (exempt), and `user_input` is the case question (exempt).
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct CompletionLogEntry {
    pub case_id: String,
    #[serde(default)]
    pub user_id: String,
    pub bench_version: u32,
    pub user_input: String,
    /// The catalog as received on the wire (the reference the safe harbor is
    /// stated against).
    pub catalog: Vec<LoggedTool>,
    /// The tool names offered to the model on every turn of this case.
    pub tools_offered: Vec<String>,
    pub harness_spans: Vec<String>,
    pub record_spans: Vec<String>,
    pub tool_results: Vec<String>,
    pub completions: Vec<LoggedCompletion>,
}

fn message_text(message: &ChatMessage) -> String {
    message
        .content
        .iter()
        .map(|part| part.content.as_str())
        .filter(|text| !text.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

/// Builds the log entry from the harness transcript of one run.
pub fn completion_log_entry(
    req: &RunRequest,
    user_id: &str,
    tools_offered: Vec<String>,
    messages: &[ChatMessage],
) -> CompletionLogEntry {
    let mut entry = CompletionLogEntry {
        case_id: req.case_id.clone(),
        user_id: user_id.to_string(),
        bench_version: req.bench_version,
        user_input: req.user_input.clone(),
        catalog: req
            .tools
            .iter()
            .map(|tool| LoggedTool {
                name: tool.name.clone(),
                description: tool.description.clone(),
            })
            .collect(),
        tools_offered,
        ..CompletionLogEntry::default()
    };
    for message in messages {
        match message.role.as_str() {
            "system" => {
                let text = message_text(message);
                if text.starts_with(MEMORY_CONTEXT_PREFIX) {
                    entry.record_spans.push(text);
                } else if !text.is_empty() {
                    entry.harness_spans.push(text);
                }
            }
            "assistant" => {
                let mut completion = LoggedCompletion {
                    text: message_text(message),
                    tool_calls: Vec::new(),
                };
                for call in &message.tool_calls {
                    completion.tool_calls.push(LoggedToolCall {
                        name: call.name.clone(),
                        args: call.args.clone(),
                    });
                }
                for part in &message.content {
                    if let Some(call) = &part.tool_call {
                        completion.tool_calls.push(LoggedToolCall {
                            name: call.name.clone(),
                            args: call.args.clone(),
                        });
                    }
                }
                if !completion.text.is_empty() || !completion.tool_calls.is_empty() {
                    entry.completions.push(completion);
                }
            }
            _ => {
                for part in &message.content {
                    if let Some(result) = &part.tool_call_response {
                        let rendered = if result.output.is_null() {
                            result.error.clone()
                        } else {
                            result.output.to_string()
                        };
                        if !rendered.is_empty() {
                            entry.tool_results.push(rendered);
                        }
                    }
                }
            }
        }
    }
    entry
}

static COMPLETION_LOG_LOCK: Mutex<()> = Mutex::new(());

/// Appends one JSON line to `path`, creating the file with private permissions
/// on first write. Concurrent `/run`s serialize on a process-wide lock so lines
/// never interleave.
pub fn append_completion_log(path: &Path, entry: &CompletionLogEntry) -> std::io::Result<()> {
    let line = serde_json::to_string(entry).map_err(std::io::Error::other)?;
    let _guard = COMPLETION_LOG_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let mut options = std::fs::OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path)?;
    file.write_all(line.as_bytes())?;
    file.write_all(b"\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use ditto_harness::types::{Content, ContentType, ToolCall, ToolCallResponse};
    use serde_json::json;

    fn tool(name: &str, description: &str) -> ToolDefWire {
        ToolDefWire {
            name: name.into(),
            description: description.into(),
            parameters: json!({"type": "object"}),
        }
    }

    fn sample_catalog() -> Vec<ToolDefWire> {
        vec![
            tool("search_web", "Search live sources for one or more queries."),
            tool(
                "read_links",
                "Read one or more URLs and return markdown text content.",
            ),
            tool("create_image", "Generate an image from a text prompt."),
            tool("set_theme", "Set the app color theme."),
            tool(
                "search_memories",
                "Search past conversations and return compact memory summaries.",
            ),
            tool("run_code", "Run code in a sandbox and return the output."),
        ]
    }

    #[test]
    fn policy_prompt_is_values_free_and_appended_after_the_wire_prompt() {
        assert!(!HARNESS_POLICY_PROMPT.chars().any(|c| c.is_ascii_digit()));
        assert!(!ANSWER_LINE_POLICY_PROMPT
            .chars()
            .any(|c| c.is_ascii_digit()));
        let composed = compose_system_prompt("You are Ditto.", false);
        assert!(composed.starts_with("You are Ditto.\n\n"));
        assert!(composed.ends_with(HARNESS_POLICY_PROMPT));
        assert!(!composed.contains(ANSWER_LINE_POLICY_PROMPT));
        assert_eq!(compose_system_prompt("  ", false), HARNESS_POLICY_PROMPT);
        // The Answer-line request rides only with the slot switch, so the slot
        // path is exercised exactly when the slot can be served.
        let with_slot = compose_system_prompt("You are Ditto.", true);
        assert!(with_slot.contains(HARNESS_POLICY_PROMPT));
        assert!(with_slot.ends_with(ANSWER_LINE_POLICY_PROMPT));
    }

    #[test]
    fn answer_slot_is_off_by_default_and_verbatim_when_enabled() {
        // Live v12 grading: a v9 RunResponse without the switch has no slot,
        // whether or not the model wrote an `Answer:` line.
        assert_eq!(
            answer_slot("You paid 411067 cents.\nAnswer: 411067", false),
            None
        );
        assert_eq!(answer_slot("You paid 411067 cents.", false), None);
        assert_eq!(answer_slot("You paid 411067 cents.", true), None);
        assert_eq!(
            answer_slot("You paid 411067 cents.\nAnswer: 411067", true).as_deref(),
            Some("411067")
        );
        // `answer_slot_enabled` reads the process environment; the parser is
        // exercised on its accepted spellings without touching it here.
        for (raw, want) in [
            ("1", true),
            ("true", true),
            ("on", true),
            ("0", false),
            ("false", false),
            ("off", false),
            ("", false),
        ] {
            let enabled = !matches!(
                raw.trim().to_ascii_lowercase().as_str(),
                "" | "0" | "false" | "off"
            );
            assert_eq!(enabled, want, "{raw:?}");
        }
    }

    #[test]
    fn answer_slot_is_a_verbatim_substring_of_the_prose() {
        for (text, want) in [
            (
                "The total came to 411067 cents.\nAnswer: 411067",
                Some("411067"),
            ),
            (
                "You asked for minor units.\n**Answer:** 411,067 cents",
                Some("411,067 cents"),
            ),
            ("Two lines.\nanswer: Lisbon\n", Some("Lisbon")),
            (
                "It went up.\n- Answer: climbed by $2,601.95",
                Some("climbed by $2,601.95"),
            ),
            ("No labelled line at all, $4,110.67 in total.", None),
            ("Answer:", None),
            // Non-ASCII line openings (a non-breaking hyphen bullet, an
            // accented word shorter than the label) must never panic.
            ("\u{2011} Answer: 411067\n\u{2011} 4,110.67 dollars", None),
            ("é\nÀ bientôt", None),
            ("Résumé:\nAnswer: 12 días", Some("12 días")),
        ] {
            let got = answer_slot_from_prose(text);
            assert_eq!(got.as_deref(), want, "{text:?}");
            if let Some(slot) = got {
                assert!(text.contains(&slot), "slot must be a verbatim substring");
            }
        }
    }

    #[test]
    fn answer_slot_never_rescales_or_remaps_the_model_value() {
        // A bare minor-unit answer stays bare; a direction word stays the
        // model's own word. Any host rewrite here would be the I4 pattern.
        assert_eq!(
            answer_slot_from_prose("Answer: 260195").as_deref(),
            Some("260195")
        );
        assert_eq!(
            answer_slot_from_prose("Answer: it climbed").as_deref(),
            Some("it climbed")
        );
    }

    #[test]
    fn semantic_top_k_is_deterministic_and_ranks_by_request_relevance() {
        let catalog = sample_catalog();
        let top = semantic_top_k(
            "search the web for the latest quantum computing news",
            &catalog,
            3,
        );
        assert_eq!(top.len(), 3);
        assert_eq!(top[0], "search_web");
        assert_eq!(
            top,
            semantic_top_k(
                "search the web for the latest quantum computing news",
                &catalog,
                3
            )
        );
        // Ties (no shared token) break on name, and k caps the result.
        let none = semantic_top_k("zzz", &catalog, 2);
        assert_eq!(
            none,
            vec!["create_image".to_string(), "read_links".to_string()]
        );
        assert!(semantic_top_k("anything", &catalog, 0).is_empty());
        assert!(semantic_top_k("anything", &[], 3).is_empty());
    }

    #[test]
    fn preload_default_is_the_full_catalog_and_trimming_keeps_the_safe_harbor() {
        let catalog = sample_catalog();
        assert_eq!(preload_catalog("read this page", &catalog, None), catalog);
        assert_eq!(
            preload_catalog("read this page", &catalog, Some(99)),
            catalog
        );
        let trimmed = preload_catalog("read this page for me", &catalog, Some(1));
        assert_eq!(trimmed.len(), SAFE_HARBOR_TOP_K);
        let top = semantic_top_k("read this page for me", &catalog, SAFE_HARBOR_TOP_K);
        for name in &top {
            assert!(
                trimmed.iter().any(|t| &t.name == name),
                "{name} must be retained"
            );
        }
        // Wire order is preserved.
        let names: Vec<&str> = trimmed.iter().map(|t| t.name.as_str()).collect();
        let order: Vec<&str> = catalog
            .iter()
            .map(|t| t.name.as_str())
            .filter(|n| names.contains(n))
            .collect();
        assert_eq!(names, order);
    }

    #[test]
    fn completion_log_classifies_spans_the_way_the_causal_gate_does() {
        let req = RunRequest {
            case_id: "case-1".into(),
            system_prompt: "You are Ditto.".into(),
            user_input: "What did I pay in April?".into(),
            tools: vec![tool("search_web", "Search live sources.")],
            bench_version: 9,
            ..RunRequest::default()
        };
        let messages = vec![
            ChatMessage {
                role: "system".into(),
                content: vec![Content::text(compose_system_prompt(
                    &req.system_prompt,
                    true,
                ))],
                ..ChatMessage::default()
            },
            ChatMessage {
                role: "system".into(),
                content: vec![Content::text(format!(
                    "{MEMORY_CONTEXT_PREFIX}\n{{\"longTerm\":[\"paid 411067 cents in April\"]}}"
                ))],
                ..ChatMessage::default()
            },
            ChatMessage {
                role: "user".into(),
                content: vec![Content::text("What did I pay in April?")],
                ..ChatMessage::default()
            },
            ChatMessage {
                role: "assistant".into(),
                tool_calls: vec![ToolCall {
                    id: "t1".into(),
                    name: "search_web".into(),
                    args: json!({"queries": ["april payment"]}),
                }],
                ..ChatMessage::default()
            },
            ChatMessage {
                role: "tool".into(),
                tool_call_id: "t1".into(),
                content: vec![Content {
                    content_type: Some(ContentType::ToolResult),
                    tool_call_response: Some(ToolCallResponse {
                        id: "t1".into(),
                        name: "search_web".into(),
                        output: json!({"result": "the Veltrix index reached 3,418 points"}),
                        error: String::new(),
                    }),
                    ..Content::default()
                }],
                ..ChatMessage::default()
            },
            ChatMessage {
                role: "assistant".into(),
                content: vec![Content::text("You paid 411067 cents.\nAnswer: 411067")],
                ..ChatMessage::default()
            },
        ];
        let entry = completion_log_entry(&req, "miner", vec!["search_web".into()], &messages);
        assert_eq!(entry.case_id, "case-1");
        assert_eq!(entry.harness_spans.len(), 1);
        assert!(entry.harness_spans[0].starts_with("You are Ditto."));
        assert_eq!(entry.record_spans.len(), 1);
        assert!(entry.record_spans[0].contains("411067"));
        assert_eq!(entry.tool_results.len(), 1);
        assert!(entry.tool_results[0].contains("3,418"));
        assert_eq!(entry.completions.len(), 2);
        assert_eq!(entry.completions[0].tool_calls[0].name, "search_web");
        assert_eq!(
            entry.completions[1].text,
            "You paid 411067 cents.\nAnswer: 411067"
        );
        assert_eq!(entry.catalog[0].name, "search_web");
        assert_eq!(entry.tools_offered, vec!["search_web".to_string()]);
        let round: CompletionLogEntry =
            serde_json::from_str(&serde_json::to_string(&entry).unwrap()).unwrap();
        assert_eq!(round, entry);
    }

    #[test]
    fn completion_log_appends_private_json_lines() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("completions.jsonl");
        let entry = CompletionLogEntry {
            case_id: "c".into(),
            bench_version: 9,
            ..CompletionLogEntry::default()
        };
        append_completion_log(&path, &entry).unwrap();
        append_completion_log(&path, &entry).unwrap();
        let body = std::fs::read_to_string(&path).unwrap();
        assert_eq!(body.lines().count(), 2);
        for line in body.lines() {
            let parsed: CompletionLogEntry = serde_json::from_str(line).unwrap();
            assert_eq!(parsed.case_id, "c");
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }
}
