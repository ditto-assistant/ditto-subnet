"""Screener policy v14 lead tests: catalog writers, schema mutation, draft
replacement, and generator-template fixtures.

Each positive fixture mirrors the decisive construct of one of the five
2026-09-13 top-5 board rejects (lets_623, comeτ-2 v1, Pentagon v1, Clear v14,
unione v27) at the shape the operator read cited; each must yield at least one
new-kind lead whose location sits on the decisive line. The negatives -- the
real public starter kit, an honest semantic (embedding top-k) preloader, a
threshold preloader, and a pass-through model client -- must yield zero
catalog-writer leads: a lead is tolerated on a negative, a citation never is,
and a lead is never promoted here. Every regex here is dodged in one commit;
these tests pin only that the five known artifacts fire.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

import pytest

from ditto_screener import generator_ngrams
from ditto_screener import source_review as source_review_module
from ditto_screener.source_review import TarSourceRepository
from ditto_screener.source_signals import (
    _EMULATION_FINGERPRINTS,
    CATALOG_WRITER_FINGERPRINT_KINDS,
    find_benchmark_emulation_fingerprints,
    find_fixture_generator_ngram_leads,
)
from ditto_screening_protocol import (
    CATALOG_WRITER_LEADS_POLICY_VERSION,
    SCREENING_FLOOR_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
    SourceReviewInvariant,
    SourceReviewInvariantDisposition,
)

_NEW_KINDS = CATALOG_WRITER_FINGERPRINT_KINDS | {
    "tool-schema-mutation",
    "draft-replacement-guard",
}

_REPO_ROOT = Path(__file__).resolve().parents[3]
_STARTER_KIT = _REPO_ROOT / "miners" / "dittobench-starter-kit"
_DATAGEN = _REPO_ROOT / "research" / "dittobench-datagen"

LETS_623_BASELINE_PY = '''\
"""Wire tool adapter (lets_623 shape)."""
from .routing import requests_missing_action_details, requests_social_chitchat


class WireTool:
    @classmethod
    def from_wire(cls, d, exec_ctx):
        description = str(d.get("description", ""))
        # Fallible examples provide context; the model freely authors arguments.
        if d.get("name") == "set_main_model":
            description += (
                " Model identifier examples include gpt-5, claude-sonnet-5, "
                "gemini-3-pro, and llama-4-70b. These are illustrative "
                "candidates, not a verified supported-model inventory."
            )
        elif d.get("name") == "set_chat_font":
            description += (
                " Apply the font change. Normalize case and a small spelling "
                "error in a clearly named font; canonical examples include "
                "Atkinson Hyperlegible, Inter, Source Sans 3, IBM Plex Sans."
            )
        return cls(name=d["name"], description=description, exec_ctx=exec_ctx)


class Baseline:
    def prepare(self, user_input, allowed_wire_defs, exec_ctx, preference_advisory):
        host_tools = [
            WireTool.from_wire(d, exec_ctx) for d in allowed_wire_defs
        ]
        missing_action_details = requests_missing_action_details(user_input)
        policy_tool_free = bool(
            not preference_advisory
            and (missing_action_details or requests_social_chitchat(user_input))
        )
        dated_memory_flow = False
        expose_host_catalog = True
        if policy_tool_free:
            selected_memory_tool_names = set()
        elif dated_memory_flow and not expose_host_catalog:
            selected_memory_tool_names = set()
        else:
            selected_memory_tool_names = set(MEMORY_TOOL_NAMES)
        return host_tools, selected_memory_tool_names, policy_tool_free
'''

LETS_623_CHAT_PY = '''\
class Harness:
    def agent_loop(self, req, advisory_tool_names=(), disable_tools=False):
        """chat.rs:240 -- an agent loop wired with this harness's model."""
        resolved_kg_id = self.kg_id(req.user_id)
        tools = (
            []
            if disable_tools
            else self.tools_for(req.user_id, resolved_kg_id)
        )
        defs = [t.definition() for t in tools]
        return Loop(
            self.model,
            defs,
            {t.name: t for t in tools},
            max_tool_calls=self.max_tool_calls,
            time_budget_seconds=self.time_budget_seconds,
            fallback_tool=None if disable_tools else self.fallback_tool,
            advise_after_success=self.advise_after_success,
        )
