//! Turns harness `RunResponse`s into a DittoBench `ScoreReport`.
//!
//! Mirrors the production DittoBench scorer that produced the published
//! numbers. Scoring is judge-free and deterministic; there is no LLM judge.
//!
//! Tool case score (0–1) = deterministic tool-accuracy:
//!   - tool-accuracy = `matched / total_expected`, −0.1 per extra/unexpected call
//!     (skipped if `allow_extra_tools`), clamped 0–1; no-expected-tool cases score
//!     1.0 (Go: `computeToolAccuracy`, /50). This is the deterministic
//!     [`score_tool_case`] below, and that accuracy IS the case score (matching
//!     the validator's FinishTool).
//!
//! Memory accuracy = the deterministic boolean in `mem_results`, produced by
//! [`crate::grade::memory_correct`] (normalized bounded containment with an
//! exact number-token path, mirroring the validator's grader — so substring
//! over-matching like "5" in "500" cannot credit).

use std::collections::HashMap;

use chrono::Utc;

use crate::protocol::{CaseScore, Dataset, RunResponse, ScoreReport, ToolCase};

/// Composite weights when memory cases are present.
// DittoBench v2 composite (bench_version 2, held pre-launch): 0.5 tool +
// 0.5 memory. Rebalanced from v1's 0.6/0.4 — memory is the core product value
// and the raw-pairs seeding tier makes memory_mean the harder axis.
pub const TOOL_WEIGHT: f64 = 0.5;
pub const MEMORY_WEIGHT: f64 = 0.5;

/// Builds the aggregate report.
///
/// - `tool_resps`: case_id -> RunResponse for tool cases. Missing responses
///   (harness error / timeout) are scored as zero.
/// - `mem_results`: memory case_id -> (answered_correctly, latency_ms).
pub fn score(
    run_id: &str,
    ds: &Dataset,
    tool_resps: &HashMap<String, RunResponse>,
    mem_results: &HashMap<String, (bool, i64)>,
) -> ScoreReport {
    let mut per_case = Vec::with_capacity(ds.tool_cases.len() + ds.memory_cases.len());
    let mut tool_sum = 0.0;
    let mut latencies: Vec<i64> = Vec::with_capacity(per_case.capacity());

    for c in &ds.tool_cases {
        let resp = tool_resps.get(&c.id);
        let cs = score_tool_case(c, resp);
        tool_sum += cs.tool_score;
        latencies.push(cs.latency_ms);
        per_case.push(cs);
    }

    // Memory cases.
    let mut mem_sum = 0.0;
    for mc in &ds.memory_cases {
        let (correct, latency) = mem_results.get(&mc.id).copied().unwrap_or((false, 0));
        let s = if correct { 1.0 } else { 0.0 };
        mem_sum += s;
        latencies.push(latency);
        let mut notes = Vec::new();
        if !mem_results.contains_key(&mc.id) {
            notes.push("no response from harness (error or timeout)".to_string());
        } else if !correct {
            notes.push(format!(
                "graded incorrect (deterministic containment); expected {:?}",
                mc.expected_answer
            ));
        }
        per_case.push(CaseScore {
            case_id: mc.id.clone(),
            category: "memory_recall".to_string(),
            tool_score: s,
            result_usage: 0.0,
            latency_ms: latency,
            called: Vec::new(),
            expected: vec![mc.expected_answer.clone()],
            notes,
        });
    }

    let n_tool = ds.tool_cases.len();
    let n_mem = ds.memory_cases.len();
    let tool_mean = if n_tool > 0 {
        tool_sum / n_tool as f64
    } else {
        0.0
    };
    let memory_mean = if n_mem > 0 {
        mem_sum / n_mem as f64
    } else {
        0.0
    };

    let composite = if n_mem > 0 && n_tool > 0 {
        TOOL_WEIGHT * tool_mean + MEMORY_WEIGHT * memory_mean
    } else if n_mem > 0 {
        memory_mean
    } else {
        tool_mean
    };

    ScoreReport {
        run_id: run_id.to_string(),
        generated_at: Utc::now().to_rfc3339(),
        composite,
        tool_mean,
        memory_mean,
        median_ms: median(&latencies),
        n: (n_tool + n_mem) as i32,
        per_case,
    }
}

