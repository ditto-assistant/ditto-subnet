//! Client and question builders for the scored decision lane
//! (`DITTOBENCH_DECISIONS_URL`, TypeSafe Jev System One).
//!
//! A System One request is `{model, state, questions: {<id>: {type,
//! instructions, criteria}}}`; the answer is `{model, answers: {<id>: ...},
//! usage: {input_tokens, output_tokens}}`. Three primitives matter here
//! (<https://docs.typesafe.ai/primitives>):
//!
//! * `choice`: pick one of up to 255 named options. The answer carries the
//!   full `probabilities` map, so one question also ranks every option.
//! * `noul`: probability that a statement is true.
//! * `score`: an ordinal scale (unused by the reference path).
//!
//! Limits: 32k tokens per question (state plus that question) and 64k per
//! request (state plus every question). Validators price decision input at
//! 42 nano-USD per token and decision output at 0, against 70 for a chat
//! prompt token, so Jev reads are cheaper than chat reads but not free: send
//! it short option text and a short state.
//!
//! The canonical broker signs the request with the run's grant and forces the
//! pinned model, so a scored run needs no key. Local practice can point the
//! URL at OpenRouter's System One route and set `DITTOBENCH_DECISIONS_API_KEY`
//! (or let the rehearsal's cost meter front it keylessly).

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::Duration;

use serde_json::{json, Map, Value};

/// Endpoint the sandbox receives on bench 15+.
pub const DECISIONS_URL_ENV: &str = "DITTOBENCH_DECISIONS_URL";
/// Optional bearer for local practice against a provider directly.
pub const DECISIONS_API_KEY_ENV: &str = "DITTOBENCH_DECISIONS_API_KEY";
/// Model id sent in the body. The canonical relay forces its own pin.
pub const DECISIONS_MODEL_ENV: &str = "DITTOBENCH_DECISIONS_MODEL";
pub const DEFAULT_DECISIONS_MODEL: &str = "typesafe/jev-1.14";

/// System One `choice` accepts at most this many options.
pub const MAX_CHOICE_OPTIONS: usize = 255;
/// Per-question context limit (state plus one question), in tokens.
pub const MAX_QUESTION_TOKENS: usize = 32_000;
/// Per-request context limit (state plus all questions), in tokens.
pub const MAX_REQUEST_TOKENS: usize = 64_000;
/// Longest `Retry-After` a decision call waits before retrying, in seconds.
pub const MAX_RETRY_AFTER_SECS: u64 = 30;
/// Headroom kept under each limit, because [`estimate_tokens`] is an estimate.
const LIMIT_HEADROOM: f64 = 0.85;

/// Conservative token estimate for JSON text: about 3 ASCII characters per
/// token, rounded up, plus one token per non-ASCII byte. Real tokenizers
/// average closer to 4 characters on English and never emit more tokens than
/// bytes, so CJK or emoji text (often a token a character) is not undercounted
/// and packed requests stay under the provider limit.
pub fn estimate_tokens(text: &str) -> usize {
    let ascii = text.bytes().filter(u8::is_ascii).count();
    ascii.div_ceil(3) + (text.len() - ascii)
}

/// The longest prefix of `text` whose [`estimate_tokens`] is at most `tokens`.
fn truncate_to_tokens(text: &str, tokens: usize) -> &str {
    let (mut ascii, mut other) = (0usize, 0usize);
    for (i, c) in text.char_indices() {
        if c.is_ascii() {
            ascii += 1;
        } else {
            other += c.len_utf8();
        }
        if ascii.div_ceil(3) + other > tokens {
            return &text[..i];
        }
    }
    text
}

fn estimate_value_tokens(value: &Value) -> usize {
    estimate_tokens(&value.to_string())
}

/// One question in a System One request.
#[derive(Debug, Clone, PartialEq)]
pub struct Question {
    pub id: String,
    pub body: Value,
}

impl Question {
    pub fn noul(id: impl Into<String>, instructions: impl Into<Value>) -> Question {
        Question {
            id: id.into(),
            body: json!({"type": "noul", "instructions": instructions.into()}),
        }
    }

