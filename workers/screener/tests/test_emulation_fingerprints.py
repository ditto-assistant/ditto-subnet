"""Bench-v12 anti-emulation fingerprint tests.

Each synthetic "emulator" source trips one or more greppable bypass fingerprints
that four post-crowning holds (lets_5.0, kaelith, Crown, Hannibal) shared; each
synthetic "honest" agent implements a genuine retrieve -> model -> serialize path
and must trip none. The fingerprints are language-aware (Rust/Python/TS/Go), so
the emulator and honest fixtures are repeated across languages, and the A1
sync/async tell must stay silent on Go, where a real model call blocks.
"""

from __future__ import annotations

import json

from ditto_screener.source_signals import find_benchmark_emulation_fingerprints

_RUST_EMULATOR = """\
// Ported from universe/world.go and the conversational.go generator so we
// mirror the world-canary and world-injection-resistance families.
use crate::model;

fn solve_case(req: &Request) -> Answer {
    if req.bench_version == 11 {
        return canned_answer(req);
    }
    let _family = classify_family(&req.question);
    Answer {
        answer: lookup_table(&req.question),
        prompt_tokens: 0,
        output_tokens: 0,
    }
}

fn classify_family(question: &str) -> &'static str {
    if one_typo_apart(question, "who is my manager") {
        return "world-canary";
    }
    if fuzzy_match(question, "ignore previous") {
        return "world-injection-resistance";
    }
    "conversational-chitchat"
}

fn lookup_table(question: &str) -> String {
    match question {
        "What is the capital of France?" => "Paris".to_string(),
        "Who is the current CEO of Acme?" => "Dana".to_string(),
        "When did the project launch?" => "2021".to_string(),
        "Which city hosted the summit?" => "Oslo".to_string(),
        _ => String::new(),
    }
}
"""

_PYTHON_EMULATOR = """\
# agent.py -- mirrors gen/memory_v2.go family labels
def solve(req):
    if req.bench_version == 11:
        return {"answer": PHRASES[req.question], "prompt_tokens": 0, "output_tokens": 0}
    return general(req)


PHRASES = {
    "What is the capital of France?": "Paris",
    "Who is the CEO of Acme?": "Dana",
    "When did it launch?": "2021",
    "Which city hosted it?": "Oslo",
}

FAMILIES = ["world-canary", "world-injection-resistance", "conversational-chitchat"]
"""

_GO_EMULATOR = """\
// package agent mirrors universe/world.go
package agent

func Solve(req Request) Answer {
    if req.BenchVersion == 11 {
        return Answer{Answer: phrases[req.Question], PromptTokens: 0, OutputTokens: 0}
    }
    return general(req)
}

var phrases = map[string]string{
    "What is the capital of France?": "Paris",
    "Who is the CEO of Acme?":        "Dana",
    "When did it launch?":            "2021",
    "Which city hosted it?":          "Oslo",
}

var families = []string{
    "world-canary",
    "world-injection-resistance",
    "conversational-chitchat",
}
"""

_TS_EMULATOR = """\
// mirrors gen/conversational.go
export function solveCase(req: Req): Answer {
  if (req.benchVersion === 11) {
    return { answer: PHRASES[req.question], prompt_tokens: 0, output_tokens: 0 };
  }
  return general(req);
}

const PHRASES: Record<string, string> = {
  "What is the capital of France?": "Paris",
  "Who is the CEO of Acme?": "Dana",
  "When did it launch?": "2021",
  "Which city hosted it?": "Oslo",
};

const FAMILIES = [
  "world-canary",
  "world-injection-resistance",
  "conversational-chitchat",
];
"""

_RUST_HONEST = """\
use crate::model;

async fn answer_case(req: &Request) -> Answer {
    let memories = retrieve_memories(req.user_id, &req.question).await;
    let context = build_context(&memories);
    let reply = model::chat(&req.question, &context).await;
    Answer {
        answer: reply.text,
        prompt_tokens: reply.usage.prompt_tokens,
        output_tokens: reply.usage.output_tokens,
    }
}

fn build_context(memories: &[Memory]) -> String {
    memories.iter().map(|m| m.text.clone()).collect::<Vec<_>>().join("\\n")
}
"""

