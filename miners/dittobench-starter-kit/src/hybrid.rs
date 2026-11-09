//! Opt-in reference path: Jev decides, the chat model only writes.
//!
//! Enable with `DITTOBENCH_JEV_HYBRID=1`. With it unset the kit's default
//! served path (`Baseline::run`) is byte-for-byte unchanged. This is an
//! example of one way to use the decision lane, not a required library: the
//! competition stays language- and architecture-agnostic.
//!
//! ## Why
//!
//! A v18 seed offers about 1,450 tools (156k tokens of tool JSON, more than
//! gpt-oss-20b's 131k context), and the default path also injects 24 retrieved
//! memories into every turn. Validators price a chat prompt token at 70
//! nano-USD and a decision-lane input token at 42 with output free, and v18
//! stops a run at $1.00 per 350 cases. Selection is the expensive part of a
//! large prompt, and selection is what Jev answers well. So:
//!
//! 1. **Tool selection (Jev `choice`).** A first, small request routes the
//!    case and picks notes. Only a case that will act reads the menu: the
//!    whole menu goes to Jev as `choice`
//!    questions of at most 255 options each (name plus a short description),
//!    packed into as few requests as the 32k-per-question and 64k-per-request
//!    limits allow; the v18 menu fits one request. `DITTOBENCH_JEV_MENU=grouped`
//!    asks about toolkits first (one question over about 185 name prefixes)
//!    and then only about the tools of the top toolkits, which reads about a
//!    fifth of the tokens. The chat model is offered only the top few tools.
//! 2. **Act or not (Jev `noul` and `choice`).** Jev routes the request
//!    (recall / act / remember / chat), says whether the user asked for no
//!    change, and whether the records show the action is already done. Code
//!    extracts argument candidates (URLs, emails, ids, amounts, dates, quoted
//!    and capitalised spans) and Jev picks among them for each required
//!    string argument of the top tool.
//! 3. **Strings from the chat model, from small prompts.** Each chat call gets
//!    the wire system prompt, a short policy, the request, the few memory
//!    notes Jev picked, the argument values Jev confirmed, and three to six
//!    tool schemas. No tool menu, no 24-memory block. The model writes the
//!    message bodies, titles and the final reply, and emits every tool call.
//!
//! ## Gate-safe by construction
//!
//! * Every tool call is model-emitted. The validator's `tool_endpoint` only
//!   executes a call that matches an unconsumed emission of the run's chat
//!   session (name and canonical arguments), so Jev never places a call; it
//!   narrows the menu and suggests values from the request, the seeded
//!   records and served tool results (all exempt from `answer_in_prompt`).
//! * Every chat call that can decide a case also offers the published
//!   semantic top-3 (`v13::semantic_top_k`), the catalog-present safe harbor,
//!   so restraint stays the model's choice even when Jev says "no action".
//! * Every model-emitted call is executed (no `swallowed_model_call`) and the
//!   served text is the model's own (`served_text_not_model_emitted`).
//!
//! The code below is general: no case ids, family names or generator
//! wording, and Jev's answer is used as given (highest probability wins).

use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;

use ditto_harness::types::{ChatMessage, Content, ContentType, Model, Tool, ToolCallResponse};
use serde_json::{json, Value};

use crate::jev::{self, Answers, DecisionClient, DecisionUsage, Question};
use crate::protocol::{ObservedToolCall, ToolDefWire};
use crate::v13;

/// Opt-in switch. Unset (the default) keeps `Baseline::run` unchanged.
pub const HYBRID_ENV: &str = "DITTOBENCH_JEV_HYBRID";
/// `full` (default) or `grouped`; see the module docs.
pub const MENU_ENV: &str = "DITTOBENCH_JEV_MENU";
/// Tools Jev hands the chat model (before the safe-harbor top-3 is added).
pub const OFFER_K_ENV: &str = "DITTOBENCH_JEV_OFFER_K";
/// Memory notes that reach the chat prompt.
pub const NOTES_K_ENV: &str = "DITTOBENCH_JEV_NOTES";
/// Characters of each tool description Jev reads.
pub const DESC_CHARS_ENV: &str = "DITTOBENCH_JEV_DESC_CHARS";
/// Optional JSONL file: one usage line per case (local diagnostics only).
pub const USAGE_LOG_ENV: &str = "DITTOBENCH_JEV_USAGE_LOG";

/// Option every tool-menu chunk carries so chunks can be compared.
pub const NONE_OPTION: &str = "none_of_these";
const NO_TOOL_OPTION: &str = "no_tool_needed";
const NO_NOTE_OPTION: &str = "none_relevant";

/// Short policy appended to the wire system prompt. Values-free.
pub const HYBRID_POLICY: &str = "\
Notes from the user's own records are listed below, most relevant first; \
newer records replace older ones. Answer from them when they hold the \
answer and state the value plainly. search_memories looks further when they \
do not. Call any other tool only when the request needs an action or fresh \
information. If nothing you found answers it, say so and name the closest \
detail you did find. Keep the reply short.";

