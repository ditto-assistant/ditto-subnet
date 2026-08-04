//! Shared evaluation loop — runs a dataset's tool + memory cases through a
//! [`Baseline`], grading deterministically (judge-free, matching the
//! validator), and collecting the raw per-case results in
//! the shapes [`crate::scorer::score`] consumes.
//!
//! Used by the `evaluate`/`practice` CLI commands and the playground's live
//! scoring; callers differ only by their progress callback.

use std::collections::HashMap;

use crate::baseline::Baseline;
use crate::protocol::{Dataset, RunRequest, RunResponse};

/// System prompt sent with every tool case.
pub const TOOL_CASE_SYSTEM_PROMPT: &str =
    "You are Ditto, a helpful assistant. Use a tool when the user's request \
    clearly needs one; otherwise just answer.";

/// System prompt sent with every memory case.
pub const MEMORY_CASE_SYSTEM_PROMPT: &str =
    "You are Ditto. Answer using the user's memories when relevant; search \
    memories if needed.";

/// One case's outcome, streamed to the caller as the run progresses.
pub struct CaseOutcome<'a> {
    /// "tool" | "memory".
    pub kind: &'static str,
    pub case_id: &'a str,
    pub category: &'a str,
    pub prompt: &'a str,
    /// Case score: deterministic tool-accuracy for tool cases; the 0/1
    /// deterministic grade for memory cases; 0.0 on harness error.
    pub score: f64,
    pub latency_ms: i64,
    /// Human-readable per-case detail (the error text when `error` is set).
    pub detail: String,
    /// True when the harness failed on this case (no response recorded).
    pub error: bool,
}

/// The raw per-case results, keyed by case id — exactly the maps
/// [`crate::scorer::score`] takes. Failed cases are absent (scored zero there).
#[derive(Default)]
pub struct SuiteResults {
    pub tool_resps: HashMap<String, RunResponse>,
    pub mem_results: HashMap<String, (bool, i64)>,
}

/// Runs every tool + memory case in `ds` through `baseline`, grading
/// deterministically: tool accuracy via [`crate::scorer::score_tool_case`]
/// (no quality-judge half, matching the validator's judge-free scoring) and
/// memory correctness via [`crate::grade::memory_correct`]. `qtype_by_id`
/// labels memory outcomes with their question type. `on_case` fires after
/// each case with its outcome.
pub async fn run_suite(
    baseline: &Baseline,
    ds: &Dataset,
    qtype_by_id: &HashMap<String, String>,
    mut on_case: impl FnMut(CaseOutcome<'_>),
) -> SuiteResults {
    let catalog = crate::catalog::catalog();
    let mut out = SuiteResults::default();

    // Tool cases: run + deterministic trajectory accuracy (the score).
    for c in &ds.tool_cases {
        let req = RunRequest {
            case_id: c.id.clone(),
            system_prompt: TOOL_CASE_SYSTEM_PROMPT.to_string(),
            user_input: c.prompt.clone(),
            tools: catalog.clone(),
            bench_version: crate::protocol::ACTIVE_BENCH_VERSION,
            ..Default::default()
        };
        let (score, latency, detail, error) = match baseline.run(req).await {
            Ok(resp) => {
                let cs = crate::scorer::score_tool_case(c, Some(&resp));
                let composite = cs.tool_score;
                let exp = if cs.expected.is_empty() {
                    "no tool".to_string()
                } else {
                    cs.expected.join(", ")
                };
                let got = if cs.called.is_empty() {
                    "none".to_string()
                } else {
                    cs.called.join(", ")
                };
                let latency = resp.latency_ms;
                out.tool_resps.insert(c.id.clone(), resp);
                (
                    composite,
                    latency,
                    format!(
                        "called [{got}] · expected [{exp}] · tool-acc {:.2}",
                        cs.tool_score
                    ),
                    false,
                )
            }
            Err(e) => (0.0, 0, e.to_string(), true),
        };
        on_case(CaseOutcome {
            kind: "tool",
            case_id: &c.id,
            category: &c.category,
            prompt: &c.prompt,
            score,
            latency_ms: latency,
            detail,
            error,
        });
    }

    // Memory cases: run + deterministic containment grading (judge-free,
    // matching the validator's value-kind check).
    for mc in &ds.memory_cases {
        let req = RunRequest {
            case_id: mc.id.clone(),
            system_prompt: MEMORY_CASE_SYSTEM_PROMPT.to_string(),
            user_input: mc.question.clone(),
            tools: catalog.clone(),
            bench_version: crate::protocol::ACTIVE_BENCH_VERSION,
            ..Default::default()
        };
        let qtype = qtype_by_id.get(&mc.id).map(String::as_str).unwrap_or("");
        let (score, latency, detail, error) = match baseline.run(req).await {
            Ok(resp) => {
                let correct = crate::grade::memory_correct(
                    &mc.expected_answer,
                    resp.answer.as_deref(),
                    &resp.final_text,
                    resp.abstain.unwrap_or(false),
                );
                out.mem_results
                    .insert(mc.id.clone(), (correct, resp.latency_ms));
                (
                    if correct { 1.0 } else { 0.0 },
                    resp.latency_ms,
                    format!(
                        "expected \"{}\" — {}",
                        mc.expected_answer,
                        if correct {
                            "matched ✓"
                        } else {
                            "not matched ✗"
                        }
                    ),
                    false,
                )
            }
            Err(e) => (0.0, 0, e.to_string(), true),
        };
        on_case(CaseOutcome {
            kind: "memory",
            case_id: &mc.id,
            category: if qtype.is_empty() {
                "memory_recall"
            } else {
                qtype
            },
            prompt: &mc.question,
            score,
            latency_ms: latency,
            detail,
            error,
        });
    }

    out
}