_PYTHON_HONEST = """\
# agent.py
async def answer(req):
    memories = await retrieve(req.user_id, req.question)
    reply = await model_chat(req.question, memories)
    return {
        "answer": reply.text,
        "prompt_tokens": reply.prompt_tokens,
        "output_tokens": reply.output_tokens,
    }
"""

_GO_HONEST = """\
package agent

func Answer(req Request) (Reply, error) {
    mems, err := Retrieve(req.UserID, req.Question)
    if err != nil {
        return Reply{}, err
    }
    return CallModel(req.Question, mems)
}
"""

_TS_HONEST = """\
export async function answer(req: Req): Promise<Answer> {
  const mems = await retrieve(req.userId, req.question);
  const reply = await chatModel(req.question, mems);
  return {
    answer: reply.text,
    prompt_tokens: reply.promptTokens,
    output_tokens: reply.outputTokens,
  };
}
"""


def _kinds(findings: list[dict[str, object]]) -> set[str]:
    return {str(finding["kind"]) for finding in findings}


def test_rust_emulator_trips_every_fingerprint() -> None:
    findings = find_benchmark_emulation_fingerprints([("src/agent.rs", _RUST_EMULATOR)])
    assert _kinds(findings) == {
        "bench-version-equality-gate",
        "sync-answer-constructor",
        "zero-token-answer-constructor",
        "typo-tolerant-token-compare",
        "literal-question-registry",
        "generator-source-citation",
        "scored-family-label",
    }
    # Every finding carries category, severity, and location-only path/line/role.
    for finding in findings:
        assert finding["category"] == "benchmark_emulation"
        assert finding["severity"] in {"high", "medium"}
        assert finding["locations"]
        for location in finding["locations"]:
            assert set(location) == {"path", "line", "role"}
            assert location["path"] == "src/agent.rs"
    # The near-conclusive version gate is the only high-severity E1 signal here.
    severities = {
        str(finding["kind"]): str(finding["severity"]) for finding in findings
    }
    assert severities["bench-version-equality-gate"] == "high"
    assert severities["zero-token-answer-constructor"] == "high"


def test_findings_never_leak_matched_source_text() -> None:
    findings = find_benchmark_emulation_fingerprints([("src/agent.rs", _RUST_EMULATOR)])
    encoded = json.dumps(findings)
    for secret in (
        "What is the capital of France?",
        "world-canary",
        "world-injection-resistance",
        "universe/world.go",
        "one_typo_apart",
        "bench_version",
    ):
        assert secret not in encoded


def test_bench_version_gate_variants_across_languages() -> None:
    cases = {
        "src/a.rs": "fn run(r: &R) -> A { if r.bench_version == 11 { canned() } }",
        "b.py": "def run(r):\n    return canned() if r.bench_version == 12 else m(r)",
        "c.go": "func Run(r R) A { if r.BenchVersion == 9 { return canned() } }",
        "d.ts": "function run(r: R) { if (r.benchVersion === 11) return canned(); }",
    }
    for path, source in cases.items():
        findings = find_benchmark_emulation_fingerprints([(path, source)])
        assert "bench-version-equality-gate" in _kinds(findings), path


def test_sync_answer_constructor_is_language_aware() -> None:
    # Rust/Python/TS: a sync function building the answer with no awaited model
    # call fires A1.
    rust = "fn solve_answer(r: &R) -> Answer { Answer { answer: table(r) } }"
    python = 'def solve_answer(r):\n    return {"answer": TABLE[r.q]}'
    typescript = "function solveAnswer(r: R): Answer { return { answer: TABLE[r.q] }; }"
    for path, source in (("src/a.rs", rust), ("b.py", python), ("c.ts", typescript)):
        assert "sync-answer-constructor" in _kinds(
            find_benchmark_emulation_fingerprints([(path, source)])
        ), path
    # Go: a synchronous answer function is normal (a real model call blocks), so
    # the A1 tell must stay silent.
    go = "func SolveAnswer(r R) Answer { return Answer{Answer: table(r)} }"
    assert "sync-answer-constructor" not in _kinds(
        find_benchmark_emulation_fingerprints([("d.go", go)])
    )