/// Scores a single tool case against a harness response (exposed for live,
/// per-case scoring in the playground; the rule matches [`score`]).
pub fn score_tool_case(c: &ToolCase, resp: Option<&RunResponse>) -> CaseScore {
    let called: Vec<String> = resp
        .map(|r| r.tool_calls.iter().map(|tc| tc.name.clone()).collect())
        .unwrap_or_default();
    let expected: Vec<String> = c.expected_tools.iter().map(|s| s.name.clone()).collect();
    let latency_ms = resp.map(|r| r.latency_ms).unwrap_or(0);

    let mut cs = CaseScore {
        case_id: c.id.clone(),
        category: c.category.clone(),
        tool_score: 0.0,
        result_usage: 0.0,
        latency_ms,
        called,
        expected,
        notes: Vec::new(),
    };

    let Some(resp) = resp else {
        cs.notes
            .push("no response from harness (error or timeout)".to_string());
        return cs;
    };

    // Count observed calls by name.
    let mut observed: HashMap<&str, i32> = HashMap::new();
    for tc in &resp.tool_calls {
        *observed.entry(tc.name.as_str()).or_insert(0) += 1;
    }

    // No-expected-tool cases: perfect only if nothing was called.
    if c.expected_tools.is_empty() {
        if resp.tool_calls.is_empty() {
            cs.tool_score = 1.0;
        } else {
            cs.tool_score = 0.0;
            cs.notes.push(format!(
                "expected no tools but harness called {}",
                resp.tool_calls.len()
            ));
        }
        return cs;
    }

    // Count expected calls by name.
    let mut expected_counts: HashMap<&str, i32> = HashMap::new();
    for ts in &c.expected_tools {
        *expected_counts.entry(ts.name.as_str()).or_insert(0) += 1;
    }

    let mut total_expected = 0;
    let mut matched = 0;
    for (name, want) in &expected_counts {
        total_expected += *want;
        let got = *observed.get(name).unwrap_or(&0);
        matched += got.min(*want);
    }

    let base = if total_expected > 0 {
        matched as f64 / total_expected as f64
    } else {
        0.0
    };

    // Extra/unexpected calls (anything beyond what's expected).
    let mut extra = 0;
    for (name, got) in &observed {
        let want = *expected_counts.get(name).unwrap_or(&0);
        if *got > want {
            extra += *got - want;
        }
    }

    let mut s = base;
    if extra > 0 && !c.allow_extra_tools {
        let penalty = 0.1 * extra as f64;
        s -= penalty;
        cs.notes.push(format!(
            "{} extra/unexpected tool call(s) (-{:.1})",
            extra, penalty
        ));
    }
    cs.tool_score = s.clamp(0.0, 1.0);
    cs
}

