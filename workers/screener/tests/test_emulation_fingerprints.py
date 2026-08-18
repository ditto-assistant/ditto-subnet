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

# --------------------------------------------------------------------------- #
# Emulator fixtures (trip fingerprints).                                       #
# --------------------------------------------------------------------------- #

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

# --------------------------------------------------------------------------- #
# Honest fixtures (trip nothing).                                              #
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# The Rust emulator trips every fingerprint.                                   #
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Individual fingerprints on focused fixtures.                                 #
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Honest agents trip nothing, in every language.                              #
# --------------------------------------------------------------------------- #


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
    # served/executable source (issue #424 -- test modules can be stripped anyway).
    assert (
        find_benchmark_emulation_fingerprints([("tests/agent.rs", _RUST_EMULATOR)])
        == []
    )


# --------------------------------------------------------------------------- #
# Rev7 (2026-08-18) FAMILY COMPILER fixtures and fingerprints.                 #
# --------------------------------------------------------------------------- #

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