    /// A `choice` over `options` (`name -> description`, in order). Never
    /// panics: a repeated name keeps its first description, and past
    /// [`MAX_CHOICE_OPTIONS`] options the middle is dropped so the trailing
    /// reference option ("none of these") survives. Both are reported on
    /// stderr; use [`choice_chunks`] for longer lists.
    pub fn choice(
        id: impl Into<String>,
        instructions: impl Into<Value>,
        options: &[(String, String)],
    ) -> Question {
        let id = id.into();
        let mut criteria = Map::new();
        let mut duplicates = 0usize;
        for (name, description) in options {
            if criteria.contains_key(name) {
                duplicates += 1;
                continue;
            }
            let description = if description.is_empty() {
                Value::Null
            } else {
                Value::String(description.clone())
            };
            criteria.insert(name.clone(), description);
        }
        let dropped = criteria.len().saturating_sub(MAX_CHOICE_OPTIONS);
        if dropped > 0 {
            let last = options.last().map(|(name, _)| name.as_str());
            // Each kept name once: a repeated name must not use up a drop.
            let mut seen = std::collections::HashSet::new();
            let excess: Vec<String> = options
                .iter()
                .map(|(name, _)| name)
                .filter(|name| {
                    Some(name.as_str()) != last
                        && criteria.contains_key(*name)
                        && seen.insert(name.as_str())
                })
                .skip(MAX_CHOICE_OPTIONS - 1)
                .take(dropped)
                .cloned()
                .collect();
            for name in excess {
                criteria.remove(&name);
            }
        }
        if duplicates > 0 || dropped > 0 {
            eprintln!(
                "jev: choice {id}: dropped {duplicates} repeated and {dropped} over-limit option(s)"
            );
        }
        Question {
            id,
            body: json!({
                "type": "choice",
                "instructions": instructions.into(),
                "criteria": criteria,
            }),
        }
    }

    pub fn estimated_tokens(&self) -> usize {
        estimate_value_tokens(&self.body) + estimate_tokens(&self.id)
    }
}

/// Splits `options` into `choice` questions `<prefix>0`, `<prefix>1`, ...
/// Each holds at most `max_per_question` options (capped at 255) plus the
/// `extra` options (a "none of these" reference, for example) and stays under
/// the per-question token limit given `state_tokens`. Returns no questions
/// when `extra` alone fills the 255-option cap.
pub fn choice_chunks(
    prefix: &str,
    instructions: &str,
    options: &[(String, String)],
    extra: &[(String, String)],
    max_per_question: usize,
    state_tokens: usize,
) -> Vec<Question> {
    // Extras that fill the cap leave no room for a regular option.
    if extra.len() >= MAX_CHOICE_OPTIONS {
        return Vec::new();
    }
    let cap = max_per_question
        .min(MAX_CHOICE_OPTIONS - extra.len())
        .max(1);
    let budget = ((MAX_QUESTION_TOKENS as f64) * LIMIT_HEADROOM) as usize;
    let base = estimate_tokens(instructions) + 40 + state_tokens;
    let extra_tokens: usize = extra
        .iter()
        .map(|(n, d)| estimate_tokens(n) + estimate_tokens(d) + 6)
        .sum();
    let mut out = Vec::new();
    let mut current: Vec<(String, String)> = Vec::new();
    let mut current_tokens = base + extra_tokens;
    let flush = |current: &mut Vec<(String, String)>, out: &mut Vec<Question>| {
        if current.is_empty() {
            return;
        }
        let mut opts = std::mem::take(current);
        opts.extend(extra.iter().cloned());
        out.push(Question::choice(
            format!("{prefix}{}", out.len()),
            instructions,
            &opts,
        ));
    };
    // The most one option may cost so that it fits a question on its own.
    let room = budget.saturating_sub(base + extra_tokens);
    for option in options {
        let mut option = option.clone();
        let mut cost = estimate_tokens(&option.0) + estimate_tokens(&option.1) + 6;
        if cost > room {
            // Shorten the description to the room left; a name too long to
            // fit even alone is dropped rather than sent over the provider's
            // per-question limit.
            let name_cost = estimate_tokens(&option.0) + 6;
            if name_cost > room {
                eprintln!(
                    "jev: choice_chunks {prefix}: option {:?} dropped, too large for one question (room {room})",
                    option.0
                );
                continue;
            }
            option.1 = truncate_to_tokens(&option.1, room - name_cost).to_string();
            cost = estimate_tokens(&option.0) + estimate_tokens(&option.1) + 6;
        }
        if !current.is_empty() && (current.len() >= cap || current_tokens + cost > budget) {
            flush(&mut current, &mut out);
            current_tokens = base + extra_tokens;
        }
        current.push(option);
        current_tokens += cost;
    }
    flush(&mut current, &mut out);
    out
}