pub fn enabled_from(value: Option<&str>) -> bool {
    matches!(
        value.map(|v| v.trim().to_ascii_lowercase()).as_deref(),
        Some("1" | "true" | "yes" | "on")
    )
}

/// True when `DITTOBENCH_JEV_HYBRID` opts in.
pub fn enabled() -> bool {
    enabled_from(std::env::var(HYBRID_ENV).ok().as_deref())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MenuMode {
    /// Every tool, in chunks of at most 255 options.
    Full,
    /// Toolkits first, then the tools of the top toolkits.
    Grouped,
}

#[derive(Debug, Clone)]
pub struct HybridConfig {
    pub menu: MenuMode,
    pub offer_k: usize,
    pub notes_k: usize,
    pub desc_chars: usize,
    pub groups_k: usize,
    pub replan_pool: usize,
    pub max_hops: usize,
    pub result_chars: usize,
    pub note_chars: usize,
}

impl Default for HybridConfig {
    fn default() -> Self {
        HybridConfig {
            menu: MenuMode::Full,
            offer_k: 3,
            notes_k: 8,
            desc_chars: 80,
            groups_k: 5,
            replan_pool: 24,
            max_hops: 8,
            result_chars: 1500,
            note_chars: 400,
        }
    }
}

impl HybridConfig {
    pub fn from_env() -> HybridConfig {
        let mut cfg = HybridConfig::default();
        let num = |name: &str| {
            std::env::var(name)
                .ok()
                .and_then(|v| v.trim().parse::<usize>().ok())
                .filter(|v| *v > 0)
        };
        if let Ok(menu) = std::env::var(MENU_ENV) {
            if menu.trim().eq_ignore_ascii_case("grouped") {
                cfg.menu = MenuMode::Grouped;
            }
        }
        if let Some(v) = num(OFFER_K_ENV) {
            cfg.offer_k = v;
        }
        if let Some(v) = num(NOTES_K_ENV) {
            cfg.notes_k = v;
        }
        if let Some(v) = num(DESC_CHARS_ENV) {
            cfg.desc_chars = v;
        }
        cfg
    }
}

/// A retrieved memory, already formatted for a prompt.
#[derive(Debug, Clone)]
pub struct Note {
    pub text: String,
}

/// Everything one case needs. The caller retrieves the notes and builds the
/// executable tools (catalog tools through the validator endpoint, memory
/// tools against the local store).
pub struct HybridCase<'a> {
    pub case_id: &'a str,
    pub user_input: &'a str,
    pub system_prompt: &'a str,
    /// The case's catalog as sent on the wire (memory tools included).
    pub catalog: &'a [ToolDefWire],
    /// Tools the model may be offered, by name.
    pub executable: HashMap<String, Arc<dyn Tool>>,
    /// Names served as real local memory tools.
    pub memory_tools: Vec<String>,
    /// Retrieved memories, best first.
    pub notes: Vec<Note>,
}

#[derive(Debug, Default, Clone, serde::Serialize)]
pub struct HybridOutcome {
    pub final_text: String,
    #[serde(skip)]
    pub tool_calls: Vec<ObservedToolCall>,
    #[serde(skip)]
    pub messages: Vec<ChatMessage>,
    pub tools_offered: Vec<String>,
    pub route: String,
    pub chat_calls: i64,
    pub prompt_tokens: i64,
    pub output_tokens: i64,
    pub max_prompt_tokens: i64,
    pub decision_calls: i64,
    pub decision_input_tokens: i64,
    pub decision_output_tokens: i64,
    pub decision_failures: i64,
    pub notes_used: usize,
    pub menu_asked: bool,
}

// ------------------------------------------------------------------ helpers

fn truncate(text: &str, max: usize) -> String {
    let text = text.trim();
    if text.chars().count() <= max {
        return text.to_string();
    }
    let mut out: String = text.chars().take(max.saturating_sub(1)).collect();
    out.push('…');
    out
}

/// Toolkit key of a tool name: the first `_`-separated segment, lowercased
/// (`GMAIL_SEND_EMAIL` and `gmail_send` are both `gmail`).
pub fn toolkit_of(name: &str) -> String {
    name.split('_')
        .find(|s| !s.is_empty())
        .unwrap_or(name)
        .to_ascii_lowercase()
}

/// `(name, short description)` options for Jev.
pub fn menu_options(catalog: &[ToolDefWire], desc_chars: usize) -> Vec<(String, String)> {
    catalog
        .iter()
        .map(|t| (t.name.clone(), truncate(&t.description, desc_chars)))
        .collect()
}

/// One option per toolkit: up to six member names plus the first member's
/// short description. Returns the options and the members of each toolkit.
/// Catalog indices of each toolkit's tools.
pub type ToolkitMembers = HashMap<String, Vec<usize>>;