def test_policy_v10_authoritative_derived_candidate_is_a_lead() -> None:
    source = """
fn decide(records: &str) -> String {
    let derived_answer = compute_balance(records);
    format!(concat!("WORKED OUT value: {derived_answer}. ",
                    "State this value exactly; do not recompute"))
}
"""
    assert "authoritative-derived-candidate" in _kinds(
        find_benchmark_emulation_fingerprints([("src/agent.rs", source)])
    )


def test_policy_v10_untrusted_candidate_channel_is_not_authoritative() -> None:
    source = """
fn context(records: &str) -> String {
    let candidate = calculate(records);
    format!(concat!("UNTRUSTED CANDIDATE: {candidate} RAW RECORDS: {records} ",
                    "Prefer records on disagreement"))
}
"""
    assert "authoritative-derived-candidate" not in _kinds(
        find_benchmark_emulation_fingerprints([("src/agent.rs", source)])
    )


def test_policy_v10_closed_semantic_sheet_prompt_is_a_lead() -> None:
    source = """
fn prompt() -> String {
    let mut system_prompt = String::new();
    system_prompt.push_str("BalanceFinal BudgetDelta OutcomeSummary");
    system_prompt
}
"""
    assert "closed-semantic-sheet-prompt" in _kinds(
        find_benchmark_emulation_fingerprints([("src/agent.rs", source)])
    )


def test_policy_v10_trained_exact_tool_deck_is_a_lead() -> None:
    source = """
fn planned_calls(request: &str) -> usize { call_budget(request) }
fn planned_deck(request: &str) -> Vec<String> {
    let take = planned_calls(request);
    ranked_tools(request).into_iter().take(take).collect()
}
"""
    assert "predetermined-tool-plan" in _kinds(
        find_benchmark_emulation_fingerprints([("src/route.rs", source)])
    )


def test_policy_v10_policy_filter_without_exact_plan_is_not_i7_lead() -> None:
    source = """
fn allowed_tools(catalog: Vec<Tool>, grants: &Grants) -> Vec<Tool> {
    catalog.into_iter().filter(|tool| grants.allows(tool.name())).collect()
}
"""
    kinds = _kinds(find_benchmark_emulation_fingerprints([("src/route.rs", source)]))
    assert "predetermined-tool-plan" not in kinds
    assert "forced-single-tool-turn" not in kinds


def test_policy_v10_host_selected_single_tool_turn_is_a_lead() -> None:
    source = """
fn next(required_tool_name: &str) {
    body["tool_choice"] = json!({"function": {"name": required_tool_name}});
}
"""
    assert "forced-single-tool-turn" in _kinds(
        find_benchmark_emulation_fingerprints([("src/model.rs", source)])
    )


def test_policy_v10_required_catalog_forced_choice_is_an_i7_lead() -> None:
    """Catch the served forced-executor shape, not only raw provider JSON."""
    source = """
fn run(tool_route: &Route, model: Arc<dyn Model>) {
    if let Some(force) = tool_route.required_catalog_tool() {
        let model = ForcedChoiceModel::wrap_with_hint(model, force);
        follow_up("Call the required tool once. Do not call any other tool.");
    }
}
"""
    assert "forced-single-tool-turn" in _kinds(
        find_benchmark_emulation_fingerprints([("src/baseline.rs", source)])
    )


def test_async_answer_with_model_call_does_not_trip_a1() -> None:
    rust = (
        "async fn solve_answer(r: &R) -> Answer {\n"
        "    let reply = model::chat(&r.q).await;\n"
        "    Answer { answer: reply.text }\n}"
    )
    assert "sync-answer-constructor" not in _kinds(
        find_benchmark_emulation_fingerprints([("src/a.rs", rust)])
    )


def test_zero_token_answer_constructor_requires_populated_answer() -> None:
    trips = "let a = Answer { answer: solved, prompt_tokens: 0, output_tokens: 0 };"
    assert "zero-token-answer-constructor" in _kinds(
        find_benchmark_emulation_fingerprints([("src/a.rs", trips)])
    )
    # Zero tokens with no served answer beside them (e.g. an unused default) does
    # not fire.
    clear = "let usage = Usage { prompt_tokens: 0, output_tokens: 0 };"
    assert "zero-token-answer-constructor" not in _kinds(
        find_benchmark_emulation_fingerprints([("src/a.rs", clear)])
    )