/// Packs questions into as few requests as the per-request limit allows,
/// keeping their order. With the v18 menu (about 1,450 tools) and 80-character
/// descriptions every question fits one request. A question that cannot fit
/// the per-question limit with `state`, even alone, is not sent: its id is
/// returned second so the caller can report it, and the rest still go out.
pub fn pack_requests(questions: Vec<Question>, state: &Value) -> (Vec<Vec<Question>>, Vec<String>) {
    let budget = ((MAX_REQUEST_TOKENS as f64) * LIMIT_HEADROOM) as usize;
    let question_budget = ((MAX_QUESTION_TOKENS as f64) * LIMIT_HEADROOM) as usize;
    let state_tokens = estimate_value_tokens(state);
    let mut out: Vec<Vec<Question>> = Vec::new();
    let mut skipped: Vec<String> = Vec::new();
    let mut current: Vec<Question> = Vec::new();
    let mut used = state_tokens;
    for question in questions {
        let cost = question.estimated_tokens();
        if state_tokens + cost > question_budget {
            skipped.push(question.id);
            continue;
        }
        if !current.is_empty() && used + cost > budget {
            out.push(std::mem::take(&mut current));
            used = state_tokens;
        }
        used += cost;
        current.push(question);
    }
    if !current.is_empty() {
        out.push(current);
    }
    (out, skipped)
}

/// The answers of one or more System One requests, keyed by question id.
#[derive(Debug, Clone, Default)]
pub struct Answers {
    pub answers: BTreeMap<String, Value>,
}

impl Answers {
    pub fn merge(&mut self, raw: &Value) {
        if let Some(map) = raw.as_object() {
            for (id, answer) in map {
                self.answers.insert(id.clone(), answer.clone());
            }
        }
    }

    /// `noul` probability for `id`, if answered.
    pub fn noul(&self, id: &str) -> Option<f64> {
        self.answers.get(id)?.get("noul")?.as_f64()
    }

    /// The chosen option of a `choice` question.
    pub fn choice(&self, id: &str) -> Option<String> {
        Some(self.answers.get(id)?.get("choice")?.as_str()?.to_string())
    }