/// Median of latency values (0 for empty input). Even-length inputs average the
/// two middle values (shared with the playground's live scoring).
pub fn median(vals: &[i64]) -> i64 {
    if vals.is_empty() {
        return 0;
    }
    let mut cp = vals.to_vec();
    cp.sort_unstable();
    let mid = cp.len() / 2;
    if cp.len() % 2 == 1 {
        cp[mid]
    } else {
        (cp[mid - 1] + cp[mid]) / 2
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::{MemoryCase, ObservedToolCall, ToolSpec};

    fn tool_case(id: &str, expected: &[&str], allow_extra: bool) -> ToolCase {
        ToolCase {
            id: id.to_string(),
            category: "test".to_string(),
            prompt: "p".to_string(),
            expected_tools: expected
                .iter()
                .map(|n| ToolSpec {
                    name: n.to_string(),
                    ..ToolSpec::default()
                })
                .collect(),
            max_tool_calls: 1,
            allow_extra_tools: allow_extra,
            expected_behavior: String::new(),
        }
    }

    fn resp(tools: &[&str], latency: i64) -> RunResponse {
        RunResponse {
            final_text: String::new(),
            tool_calls: tools
                .iter()
                .map(|n| ObservedToolCall {
                    name: n.to_string(),
                    ..ObservedToolCall::default()
                })
                .collect(),
            prompt_tokens: 0,
            output_tokens: 0,
            latency_ms: latency,
            answer: None,
            abstain: None,
        }
    }

    #[test]
    fn exact_match_scores_one() {
        let ds = Dataset {
            tool_cases: vec![tool_case("a", &["search_web"], false)],
            ..Dataset::default()
        };
        let mut m = HashMap::new();
        m.insert("a".to_string(), resp(&["search_web"], 100));
        let r = score("run", &ds, &m, &HashMap::new());
        assert_eq!(r.tool_mean, 1.0);
        assert_eq!(r.composite, 1.0);
        assert_eq!(r.per_case[0].tool_score, 1.0);
    }

    #[test]
    fn extra_call_penalized() {
        let ds = Dataset {
            tool_cases: vec![tool_case("a", &["search_web"], false)],
            ..Dataset::default()
        };
        let mut m = HashMap::new();
        m.insert("a".to_string(), resp(&["search_web", "create_image"], 0));
        let r = score("run", &ds, &m, &HashMap::new());
        // base 1.0 - 0.1 extra = 0.9
        assert!((r.per_case[0].tool_score - 0.9).abs() < 1e-9);
    }

    #[test]
    fn no_tool_case_zeroed_by_any_call() {
        let ds = Dataset {
            tool_cases: vec![tool_case("a", &[], false)],
            ..Dataset::default()
        };
        let mut m = HashMap::new();
        m.insert("a".to_string(), resp(&["search_web"], 0));
        let r = score("run", &ds, &m, &HashMap::new());
        assert_eq!(r.per_case[0].tool_score, 0.0);

        let mut m2 = HashMap::new();
        m2.insert("a".to_string(), resp(&[], 0));
        let r2 = score("run", &ds, &m2, &HashMap::new());
        assert_eq!(r2.per_case[0].tool_score, 1.0);
    }

    #[test]
    fn missing_response_scores_zero() {
        let ds = Dataset {
            tool_cases: vec![tool_case("a", &["search_web"], false)],
            ..Dataset::default()
        };
        let r = score("run", &ds, &HashMap::new(), &HashMap::new());
        assert_eq!(r.per_case[0].tool_score, 0.0);
        assert!(!r.per_case[0].notes.is_empty());
    }

    #[test]
    fn median_latency() {
        let ds = Dataset {
            tool_cases: vec![
                tool_case("a", &["t"], false),
                tool_case("b", &["t"], false),
                tool_case("c", &["t"], false),
            ],
            ..Dataset::default()
        };
        let mut m = HashMap::new();
        m.insert("a".to_string(), resp(&["t"], 10));
        m.insert("b".to_string(), resp(&["t"], 30));
        m.insert("c".to_string(), resp(&["t"], 20));
        let r = score("run", &ds, &m, &HashMap::new());
        assert_eq!(r.median_ms, 20);
    }

    #[test]
    fn composite_weights_tool_and_memory() {
        let ds = Dataset {
            tool_cases: vec![tool_case("a", &["search_web"], false)],
            memory_cases: vec![MemoryCase {
                id: "m1".to_string(),
                question: "q".to_string(),
                expected_answer: "Biscuit".to_string(),
                seed_memories: Vec::new(),
            }],
            ..Dataset::default()
        };
        let mut tool = HashMap::new();
        tool.insert("a".to_string(), resp(&["search_web"], 0)); // tool_mean = 1.0
        let mut mem = HashMap::new();
        mem.insert("m1".to_string(), (false, 5)); // memory_mean = 0.0
        let r = score("run", &ds, &tool, &mem);
        assert_eq!(r.tool_mean, 1.0);
        assert_eq!(r.memory_mean, 0.0);
        // v2 0.5/0.5: 0.5*1.0 + 0.5*0.0 = 0.5.
        assert!(
            (r.composite - 0.5).abs() < 1e-9,
            "composite = {}",
            r.composite
        );
    }
}