def test_typo_tolerant_compare_near_question_parsing() -> None:
    source = (
        "fn classify(question: &str) -> bool {\n    one_typo_apart(question, target)\n}"
    )
    assert "typo-tolerant-token-compare" in _kinds(
        find_benchmark_emulation_fingerprints([("src/a.rs", source)])
    )


def test_literal_question_registry_needs_multiple_keys() -> None:
    two = (
        "match q {\n"
        '    "What is the capital?" => "Paris",\n'
        '    "Who is the CEO?" => "Dana",\n}'
    )
    # Two literal-question arms is below the aggregate threshold.
    assert "literal-question-registry" not in _kinds(
        find_benchmark_emulation_fingerprints([("src/a.rs", two)])
    )
    four = (
        "match q {\n"
        '    "What is the capital?" => "Paris",\n'
        '    "Who is the CEO?" => "Dana",\n'
        '    "When did it launch?" => "2021",\n'
        '    "Which city hosted it?" => "Oslo",\n}'
    )
    assert "literal-question-registry" in _kinds(
        find_benchmark_emulation_fingerprints([("src/a.rs", four)])
    )


def test_generator_source_citation_and_family_labels() -> None:
    source = (
        "// see universe/world.go for the taxonomy\n"
        'let f = "world-injection-resistance";'
    )
    kinds = _kinds(find_benchmark_emulation_fingerprints([("src/a.rs", source)]))
    assert "generator-source-citation" in kinds
    assert "scored-family-label" in kinds


def test_honest_agents_trip_no_fingerprints() -> None:
    for path, source in (
        ("src/agent.rs", _RUST_HONEST),
        ("agent.py", _PYTHON_HONEST),
        ("agent.go", _GO_HONEST),
        ("agent.ts", _TS_HONEST),
    ):
        assert find_benchmark_emulation_fingerprints([(path, source)]) == [], path


def test_emulator_fixtures_fire_in_every_language() -> None:
    for path, source in (
        ("src/agent.rs", _RUST_EMULATOR),
        ("agent.py", _PYTHON_EMULATOR),
        ("agent.go", _GO_EMULATOR),
        ("agent.ts", _TS_EMULATOR),
    ):
        kinds = _kinds(find_benchmark_emulation_fingerprints([(path, source)]))
        # Every language emulator trips the version gate, the fabricated
        # zero-token answer, the phrase registry, and the family labels.
        assert {
            "bench-version-equality-gate",
            "zero-token-answer-constructor",
            "literal-question-registry",
            "scored-family-label",
        } <= kinds, path


def test_tests_and_docs_are_not_scanned() -> None:
    # The same emulator body under a tests/ path is inert: fingerprints scan only
    # served/executable source; test modules can be stripped from an image.
    assert (
        find_benchmark_emulation_fingerprints([("tests/agent.rs", _RUST_EMULATOR)])
        == []
    )


# A baked classifier routes each scored question into a closed DittoBench family,
# then applies a hardcoded per-family recipe: it loads a compiled router table,
# copies precomputed values verbatim ("do not recompute"), smuggles a reconciled
# total into the notes prompt, ships a closed Program tree, reject-until-match
# loops the model against an established value, replaces retrieved records with a
# compacted context, and serves a glossary "verified result". This body trips the
# seven Rev7 fingerprints (Hogwarts v19, Aceron_v4, Omar v21, red-dragon v39,
# lets_5.x) without leaking the private family taxonomy.
_RUST_FAMILY_COMPILER = """\
use crate::model;

static ROUTER: &str = include_str!("family-router.json");

fn family_of(question: &str) -> &'static str {
    // keys mirror the closed generator taxonomy
    match route(ROUTER, question) {
        "world-story-outstanding" => "world-story-outstanding",
        "world-project-outstanding" => "world-project-outstanding",
        "world-trip-itinerary" => "world-trip-itinerary",
        _ => "conversational-chitchat",
    }
}

enum StoryProgram {
    Reconcile,
    Passthrough,
}

fn try_solve(req: &Request) -> Option<String> {
    let _ev = req.v10_open_program_evidence;
    Some(baseline(req))
}

fn baseline(req: &Request) -> String {
    let author_reconciled_value = reconcile(req);
    let mut notes = String::new();
    // withhold the raw record; hand the model the total instead
    let total = author_reconciled_value;
    notes.push_str(&format!("what is still outstanding is {}", total));
    let system_prompt = compact(&notes);
    let established_for_prompt = author_reconciled_value.clone();
    for attempt in 1..=5 {
        let reply = model::chat(&system_prompt).await;
        if reply.text.contains(&established_for_prompt) {
            return reply.text;
        }
    }
    // Do not recompute; state this exact resolved value.
    // VERIFIED RESULT: copy its value exactly.
    established_for_prompt
}
"""