'''

COMET_BASELINE_RS = """\
fn withhold_host_tools(
    attacked: bool,
    explicit_action_negation: bool,
    declarative_preference: bool,
    explicit_save_request: bool,
    route: &str,
    store_mutation: bool,
    required_action_precedence: bool,
) -> bool {
    if required_action_precedence {
        return false;
    }
    attacked
        || explicit_action_negation
        || (declarative_preference && !explicit_save_request)
        || (route == "no-action" && !store_mutation)
}

impl Baseline {
    async fn run(&self, req: RunRequest) -> anyhow::Result<RunResponse> {
        let declarative_preference = declarative_preference_value.is_some();
        let offered = req.tools.clone();
        let catalog_has_memory = offered.iter().any(|d| is_memory_tool(&d.name));
        let host_tools: Vec<Arc<dyn Tool>> = if withhold_host_tools(
            attacked,
            explicit_action_negation || no_capability_can_settle,
            declarative_preference,
            explicit_save_request,
            route,
            store_mutation,
            required_action_precedence,
        ) {
            Vec::new()
        } else {
            let graph = kg_id(&user_id);
            memory_tools(Arc::clone(&self.store), &user_id, &graph)
        };
        let mut result = model.next(&messages, &host_tools).await?;
        if own_record_read
            && crate::optimizer::extract_answer_slot(&result.text).is_none()
            && !crate::optimizer::looks_like_decline(&result.text)
        {
            let dumpish = result.text.contains("```")
                || result.text.contains("\\"memories\\"")
                || result.text.contains("\\"charLen\\"")
                || result.text.contains("<|")
                || serde_json::from_str::<Value>(&result.text)
                    .map(|v| v.is_object())
                    .unwrap_or(false)
                || result.text.to_lowercase().contains("following memories")
                || result.text.contains("the search returned")
                || result.text.contains("**p-")
                || result.text.matches("preview").count() >= 2;
            if dumpish && remaining >= Duration::from_secs(4) {
                let mut messages = messages.clone();
                messages.push(ChatMessage {
                    role: "user".to_string(),
                    content: vec![Content::text(format!(
                        "Your previous draft pasted raw tool output: {}",
                        &result.text[..result.text.len().min(1500)]
                    ))],
                });
                let review = model.next(&messages, &[]).await;
                if let Ok(chunk) = review {
                    if chunk.tool_call.is_none() && !chunk.text.trim().is_empty() {
                        result.text = chunk.text.clone();
                        result.messages.push(ChatMessage {
                            role: "assistant".to_string(),
                            content: vec![Content::text(chunk.text)],
                        });
                    }
                }
            }
        }
        Ok(final_response_fields(&result.text))
    }
}
"""

COMET_SINGLE_TOOL_MODEL_RS = """\
pub fn request_controls(tools: &[ToolDefinition], seed: Option<u64>) -> Controls {
    let mut additional_params = serde_json::Map::new();
    additional_params.insert("parallel_tool_calls".to_string(), Value::Bool(false));
    if let Some(seed) = seed {
        additional_params.insert("seed".to_string(), json!(seed));
    }

    let tool_choice = if tools.is_empty() {
        Some(RigToolChoice::None)
    } else {
        None
    };
    Controls { tool_choice, additional_params }
}
"""

COMET_TASK_SPEC_RS = """\
pub fn classify(request: &str) -> TaskSpec {
    let normal_preference_framing =
        request.contains("my normal") || request.contains("my usual");
    TaskSpec { normal_preference_framing }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn declarative_preference_sentence_is_not_an_action() {
        for request in [
            "Please keep my workspace on system mode as my normal appearance setting.",
            "Please keep my workspace on systen mode as my normal appearance setting.",
            "please keep my workspace on system mode as my normal appearance setting",
        ] {
            assert!(classify(request).normal_preference_framing, "{request}");
        }
    }
}
"""

PENTAGON_CHAT_RS = """\
impl ChatModel {
    pub async fn grounded_answer(
        &self,
        messages: Vec<Value>,
        seed: u64,
    ) -> anyhow::Result<Turn> {
        let preferred_provider = provider_preference();
        let reasoning_effort = "medium";
        let body = json!({
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 900,
            "reasoning_effort": reasoning_effort,
            "provider": provider_preferences(&preferred_provider),
            "seed": seed,
            "stream": false,
            "tools": [],
            "tool_choice": "none",
            "parallel_tool_calls": false,
        });

        let mut input_tokens = 0i64;
        let mut output_tokens = 0i64;
        let payload = self.post(&body).await?;
        Ok(parse_turn(payload, &mut input_tokens, &mut output_tokens))
    }
}
"""

PENTAGON_SERVE_RS = """\
impl Agent {
    async fn respond_legacy(&self, req: RunRequest) -> anyhow::Result<RunResponse> {
        let declines_lookup_only = declines_live_lookup(&req.user_input);
        let explicit_live_state_read = requests_live_state(&req.user_input);
        let missing_value = clearly_missing_required_action_value(&req.user_input);
        let deterministic_read_only = (missing_value
            && !requests_explicit_page_read(&req.user_input)
            && !requests_external_lookup_delivery(&req.user_input))
            || declines_lookup_only
            || (states_personal_product_preference(&req.user_input)
                && !requests_memory_persistence(&req.user_input))
            || (clearly_historical_read_only(&req.user_input)
                && !explicit_live_state_read);
        let request_route = if deterministic_read_only {
            ContextPass {
                grounded_tools: Vec::new(),
                lane: Lane::NoCatalog,
            }
        } else {
            self.model.route(&req).await?
        };
        self.finish(req, request_route).await
    }
}

/// A personal product preference stated as a fact is conversational memory.
fn states_personal_product_preference(request: &str) -> bool {
    let lower = request.to_ascii_lowercase();
    let words = request_words(&lower);
    if lower.trim_end().ends_with('?') || requests_memory_persistence(request) {
        return false;
    }
    words.contains("personal") && (words.contains("accent") || words.contains("font"))
}
"""

CLEAR_AGENT_RS = """\
impl Agent {
    pub async fn run(&self, req: RunRequest) -> anyhow::Result<RunResponse> {
        let model = self
            .case_model(inference_base_url, native_provenance)
            .unwrap_or_else(|| Arc::clone(&self.model));

        if !req.tools.is_empty() {
            let mut prompt_tokens = 0i64;
            let mut output_tokens = 0i64;
            // Avoid a separate API call for grammatically explicit recall.
            let live_read = wants_live_fact(&req.user_input)
                || wants_lookup_delivery(&req.user_input)
                || wants_page_read(&req.user_input);
            let forced_read_only = (missing_action_value(&req.user_input)
                && !wants_page_read(&req.user_input)
                && !wants_lookup_delivery(&req.user_input))
                || (is_recall(&req.user_input) && !live_read);
            let route = if forced_read_only {
                Route {
                    mode: Mode::ReadOnly,
                    ops: Vec::new(),
                    input_tokens: 0,
                    output_tokens: 0,
                }
            } else {
                model.route(&req).await?
            };
            match route.mode {
                Mode::ReadOnly => {
                    let grounded = model.answer(&grounded_messages).await?;
                    return Ok(RunResponse::from_turn(grounded, Vec::new()));
                }
                Mode::RuntimeTools => {}
            }
        }
        self.tool_loop(req, model).await
    }
}
"""

UNIONE_BASELINE_RS = """\
fn requested_memory_mutation(request: &str, authorized: bool) -> Option<Capability> {
    if !requests_explicit_memory_mutation(request, authorized) {
        return None;
    }
    let lower = request.to_ascii_lowercase();
    let deletes = ["delete", "remove", "bin ", "bin that", "discard"];
    let updates = ["update", "correct memory", "change memory", "scratchpad"];
    if deletes.iter().any(|cue| lower.contains(cue)) {
        Some(Capability::MemoryDelete)
    } else if updates.iter().any(|cue| lower.contains(cue)) {
        Some(Capability::MemoryUpdate)
    } else {
        Some(Capability::MemoryWrite)
    }
}

fn constrain_grounded(def: &ToolDefinition, grounded: &Value) -> ToolDefinition {
    let mut constrained = def.clone();
    let Some(schema) = constrained.parameters.as_object_mut() else {
        return constrained;
    };
    let mut required = schema
        .get("required")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let Some(properties) = schema
        .get_mut("properties")
        .and_then(Value::as_object_mut)
    else {
        return constrained;
    };
    for (name, value) in arguments {
        let Some(property) = properties.get_mut(name).and_then(Value::as_object_mut)
        else {
            return def.clone();
        };
        // A single-value enum states the already-grounded value.
        property.insert("enum".to_string(), Value::Array(vec![value.clone()]));
        if !required.iter().any(|item| item.as_str() == Some(name)) {
            required.push(Value::String(name.clone()));
        }
    }
    schema.insert("required".to_string(), Value::Array(required));
    schema.insert("additionalProperties".to_string(), Value::Bool(false));
    constrained
}

impl Baseline {
    async fn select_tools(&self, req: &RunRequest, binder: &Binder) -> Vec<Tool> {
        let action_authorized = true;
        let memory_mutation = (operational_route && !operational_object)
            .then(|| requested_memory_mutation(&req.user_input, action_authorized))
            .flatten();
        let mut host_tools: Vec<Arc<dyn Tool>> = binder.request_tools(req);
        if needs_computation {
            host_tools.retain(|tool| {
                binder
                    .registry
                    .get(&tool.definition().name)
                    .is_some_and(|registered| {
                        registered.capabilities.contains(&Capability::Compute)
                    })
            });
        } else if let Some(capability) = memory_mutation {
            host_tools.retain(|tool| {
                binder
                    .registry
                    .get(&tool.definition().name)
                    .is_some_and(|registered| {
                        registered.capabilities.contains(&capability)
                    })
            });
        }
        host_tools
    }
}
"""

# ---------------- negatives ----------------
SEMANTIC_PRELOADER_RS = """\
//! Honest semantic preloading: the request catalog is embedded once, the top-k
//! tools by cosine similarity to the request stay on the deciding turn, and
//! any model call to an unlisted request tool still executes.
impl Agent {
    pub async fn run(&self, req: RunRequest) -> anyhow::Result<RunResponse> {
        let query = self.embedder.embed(&req.user_input).await?;
        let mut scored: Vec<(f32, ToolDefinition)> = Vec::new();
        for def in &req.tools {
            let vector = self.embedder.embed(&def.description).await?;
            scored.push((cosine(&query, &vector), def.clone()));
        }
        scored.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(Ordering::Equal));
        let top_k = 6usize.min(scored.len());
        let tools: Vec<ToolDefinition> =
            scored.into_iter().take(top_k).map(|(_, d)| d).collect();
        let turn = self.model.next(&messages, &tools).await?;
        let mut executed = Vec::new();
        for call in turn.tool_calls {
            // Any request tool executes, listed on the deciding turn or not.
            let result = self.executor.execute(&req.tools, &call).await?;
            executed.push(result);
        }
        Ok(RunResponse::from_turn(turn, executed))
    }
}
"""

THRESHOLD_PRELOADER_PY = '''\
"""Honest threshold preloader: tools whose relevance score clears a fixed
threshold stay on the deciding request; the full catalog remains executable."""