    /// Every option of a `choice` question with its probability, best first.
    pub fn ranked(&self, id: &str) -> Vec<(String, f64)> {
        let Some(probs) = self
            .answers
            .get(id)
            .and_then(|a| a.get("probabilities"))
            .and_then(Value::as_object)
        else {
            return Vec::new();
        };
        let mut out: Vec<(String, f64)> = probs
            .iter()
            .filter_map(|(k, v)| v.as_f64().map(|p| (k.clone(), p)))
            .collect();
        out.sort_by(|a, b| b.1.total_cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
        out
    }

    /// Ranks options across every chunk `<prefix>0..` of a chunked choice.
    ///
    /// Probabilities from different chunks are normalised within their own
    /// chunk, so each chunk carries the same `reference` option ("none of
    /// these"). An option's cross-chunk score is its probability divided by
    /// its chunk's reference probability (floored), which puts a strong match
    /// in a weak chunk above a near-tie in a strong one. The reference itself
    /// is dropped.
    pub fn ranked_chunks(&self, prefix: &str, reference: &str) -> Vec<(String, f64)> {
        let mut out = Vec::new();
        for id in self.answers.keys() {
            let Some(suffix) = id.strip_prefix(prefix) else {
                continue;
            };
            if suffix.is_empty() || !suffix.chars().all(|c| c.is_ascii_digit()) {
                continue;
            }
            let ranked = self.ranked(id);
            // A chunk whose answer lacks the reference cannot be normalised
            // against the others; skip it rather than inflate it.
            let Some(none) = ranked
                .iter()
                .find(|(name, _)| name == reference)
                .map(|(_, p)| *p)
            else {
                continue;
            };
            let none = none.max(1e-3);
            for (name, p) in ranked {
                if name != reference {
                    out.push((name, p / none));
                }
            }
        }
        out.sort_by(|a, b| b.1.total_cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
        out
    }
}

/// Token and call counters for one case (or one run).
#[derive(Debug, Default)]
pub struct DecisionUsage {
    pub calls: AtomicI64,
    pub input_tokens: AtomicI64,
    pub output_tokens: AtomicI64,
    pub failures: AtomicI64,
}

impl DecisionUsage {
    pub fn snapshot(&self) -> (i64, i64, i64, i64) {
        (
            self.calls.load(Ordering::Relaxed),
            self.input_tokens.load(Ordering::Relaxed),
            self.output_tokens.load(Ordering::Relaxed),
            self.failures.load(Ordering::Relaxed),
        )
    }
}

/// HTTP client for the decision lane.
#[derive(Clone)]
pub struct DecisionClient {
    http: reqwest::Client,
    url: String,
    model: String,
    api_key: Option<String>,
}

impl DecisionClient {
    pub fn new(
        http: reqwest::Client,
        url: impl Into<String>,
        model: impl Into<String>,
        api_key: Option<String>,
    ) -> DecisionClient {
        DecisionClient {
            http,
            url: url.into(),
            model: model.into(),
            // Trimmed: a key pasted with a trailing newline is not a valid
            // header value, and every call would fail.
            api_key: api_key
                .map(|k| k.trim().to_string())
                .filter(|k| !k.is_empty()),
        }
    }

    /// The client the sandbox environment describes, or `None` when
    /// `DITTOBENCH_DECISIONS_URL` is unset (bench < 15, confirmation runs, or
    /// local practice without a decision endpoint).
    pub fn from_env(http: reqwest::Client) -> Option<DecisionClient> {
        let url = std::env::var(DECISIONS_URL_ENV).ok()?;
        if url.trim().is_empty() {
            return None;
        }
        let model = std::env::var(DECISIONS_MODEL_ENV)
            .ok()
            .map(|m| m.trim().to_string())
            .filter(|m| !m.is_empty())
            .unwrap_or_else(|| DEFAULT_DECISIONS_MODEL.to_string());
        Some(DecisionClient::new(
            http,
            url.trim(),
            model,
            std::env::var(DECISIONS_API_KEY_ENV).ok(),
        ))
    }

    pub fn request_body(&self, state: &Value, questions: &[Question]) -> Value {
        let mut map = Map::new();
        for q in questions {
            map.insert(q.id.clone(), q.body.clone());
        }
        json!({"model": self.model, "state": state, "questions": map})
    }

    /// Sends one request. Retries 429/5xx twice with a short backoff
    /// (honouring `Retry-After` up to [`MAX_RETRY_AFTER_SECS`]), then gives up and returns an error; callers
    /// fall back to code-only decisions rather than failing the case.
    pub async fn ask_one(
        &self,
        case_id: &str,
        state: &Value,
        questions: &[Question],
        usage: &DecisionUsage,
    ) -> anyhow::Result<Value> {
        let body = self.request_body(state, questions);
        let mut last_err = None;
        const ATTEMPTS: u64 = 3;
        // Only wait when another attempt follows; the last failure returns now.
        let backoff = |attempt: u64, wait: Duration| async move {
            if attempt + 1 < ATTEMPTS {
                tokio::time::sleep(wait).await;
            }
        };
        for attempt in 0..ATTEMPTS {
            let mut req = self
                .http
                .post(&self.url)
                .timeout(Duration::from_secs(60))
                .header("X-Ditto-Case-Id", case_id)
                .json(&body);
            if let Some(key) = &self.api_key {
                req = req.bearer_auth(key);
            }
            match req.send().await {
                Ok(resp) => {
                    let status = resp.status();
                    let retry_after = resp
                        .headers()
                        .get("retry-after")
                        .and_then(|v| v.to_str().ok())
                        .and_then(|v| v.trim().parse::<u64>().ok());
                    let text = resp.text().await.unwrap_or_default();
                    let parsed: Value = match serde_json::from_str(&text) {
                        Ok(parsed) => parsed,
                        Err(err) => {
                            // Unreadable even on a 2xx: a reported failure,
                            // never a silent empty answer set. Only 429/5xx
                            // retry; a metered 2xx would be billed again.
                            usage.calls.fetch_add(1, Ordering::Relaxed);
                            last_err = Some(anyhow::anyhow!(
                                "decision lane returned {status} with an unparseable body: {err}: {}",
                                text.chars().take(300).collect::<String>()
                            ));
                            if !(status.as_u16() == 429 || status.is_server_error()) {
                                break;
                            }
                            backoff(attempt, Duration::from_millis(500 * (attempt + 1))).await;
                            continue;
                        }
                    };
                    // A metered call is priced from its usage even when it
                    // answers an error, so count usage whenever it is present.
                    if let Some(u) = parsed.get("usage") {
                        usage.input_tokens.fetch_add(
                            u.get("input_tokens").and_then(Value::as_i64).unwrap_or(0),
                            Ordering::Relaxed,
                        );
                        usage.output_tokens.fetch_add(
                            u.get("output_tokens").and_then(Value::as_i64).unwrap_or(0),
                            Ordering::Relaxed,
                        );
                    }
                    usage.calls.fetch_add(1, Ordering::Relaxed);
                    if status.is_success() {
                        if let Some(answers) = parsed.get("answers").filter(|a| a.is_object()) {
                            return Ok(answers.clone());
                        }
                        // Not retried: a metered 2xx would be billed again.
                        last_err = Some(anyhow::anyhow!(
                            "decision lane returned {status} without an answers object"
                        ));
                        break;
                    }
                    last_err = Some(anyhow::anyhow!(
                        "decision lane returned {status}: {}",
                        text.chars().take(300).collect::<String>()
                    ));
                    if !(status.as_u16() == 429 || status.is_server_error()) {
                        break;
                    }
                    // Honour Retry-After, capped so one throttled call cannot
                    // hold a case past the validator's case deadline.
                    let wait = retry_after.unwrap_or(1 + attempt).min(MAX_RETRY_AFTER_SECS);
                    backoff(attempt, Duration::from_secs(wait)).await;
                }
                Err(err) => {
                    last_err = Some(anyhow::anyhow!("decision lane unreachable: {err}"));
                    backoff(attempt, Duration::from_millis(500 * (attempt + 1))).await;
                }
            }
        }
        usage.failures.fetch_add(1, Ordering::Relaxed);
        Err(last_err.unwrap_or_else(|| anyhow::anyhow!("decision lane failed")))
    }

    /// Packs `questions` into as few requests as the limits allow, sends them
    /// concurrently and merges the answers. Requests that fail, and questions
    /// too large to send, are skipped and counted as failures: their
    /// questions are simply unanswered.
    pub async fn ask(
        &self,
        case_id: &str,
        state: &Value,
        questions: Vec<Question>,
        usage: &DecisionUsage,
    ) -> Answers {
        let (batches, skipped) = pack_requests(questions, state);
        if !skipped.is_empty() {
            usage
                .failures
                .fetch_add(skipped.len() as i64, Ordering::Relaxed);
            eprintln!(
                "jev: {case_id}: not sent, over the per-question limit: {}",
                skipped.join(", ")
            );
        }
        let futures = batches
            .iter()
            .map(|batch| self.ask_one(case_id, state, batch, usage));
        let mut answers = Answers::default();
        for result in futures::future::join_all(futures).await {
            match result {
                Ok(raw) => answers.merge(&raw),
                Err(err) => eprintln!("jev: {case_id}: {err}"),
            }
        }
        answers
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn opts(n: usize, desc: &str) -> Vec<(String, String)> {
        (0..n)
            .map(|i| (format!("tool_{i}"), desc.to_string()))
            .collect()
    }

    #[test]
    fn chunks_respect_the_option_cap_and_carry_the_reference() {
        let none = vec![(
            "none_of_these".to_string(),
            "No listed tool fits.".to_string(),
        )];
        let qs = choice_chunks("t", "Which tool?", &opts(1454, "short"), &none, 255, 20);
        assert_eq!(qs.len(), 6, "1454 tools at 254 per chunk");
        let mut seen = 0;
        for (i, q) in qs.iter().enumerate() {
            assert_eq!(q.id, format!("t{i}"));
            let criteria = q.body["criteria"].as_object().unwrap();
            assert!(criteria.len() <= MAX_CHOICE_OPTIONS);
            assert!(criteria.contains_key("none_of_these"));
            seen += criteria.len() - 1;
            assert!(q.estimated_tokens() < MAX_QUESTION_TOKENS);
        }
        assert_eq!(seen, 1454, "every tool is asked about exactly once");
    }

    #[test]
    fn a_choice_drops_repeats_and_excess_without_panicking() {
        let mut options = opts(3, "d");
        options.push(("tool_1".to_string(), "later".to_string()));
        let q = Question::choice("c", "Which?", &options);
        let criteria = q.body["criteria"].as_object().unwrap();
        assert_eq!(criteria.len(), 3);
        assert_eq!(criteria["tool_1"], json!("d"), "the first description wins");

        let mut options = opts(300, "d");
        options.push(("none_of_these".to_string(), String::new()));
        let q = Question::choice("c", "Which?", &options);
        let criteria = q.body["criteria"].as_object().unwrap();
        assert_eq!(criteria.len(), MAX_CHOICE_OPTIONS);
        assert!(
            criteria.contains_key("none_of_these"),
            "the reference survives"
        );
        assert!(criteria.contains_key("tool_253"));
        assert!(!criteria.contains_key("tool_254"));

        // Repeats inside the dropped window must not use up a drop.
        let mut options = opts(258, "d");
        for i in 254..258 {
            options.insert(i + 1, (format!("tool_{i}"), "again".to_string()));
        }
        options.push(("none_of_these".to_string(), String::new()));
        let q = Question::choice("c", "Which?", &options);
        assert_eq!(
            q.body["criteria"].as_object().unwrap().len(),
            MAX_CHOICE_OPTIONS
        );
    }

    #[test]
    fn an_oversized_option_is_shortened_to_fit_one_question() {
        let huge = vec![("big".to_string(), "y".repeat(200_000))];
        let qs = choice_chunks("t", "Which?", &huge, &[], 255, 0);
        assert_eq!(qs.len(), 1);
        assert!(qs[0].estimated_tokens() <= MAX_QUESTION_TOKENS);
        let name = vec![("n".repeat(200_000), String::new())];
        assert!(choice_chunks("t", "Which?", &name, &[], 255, 0).is_empty());
    }

    #[test]
    fn non_ascii_text_is_not_undercounted() {
        assert_eq!(estimate_tokens("abcdefg"), 3);
        let cjk = "日本語".repeat(1000);
        assert!(estimate_tokens(&cjk) >= cjk.chars().count());
        assert_eq!(truncate_to_tokens("ab日c", 1), "ab");
        assert_eq!(truncate_to_tokens("ab日c", 4), "ab日c");
        let huge = vec![("big".to_string(), "語".repeat(200_000))];
        let qs = choice_chunks("t", "Which?", &huge, &[], 255, 0);
        assert_eq!(qs.len(), 1);
        assert!(qs[0].estimated_tokens() <= MAX_QUESTION_TOKENS);
    }

    #[test]
    fn extras_that_fill_the_cap_yield_no_questions() {
        let extras = |n: usize| -> Vec<(String, String)> {
            (0..n)
                .map(|i| (format!("ref_{i}"), String::new()))
                .collect()
        };
        let extra = extras(MAX_CHOICE_OPTIONS);
        assert!(choice_chunks("t", "Which?", &opts(3, "d"), &extra, 255, 0).is_empty());
        let extra = extras(MAX_CHOICE_OPTIONS - 1);
        let qs = choice_chunks("t", "Which?", &opts(3, "d"), &extra, 255, 0);
        assert_eq!(qs.len(), 3);
        for q in &qs {
            assert_eq!(
                q.body["criteria"].as_object().unwrap().len(),
                MAX_CHOICE_OPTIONS
            );
        }
    }

    #[test]
    fn chunks_split_on_tokens_before_the_option_cap() {
        let long = "x".repeat(1200);
        let qs = choice_chunks("t", "Which?", &opts(255, &long), &[], 255, 0);
        assert!(qs.len() > 1);
        for q in &qs {
            assert!(q.estimated_tokens() <= MAX_QUESTION_TOKENS);
        }
    }

    #[test]
    fn the_v18_menu_packs_into_one_request() {
        let none = vec![("none_of_these".to_string(), String::new())];
        let desc = "d".repeat(80);
        let state = json!({"request": "Kick off the weekly digest workflow."});
        let qs = choice_chunks("t", "Which tool?", &opts(1454, &desc), &none, 255, 20);
        let (batches, skipped) = pack_requests(qs, &state);
        assert_eq!(batches.len(), 1, "names plus 80-char descriptions fit 64k");
        assert!(skipped.is_empty());
        let qs = choice_chunks("t", "Which tool?", &opts(4000, &desc), &none, 255, 20);
        assert!(
            pack_requests(qs, &state).0.len() > 1,
            "a 4,000-tool menu splits"
        );
    }

    #[test]
    fn an_oversized_question_is_skipped_and_the_rest_still_go() {
        let state = json!({"request": "Was the invoice already paid?"});
        let questions = vec![
            Question::noul("small", "The invoice was paid."),
            Question::noul("huge", "x".repeat(200_000)),
            Question::noul("after", "The user asked twice."),
        ];
        let (batches, skipped) = pack_requests(questions, &state);
        assert_eq!(skipped, vec!["huge".to_string()]);
        let ids: Vec<&str> = batches.iter().flatten().map(|q| q.id.as_str()).collect();
        assert_eq!(ids, vec!["small", "after"]);
    }

    #[tokio::test]
    async fn ask_reports_an_oversized_question_without_sending_it() {
        // Nothing listens here: a request that were sent would fail as
        // unreachable after its retries, not instantly.
        let client = DecisionClient::new(reqwest::Client::new(), "http://127.0.0.1:9", "m", None);
        let usage = DecisionUsage::default();
        let answers = client
            .ask(
                "case-1",
                &json!({"request": "x"}),
                vec![Question::noul("huge", "x".repeat(200_000))],
                &usage,
            )
            .await;
        assert!(answers.answers.is_empty());
        let (calls, _, _, failures) = usage.snapshot();
        assert_eq!((calls, failures), (0, 1));
    }

    #[tokio::test]
    async fn retry_exhaustion_returns_without_a_final_wait() {
        // Nothing listens: three unreachable attempts back off 0.5 s and 1 s
        // between them and return right after the third, not 1.5 s later.
        let client = DecisionClient::new(reqwest::Client::new(), "http://127.0.0.1:9", "m", None);
        let usage = DecisionUsage::default();
        let started = std::time::Instant::now();
        let result = client
            .ask_one("case-1", &json!({}), &[Question::noul("q", "x")], &usage)
            .await;
        assert!(result.is_err());
        let elapsed = started.elapsed();
        assert!(elapsed < Duration::from_millis(2_500), "{elapsed:?}");
    }

    #[test]
    fn the_api_key_is_trimmed_before_bearer_use() {
        let client = |key: &str| {
            DecisionClient::new(reqwest::Client::new(), "http://x", "m", Some(key.into())).api_key
        };
        assert_eq!(client(" sk-test\n").as_deref(), Some("sk-test"));
        assert_eq!(client(" \n"), None);
    }

    #[test]
    fn ranked_chunks_normalise_by_each_chunks_reference() {
        let mut a = Answers::default();
        a.merge(&json!({
            "t0": {"type": "choice", "choice": "none_of_these",
                   "probabilities": {"a": 0.2, "b": 0.05, "none_of_these": 0.75}},
            "t1": {"type": "choice", "choice": "c",
                   "probabilities": {"c": 0.5, "d": 0.45, "none_of_these": 0.05}},
            "tx": {"type": "choice", "probabilities": {"zzz": 1.0}},
        }));
        let ranked: Vec<String> = a
            .ranked_chunks("t", "none_of_these")
            .into_iter()
            .map(|(n, _)| n)
            .collect();
        assert_eq!(ranked, vec!["c", "d", "a", "b"]);
    }
}