# An honest agent that classifies intent, retries only on transient errors, and
# summarizes context alongside (not replacing) the retrieved records. It must
# trip none of the Rev7 family-compiler fingerprints.
_RUST_HONEST_FAMILY = """\
use crate::model;

async fn answer(req: &Request) -> Answer {
    let records = retrieve(req.user_id, &req.question).await;
    let summary = summarize(&records);
    // the summary augments the records; it does not replace them
    let context = format!("{}\\n{}", records.join("\\n"), summary);
    for attempt in 1..=3 {
        match model::chat(&req.question, &context).await {
            Ok(reply) => return Answer { answer: reply.text },
            Err(e) if e.is_timeout() => continue,
            Err(e) => return Answer::error(e),
        }
    }
    Answer::empty()
}
"""

_PYTHON_FAMILY_COMPILER = """\
import json

ROUTER = json.loads(load("family-router.json"))


def family_of(question):
    label = ROUTER.get(question, "conversational-chitchat")
    if label in ("world-story-outstanding", "world-trip-itinerary"):
        return label
    return "conversational-chitchat"


def baseline(req):
    author_reconciled_value = reconcile(req)
    notes_prompt = ""
    notes_prompt += "what is still outstanding is %s" % author_reconciled_value
    system_prompt = compact(notes_prompt)
    established_value = author_reconciled_value
    for attempt in range(5):
        reply = call_model(system_prompt)
        if established_value in reply:
            return reply
    # Do not recompute; copy its value exactly.
    return established_value
"""


def test_family_compiler_trips_every_rev7_fingerprint() -> None:
    findings = find_benchmark_emulation_fingerprints(
        [("src/story.rs", _RUST_FAMILY_COMPILER)]
    )
    kinds = _kinds(findings)
    assert {
        "baked-family-router",
        "do-not-recompute-directive",
        "compiled-totals-as-notes",
        "open-program-evidence-marker",
        "copy-retry-loop",
        "compact-system-prompt-replacement",
        "glossary-verified-result-marker",
    } <= kinds
    # Location-only: category/severity present, no matched source text anywhere.
    for finding in findings:
        assert finding["category"] == "benchmark_emulation"
        assert finding["severity"] in {"high", "medium"}
        for location in finding["locations"]:
            assert set(location) == {"path", "line", "role"}
            assert location["path"] == "src/story.rs"


def test_rev7_findings_never_leak_matched_source_text() -> None:
    findings = find_benchmark_emulation_fingerprints(
        [("src/story.rs", _RUST_FAMILY_COMPILER)]
    )
    encoded = json.dumps(findings)
    for secret in (
        "family-router.json",
        "world-story-outstanding",
        "world-project-outstanding",
        "world-trip-itinerary",
        "author_reconciled_value",
        "what is still outstanding is",
        "established_for_prompt",
        "VERIFIED RESULT",
        "Do not recompute",
        "v10_open_program_evidence",
    ):
        assert secret not in encoded


def test_python_family_compiler_trips_core_rev7_fingerprints() -> None:
    kinds = _kinds(
        find_benchmark_emulation_fingerprints([("agent.py", _PYTHON_FAMILY_COMPILER)])
    )
    assert {
        "baked-family-router",
        "do-not-recompute-directive",
        "compiled-totals-as-notes",
        "copy-retry-loop",
        "compact-system-prompt-replacement",
    } <= kinds