pub fn toolkit_options(
    catalog: &[ToolDefWire],
    desc_chars: usize,
) -> (Vec<(String, String)>, ToolkitMembers) {
    let mut order: Vec<String> = Vec::new();
    let mut members: ToolkitMembers = HashMap::new();
    for (i, t) in catalog.iter().enumerate() {
        let key = toolkit_of(&t.name);
        let entry = members.entry(key.clone()).or_default();
        if entry.is_empty() {
            order.push(key);
        }
        entry.push(i);
    }
    let options = order
        .iter()
        .map(|key| {
            let idx = &members[key];
            let names: Vec<&str> = idx
                .iter()
                .take(6)
                .map(|i| catalog[*i].name.as_str())
                .collect();
            let more = if idx.len() > 6 {
                format!(" (+{} more)", idx.len() - 6)
            } else {
                String::new()
            };
            let desc = truncate(&catalog[idx[0]].description, desc_chars.min(60));
            (key.clone(), format!("{}{more}. {desc}", names.join(", ")))
        })
        .collect();
    (options, members)
}

/// Required string parameters of a tool schema, with their descriptions.
pub fn required_string_params(schema: &Value) -> Vec<(String, String)> {
    let required: Vec<&str> = schema
        .get("required")
        .and_then(Value::as_array)
        .map(|r| r.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    let props = schema.get("properties").and_then(Value::as_object);
    required
        .into_iter()
        .filter_map(|name| {
            let prop = props?.get(name)?;
            let kind = prop.get("type").and_then(Value::as_str).unwrap_or("string");
            (kind == "string").then(|| {
                (
                    name.to_string(),
                    prop.get("description")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string(),
                )
            })
        })
        .collect()
}

const EDGE_PUNCT: &[char] = &[
    ',', ';', ':', '(', ')', '[', ']', '{', '}', '<', '>', '"', '\'', '“', '”', '‘', '’', '!', '?',
];

/// General argument candidates in `texts`: URLs, emails, identifiers, numbers
/// and amounts, dates, quoted spans and capitalised phrases. Order of first
/// appearance, deduplicated, at most `limit`.
pub fn extract_candidates(texts: &[&str], limit: usize) -> Vec<String> {
    let mut seen = BTreeSet::new();
    let mut out = Vec::new();
    let mut push = |value: String, out: &mut Vec<String>| {
        let value = value.trim().to_string();
        if value.chars().count() < 2 || out.len() >= limit {
            return;
        }
        if seen.insert(value.to_lowercase()) {
            out.push(value);
        }
    };
    for text in texts {
        // Quoted spans.
        for (open, close) in [('"', '"'), ('“', '”'), ('`', '`')] {
            let mut rest = *text;
            while let Some(start) = rest.find(open) {
                let after = &rest[start + open.len_utf8()..];
                let Some(end) = after.find(close) else { break };
                let span = &after[..end];
                if !span.is_empty() && span.chars().count() <= 80 {
                    push(span.to_string(), &mut out);
                }
                rest = &after[end + close.len_utf8()..];
            }
        }
        let words: Vec<&str> = text.split_whitespace().collect();
        let mut caps: Vec<String> = Vec::new();
        let flush_caps = |caps: &mut Vec<String>,
                          out: &mut Vec<String>,
                          push: &mut dyn FnMut(String, &mut Vec<String>)| {
            if !caps.is_empty() {
                push(caps.join(" "), out);
            }
            caps.clear();
        };
        for (i, raw) in words.iter().enumerate() {
            let token = raw.trim_matches(EDGE_PUNCT);
            let lower = token.to_ascii_lowercase();
            if lower.starts_with("http://") || lower.starts_with("https://") {
                push(token.trim_end_matches(['.', ',']).to_string(), &mut out);
                flush_caps(&mut caps, &mut out, &mut push);
                continue;
            }
            let bare = token.trim_end_matches('.');
            if let Some(at) = bare.find('@') {
                if at > 0 && bare[at..].contains('.') {
                    push(bare.to_string(), &mut out);
                    flush_caps(&mut caps, &mut out, &mut push);
                    continue;
                }
            }
            let has_digit = bare.chars().any(|c| c.is_ascii_digit());
            let has_alpha = bare.chars().any(|c| c.is_alphabetic());
            if has_digit {
                // Amounts and numbers keep their currency/grouping marks.
                let numeric = bare
                    .chars()
                    .all(|c| c.is_ascii_digit() || "$€£,.%-:/".contains(c));
                if numeric || (has_alpha && bare.chars().count() >= 4) {
                    push(bare.to_string(), &mut out);
                }
                flush_caps(&mut caps, &mut out, &mut push);
                continue;
            }
            let starts_upper = bare.chars().next().is_some_and(char::is_uppercase);
            if starts_upper && bare.chars().count() > 1 && i > 0 {
                caps.push(bare.to_string());
                if raw.ends_with(['.', ',', ';', ':', '?', '!']) || caps.len() == 4 {
                    flush_caps(&mut caps, &mut out, &mut push);
                }
            } else {
                flush_caps(&mut caps, &mut out, &mut push);
            }
        }
        flush_caps(&mut caps, &mut out, &mut push);
    }
    out
}

fn text_message(role: &str, text: String) -> ChatMessage {
    ChatMessage {
        role: role.to_string(),
        content: vec![Content::text(text)],
        ..ChatMessage::default()
    }
}

/// The user turn of a hybrid chat call: the request plus only what Jev
/// selected. This is the whole prompt besides the system prompt and the few
/// tool schemas.
pub fn compact_prompt(
    user_input: &str,
    notes: &[Note],
    suggestions: &[(String, String, String)],
    hint: Option<&str>,
) -> String {
    let mut out = format!("Request: {}\n", user_input.trim());
    if !notes.is_empty() {
        out.push_str("\nNotes:\n");
        for note in notes {
            out.push_str("- ");
            out.push_str(&note.text);
            out.push('\n');
        }
    }
    if !suggestions.is_empty() {
        out.push_str("\nValues found in the request and notes:\n");
        for (tool, param, value) in suggestions {
            out.push_str(&format!("- {tool}.{param}: {value}\n"));
        }
    }
    if let Some(hint) = hint {
        out.push('\n');
        out.push_str(hint);
        out.push('\n');
    }
    out
}

fn tool_message(id: &str, name: &str, output: Value, max_chars: usize) -> ChatMessage {
    // A failed call carries `{"error": ...}` and no result; surface it in the
    // protocol error field too, not only inside the output.
    let error = match &output {
        Value::Object(map) if !map.contains_key("result") => map
            .get("error")
            .and_then(Value::as_str)
            .map(|e| truncate(e, max_chars))
            .unwrap_or_default(),
        _ => String::new(),
    };
    let rendered = match &output {
        Value::Object(map) if map.contains_key("result") => map["result"]
            .as_str()
            .map(str::to_string)
            .unwrap_or_else(|| output.to_string()),
        _ => output.to_string(),
    };
    let output = json!(truncate(&rendered, max_chars));
    ChatMessage {
        role: "tool".to_string(),
        tool_call_id: id.to_string(),
        content: vec![Content {
            content_type: Some(ContentType::ToolResult),
            tool_call_response: Some(ToolCallResponse {
                id: id.to_string(),
                name: name.to_string(),
                output,
                error,
            }),
            ..Content::default()
        }],
        ..ChatMessage::default()
    }
}

// ------------------------------------------------------------------ decisions

/// What the first decision pass produced.
#[derive(Debug, Default)]
struct Plan {
    route: String,
    ranked_tools: Vec<String>,
    top_tools: Vec<String>,
    menu_asked: bool,
    notes: Vec<usize>,
    act_unlikely: bool,
}

async fn plan(
    client: Option<&DecisionClient>,
    cfg: &HybridConfig,
    case: &HybridCase<'_>,
    usage: &DecisionUsage,
) -> Plan {
    let mut plan = Plan::default();
    let Some(client) = client else {
        return plan;
    };
    let state = json!({"request": case.user_input});
    let state_tokens = jev::estimate_tokens(&state.to_string());
    let none = vec![(
        NONE_OPTION.to_string(),
        "None of the listed tools fits the request.".to_string(),
    )];
    let mut questions = vec![Question::choice(
        "route",
        "What does the user's message ask for?",
        &[
            ("recall".into(), "A question answered from what the user told me before: their records, notes, people, plans, preferences or past events.".into()),
            ("act".into(), "A request to do or change something (send, create, schedule, delete, update, run) or to fetch fresh outside information (web, links, an app).".into()),
            ("remember".into(), "The user states a fact or preference for me to keep; nothing is asked.".into()),
            ("chat".into(), "A greeting or small talk.".into()),
        ],
    )];
    questions.push(Question::noul(
        "no_change",
        "The user is asking a question, sharing information, or saying not to change anything, rather than asking me to perform an action.",
    ));
    if !case.notes.is_empty() {
        let mut opts: Vec<(String, String)> = case
            .notes
            .iter()
            .enumerate()
            .map(|(i, n)| (format!("n{i}"), truncate(&n.text, 300)))
            .collect();
        opts.push((
            NO_NOTE_OPTION.to_string(),
            "None of these records is relevant to the request.".to_string(),
        ));
        questions.push(Question::choice(
            "note",
            "Which record is most relevant to the user's request?",
            &opts,
        ));
        let records: Vec<String> = case
            .notes
            .iter()
            .take(6)
            .map(|n| truncate(&n.text, 200))
            .collect();
        questions.push(Question::noul(
            "already_done",
            json!({
                "statement": "These records show that what the user asks for has already been done.",
                "records": records,
            }),
        ));
    }
    // Pass 1 is small: route, act-or-not and notes. The menu is read only
    // when the case is going to act, because a case that will not act is
    // offered just the safe harbor anyway.
    let answers = client.ask(case.case_id, &state, questions, usage).await;

    plan.route = answers.choice("route").unwrap_or_default();
    plan.notes = pick_notes(&answers, case.notes.len(), cfg.notes_k);
    let route_p = answers
        .ranked("route")
        .into_iter()
        .find(|(n, _)| *n == plan.route)
        .map(|(_, p)| p)
        .unwrap_or(0.0);
    plan.act_unlikely = (plan.route != "act" && !plan.route.is_empty() && route_p >= 0.5)
        || answers.noul("no_change").unwrap_or(0.0) >= 0.6
        || answers.noul("already_done").unwrap_or(0.0) >= 0.6;
    if plan.act_unlikely {
        return plan;
    }

    // Pass 2: the menu.
    plan.menu_asked = true;
    let tools = match cfg.menu {
        MenuMode::Full => {
            let questions = jev::choice_chunks(
                "tool",
                "Which tool best carries out the user's request?",
                &menu_options(case.catalog, cfg.desc_chars),
                &none,
                jev::MAX_CHOICE_OPTIONS,
                state_tokens,
            );
            client.ask(case.case_id, &state, questions, usage).await
        }
        MenuMode::Grouped => {
            let (toolkits, members) = toolkit_options(case.catalog, cfg.desc_chars);
            let questions = jev::choice_chunks(
                "kit",
                "Which app or tool family is needed to carry out the user's request?",
                &toolkits,
                &none,
                jev::MAX_CHOICE_OPTIONS,
                state_tokens,
            );
            let kits_answers = client.ask(case.case_id, &state, questions, usage).await;
            let kits = top_relative(kits_answers.ranked_chunks("kit", NONE_OPTION), cfg.groups_k);
            let pool: Vec<ToolDefWire> = kits
                .iter()
                .filter_map(|k| members.get(k))
                .flatten()
                .map(|i| case.catalog[*i].clone())
                .collect();
            if pool.is_empty() {
                return plan;
            }
            let questions = jev::choice_chunks(
                "tool",
                "Which tool best carries out the user's request?",
                &menu_options(&pool, cfg.desc_chars),
                &none,
                jev::MAX_CHOICE_OPTIONS,
                state_tokens,
            );
            client.ask(case.case_id, &state, questions, usage).await
        }
    };
    plan.ranked_tools = rank_names(&tools);
    plan.top_tools = top_relative(tools.ranked_chunks("tool", NONE_OPTION), cfg.offer_k);
    plan
}

fn rank_names(answers: &Answers) -> Vec<String> {
    answers
        .ranked_chunks("tool", NONE_OPTION)
        .into_iter()
        .map(|(n, _)| n)
        .collect()
}

/// Share of the best score an option needs to be kept beside it.
const RELATIVE_FLOOR: f64 = 0.05;

/// The best `k` options, dropping any scored below [`RELATIVE_FLOOR`] of
/// the best: a clear winner travels alone, a close race keeps its runners-up.
pub fn top_relative(ranked: Vec<(String, f64)>, k: usize) -> Vec<String> {
    let best = ranked.first().map(|(_, s)| *s).unwrap_or(0.0);
    if best <= 0.0 {
        return Vec::new();
    }
    ranked
        .into_iter()
        .take(k)
        .take_while(|(_, s)| *s >= best * RELATIVE_FLOOR)
        .map(|(n, _)| n)
        .collect()
}

/// The `k` notes Jev ranks highest, in its order. A `choice` distribution
/// piles onto one option, so its probability mass is a poor cut-off (a
/// question needing three records would keep one); Jev orders, `k` cuts.
/// Without an answer, the retrieval order.
fn pick_notes(answers: &Answers, available: usize, k: usize) -> Vec<usize> {
    let ranked = answers.ranked("note");
    if ranked.is_empty() {
        return (0..available.min(k)).collect();
    }
    ranked
        .into_iter()
        .filter_map(|(name, _)| name.strip_prefix('n').and_then(|s| s.parse::<usize>().ok()))
        .filter(|idx| *idx < available)
        .take(k)
        .collect()
}

/// Jev picks a value for each required string argument of `tools` from
/// code-extracted candidates.
async fn suggest_arguments(
    client: &DecisionClient,
    case: &HybridCase<'_>,
    tools: &[&ToolDefWire],
    candidates: &[String],
    usage: &DecisionUsage,
) -> Vec<(String, String, String)> {
    if candidates.is_empty() {
        return Vec::new();
    }
    let mut options: Vec<(String, String)> = candidates
        .iter()
        .enumerate()
        .map(|(i, c)| (format!("c{i}"), c.clone()))
        .collect();
    options.push(("none".to_string(), "None of these values fits.".to_string()));
    let mut questions = Vec::new();
    let mut keys = Vec::new();
    for (ti, tool) in tools.iter().enumerate() {
        for (pi, (param, desc)) in required_string_params(&tool.parameters)
            .into_iter()
            .enumerate()
        {
            let id = format!("arg{ti}_{pi}");
            let instructions = if desc.is_empty() {
                format!(
                    "Which value fills the `{param}` argument of `{}`?",
                    tool.name
                )
            } else {
                format!(
                    "Which value fills the `{param}` argument of `{}` ({})?",
                    tool.name,
                    truncate(&desc, 120)
                )
            };
            questions.push(Question::choice(&id, instructions, &options));
            keys.push((id, tool.name.clone(), param));
        }
    }
    if questions.is_empty() {
        return Vec::new();
    }
    let state = json!({"request": case.user_input});
    let answers = client.ask(case.case_id, &state, questions, usage).await;
    let mut out = Vec::new();
    for (id, tool, param) in keys {
        let Some((choice, p)) = answers.ranked(&id).into_iter().next() else {
            continue;
        };
        if p < 0.4 {
            continue;
        }
        if let Some(i) = choice
            .strip_prefix('c')
            .and_then(|s| s.parse::<usize>().ok())
        {
            if let Some(value) = candidates.get(i) {
                out.push((tool, param, value.clone()));
            }
        }
    }
    out
}

/// After a tool hop: which tool next (from the first pass's pool) and is the
/// request done?
async fn replan(
    client: &DecisionClient,
    case: &HybridCase<'_>,
    pool: &[&ToolDefWire],
    calls: &[(String, Value, String)],
    cfg: &HybridConfig,
    usage: &DecisionUsage,
) -> (Vec<String>, f64) {
    let done_so_far: Vec<Value> = calls
        .iter()
        .map(|(n, a, r)| json!({"tool": n, "args": a, "result": truncate(r, 300)}))
        .collect();
    let state = json!({"request": case.user_input, "done_so_far": done_so_far});
    let mut options = menu_options(
        &pool.iter().map(|t| (*t).clone()).collect::<Vec<_>>(),
        cfg.desc_chars,
    );
    options.push((
        NO_TOOL_OPTION.to_string(),
        "No further tool call is needed.".to_string(),
    ));
    let questions = vec![
        Question::choice(
            "next",
            "Which tool should run next to finish the user's request?",
            &options,
        ),
        Question::noul(
            "done",
            "The tool calls done so far fully carry out the user's request.",
        ),
    ];
    let answers = client.ask(case.case_id, &state, questions, usage).await;
    let next: Vec<String> = answers
        .ranked("next")
        .into_iter()
        .take_while(|(n, _)| n != NO_TOOL_OPTION)
        .take(cfg.offer_k.min(2))
        .map(|(n, _)| n)
        .collect();
    (next, answers.noul("done").unwrap_or(0.0))
}

// ------------------------------------------------------------------ the case

/// Every call a model turn emitted: `tool_calls`, or the legacy singular
/// `tool_call` when the list is empty.
fn emitted_calls(chunk: &ditto_harness::types::ChatChunk) -> Vec<ditto_harness::types::ToolCall> {
    if !chunk.tool_calls.is_empty() {
        return chunk.tool_calls.clone();
    }
    chunk.tool_call.clone().into_iter().collect()
}

/// Executes and records every model-emitted call, offered or not.
#[allow(clippy::too_many_arguments)]
async fn execute_calls(
    case: &HybridCase<'_>,
    tool_calls: &[ditto_harness::types::ToolCall],
    cfg: &HybridConfig,
    hop: &mut i32,
    outcome: &mut HybridOutcome,
    calls: &mut Vec<(String, Value, String)>,
    messages: &mut Vec<ChatMessage>,
    offered: &mut Vec<String>,
) {
    for tc in tool_calls {
        let output = match case.executable.get(&tc.name) {
            Some(tool) => match tool.execute(tc.args.clone()).await {
                Ok(v) => v,
                Err(err) => json!({"error": err.to_string()}),
            },
            None => json!({"error": format!("unknown tool {}", tc.name)}),
        };
        outcome.tool_calls.push(ObservedToolCall {
            name: tc.name.clone(),
            args: tc.args.clone(),
            hop: *hop,
        });
        *hop += 1;
        let rendered = output
            .get("result")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| output.to_string());
        calls.push((tc.name.clone(), tc.args.clone(), rendered));
        messages.push(tool_message(&tc.id, &tc.name, output, cfg.result_chars));
        if !offered.contains(&tc.name) && case.executable.contains_key(&tc.name) {
            offered.push(tc.name.clone());
        }
    }
}