class Agent:
    async def run(self, req):
        descriptions = [t.description for t in req.tools]
        scores = await self.reranker.score(req.user_input, descriptions)
        threshold = 0.35
        tools = [t for t, score in zip(req.tools, scores) if score >= threshold]
        if not tools:
            tools = list(req.tools)
        turn = await self.model.next(self.messages(req), tools)
        executed = []
        for call in turn.tool_calls:
            executed.append(await self.executor.execute(req.tools, call))
        response = RunResponse(text=turn.text, tool_calls=executed)
        return response
'''

HONEST_PASSTHROUGH_RS = """\
impl Model {
    pub async fn next(&self, messages: &[Value], tools: &[Def]) -> Result<Turn> {
        let mut body = json!({
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        });
        if !tools.is_empty() {
            body["tools"] = Value::Array(tools.iter().map(definition_json).collect());
            body["tool_choice"] = Value::String("auto".to_string());
        }
        let payload = self.post(&body).await?;
        let mut result = parse_turn(payload)?;
        if result.text.is_empty() {
            result.text = String::new();
        }
        Ok(result)
    }
}
"""


def _new_kind_findings(files: list[tuple[str, str]]) -> list[dict[str, object]]:
    return [
        finding
        for finding in find_benchmark_emulation_fingerprints(files)
        if finding["kind"] in _NEW_KINDS
    ]


def _lines(findings: list[dict[str, object]], kind: str, path: str) -> set[int]:
    lines: set[int] = set()
    for finding in findings:
        if finding["kind"] != kind:
            continue
        for location in finding["locations"]:
            assert isinstance(location, dict)
            if location["path"] == path:
                lines.add(int(location["line"]))
    return lines


def _line_of(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number
    raise AssertionError(f"fixture lacks {needle!r}")


# ── the five 2026-09-13 rejects: ≥1 lead at the decisive line ────────────────


def test_lets_623_tool_free_policy_and_schema_vocabulary_fire() -> None:
    files = [("baseline.py", LETS_623_BASELINE_PY), ("chat.py", LETS_623_CHAT_PY)]
    findings = _new_kind_findings(files)
    empty = _lines(findings, "catalog-writer-empty-tools", "baseline.py")
    assert _line_of(LETS_623_BASELINE_PY, "selected_memory_tool_names = set()") in empty
    assert _line_of(LETS_623_BASELINE_PY, "if policy_tool_free:") in empty
    chat = _lines(findings, "catalog-writer-empty-tools", "chat.py")
    assert _line_of(LETS_623_CHAT_PY, "if disable_tools") in chat
    assert _line_of(LETS_623_CHAT_PY, "fallback_tool=None if disable_tools") in chat
    schema = _lines(findings, "tool-schema-mutation", "baseline.py")
    assert _line_of(LETS_623_BASELINE_PY, 'd.get("name") == "set_main_model"') in schema
    assert _line_of(LETS_623_BASELINE_PY, "description += (") in schema


def test_comet_withhold_host_tools_dumpish_and_tool_choice_fire() -> None:
    files = [
        ("src/baseline.rs", COMET_BASELINE_RS),
        ("src/single_tool_model.rs", COMET_SINGLE_TOOL_MODEL_RS),
        ("src/task_spec.rs", COMET_TASK_SPEC_RS),
    ]
    findings = _new_kind_findings(files)
    empty = _lines(findings, "catalog-writer-empty-tools", "src/baseline.rs")
    assert _line_of(COMET_BASELINE_RS, "= if withhold_host_tools(") in empty
    assert _line_of(COMET_BASELINE_RS, "            Vec::new()") in empty
    draft = _lines(findings, "draft-replacement-guard", "src/baseline.rs")
    assert _line_of(COMET_BASELINE_RS, "result.text = chunk.text.clone();") in draft
    dump_block = range(
        _line_of(COMET_BASELINE_RS, "looks_like_decline(&result.text)"),
        _line_of(COMET_BASELINE_RS, "if dumpish && remaining") + 1,
    )
    assert draft & set(dump_block)
    choice = _lines(findings, "tool-choice-none-literal", "src/single_tool_model.rs")
    assert _line_of(COMET_SINGLE_TOOL_MODEL_RS, "Some(RigToolChoice::None)") in choice


def test_pentagon_no_catalog_lane_fires_on_the_literal_alone() -> None:
    findings = _new_kind_findings([("src/agent/model/chat.rs", PENTAGON_CHAT_RS)])
    choice = _lines(findings, "tool-choice-none-literal", "src/agent/model/chat.rs")
    assert _line_of(PENTAGON_CHAT_RS, '"tool_choice": "none"') in choice
    # The classifier lives in another file: the literal must fire without it.
    assert not _lines(findings, "catalog-writer-empty-tools", "src/agent/model/chat.rs")
    routed = _new_kind_findings([("src/agent/serve.rs", PENTAGON_SERVE_RS)])
    empty = _lines(routed, "catalog-writer-empty-tools", "src/agent/serve.rs")
    assert _line_of(PENTAGON_SERVE_RS, "grounded_tools: Vec::new(),") in empty
    classifier_block = range(
        _line_of(PENTAGON_SERVE_RS, "let declines_lookup_only ="),
        _line_of(PENTAGON_SERVE_RS, "let request_route = if deterministic_read_only"),
    )
    assert empty & set(classifier_block)


def test_clear_forced_read_only_route_fires_before_any_model_pass() -> None:
    findings = _new_kind_findings([("src/agent.rs", CLEAR_AGENT_RS)])
    empty = _lines(findings, "catalog-writer-empty-tools", "src/agent.rs")
    assert _line_of(CLEAR_AGENT_RS, "mode: Mode::ReadOnly,") in empty
    classifier_block = range(
        _line_of(CLEAR_AGENT_RS, "let live_read = wants_live_fact"),
        _line_of(CLEAR_AGENT_RS, "let route = if forced_read_only"),
    )
    assert empty & set(classifier_block)


def test_unione_capability_retain_and_one_value_enum_fire() -> None:
    findings = _new_kind_findings([("src/baseline.rs", UNIONE_BASELINE_RS)])
    retain = _lines(findings, "catalog-narrowing-retain", "src/baseline.rs")
    # The decisive line is the retain that collapses the class to the one
    # host-selected mutation capability, directly under the cue-table branch.
    assert (
        _line_of(UNIONE_BASELINE_RS, "Some(capability) = memory_mutation") + 1 in retain
    )
    schema = _lines(findings, "tool-schema-mutation", "src/baseline.rs")
    assert _line_of(UNIONE_BASELINE_RS, 'property.insert("enum".to_string()') in schema
    assert _line_of(UNIONE_BASELINE_RS, "properties.get_mut(name)") in schema


def test_each_reject_carries_a_model_tool_planning_or_dissent_invariant() -> None:
    from ditto_screener.source_review import _STATIC_INVARIANT_BY_KIND
    from ditto_screening_protocol import SourceReviewInvariant

    for kind in CATALOG_WRITER_FINGERPRINT_KINDS | {"tool-schema-mutation"}:
        assert (
            _STATIC_INVARIANT_BY_KIND[kind] is SourceReviewInvariant.MODEL_TOOL_PLANNING
        )
    assert (
        _STATIC_INVARIANT_BY_KIND["draft-replacement-guard"]
        is SourceReviewInvariant.MODEL_DISSENT
    )


# ── negatives: zero catalog-writer citations ─────────────────────────────────


def _starter_kit_files() -> list[tuple[str, str]]:
    if not _STARTER_KIT.is_dir():
        pytest.skip("starter kit not checked out beside the screener")
    files: list[tuple[str, str]] = []
    for path in sorted(_STARTER_KIT.rglob("*")):
        if not path.is_file() or "target" in path.parts:
            continue
        if path.suffix not in {".rs", ".py", ".toml", ".md", ".json", ".html"}:
            continue
        files.append((str(path.relative_to(_STARTER_KIT)), path.read_text("utf-8")))
    assert files
    return files


def test_starter_kit_yields_no_new_kind_leads() -> None:
    assert _new_kind_findings(_starter_kit_files()) == []


def test_starter_kit_public_templates_yield_no_generator_ngram_lead() -> None:
    # The kit's public datagen mirrors the public template surface; the corpus
    # subtracts it so a mirror of public text is never a private-surface lead.
    assert find_fixture_generator_ngram_leads(_starter_kit_files()) == []


def test_semantic_preloader_is_the_published_safe_harbor() -> None:
    assert _new_kind_findings([("src/agent.rs", SEMANTIC_PRELOADER_RS)]) == []


def test_threshold_preloader_is_not_a_catalog_writer() -> None:
    assert _new_kind_findings([("agent.py", THRESHOLD_PRELOADER_PY)]) == []


def test_pass_through_client_with_conditional_tools_key_stays_clear() -> None:
    assert _new_kind_findings([("src/model.rs", HONEST_PASSTHROUGH_RS)]) == []


def test_request_keyed_retain_is_suppressed_by_similarity_scoring() -> None:
    source = UNIONE_BASELINE_RS.replace(
        "        } else if let Some(capability) = memory_mutation {",
        "        } else if let Some(capability) = memory_mutation {\n"
        "            // rank by cosine similarity score against the request\n"
        "            let similarity_threshold = 0.4;",
    )
    findings = _new_kind_findings([("src/baseline.rs", source)])
    retain = _lines(findings, "catalog-narrowing-retain", "src/baseline.rs")
    assert _line_of(source, "Some(capability) = memory_mutation") not in retain


def test_tool_choice_parameter_default_is_not_the_literal() -> None:
    source = (
        "class Client:\n"
        "    def complete(self, messages, tools=None, tool_choice=None):\n"
        "        body = {'messages': messages}\n"
        "        if tools:\n"
        "            body['tools'] = tools\n"
        "        return self.post(body)\n"
    )
    assert _new_kind_findings([("client.py", source)]) == []


def test_new_kind_findings_never_leak_matched_source_text() -> None:
    files = [
        ("baseline.py", LETS_623_BASELINE_PY),
        ("src/baseline.rs", COMET_BASELINE_RS),
        ("src/agent.rs", CLEAR_AGENT_RS),
    ]
    rendered = json.dumps(_new_kind_findings(files))
    for secret in (
        "gpt-5",
        "withhold_host_tools",
        "dumpish",
        "missing_action_value",
        "canonical examples",
    ):
        assert secret not in rendered
    for finding in _new_kind_findings(files):
        assert set(finding) == {"category", "kind", "severity", "locations"}
        assert finding["category"] == "benchmark_emulation"
        for location in finding["locations"]:
            assert isinstance(location, dict)
            assert set(location) == {"path", "line", "role"}


# ── generator-template fixtures: leads, never citations ──────────────────────


def test_comet_task_spec_fixture_is_an_inadmissible_ngram_lead() -> None:
    leads = find_fixture_generator_ngram_leads(
        [("src/task_spec.rs", COMET_TASK_SPEC_RS)]
    )
    assert len(leads) == 1
    lead = leads[0]
    assert lead["kind"] == "fixture-generator-ngram"
    assert lead["admissible"] is False
    assert int(lead["matched_grams"]) >= 2
    cited = {int(location["line"]) for location in lead["locations"]}
    assert _line_of(COMET_TASK_SPEC_RS, "system mode as my normal appearance") in cited
    rendered = json.dumps(leads)
    assert "appearance setting" not in rendered
    hex_tokens = set(re.findall(r"\b[0-9a-f]{16}\b", rendered))
    assert not hex_tokens & generator_ngrams.load_corpus()


def test_generator_sentence_in_tests_directory_is_inadmissible() -> None:
    fixture = (
        "def test_declarative():\n"
        "    assert route(\n"
        '        "Please keep my workspace on system mode "\n'
        '        "as my normal appearance setting."\n'
        "    ) == Route.ACK\n"
    )
    leads = find_fixture_generator_ngram_leads([("tests/test_route.py", fixture)])
    assert len(leads) == 1
    assert leads[0]["admissible"] is False


def test_generator_sentence_on_the_served_path_stays_admissible() -> None:
    served = (
        "fn classify(request: &str) -> Route {\n"
        '    let ack = "Please keep my workspace on system mode '
        'as my normal appearance setting.";\n'
        "    if request == ack {\n"
        "        return Route::Ack;\n"
        "    }\n"
        "    Route::Model\n"
        "}\n"
    )
    leads = find_fixture_generator_ngram_leads([("src/intent.rs", served)])
    assert len(leads) == 1
    assert leads[0]["admissible"] is True


def test_single_english_overlap_is_not_a_lead() -> None:
    fixture = 'assert route("Please keep my workspace on tonight") == Route.OTHER\n'
    assert find_fixture_generator_ngram_leads([("tests/test_one.py", fixture)]) == []


def test_ngram_leads_are_surfaced_in_the_review_inventory() -> None:
    from ditto_screener import source_review

    assert "fixture_generator_ngrams" in Path(source_review.__file__).read_text("utf-8")


# ── the hashed corpus ────────────────────────────────────────────────────────


def test_corpus_is_well_formed_and_loaded() -> None:
    corpus = generator_ngrams.load_corpus()
    assert len(corpus) > 1_000
    assert all(len(item) == generator_ngrams.HASH_HEX_CHARS for item in corpus)


def test_placeholders_split_segments_and_grams_are_informative() -> None:
    grams = list(generator_ngrams.iter_grams("What's on my calendar about %s?"))
    assert grams == []
    grams = list(
        generator_ngrams.iter_grams(
            "Please keep my workspace on %s mode as my normal appearance setting."
        )
    )
    assert ("please", "keep", "my", "workspace", "on") in grams
    assert ("mode", "as", "my", "normal", "appearance") in grams
    assert all("%" not in token for gram in grams for token in gram)


def test_go_string_literals_skip_comments_and_runes() -> None:
    source = (
        '// "not a literal"\n'
        'var x = []string{"first template here", `raw one`}\n'
        "/* \"also not\" */ r := 'x'\n"
        'y := "escaped \\"quote\\" inside"\n'
    )
    assert generator_ngrams.go_string_literals(source) == [
        "first template here",
        "raw one",
        'escaped "quote" inside',
    ]


def test_committed_corpus_is_derivable_from_the_generator_tree() -> None:
    """Every committed hash must come from the research tree.

    Bench versions are immutable, so once a template exists its grams stay
    derivable forever; a committed hash the generator cannot produce means
    the corpus was built from something else. Coverage in the other direction
    (new v13 surfaces not yet hashed) is reported by
    ``scripts/build_generator_ngram_corpus.py --check`` and is deliberately not
    a CI gate so a datagen surface PR never turns the screener red.
    """
    if not _DATAGEN.is_dir():
        pytest.skip("research tree not checked out beside the screener")
    import importlib.util

    script = _REPO_ROOT / "workers/screener/scripts/build_generator_ngram_corpus.py"
    spec = importlib.util.spec_from_file_location("build_corpus", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    regenerated = set(module.regenerate()["hashes"])
    committed = generator_ngrams.load_corpus()
    assert committed <= regenerated, "corpus carries hashes the generator cannot yield"
    coverage = len(committed) / max(len(regenerated), 1)
    assert coverage >= 0.5, "corpus lags the generator by more than half; regenerate"


# ── the I7 prompt addendum ───────────────────────────────────────────────────


def test_policy_v14_is_the_built_in_version_carrying_the_leads() -> None:
    assert CATALOG_WRITER_LEADS_POLICY_VERSION == 14
    assert SCREENING_POLICY_VERSION == CATALOG_WRITER_LEADS_POLICY_VERSION


def test_policy_v14_prompt_requires_the_catalog_writer_inventory() -> None:
    from ditto_screener.source_review import (
        _POLICY_TAILS,
        _source_review_system_prompt,
    )

    prompt = _source_review_system_prompt(CATALOG_WRITER_LEADS_POLICY_VERSION)
    assert "I7 CATALOG-WRITER INVENTORY" in prompt
    for kind in sorted(_NEW_KINDS):
        assert f"- {kind}:" in prompt
    assert "fixture_generator_ngrams" in prompt
    assert "inconclusive" in prompt
    # Older frozen policy texts are unchanged; v14 extends v13 which extends v12.
    for frozen in (12, 13):
        assert "I7 CATALOG-WRITER INVENTORY" not in _source_review_system_prompt(frozen)
    assert _POLICY_TAILS[13].startswith(_POLICY_TAILS[12])
    assert _POLICY_TAILS[14].startswith(_POLICY_TAILS[13])


# Policy v13 was activated and signed in production on 2026-09-14. Its prompt
# bytes and lead set must never change again; these digests were taken from
# origin/main at 4ae5f1cb7 (v0.265.0), the last commit before v14 landed.
# `git show 4ae5f1cb7:workers/screener/ditto_screener/source_review.py` and
# `.../l2_review.py` reproduce them. A diff here means a v13 byte moved.
_V13_L1_TAIL_SHA256 = "ef62995493ce056ba7568685d1cd5a4b85716b80324fbf3d5c2b560bdfff408d"
_V13_L1_PROMPT_SHA256 = (
    "87045e01ed9187d82e335b3bbbd28090acd4e9add833afb169a8f59c1df247cd"
)
_V13_L2_TAIL_SHA256 = "fd865ad806cc575fe80269195fc5ebf96e0817f4fc0ef62b8f6454beb990a1eb"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_policy_v13_prompt_bytes_are_pinned_to_the_signed_main_text() -> None:
    from ditto_screener.l2_review import _L2_POLICY_TAILS, _l2_review_system_prompt
    from ditto_screener.source_review import (
        _POLICY_TAILS,
        _source_review_system_prompt,
    )

    assert _sha256(_POLICY_TAILS[13]) == _V13_L1_TAIL_SHA256
    assert _sha256(_source_review_system_prompt(13)) == _V13_L1_PROMPT_SHA256
    assert _sha256(_L2_POLICY_TAILS[13]) == _V13_L2_TAIL_SHA256
    # v14 is purely additive on top of the pinned v13 text at both layers.
    assert _POLICY_TAILS[14].startswith(_POLICY_TAILS[13])
    assert _L2_POLICY_TAILS[14].startswith(_L2_POLICY_TAILS[13])
    assert _POLICY_TAILS[14] != _POLICY_TAILS[13]
    assert "POLICY V14" in _l2_review_system_prompt(14)
    assert "POLICY V14" not in _l2_review_system_prompt(13)


# ── policy floor: the v14 leads never reach a frozen v10–v13 inventory ───────

_POSITIVE_FILES: list[tuple[str, str]] = [
    ("baseline.py", LETS_623_BASELINE_PY),
    ("chat.py", LETS_623_CHAT_PY),
    ("src/baseline.rs", COMET_BASELINE_RS),
    ("src/single_tool_model.rs", COMET_SINGLE_TOOL_MODEL_RS),
    ("src/task_spec.rs", COMET_TASK_SPEC_RS),
    ("src/agent/model/chat.rs", PENTAGON_CHAT_RS),
    ("src/agent.rs", CLEAR_AGENT_RS),
    ("src/unione/baseline.rs", UNIONE_BASELINE_RS),
]
_GENERATOR_SENTENCE = (
    "Please keep my workspace on system mode as my normal appearance setting."
)


def _archive(tmp_path: Path, files: list[tuple[str, str]]) -> str:
    path = tmp_path / "agent.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, text in [
            ("Cargo.toml", '[package]\nname="agent"\nversion="0.1.0"\n'),
            *files,
        ]:
            raw = text.encode("utf-8")
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return str(path)


def _kinds(findings: list[dict[str, object]]) -> list[str]:
    return sorted(str(finding["kind"]) for finding in findings)


def test_v14_fingerprints_carry_a_policy_14_floor_and_older_ones_do_not() -> None:
    by_kind = {fingerprint.kind: fingerprint for fingerprint in _EMULATION_FINGERPRINTS}
    assert set(by_kind) >= _NEW_KINDS
    for kind, fingerprint in by_kind.items():
        expected = (
            CATALOG_WRITER_LEADS_POLICY_VERSION
            if kind in _NEW_KINDS
            else SCREENING_FLOOR_POLICY_VERSION
        )
        assert fingerprint.min_policy_version == expected, kind


@pytest.mark.parametrize("policy_version", [10, 11, 12, 13])
def test_frozen_policy_fingerprints_exclude_every_v14_kind(policy_version: int) -> None:
    frozen = find_benchmark_emulation_fingerprints(
        _POSITIVE_FILES, policy_version=policy_version
    )
    assert not set(_kinds(frozen)) & _NEW_KINDS
    current = find_benchmark_emulation_fingerprints(_POSITIVE_FILES, policy_version=14)
    assert set(_kinds(current)) >= _NEW_KINDS
    # Everything a frozen policy already carried is byte-identical at v14: the
    # new kinds are purely additive.
    older_at_14 = [item for item in current if item["kind"] not in _NEW_KINDS]
    assert older_at_14 == frozen
    # The default remains the built-in policy version.
    assert find_benchmark_emulation_fingerprints(_POSITIVE_FILES) == current


@pytest.mark.parametrize("policy_version", [12, 13])
def test_frozen_policy_generator_ngram_leads_are_empty(policy_version: int) -> None:
    files = [("src/task_spec.rs", COMET_TASK_SPEC_RS)]
    assert (
        find_fixture_generator_ngram_leads(files, policy_version=policy_version) == []
    )
    assert find_fixture_generator_ngram_leads(files, policy_version=14)
    assert find_fixture_generator_ngram_leads(
        files
    ) == find_fixture_generator_ngram_leads(files, policy_version=14)


@pytest.mark.parametrize("policy_version", [12, 13])
def test_review_leads_at_a_frozen_policy_match_the_frozen_inventory(
    tmp_path: Path, policy_version: int
) -> None:
    archive = _archive(tmp_path, _POSITIVE_FILES)
    frozen = TarSourceRepository(archive, policy_version=policy_version).review_leads()
    current = TarSourceRepository(archive, policy_version=14).review_leads()
    frozen_fingerprints = frozen["emulation_fingerprints"]
    current_fingerprints = current["emulation_fingerprints"]
    assert isinstance(frozen_fingerprints, list)
    assert isinstance(current_fingerprints, list)
    assert not set(_kinds(frozen_fingerprints)) & _NEW_KINDS
    assert set(_kinds(current_fingerprints)) & _NEW_KINDS
    # Key shape is stable; content is frozen for policy <= 13.
    assert frozen["fixture_generator_ngrams"] == []
    assert current["fixture_generator_ngrams"]
    assert set(frozen) == set(current)
    inventory = json.loads(
        TarSourceRepository(archive, policy_version=policy_version).inventory()
    )
    assert inventory["review_leads"]["fixture_generator_ngrams"] == []
    assert not set(_kinds(inventory["review_leads"]["emulation_fingerprints"])) & (
        _NEW_KINDS
    )
    # The repository default tracks the built-in policy version.
    assert TarSourceRepository(archive).policy_version == SCREENING_POLICY_VERSION == 14


# ── I7 catalog-writer inventory: fail-closed in code, not only in prose ──────

_V13_PASS_CLAUSES = {
    "i1_model_invocation": "genuine_model_result",
    "i2_evidence_retention": "full_records_on_deciding_turn",
    "i3_model_dissent": "model_dissent_preserved",
    "i4_derived_value_authority": "no_derived_value",
    "i5_production_engine": "no_family_compiler",
    "i6_tool_execution_fidelity": "no_reported_tool_calls",
    "i7_model_tool_planning": "no_tool_planning",
    "i8_evaluation_independence": "evaluation_independent_runtime",
}


def _all_pass_decisions(policy_version: int = 14) -> list[dict[str, object]]:
    clauses = dict(_V13_PASS_CLAUSES)
    if policy_version < 13:
        clauses.pop("i8_evaluation_independence")
    return [
        {
            "invariant": invariant,
            "disposition": "pass",
            "pass_clause": clause,
            "summary": "The reviewed path satisfies the published pass clause.",
            "evidence_indices": [],
        }
        for invariant, clause in clauses.items()
    ]


def _i7(assessment: object) -> object:
    decisions = assessment.decisions
    return next(
        decision
        for decision in decisions
        if decision.invariant is SourceReviewInvariant.MODEL_TOOL_PLANNING
    )


def test_i7_pass_without_a_tool_dispatch_note_is_coerced_to_inconclusive() -> None:
    coercions: list[str] = []
    assessment = source_review_module._validated_invariant_assessment(
        _all_pass_decisions(),
        submitted_evidence=[],
        finding_evidence=[],
        demoted_to_low=False,
        policy_version=14,
        notes=[
            # A note on the right area but the wrong file does not finish
            # the inventory; a note on the right file but another area does
            # not either.
            {"kind": "cleared", "area": "tool_dispatch", "path": "src/other.rs"},
            {"kind": "concern", "area": "model_call", "path": "src/baseline.rs"},
        ],
        catalog_writer_lead_paths=frozenset({"src/baseline.rs"}),
        coercions=coercions,
    )
    decision = _i7(assessment)
    assert decision.disposition is SourceReviewInvariantDisposition.INCONCLUSIVE
    assert decision.pass_clause is None
    assert "catalog-writer" in decision.summary
    assert coercions == [decision.summary]
    # Every other invariant keeps the model's decision.
    others = [item for item in assessment.decisions if item is not decision]
    assert len(others) == 7
    assert all(
        item.disposition is SourceReviewInvariantDisposition.PASS for item in others
    )


def test_i7_pass_with_a_tool_dispatch_note_on_a_lead_file_is_kept() -> None:
    coercions: list[str] = []
    assessment = source_review_module._validated_invariant_assessment(
        _all_pass_decisions(),
        submitted_evidence=[],
        finding_evidence=[],
        demoted_to_low=False,
        policy_version=14,
        notes=[
            {
                "kind": "observation",
                "area": "tool_dispatch",
                "path": "./src/baseline.rs",
                "line": 12,
            }
        ],
        catalog_writer_lead_paths=frozenset({"src/baseline.rs", "src/route.rs"}),
        coercions=coercions,
    )
    decision = _i7(assessment)
    assert decision.disposition is SourceReviewInvariantDisposition.PASS
    assert decision.pass_clause is not None
    assert coercions == []


def test_i7_breach_and_inconclusive_are_never_rewritten_by_the_inventory() -> None:
    decisions = _all_pass_decisions()
    decisions[6] = {
        "invariant": "i7_model_tool_planning",
        "disposition": "inconclusive",
        "pass_clause": None,
        "summary": "The bounded review could not finish the writer inventory.",
        "evidence_indices": [],
    }
    assessment = source_review_module._validated_invariant_assessment(
        decisions,
        submitted_evidence=[],
        finding_evidence=[],
        demoted_to_low=False,
        policy_version=14,
        notes=[],
        catalog_writer_lead_paths=frozenset({"src/baseline.rs"}),
    )
    decision = _i7(assessment)
    assert decision.disposition is SourceReviewInvariantDisposition.INCONCLUSIVE
    assert "catalog-writer" not in decision.summary


def test_i7_without_any_catalog_writer_lead_is_untouched() -> None:
    coercions: list[str] = []
    assessment = source_review_module._validated_invariant_assessment(
        _all_pass_decisions(),
        submitted_evidence=[],
        finding_evidence=[],
        demoted_to_low=False,
        policy_version=14,
        notes=[],
        catalog_writer_lead_paths=frozenset(),
        coercions=coercions,
    )
    assert _i7(assessment).disposition is SourceReviewInvariantDisposition.PASS
    assert coercions == []


@pytest.mark.parametrize(("policy_version", "schema_version"), [(12, 1), (13, 2)])
def test_frozen_policy_i7_pass_is_untouched_even_with_lead_paths(
    policy_version: int, schema_version: int
) -> None:
    coercions: list[str] = []
    assessment = source_review_module._validated_invariant_assessment(
        _all_pass_decisions(policy_version),
        submitted_evidence=[],
        finding_evidence=[],
        demoted_to_low=False,
        policy_version=policy_version,
        notes=[],
        catalog_writer_lead_paths=frozenset({"src/baseline.rs"}),
        coercions=coercions,
    )
    assert assessment.schema_version == schema_version
    assert _i7(assessment).disposition is SourceReviewInvariantDisposition.PASS
    assert coercions == []


def _benign_review(policy_version: int) -> dict[str, object]:
    return {
        "risk_level": "low",
        "confidence": 0.9,
        "categories": ["none"],
        "evidence": [],
        "invariants": _all_pass_decisions(policy_version),
        "summary": "General model-backed request path.",
    }


def test_parse_review_coerces_i7_from_the_live_inventory_and_records_it(
    tmp_path: Path,
) -> None:
    files = [
        ("src/baseline.rs", COMET_BASELINE_RS),
        ("src/single_tool_model.rs", COMET_SINGLE_TOOL_MODEL_RS),
    ]
    repository = TarSourceRepository(_archive(tmp_path, files), policy_version=14)
    assert repository.catalog_writer_lead_paths() >= {"src/baseline.rs"}
    notes: list[dict[str, object]] = []
    observation = source_review_module._parse_review(
        _benign_review(14),
        artifact_sha256="a" * 64,
        repository=repository,
        policy_version=14,
        notes=notes,
    )
    # A low-risk finding cannot carry an inconclusive invariant, so the
    # would-be clear becomes an inconclusive review outcome (operator hold /
    # deeper review) instead of a signed clear.
    assert observation.ok is False
    assert observation.finding is None and observation.finding_digest is None
    assert observation.failure_disposition == "inconclusive"
    assert observation.error_code == "source-review-i7-inventory-unfinished"
    # The coercion travels with the ledger as a host-authored observation.
    assert len(notes) == 1
    assert notes[0]["kind"] == "observation"
    assert notes[0]["area"] == "tool_dispatch"
    assert str(notes[0]["summary"]).startswith("host: Coerced from PASS")
    assert observation.notes == (notes[0],)
    assert "text" not in json.dumps(notes)


def test_parse_review_coerces_i7_inside_an_elevated_signed_finding(
    tmp_path: Path,
) -> None:
    files = [("src/baseline.rs", COMET_BASELINE_RS)]
    repository = TarSourceRepository(_archive(tmp_path, files), policy_version=14)
    decisions = _all_pass_decisions()
    decisions[4] = {
        "invariant": "i5_production_engine",
        "disposition": "breach",
        "pass_clause": None,
        "summary": "The reviewed path retains a reachable strict-policy breach.",
        "evidence_indices": [0, 1],
    }
    review: dict[str, object] = {
        "risk_level": "high",
        "confidence": 0.95,
        "categories": ["benchmark_emulation"],
        "evidence": [
            {
                "path": "src/baseline.rs",
                "line": _line_of(COMET_BASELINE_RS, "= if withhold_host_tools("),
                "category": "benchmark_emulation",
            },
            {
                "path": "src/baseline.rs",
                "line": _line_of(COMET_BASELINE_RS, "            Vec::new()"),
                "category": "benchmark_emulation",
            },
        ],
        "invariants": decisions,
        "summary": "Request-keyed host writes the catalog offered to the model.",
    }
    notes: list[dict[str, object]] = []
    observation = source_review_module._parse_review(
        review,
        artifact_sha256="a" * 64,
        repository=repository,
        policy_version=14,
        notes=notes,
    )
    assert observation.ok and observation.finding is not None
    assert observation.risk_level == "high"
    signed = observation.finding["invariant_assessment"]["decisions"]
    i7 = next(item for item in signed if item["invariant"] == "i7_model_tool_planning")
    assert i7["disposition"] == "inconclusive"
    assert "catalog-writer" in i7["summary"]
    assert len(notes) == 1 and str(notes[0]["summary"]).startswith("host: ")


def test_parse_review_keeps_i7_pass_when_the_ledger_inventoried_the_writer(
    tmp_path: Path,
) -> None:
    files = [("src/baseline.rs", COMET_BASELINE_RS)]
    repository = TarSourceRepository(_archive(tmp_path, files), policy_version=14)
    notes: list[dict[str, object]] = [
        {
            "kind": "observation",
            "category": "none",
            "area": "tool_dispatch",
            "path": "src/baseline.rs",
            "line": _line_of(COMET_BASELINE_RS, "= if withhold_host_tools("),
            "summary": "One catalog writer; reachable with a non-empty catalog.",
            "stage": "l1",
        }
    ]
    observation = source_review_module._parse_review(
        _benign_review(14),
        artifact_sha256="a" * 64,
        repository=repository,
        policy_version=14,
        notes=notes,
    )
    assert observation.finding is not None
    decisions = observation.finding["invariant_assessment"]["decisions"]
    i7 = next(
        item for item in decisions if item["invariant"] == "i7_model_tool_planning"
    )
    assert i7["disposition"] == "pass"
    assert len(notes) == 1


@pytest.mark.parametrize(("policy_version", "decision_count"), [(12, 7), (13, 8)])
def test_parse_review_at_a_frozen_policy_never_coerces(
    tmp_path: Path, policy_version: int, decision_count: int
) -> None:
    files = [("src/baseline.rs", COMET_BASELINE_RS)]
    repository = TarSourceRepository(
        _archive(tmp_path, files), policy_version=policy_version
    )
    assert repository.catalog_writer_lead_paths() == frozenset()
    notes: list[dict[str, object]] = []
    observation = source_review_module._parse_review(
        _benign_review(policy_version),
        artifact_sha256="a" * 64,
        repository=repository,
        policy_version=policy_version,
        notes=notes,
    )
    assert observation.finding is not None
    decisions = observation.finding["invariant_assessment"]["decisions"]
    assert len(decisions) == decision_count
    assert all(item["disposition"] == "pass" for item in decisions)
    assert notes == []


# ── literal extraction: lifetimes, char literals, and Rust test modules ──────


def test_rust_lifetimes_do_not_swallow_the_literal_that_follows() -> None:
    source = (
        "fn f<'a>(s: &'a str) -> &'a str { \"generator sentence here\" }\n"
        'const X: &\'static str = "second generator sentence";\n'
        "let c = 'x'; let n = '\\n'; let u = '\\u{1F600}'; let q = '\\'';\n"
        "r1, r2, r3 := '\\u00e9', '\\U0001F600', '\\101'\n"
        'let after = "third one survives the char literals";\n'
    )
    assert generator_ngrams.go_string_literals(source) == [
        "generator sentence here",
        "second generator sentence",
        "third one survives the char literals",
    ]


def test_public_subtraction_file_keeps_every_long_literal() -> None:
    datagen = _STARTER_KIT / "src" / "datagen.rs"
    if not datagen.is_file():
        pytest.skip("starter kit not checked out beside the screener")
    source = datagen.read_text("utf-8")
    extracted = generator_ngrams.go_string_literals(source)
    naive = [
        item.replace('\\"', '"')
        for item in re.findall(r'"((?:[^"\\\n]|\\.)*)"', source)
    ]
    long_naive = {item for item in naive if len(item.split()) >= 5}
    assert long_naive <= set(extracted)


def test_served_rust_line_with_lifetimes_still_yields_an_admissible_lead() -> None:
    served = (
        "fn ack<'a>(request: &'a str) -> &'a str {\n"
        f'    let ack: &\'static str = "{_GENERATOR_SENTENCE}";\n'
        "    if request == ack { return ack; }\n"
        "    request\n"
        "}\n"
    )
    leads = find_fixture_generator_ngram_leads([("src/intent.rs", served)])
    assert len(leads) == 1
    assert leads[0]["admissible"] is True
    cited = {int(location["line"]) for location in leads[0]["locations"]}
    assert _line_of(served, "let ack") in cited


def test_cfg_test_use_at_the_top_does_not_demote_the_served_path() -> None:
    source = (
        "#[cfg(test)]\n"
        "use pretty_assertions::assert_eq;\n"
        "#[cfg(test)]\n"
        "mod fixtures;\n"
        "\n"
        "pub fn classify(request: &str) -> Route {\n"
        f'    let ack = "{_GENERATOR_SENTENCE}";\n'
        "    if request == ack { return Route::Ack; }\n"
        "    Route::Model\n"
        "}\n"
    )
    leads = find_fixture_generator_ngram_leads([("src/intent.rs", source)])
    assert len(leads) == 1
    assert leads[0]["admissible"] is True
    assert all(location["admissible"] for location in leads[0]["locations"])


def test_cfg_test_module_is_bounded_by_its_braces() -> None:
    source = (
        "pub fn served() -> &'static str {\n"
        '    "nothing generator-shaped here"\n'
        "}\n"
        "\n"
        '#[cfg(all(test, feature = "fixtures"))]\n'
        "#[allow(dead_code)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "    #[test]\n"
        "    fn ack() {\n"
        f'        assert_eq!(route("{_GENERATOR_SENTENCE}"), Route::Ack);\n'
        "    }\n"
        "}\n"
        "\n"
        "pub fn after_the_tests(request: &str) -> Route {\n"
        f'    if request == "{_GENERATOR_SENTENCE}" {{ return Route::Ack; }}\n'
        "    Route::Model\n"
        "}\n"
    )
    leads = find_fixture_generator_ngram_leads([("src/intent.rs", source)])
    assert len(leads) == 1
    by_line = {
        int(location["line"]): bool(location["admissible"])
        for location in leads[0]["locations"]
    }
    assert by_line[_line_of(source, "assert_eq!(route(")] is False
    assert by_line[_line_of(source, "if request ==")] is True
    assert leads[0]["admissible"] is True