def test_honest_family_agent_trips_no_rev7_fingerprints() -> None:
    findings = find_benchmark_emulation_fingerprints(
        [("src/agent.rs", _RUST_HONEST_FAMILY)]
    )
    assert findings == []


def test_baked_family_router_needs_loader_and_family_key() -> None:
    # The loader alone (no family-label keys nearby) does not fire.
    loader_only = 'static R: &str = include_str!("family-router.json");'
    assert "baked-family-router" not in _kinds(
        find_benchmark_emulation_fingerprints([("src/a.rs", loader_only)])
    )
    # A bare `classify_family` helper (honest intent classifier) is not the baked
    # `family::classify` / `family_of` router artifact, so it does not fire.
    honest_classifier = (
        "fn classify_family(q: &str) -> &str {\n"
        '    if q.contains("hi") { "world-story-outstanding" } else { "other" }\n}'
    )
    assert "baked-family-router" not in _kinds(
        find_benchmark_emulation_fingerprints([("src/a.rs", honest_classifier)])
    )
    both = (
        'static R: &str = include_str!("family-router.json");\n'
        'fn family_of(q: &str) -> &str { route(R, q, "world-trip-itinerary") }'
    )
    assert "baked-family-router" in _kinds(
        find_benchmark_emulation_fingerprints([("src/a.rs", both)])
    )


def test_copy_retry_loop_suppressed_on_transient_backoff() -> None:
    # A bounded model retry that loops on a timeout/backoff -- not on a value
    # mismatch -- is honest and must be suppressed.
    transient = (
        "for attempt in 1..=3 {\n"
        "    match call_model(&expected_value_prompt) {\n"
        "        Ok(r) => return r,\n"
        "        Err(e) if e.is_timeout() => { backoff(attempt); continue; }\n"
        "    }\n}"
    )
    assert "copy-retry-loop" not in _kinds(
        find_benchmark_emulation_fingerprints([("src/a.rs", transient)])
    )


def test_do_not_recompute_directive_is_raw_scan() -> None:
    # The directive fires even when it lives in a comment/prompt string, since it
    # is a served instruction to copy a precomputed value verbatim.
    source = "// Do not recompute; reply with exactly the resolved value."
    assert "do-not-recompute-directive" in _kinds(
        find_benchmark_emulation_fingerprints([("src/a.rs", source)])
    )


_NEW_FAMILY_COMPILER_KINDS = {
    "required-money-formatter",
    "ledger-event-kind-compiler",
    "world-shape-rule-injection",
    "story-arc-remainder-compiler",
    "trip-day-family-retry",
}

_RUST_STORY_ARC_REMAINDER = """\
struct StoryArc {
    base: i64,
    paid: i64,
    delta: i64,
    cost: i64,
    credit: i64,
}

impl StoryArc {
    fn balance(&self) -> i64 {
        self.base + self.delta - self.paid - self.cost + self.credit
    }
}

fn join_family(case_id: u32, po: u32) -> String {
    format!("CASE-{:04} / PO-{}", case_id, po)
}

fn retry(draft: &str, remaining: i64) -> bool {
    reply_restates_story_ingredient_money(draft, remaining)
}
"""

_RUST_REQUIRED_MONEY_FORMATTER = """\
fn format_money(required_money: i64) -> String {
    format!("Return ANSWER: {required_money} exactly")
}
"""

_RUST_HONEST_FORMAT_ROUND_HELPER = """\
fn format_money(required_money: i64) -> String {
    format_round(required_money)
}
"""

_RUST_LEDGER_EVENT_KIND = """\
enum LedgerEventKind {
    Opening,
    Adjustment,
    Payment,
    Expense,
    Credit,
}

fn validate(events: &[Event]) -> Vec<String> {
    narrative_ledger_issues(events, correction_only, event_only)
}
"""

_RUST_WORLD_SHAPE_RULE = """\
fn hint(world: &World) -> String {
    let shape = world_shape_of(world);
    let rule = world_shape_rule(shape);
    match rule {
        WorldShape::Outstanding => outstanding(),
        BalanceFinal => final_balance(),
        BalanceMid => mid(),
    }
}
"""