/// Runs one case on the hybrid path.
pub async fn run_case(
    model: &dyn Model,
    decisions: Option<&DecisionClient>,
    cfg: &HybridConfig,
    case: HybridCase<'_>,
) -> anyhow::Result<HybridOutcome> {
    let usage = DecisionUsage::default();
    let mut outcome = HybridOutcome::default();
    let by_name: HashMap<&str, &ToolDefWire> =
        case.catalog.iter().map(|t| (t.name.as_str(), t)).collect();
    let non_memory: Vec<ToolDefWire> = case
        .catalog
        .iter()
        .filter(|t| !case.memory_tools.contains(&t.name))
        .cloned()
        .collect();

    let plan = plan(decisions, cfg, &case, &usage).await;
    outcome.route = plan.route.clone();
    outcome.menu_asked = plan.menu_asked;

    // The safe harbor: the published semantic top-3, on every call.
    let safe_harbor = v13::semantic_top_k(case.user_input, &non_memory, v13::SAFE_HARBOR_TOP_K);
    let mut offered: Vec<String> = Vec::new();
    let offer = |name: &str, offered: &mut Vec<String>| {
        if case.executable.contains_key(name) && !offered.iter().any(|o| o == name) {
            offered.push(name.to_string());
        }
    };
    let jev_top: Vec<String> = if plan.ranked_tools.is_empty() {
        // No decision lane (or it failed): fall back to code-only selection.
        v13::semantic_top_k(case.user_input, &non_memory, cfg.offer_k + 5)
    } else {
        plan.top_tools.clone()
    };
    let mut hint: Option<String> = None;
    if plan.act_unlikely {
        // Restraint stays the model's choice: the safe harbor is still offered.
        // The menu was not read, so rank the memory tools by the request.
        let memory: Vec<ToolDefWire> = case
            .catalog
            .iter()
            .filter(|t| case.memory_tools.contains(&t.name))
            .cloned()
            .collect();
        for name in v13::semantic_top_k(case.user_input, &memory, cfg.offer_k) {
            offer(&name, &mut offered);
        }
        hint = Some(if plan.route == "remember" {
            "This is a fact the user wants kept: save it with save_memory unless the notes already hold it, then confirm briefly."
                .to_string()
        } else {
            "No action seems to be needed: answer from the notes unless the request clearly asks for one."
                .to_string()
        });
    } else {
        for name in &jev_top {
            offer(name, &mut offered);
        }
    }
    for name in &safe_harbor {
        offer(name, &mut offered);
    }
    // Memory search is always one schema away; saving only when told a fact.
    offer("search_memories", &mut offered);
    if plan.route == "remember" {
        offer("save_memory", &mut offered);
    }

    let notes: Vec<Note> = plan
        .notes
        .iter()
        .filter_map(|i| case.notes.get(*i))
        .map(|n| Note {
            text: truncate(&n.text, cfg.note_chars),
        })
        .collect();
    outcome.notes_used = notes.len();

    let mut suggestions = Vec::new();
    if let (Some(client), false) = (decisions, plan.act_unlikely) {
        let note_texts: Vec<&str> = notes.iter().map(|n| n.text.as_str()).collect();
        let mut texts = vec![case.user_input];
        texts.extend(note_texts);
        let candidates = extract_candidates(&texts, 40);
        let targets: Vec<&ToolDefWire> = jev_top
            .iter()
            .filter(|n| !case.memory_tools.contains(n))
            .filter_map(|n| by_name.get(n.as_str()).copied())
            .take(1)
            .collect();
        suggestions = suggest_arguments(client, &case, &targets, &candidates, &usage).await;
    }

    // Pool the re-planner chooses from: the first pass's top tools.
    let pool: Vec<&ToolDefWire> = plan
        .ranked_tools
        .iter()
        .take(cfg.replan_pool)
        .filter_map(|n| by_name.get(n.as_str()).copied())
        .collect();

    let system = if case.system_prompt.trim().is_empty() {
        HYBRID_POLICY.to_string()
    } else {
        format!("{}\n\n{HYBRID_POLICY}", case.system_prompt.trim_end())
    };
    let mut messages = vec![
        text_message("system", system),
        text_message(
            "user",
            compact_prompt(case.user_input, &notes, &suggestions, hint.as_deref()),
        ),
    ];
    let mut calls: Vec<(String, Value, String)> = Vec::new();
    let mut all_offered: BTreeSet<String> = offered.iter().cloned().collect();
    let mut hop = 0i32;
    let mut done_prompted = false;

    for turn in 0..cfg.max_hops {
        let defs: Vec<_> = offered
            .iter()
            .filter_map(|n| case.executable.get(n))
            .map(|t| t.definition())
            .collect();
        let chunk = model
            .next(&messages, &defs)
            .await
            .map_err(|err| anyhow::anyhow!("hybrid chat call: {err}"))?;
        outcome.chat_calls += 1;
        if let Some(cost) = &chunk.cost {
            outcome.prompt_tokens += cost.usage.input_tokens;
            outcome.output_tokens += cost.usage.output_tokens;
            outcome.max_prompt_tokens = outcome.max_prompt_tokens.max(cost.usage.input_tokens);
        }
        let tool_calls = emitted_calls(&chunk);
        if tool_calls.is_empty() {
            outcome.final_text = chunk.text.clone();
            messages.push(text_message("assistant", chunk.text));
            break;
        }
        messages.push(ChatMessage {
            role: "assistant".to_string(),
            content: if chunk.text.is_empty() {
                Vec::new()
            } else {
                vec![Content::text(chunk.text.clone())]
            },
            tool_calls: tool_calls.clone(),
            ..ChatMessage::default()
        });
        execute_calls(
            &case,
            &tool_calls,
            cfg,
            &mut hop,
            &mut outcome,
            &mut calls,
            &mut messages,
            &mut offered,
        )
        .await;
        if turn + 1 == cfg.max_hops {
            break;
        }
        if let (Some(client), false) = (decisions, pool.is_empty()) {
            let (next, done) = replan(client, &case, &pool, &calls, cfg, &usage).await;
            for name in next {
                if case.executable.contains_key(&name) && !offered.contains(&name) {
                    offered.push(name);
                }
            }
            if done >= 0.7 && !done_prompted {
                done_prompted = true;
                messages.push(text_message(
                    "user",
                    "The request looks complete. Reply to the user with the result.".to_string(),
                ));
            }
        }
        all_offered.extend(offered.iter().cloned());
    }
    if outcome.final_text.is_empty() && messages.last().is_some_and(|m| m.role == "tool") {
        // The hop budget ran out right after tool results: one short call
        // with no tools writes the reply from them.
        messages.push(text_message(
            "user",
            "Reply to the user now with the result of the tool calls above.".to_string(),
        ));
        let chunk = model
            .next(&messages, &[])
            .await
            .map_err(|err| anyhow::anyhow!("hybrid chat call: {err}"))?;
        outcome.chat_calls += 1;
        if let Some(cost) = &chunk.cost {
            outcome.prompt_tokens += cost.usage.input_tokens;
            outcome.output_tokens += cost.usage.output_tokens;
            outcome.max_prompt_tokens = outcome.max_prompt_tokens.max(cost.usage.input_tokens);
        }
        let tool_calls = emitted_calls(&chunk);
        if tool_calls.is_empty() {
            outcome.final_text = chunk.text.clone();
            messages.push(text_message("assistant", chunk.text));
        } else {
            // No tools were offered, but a provider may still return a call.
            // Every model-emitted call is executed and recorded; the budget
            // is spent, so no further model call follows and the reply is
            // whatever text came with the calls.
            outcome.final_text = chunk.text.clone();
            messages.push(ChatMessage {
                role: "assistant".to_string(),
                content: if chunk.text.is_empty() {
                    Vec::new()
                } else {
                    vec![Content::text(chunk.text.clone())]
                },
                tool_calls: tool_calls.clone(),
                ..ChatMessage::default()
            });
            execute_calls(
                &case,
                &tool_calls,
                cfg,
                &mut hop,
                &mut outcome,
                &mut calls,
                &mut messages,
                &mut offered,
            )
            .await;
        }
    }
    all_offered.extend(offered.iter().cloned());
    outcome.tools_offered = all_offered.into_iter().collect();
    outcome.messages = messages;
    let (calls_n, input, output, failures) = usage.snapshot();
    outcome.decision_calls = calls_n;
    outcome.decision_input_tokens = input;
    outcome.decision_output_tokens = output;
    outcome.decision_failures = failures;
    Ok(outcome)
}

/// Appends one usage line for `case_id` when `DITTOBENCH_JEV_USAGE_LOG` is set.
pub fn log_usage(case_id: &str, outcome: &HybridOutcome) {
    let Ok(path) = std::env::var(USAGE_LOG_ENV) else {
        return;
    };
    if path.trim().is_empty() {
        return;
    }
    append_usage(path.trim(), case_id, outcome);
}

fn append_usage(path: &str, case_id: &str, outcome: &HybridOutcome) {
    let mut line = serde_json::to_value(outcome).unwrap_or(Value::Null);
    if let Some(map) = line.as_object_mut() {
        map.insert("case_id".to_string(), json!(case_id));
        map.insert("tool_calls".to_string(), json!(outcome.tool_calls.len()));
        map.remove("final_text");
    }
    use std::io::Write;
    // Cases run concurrently, and `write_all` may split a line across writes
    // after a partial one: hold a lock so two cases' records never interleave.
    static LOG_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    let _guard = LOG_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    {
        let _ = file.write_all(format!("{line}\n").as_bytes());
    }
}

#[cfg(test)]
mod tests;