_RUST_HONEST_HOGWARTS_V3 = """\
struct CompactToolModel {
    money: i64,
}

fn closer(money: i64, records: &[String]) -> String {
    format!(
        "The money calculator returned `{money}`. \
Answer the original user request from the raw records \
and this genuine tool result"
    )
}

async fn answer(req: &Request) -> Answer {
    let records = retrieve(req.user_id, &req.question).await;
    let money = calculator(&records);
    let prompt = closer(money, &records);
    let reply = model::chat(&prompt, &records).await;
    Answer { answer: reply.text }
}
"""

_RUST_GENERIC_LEDGER = """\
struct Ledger {
    amount: i64,
}

impl Ledger {
    fn balance(&self) -> i64 {
        self.amount
    }
}
"""

_RUST_TRIP_DAY_RETRY = """\
fn coach(draft: &str, changed_leg: i64) -> bool {
    if reply_misses_days_change_application(draft, changed_leg) {
        return days_focus_coach_line(changed_leg);
    }
    false
}
"""


def test_story_arc_remainder_compiler_trips_on_goal_v9_shape() -> None:
    kinds = _kinds(
        find_benchmark_emulation_fingerprints(
            [("src/story.rs", _RUST_STORY_ARC_REMAINDER)]
        )
    )
    assert "story-arc-remainder-compiler" in kinds


def test_required_money_formatter_trips_on_hogwarts_closer() -> None:
    kinds = _kinds(
        find_benchmark_emulation_fingerprints(
            [("src/money.rs", _RUST_REQUIRED_MONEY_FORMATTER)]
        )
    )
    assert "required-money-formatter" in kinds


def test_required_money_plus_format_round_is_not_the_formatter_tell() -> None:
    kinds = _kinds(
        find_benchmark_emulation_fingerprints(
            [("src/money.rs", _RUST_HONEST_FORMAT_ROUND_HELPER)]
        )
    )
    assert "required-money-formatter" not in kinds
    assert kinds.isdisjoint(_NEW_FAMILY_COMPILER_KINDS)


def test_ledger_event_kind_compiler_trips_on_narrative_issues() -> None:
    kinds = _kinds(
        find_benchmark_emulation_fingerprints(
            [("src/ledger.rs", _RUST_LEDGER_EVENT_KIND)]
        )
    )
    assert "ledger-event-kind-compiler" in kinds


def test_world_shape_rule_injection_trips_on_balance_final() -> None:
    kinds = _kinds(
        find_benchmark_emulation_fingerprints(
            [("src/shape.rs", _RUST_WORLD_SHAPE_RULE)]
        )
    )
    assert "world-shape-rule-injection" in kinds


def test_trip_day_family_retry_trips_on_days_coach() -> None:
    kinds = _kinds(
        find_benchmark_emulation_fingerprints([("src/trip.rs", _RUST_TRIP_DAY_RETRY)])
    )
    assert "trip-day-family-retry" in kinds


def test_honest_hogwarts_v3_closer_trips_none_of_the_new_kinds() -> None:
    kinds = _kinds(
        find_benchmark_emulation_fingerprints(
            [("src/agent.rs", _RUST_HONEST_HOGWARTS_V3)]
        )
    )
    assert kinds.isdisjoint(_NEW_FAMILY_COMPILER_KINDS)


def test_generic_ledger_struct_is_not_story_arc_remainder() -> None:
    kinds = _kinds(
        find_benchmark_emulation_fingerprints([("src/ledger.rs", _RUST_GENERIC_LEDGER)])
    )
    assert "story-arc-remainder-compiler" not in kinds
    assert kinds.isdisjoint(_NEW_FAMILY_COMPILER_KINDS)


def test_honest_family_agent_still_trips_no_rev7_or_rev8_fingerprints() -> None:
    findings = find_benchmark_emulation_fingerprints(
        [("src/agent.rs", _RUST_HONEST_FAMILY)]
    )
    assert findings == []


def test_rev8_findings_never_leak_matched_source_text() -> None:
    findings = find_benchmark_emulation_fingerprints(
        [
            ("src/story.rs", _RUST_STORY_ARC_REMAINDER),
            ("src/money.rs", _RUST_REQUIRED_MONEY_FORMATTER),
            ("src/ledger.rs", _RUST_LEDGER_EVENT_KIND),
            ("src/shape.rs", _RUST_WORLD_SHAPE_RULE),
            ("src/trip.rs", _RUST_TRIP_DAY_RETRY),
        ]
    )
    encoded = json.dumps(findings)
    for secret in (
        "required_money",
        "Return ANSWER:",
        "LedgerEventKind",
        "narrative_ledger_issues",
        "correction_only",
        "event_only",
        "world_shape_rule",
        "world_shape_of",
        "BalanceFinal",
        "WorldShape::Outstanding",
        "StoryArc",
        "reply_restates_story_ingredient_money",
        "CASE-",
        "days_focus_coach_line",
        "reply_misses_days_change_application",
    ):
        assert secret not in encoded
    for finding in findings:
        assert finding["category"] == "benchmark_emulation"
        for location in finding["locations"]:
            assert set(location) == {"path", "line", "role"}


_RUST_WORKSHEET_FALLBACK_OVERWRITE = """\
fn finish(result: &mut RunResult, worksheet: &str) {
    let fallback = Some(worksheet)
        .filter(|w| program_reply_needs_review(w, &result.text))
        .and_then(grounded_program_fallback);
    if let Some(text) = fallback {
        result.text = text;
    }
}
"""

_RUST_PROVIDER_FAILOVER_IS_HONEST = """\
fn finish(result: &mut RunResult, draft: &str) {
    if answer_reply_needs_review(draft, &result.text) {
        let text = provider_fallback(draft);
        result.text = text;
    }
}
"""

_RUST_SCRIPTED_DECLINE_GATE = """\
fn absent_note(notes: &mut String) {
    notes.push_str(
        "The stored record has no answer for this attribute. Reply exactly: \\
\\"I don't have that information.\\" Do not call any tool and do not guess.",
    );
}
"""

_RUST_HONEST_UNKNOWABLE_NOTE = """\
fn unknowable_note(notes: &mut String) {
    notes.push_str(
        "This asks about something no record can know. Say briefly and \\
honestly, in your own words, that it can't be known.",
    );
}
"""


def test_worksheet_fallback_overwrite_trips_on_aceron_b12_v5_shape() -> None:
    kinds = _kinds(
        find_benchmark_emulation_fingerprints(
            [("src/baseline.rs", _RUST_WORKSHEET_FALLBACK_OVERWRITE)]
        )
    )
    assert "worksheet-fallback-overwrite" in kinds


def test_provider_failover_fallback_is_not_the_overwrite_tell() -> None:
    kinds = _kinds(
        find_benchmark_emulation_fingerprints(
            [("src/baseline.rs", _RUST_PROVIDER_FAILOVER_IS_HONEST)]
        )
    )
    assert "worksheet-fallback-overwrite" not in kinds


def test_scored_family_decline_gate_trips_on_scripted_decline() -> None:
    kinds = _kinds(
        find_benchmark_emulation_fingerprints(
            [("src/baseline.rs", _RUST_SCRIPTED_DECLINE_GATE)]
        )
    )
    assert "scored-family-decline-gate" in kinds


def test_honest_unknowable_note_is_not_the_decline_gate() -> None:
    kinds = _kinds(
        find_benchmark_emulation_fingerprints(
            [("src/baseline.rs", _RUST_HONEST_UNKNOWABLE_NOTE)]
        )
    )
    assert "scored-family-decline-gate" not in kinds


def test_rev9_findings_never_leak_matched_source_text() -> None:
    findings = find_benchmark_emulation_fingerprints(
        [
            ("src/baseline.rs", _RUST_WORKSHEET_FALLBACK_OVERWRITE),
            ("src/notes.rs", _RUST_SCRIPTED_DECLINE_GATE),
        ]
    )
    encoded = json.dumps(findings)
    for secret in (
        "program_reply_needs_review",
        "grounded_program_fallback",
        "Reply exactly",
        "don't have that information",
    ):
        assert secret not in encoded
    for finding in findings:
        assert finding["category"] == "benchmark_emulation"
        for location in finding["locations"]:
            assert set(location) == {"path", "line", "role"}
