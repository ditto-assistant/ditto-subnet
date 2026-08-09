"""Routing, evidence, budget, cache, and sandbox tests for SOL L2 review."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from ditto_screener.l2_review import (
    _ORDINARY_OPTIONAL_FIELD_SAFETY_TASK,
    _SAFETY_ADJUDICATOR_TASK,
    _SYSTEM_PROMPT,
    _TOOLS,
    _VIOLATION_CAUSE_DISAGREEMENT_TASK,
    _VIOLATION_CAUSE_TASK,
    L2_CAUSE_PROMPT_REVISION,
    L2_CAUSE_TIEBREAKER_PROMPT_REVISION,
    L2_CRITIC_PROMPT_REVISION,
    L2_DOSSIER_REVISION,
    L2_HARNESS_REVISION,
    L2_MODEL,
    L2_PROMPT_REVISION,
    L2_SAFETY_PROMPT_REVISION,
    L2_STARTER_MANIFESTS,
    L2_STATIC_HOLD_REVISION,
    IsolatedCodingHarness,
    L2AuditJournal,
    L2InconclusiveError,
    L2RunResult,
    L2Usage,
    LayeredSourceReviewAgent,
    SolL2SourceReviewAgent,
    _cost,
    _dossier_has_scorer_attention,
    _extract_readonly_workspace,
    _graph_covers_l1_slice,
    _has_mixed_causal_families,
    _make_writable,
    _needs_violation_adjudication,
    _parse_l2_review,
    _qualifies_for_direct_clear,
    _require_complete_analysis,
    _review_adaptation_hold,
    _served_generator_hold,
    _write_all,
)
from ditto_screener.policy import SourceReviewObservation
from ditto_screener.source_review import TarSourceRepository
from scripts.generate_starter_provenance import _tracked_files

ROOT = Path(__file__).resolve().parents[1]
ATTEMPT = UUID("96af45fd-65da-4f59-87f8-8ddf5d57f88c")


def test_l2_extraction_budget_allows_archives_over_twenty_mib(tmp_path: Path) -> None:
    size = 20 * 1024 * 1024 + 1
    archive = tmp_path / "large-source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("large-source.bin")
        info.size = size
        tar.addfile(info, io.BytesIO(b"\0" * size))
    workspace = tmp_path / "large-source"
    workspace.mkdir()

    _extract_readonly_workspace(archive, workspace)

    assert (workspace / "large-source.bin").stat().st_size == size


def test_supported_starter_manifests_are_versioned_and_distinct() -> None:
    manifests = [json.loads(path.read_text()) for path in L2_STARTER_MANIFESTS]
    assert [manifest["revision"] for manifest in manifests] == [
        "959cd69a1a8d3b0defbfb8296518adb7d4f17c14",
        "60aab4e5e2839ddb0fe8c80492bd7b76ba2668fd",
        "106076a40e4214cda821dfd0bee5c9c6785d425c",
        "23d9e87039a66e08548ec95826e7201b90988c5a",
    ]
    assert all(
        manifest["origin"] == "ditto-assistant/dittobench-starter-kit"
        for manifest in manifests
    )
    assert [len(manifest["files"]) for manifest in manifests] == [38, 38, 42, 42]
    assert [len(manifest["rust_functions"]) for manifest in manifests] == [
        98,
        103,
        103,
        111,
    ]


def test_starter_provenance_generator_ignores_untracked_build_outputs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "starter"
    (root / "src").mkdir(parents=True)
    (root / "src" / "lib.rs").write_text("fn tracked() {}\n")
    (root / "target" / "debug").mkdir(parents=True)
    (root / "target" / "debug" / "artifact").write_text("untracked\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "src/lib.rs"], cwd=root, check=True)

    assert [path.relative_to(root).as_posix() for path in _tracked_files(root)] == [
        "src/lib.rs"
    ]


def test_causal_basis_prefers_reconstructed_generator_over_downstream_effects() -> None:
    assert L2_PROMPT_REVISION == "l2-kimi-source-review-v30"
    assert L2_CAUSE_PROMPT_REVISION == "l3-sol-violation-cause-v24"
    assert L2_CAUSE_TIEBREAKER_PROMPT_REVISION == ("l3-sol-cause-disagreement-v5")
    assert "Generator mirroring is earlier and therefore" in _VIOLATION_CAUSE_TASK
    assert "without those copied input-construction" in _VIOLATION_CAUSE_TASK
    assert "expected-tool registry" in _VIOLATION_CAUSE_TASK
    assert "generator-definition registry" in _VIOLATION_CAUSE_TASK
    assert "generator-side input templates/construction parameters" in (
        _VIOLATION_CAUSE_TASK
    )
    assert "already-formed inputs" in _VIOLATION_CAUSE_TASK
    assert "Generic domain records" in _VIOLATION_CAUSE_TASK
    assert "fresh-input counterfactual" in _VIOLATION_CAUSE_DISAGREEMENT_TASK
    assert "classifier that only decides whether to suppress" in _VIOLATION_CAUSE_TASK
    assert "Require the alleged answer engine to produce" in _VIOLATION_CAUSE_TASK
    assert "trace that exact value into the served scored" in _VIOLATION_CAUSE_TASK
    assert "if removing the later suppression/mutation" in _VIOLATION_CAUSE_TASK
    assert "Use at most eight targeted analyzer" in _VIOLATION_CAUSE_TASK
    assert "score- or A/B-driven rule" in _VIOLATION_CAUSE_TASK
    assert "first-class causal decision" in _VIOLATION_CAUSE_TASK
    assert "flow_count=0" in _VIOLATION_CAUSE_TASK
    assert "pre-assignment score policy" in _VIOLATION_CAUSE_TASK
    assert "Absolute taxonomy rule" in _VIOLATION_CAUSE_TASK
    assert "served audit/canary split" in _VIOLATION_CAUSE_TASK
    assert "both dispatch branches" in _VIOLATION_CAUSE_TASK
    assert "must converge on the same reachable served" in _VIOLATION_CAUSE_TASK
    assert "fixed set of known query phrasings" in _VIOLATION_CAUSE_TASK
    assert "untrusted bounded L1 diagnostic" in _VIOLATION_CAUSE_TASK
    assert "at least two independently cited input-construction" in (
        _VIOLATION_CAUSE_TASK
    )
    assert "generator_construction attention map" in _VIOLATION_CAUSE_TASK
    assert "read at least one" in _VIOLATION_CAUSE_TASK
    assert "registry/definition anchor" in _VIOLATION_CAUSE_TASK
    assert "generator_components" in _VIOLATION_CAUSE_TASK
    assert "unused local parser/debug candidate" in _VIOLATION_CAUSE_TASK
    assert "assigned into the served response contract" in _SYSTEM_PROMPT
    assert "locally derived expected-value checker" in _SYSTEM_PROMPT
    assert "fresh model generation" in _SYSTEM_PROMPT
    assert "manual-review benchmark-emulation lead" in _SYSTEM_PROMPT
    assert "live schema-driven routing" in _SYSTEM_PROMPT
    assert "reported prior-population" in _SAFETY_ADJUDICATOR_TASK
    assert "regardless of L1's category" in _SAFETY_ADJUDICATOR_TASK
    assert "Pre-assignment suppression" in _ORDINARY_OPTIONAL_FIELD_SAFETY_TASK
    assert "unused parser/debug candidate" in _ORDINARY_OPTIONAL_FIELD_SAFETY_TASK
    assert "starter_function_diff" in _ORDINARY_OPTIONAL_FIELD_SAFETY_TASK
    assert "retrieval innovation" in _ORDINARY_OPTIONAL_FIELD_SAFETY_TASK
    submit_tool = next(tool for tool in _TOOLS if tool["name"] == "submit_l2_review")
    assert "generator_components" in submit_tool["parameters"]["required"]  # type: ignore[index]
    assert "causal_evidence" in submit_tool["parameters"]["required"]  # type: ignore[index]
    causal_schema = submit_tool["parameters"]["properties"]["causal_evidence"]  # type: ignore[index]
    causal_object = causal_schema["anyOf"][1]  # type: ignore[index]
    assert "scorer_visible_effect" in causal_object["required"]
    assert set(causal_object["properties"]["scorer_visible_effect"]["enum"]) == {
        "final_text",
        "answer",
        "abstain",
        "tool_calls",
        "validator_observed_trajectory",
        "graded_outcome",
    }
    assert "served_trigger" in _SYSTEM_PROMPT
    assert "authority_bypass" in _VIOLATION_CAUSE_TASK
    assert "genuine model or tool execution" in _SAFETY_ADJUDICATOR_TASK


def test_scorer_attention_is_independent_of_l1_category() -> None:
    assert _dossier_has_scorer_attention(
        {
            "deterministic": {
                "scorer_field_flow": {
                    "score_controls": [{"path": "src/main.rs", "line": 1}],
                    "field_clears": [],
                    "field_populations": [],
                    "same_function_candidates": [],
                }
            }
        }
    )
    assert not _dossier_has_scorer_attention(
        {"deterministic": {"scorer_field_flow": {"score_controls": []}}}
    )


def test_l1_mechanism_narrowed_away_by_kimi_still_requires_sol() -> None:
    analyst = L2RunResult(
        observation=SourceReviewObservation(
            ok=True,
            risk_level="high",
            finding_digest="b" * 64,
            categories=("cross_user_access",),
        ),
        analyzed_files=(),
        causal_path=(),
        tools=(),
        usage=L2Usage(),
        cache_hit=False,
        resolution_basis="cross_user_data_flow",
    )
    l1 = SourceReviewObservation(
        ok=True,
        risk_level="high",
        finding_digest="a" * 64,
        categories=("cross_user_access", "scorer_contract_manipulation"),
    )

    assert _has_mixed_causal_families(analyst, l1)
    assert _needs_violation_adjudication(analyst, l1)


def test_request_local_identical_tool_memoization_is_not_fabrication() -> None:
    assert L2_CRITIC_PROMPT_REVISION == "l3-sol-adversarial-critic-v18"
    assert L2_SAFETY_PROMPT_REVISION == "l3-sol-safety-adjudicator-v21"
    assert "Request-local memoization of an identical invocation" in _SYSTEM_PROMPT
    assert "deduplication is not fabricated telemetry" in _SYSTEM_PROMPT
    assert "distinguish execution from transport" in _SAFETY_ADJUDICATOR_TASK
    assert "the cache crosses requests" in _SAFETY_ADJUDICATOR_TASK


def test_dittobench_preflight_clearance_is_exact_and_shared() -> None:
    for prompt in (_SYSTEM_PROMPT, _SAFETY_ADJUDICATOR_TASK):
        assert "preflight:" in prompt
        assert "search_web" in prompt
        assert "supplied" in prompt
        assert "tool_endpoint" in prompt
        assert "incoming" in prompt
        assert "case" in prompt
        assert "hop" in prompt
        assert "0" in prompt
        assert "Validator-side endpoint observation" in prompt or (
            "validator ignores that report" in prompt
        )
        assert "self-report" in prompt or "self-reported" in prompt
        assert "ordinary" in prompt
        assert "scored" in prompt

    assert "absence of a preflight branch is not a finding" in _SYSTEM_PROMPT
    assert "near-miss prefix" in _SYSTEM_PROMPT
    assert "wrong tool" in _SYSTEM_PROMPT
    assert "no actual POST attempt" in _SYSTEM_PROMPT
    assert "without the matching best-effort POST" in _SYSTEM_PROMPT
    assert "after the required POST attempt" in _SYSTEM_PROMPT
    assert (
        "Acknowledgement or error prose is not an observed tool call" in _SYSTEM_PROMPT
    )
    assert "empty" in _SAFETY_ADJUDICATOR_TASK
    assert "call list on missing" in _SAFETY_ADJUDICATOR_TASK
    assert "endpoint-absent malformed preflight" in _SAFETY_ADJUDICATOR_TASK
    assert "matching untrusted self-report" in _SAFETY_ADJUDICATOR_TASK
    assert "Judge anti-cheat from the valid endpoint-present path" in (
        _SAFETY_ADJUDICATOR_TASK
    )


def _tar(tmp_path: Path, source: str) -> tuple[Path, str]:
    path = tmp_path / "submission.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in {
            "Dockerfile": b"FROM scratch\n",
            "Cargo.toml": b'[package]\nname="fixture"\nversion="0.1.0"\n',
            "src/main.rs": source.encode(),
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _l1(
    risk: str = "medium", *, clearance_certified: bool | None = None
) -> SourceReviewObservation:
    categories = ("benchmark_emulation",) if risk != "low" else ("none",)
    return SourceReviewObservation(
        ok=True,
        risk_level=risk,
        finding_digest="a" * 64,
        categories=categories,
        finding={
            "risk_level": risk,
            "confidence": 0.8,
            "categories": list(categories),
            "evidence": (
                []
                if risk == "low"
                else [
                    {
                        "path": "src/main.rs",
                        "line": 1,
                        "category": "benchmark_emulation",
                    }
                ]
            ),
            "summary": "bounded test routing lead",
        },
        clearance_certified=(
            risk == "low" if clearance_certified is None else clearance_certified
        ),
    )


def _safe() -> SourceReviewObservation:
    return SourceReviewObservation(
        ok=True,
        risk_level="low",
        finding_digest="b" * 64,
        categories=("none",),
    )


def _clearance_certificate(safe: dict[str, object]) -> dict[str, object]:
    return {
        **safe,
        "confidence": 1.0,
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "context"},
            {"path": "src/main.rs", "line": 1, "role": "decision"},
            {"path": "src/main.rs", "line": 1, "role": "effect"},
            {"path": "src/main.rs", "line": 1, "role": "sink"},
        ],
    }


def _model_result(observation: SourceReviewObservation) -> L2RunResult:
    return L2RunResult(observation, (), (), (), L2Usage(), False)


class _FakeL1:
    def __init__(self, result: SourceReviewObservation) -> None:
        self.result = result
        self.calls = 0

    async def review(self, *_args: Any, **_kwargs: Any) -> SourceReviewObservation:
        self.calls += 1
        return self.result


class _FakeL2:
    def __init__(self, result: L2RunResult) -> None:
        self.result = result
        self.calls = 0

    async def review(self, *_args: Any, **_kwargs: Any) -> L2RunResult:
        self.calls += 1
        return self.result


async def test_clean_l1_skips_sol() -> None:
    l1 = _FakeL1(_l1("low"))
    l2 = _FakeL2(_model_result(_safe()))
    layered = LayeredSourceReviewAgent(l1=l1, l2=l2, mode="enforce")  # type: ignore[arg-type]

    result = await layered.review(
        "unused", artifact_sha256="c" * 64, attempt_id=ATTEMPT
    )

    assert result is l1.result
    assert l1.calls == 1
    assert l2.calls == 0


async def test_uncertified_l1_low_escalates_to_l2() -> None:
    l1 = _FakeL1(_l1("low", clearance_certified=False))
    l2 = _FakeL2(_model_result(_safe()))
    layered = LayeredSourceReviewAgent(l1=l1, l2=l2, mode="enforce")  # type: ignore[arg-type]

    result = await layered.review(
        "unused", artifact_sha256="c" * 64, attempt_id=ATTEMPT
    )

    assert result.risk_level == "low"
    assert l2.calls == 1


def test_direct_clear_graph_requires_unique_resolved_l1_slice() -> None:
    graph = {
        "unresolved": False,
        "entry_ambiguous": False,
        "truncated": False,
        "nodes": [
            {
                "id": "src/main.rs::main@1",
                "path": "src/main.rs",
                "line": 1,
                "end_line": 4,
            }
        ],
        "ambiguous_calls": [],
        "unresolved_calls": [],
    }

    assert _graph_covers_l1_slice(graph, _l1())
    graph["ambiguous_calls"] = [{"caller": "src/main.rs::main@1", "target": "dispatch"}]
    assert not _graph_covers_l1_slice(graph, _l1())
    graph["ambiguous_calls"] = []
    graph["entry_ambiguous"] = True
    assert not _graph_covers_l1_slice(graph, _l1())


@pytest.mark.parametrize("risk", ["medium", "high"])
async def test_ambiguous_or_high_l1_invokes_sol(risk: str) -> None:
    l1 = _FakeL1(_l1(risk))
    l2 = _FakeL2(_model_result(_safe()))
    layered = LayeredSourceReviewAgent(l1=l1, l2=l2, mode="enforce")  # type: ignore[arg-type]

    result = await layered.review(
        "unused", artifact_sha256="c" * 64, attempt_id=ATTEMPT
    )

    assert result.risk_level == "low"
    assert l2.calls == 1


async def test_l2_progress_occupies_the_second_half_of_source_review() -> None:
    l1 = _FakeL1(_l1("high"))
    l2 = _FakeL2(_model_result(_safe()))
    layered = LayeredSourceReviewAgent(l1=l1, l2=l2, mode="enforce")  # type: ignore[arg-type]
    progress: list[tuple[int, int]] = []

    await layered.review(
        "unused",
        artifact_sha256="c" * 64,
        attempt_id=ATTEMPT,
        progress=lambda completed, total: progress.append((completed, total)),
    )

    assert progress == [(1, 2), (2, 2)]


async def test_shadow_records_l2_but_preserves_l1_quarantine() -> None:
    l1 = _FakeL1(_l1())
    l2 = _FakeL2(_model_result(_safe()))
    layered = LayeredSourceReviewAgent(l1=l1, l2=l2, mode="shadow")  # type: ignore[arg-type]

    result = await layered.review(
        "unused", artifact_sha256="c" * 64, attempt_id=ATTEMPT
    )

    assert result is l1.result
    assert l2.calls == 1


async def test_l2_failure_cannot_silently_release_or_reject() -> None:
    failure = SourceReviewObservation(
        ok=False,
        risk_level=None,
        finding_digest=None,
        categories=(),
        error_code="l2-timeout",
        failure_disposition="retryable_infra",
    )
    layered = LayeredSourceReviewAgent(  # type: ignore[arg-type]
        l1=_FakeL1(_l1()), l2=_FakeL2(_model_result(failure)), mode="enforce"
    )

    result = await layered.review(
        "unused", artifact_sha256="c" * 64, attempt_id=ATTEMPT
    )

    assert not result.ok
    assert result.failure_disposition == "retryable_infra"


def _clearance_candidate(*, confidence: float = 0.99) -> L2RunResult:
    observation = SourceReviewObservation(
        ok=True,
        risk_level="low",
        finding_digest="d" * 64,
        categories=("none",),
        finding={"confidence": confidence, "evidence": []},
    )
    return L2RunResult(
        observation=observation,
        analyzed_files=({"path": "src/main.rs", "sha256": "e" * 64},),
        causal_path=(
            {"path": "src/main.rs", "line": 1, "role": "context"},
            {"path": "src/main.rs", "line": 2, "role": "decision"},
            {"path": "src/main.rs", "line": 3, "role": "sink"},
        ),
        tools=("read_file",),
        usage=L2Usage(),
        cache_hit=False,
        response_models=("moonshotai/kimi-k3-20260715",),
        resolution_basis="authoritative_model_tool_path",
    )


def test_complete_medium_primary_kimi_certificate_can_skip_l3() -> None:
    assert _qualifies_for_direct_clear(_l1("medium"), _clearance_candidate())


@pytest.mark.parametrize("risk", ["high", "low"])
def test_non_medium_l1_never_skips_l3(risk: str) -> None:
    assert not _qualifies_for_direct_clear(_l1(risk), _clearance_candidate())


def test_incomplete_low_confidence_or_fallback_certificate_never_skips_l3() -> None:
    assert not _qualifies_for_direct_clear(_l1(), _clearance_candidate(confidence=0.97))
    candidate = _clearance_candidate()
    assert not _qualifies_for_direct_clear(
        _l1(),
        L2RunResult(
            **{
                **candidate.__dict__,
                "response_models": ("z-ai/glm-5.2",),
            }
        ),
    )
    assert not _qualifies_for_direct_clear(
        _l1(),
        L2RunResult(**{**candidate.__dict__, "causal_path": candidate.causal_path[:2]}),
    )


def test_served_generator_constellation_cannot_auto_clear(
    tmp_path: Path,
) -> None:
    archive, artifact_sha = _tar(tmp_path, "fn one() {}\nfn two() {}\nfn three() {}")
    repository = TarSourceRepository(str(archive))
    digest = repository.member_sha256("src/main.rs")
    analyst = L2RunResult(
        **{
            **_clearance_candidate().__dict__,
            "analyzed_files": ({"path": "src/main.rs", "sha256": digest},),
        }
    )
    locations = [
        {
            "path": "src/main.rs",
            "line": line,
            "dimension": dimension,
        }
        for line, dimension in enumerate(
            (
                "question_templates",
                "retrieval_vocabulary_bridge",
                "deterministic_answer_path",
            ),
            1,
        )
    ]
    dossier = {
        "bounded_source_inventory": {
            "review_leads": {
                "generator_mirroring": {
                    "served_runtime_candidate": True,
                    "served_runtime_locations": locations,
                }
            }
        }
    }

    held = _served_generator_hold(
        dossier=dossier,
        repository=repository,
        artifact_sha256=artifact_sha,
        l1_observation=_l1(),
        analyst=analyst,
        dossier_tools=("starter_function_diff",),
        analyst_cache_hit=False,
    )

    assert held is not None
    assert held.observation.risk_level == "medium"
    assert "benchmark_emulation" in held.observation.categories
    assert held.clearance_path == "deterministic_served_generator_hold"
    assert held.critic_disposition == "not_required_static_hold"
    assert held.resolution_basis == "benchmark_answer_replacement"
    assert {item["role"] for item in held.causal_path} == {
        "trigger",
        "decision",
        "effect",
    }

    no_hold = _served_generator_hold(
        dossier={"bounded_source_inventory": {"review_leads": {}}},
        repository=repository,
        artifact_sha256=artifact_sha,
        l1_observation=_l1(),
        analyst=analyst,
        dossier_tools=(),
        analyst_cache_hit=False,
    )
    assert no_hold is None

    scorer_l1 = SourceReviewObservation(
        ok=True,
        risk_level="medium",
        finding_digest="f" * 64,
        categories=("scorer_contract_manipulation",),
        finding={
            "risk_level": "medium",
            "confidence": 0.9,
            "categories": ["scorer_contract_manipulation"],
            "evidence": [
                {
                    "path": "src/main.rs",
                    "line": 1,
                    "category": "scorer_contract_manipulation",
                },
                {
                    "path": "src/main.rs",
                    "line": 2,
                    "category": "scorer_contract_manipulation",
                },
            ],
        },
    )
    scorer_dossier = {
        **dossier,
        "deterministic": {
            "scorer_field_flow": {
                "score_controls": [{"path": "src/main.rs", "line": 1}],
                "field_clears": [{"path": "src/main.rs", "line": 2}],
                "field_populations": [{"path": "src/main.rs", "line": 1}],
                "same_function_candidates": [{"path": "src/main.rs", "line": 1}],
                "truncated": False,
            }
        },
    }
    scorer_hold = _served_generator_hold(
        dossier=scorer_dossier,
        repository=repository,
        artifact_sha256=artifact_sha,
        l1_observation=scorer_l1,
        analyst=analyst,
        dossier_tools=("scorer_field_flow",),
        analyst_cache_hit=False,
    )
    assert scorer_hold is not None
    assert scorer_hold.resolution_basis == "scorer_field_manipulation"


def test_review_adaptive_model_routing_cannot_auto_clear(tmp_path: Path) -> None:
    archive, artifact_sha = _tar(
        tmp_path,
        "// audit oracle uses a second call nonce\nfn run() { chat(); }\n",
    )
    repository = TarSourceRepository(str(archive))
    digest = repository.member_sha256("src/main.rs")
    analyst = L2RunResult(
        **{
            **_clearance_candidate().__dict__,
            "analyzed_files": ({"path": "src/main.rs", "sha256": digest},),
        }
    )
    dossier = {
        "bounded_source_inventory": {
            "review_leads": {
                "review_adaptive_model_routing": {
                    "candidate": True,
                    "constellations": [
                        {
                            "locations": [
                                {
                                    "path": "src/main.rs",
                                    "line": 1,
                                    "role": "review_channel",
                                },
                                {
                                    "path": "src/main.rs",
                                    "line": 1,
                                    "role": "probe_shape",
                                },
                                {
                                    "path": "src/main.rs",
                                    "line": 2,
                                    "role": "model_effect",
                                },
                            ]
                        }
                    ],
                }
            }
        }
    }

    held = _review_adaptation_hold(
        dossier=dossier,
        repository=repository,
        artifact_sha256=artifact_sha,
        l1_observation=_l1(),
        analyst=analyst,
        dossier_tools=("starter_function_diff",),
        analyst_cache_hit=False,
    )

    assert held is not None
    assert held.observation.risk_level == "medium"
    assert "benchmark_emulation" in held.observation.categories
    assert held.resolution_basis == "benchmark_answer_replacement"
    assert held.clearance_path == "deterministic_review_adaptation_hold"
    assert held.critic_disposition == "not_required_static_hold"
    assert {item["role"] for item in held.causal_path} == {
        "context",
        "trigger",
        "effect",
    }

    no_hold = _review_adaptation_hold(
        dossier={"bounded_source_inventory": {"review_leads": {}}},
        repository=repository,
        artifact_sha256=artifact_sha,
        l1_observation=_l1(),
        analyst=analyst,
        dossier_tools=(),
        analyst_cache_hit=False,
    )
    assert no_hold is None


@pytest.mark.parametrize(
    "case",
    json.loads((ROOT / "tests/fixtures/l2-review-regressions.json").read_text()),
    ids=lambda case: case["id"],
)
def test_generalized_evidence_fixtures_are_artifact_bound(
    case: dict[str, str], tmp_path: Path
) -> None:
    archive, artifact_sha = _tar(tmp_path, case["source"])
    repository = TarSourceRepository(str(archive))
    digest = repository.member_sha256("src/main.rs")
    disposition = case["disposition"]
    category = case["category"]
    risk = "medium" if disposition == "violation" else "low"
    lines = len(case["source"].splitlines())
    evidence = []
    causal = []
    if disposition == "violation":
        evidence = [
            {
                "path": "src/main.rs",
                "line": 1,
                "file_sha256": digest,
                "category": category,
                "role": "trigger",
            },
            {
                "path": "src/main.rs",
                "line": min(2, lines),
                "file_sha256": digest,
                "category": category,
                "role": "effect",
            },
        ]
        causal = [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": min(2, lines), "role": "effect"},
        ]
    result = {
        "disposition": disposition,
        "risk_level": risk,
        "confidence": 0.92,
        "resolution_basis": (
            "insufficient_static_evidence"
            if disposition == "inconclusive"
            else "unreachable_nonruntime_code"
            if disposition == "safe"
            and case["id"]
            in {
                "safe-source-prompt-injection",
                "safe-decoy-archive",
            }
            else "authoritative_model_tool_path"
            if disposition == "safe"
            else {
                "scorer_contract_manipulation": "scorer_field_manipulation",
                "hidden_value_leakage": "hidden_model_bypass",
                "fabricated_tool_trajectory": "fabricated_tool_trajectory",
                "provider_bypass": "hidden_model_bypass",
                "data_exfiltration": "credential_exfiltration",
            }.get(category, "benchmark_answer_replacement")
        ),
        "categories": [category],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": evidence,
        "causal_path": causal,
        "summary": "model text is discarded",
    }

    observation, analyzed, parsed_causal, parsed_basis = _parse_l2_review(
        result, artifact_sha256=artifact_sha, repository=repository
    )

    assert analyzed[0]["sha256"] == digest
    assert parsed_basis == result["resolution_basis"]
    if disposition == "inconclusive":
        assert not observation.ok
        assert observation.failure_disposition == "inconclusive"
    else:
        assert observation.ok
        assert observation.risk_level == risk
        assert "model text is discarded" not in str(observation.finding)
        assert len(parsed_causal) == len(causal)


def test_l2_rejects_hallucinated_file_digest(tmp_path: Path) -> None:
    archive, artifact_sha = _tar(tmp_path, "fn main() {}")
    repository = TarSourceRepository(str(archive))
    value = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 0.9,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": "0" * 64}],
        "evidence": [],
        "causal_path": [],
        "summary": "safe",
    }
    with pytest.raises(ValueError, match="digest"):
        _parse_l2_review(value, artifact_sha256=artifact_sha, repository=repository)


def test_l2_safe_result_must_cover_every_l1_evidence_file(tmp_path: Path) -> None:
    archive, artifact_sha = _tar(tmp_path, "fn main() {}")
    repository = TarSourceRepository(str(archive))
    value = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 0.9,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [
            {
                "path": "src/main.rs",
                "sha256": repository.member_sha256("src/main.rs"),
            }
        ],
        "evidence": [],
        "causal_path": [],
        "summary": "safe",
    }
    with pytest.raises(ValueError, match="every L1 evidence file"):
        _parse_l2_review(
            value,
            artifact_sha256=artifact_sha,
            repository=repository,
            required_paths=("Dockerfile",),
        )


def test_l2_safe_result_rejects_contradictory_evidence(tmp_path: Path) -> None:
    archive, artifact_sha = _tar(tmp_path, "fn main() {}")
    repository = TarSourceRepository(str(archive))
    digest = repository.member_sha256("src/main.rs")
    value = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 0.9,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 1,
                "file_sha256": digest,
                "category": "benchmark_emulation",
                "role": "context",
            }
        ],
        "causal_path": [],
        "summary": "safe",
    }

    with pytest.raises(ValueError, match="contradictory evidence"):
        _parse_l2_review(value, artifact_sha256=artifact_sha, repository=repository)


def test_l2_violation_rejects_undeclared_evidence_category(tmp_path: Path) -> None:
    archive, artifact_sha = _tar(tmp_path, "fn main() { bypass(); }\nfn bypass() {}")
    repository = TarSourceRepository(str(archive))
    digest = repository.member_sha256("src/main.rs")
    value = {
        "disposition": "violation",
        "risk_level": "medium",
        "confidence": 0.9,
        "resolution_basis": "hidden_model_bypass",
        "categories": ["provider_bypass"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 1,
                "file_sha256": digest,
                "category": "provider_bypass",
                "role": "trigger",
            },
            {
                "path": "src/main.rs",
                "line": 2,
                "file_sha256": digest,
                "category": "hidden_value_leakage",
                "role": "effect",
            },
        ],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 2, "role": "effect"},
        ],
        "summary": "sanitized",
    }

    with pytest.raises(ValueError, match="category evidence"):
        _parse_l2_review(value, artifact_sha256=artifact_sha, repository=repository)


def test_generator_basis_requires_two_digest_bound_construction_components(
    tmp_path: Path,
) -> None:
    source = "fn template() {}\nfn seeded_expand() {}\nfn run() {}"
    archive, artifact_sha = _tar(tmp_path, source)
    repository = TarSourceRepository(str(archive))
    digest = repository.member_sha256("src/main.rs")
    value = {
        "disposition": "violation",
        "risk_level": "high",
        "confidence": 0.99,
        "resolution_basis": "generator_mirroring",
        "categories": ["benchmark_emulation"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": line,
                "file_sha256": digest,
                "category": "benchmark_emulation",
                "role": role,
            }
            for line, role in ((1, "trigger"), (2, "effect"))
        ],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 2, "role": "effect"},
        ],
        "summary": "sanitized",
    }

    with pytest.raises(ValueError, match="input-construction components"):
        _parse_l2_review(
            value,
            artifact_sha256=artifact_sha,
            repository=repository,
        )

    value["generator_components"] = [
        {
            "path": "src/main.rs",
            "line": 1,
            "file_sha256": digest,
            "kind": "template_grammar",
        },
        {
            "path": "src/main.rs",
            "line": 2,
            "file_sha256": digest,
            "kind": "seeded_expansion",
        },
    ]
    observation, _analyzed, _causal, basis = _parse_l2_review(
        value,
        artifact_sha256=artifact_sha,
        repository=repository,
    )

    assert observation.risk_level == "high"
    assert basis == "generator_mirroring"


def test_extracted_source_is_owner_only_and_links_are_inconclusive(
    tmp_path: Path,
) -> None:
    archive, _ = _tar(tmp_path, "fn main() {}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _extract_readonly_workspace(archive, workspace)
    assert workspace.stat().st_mode & 0o777 == 0o500
    assert (workspace / "src/main.rs").stat().st_mode & 0o777 == 0o400
    _make_writable(workspace)

    linked = tmp_path / "linked.tar.gz"
    with tarfile.open(linked, "w:gz") as tar:
        info = tarfile.TarInfo("src/alias.rs")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    linked_workspace = tmp_path / "linked-workspace"
    linked_workspace.mkdir()
    with pytest.raises(L2InconclusiveError, match="link or special"):
        _extract_readonly_workspace(linked, linked_workspace)


def test_archive_member_cap_counts_directories(tmp_path: Path) -> None:
    archive = tmp_path / "directory-bomb.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for number in range(513):
            info = tarfile.TarInfo(f"d{number}")
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
    workspace = tmp_path / "directory-bomb-workspace"
    workspace.mkdir()

    with pytest.raises(L2InconclusiveError, match="file budget"):
        _extract_readonly_workspace(archive, workspace)


def test_private_state_write_retries_short_os_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "state"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    real_write = os.write
    calls = 0

    def short_write(target_fd: int, value: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        chunk = memoryview(value)[: max(1, len(value) // 2)]
        return real_write(target_fd, chunk)

    monkeypatch.setattr(os, "write", short_write)
    try:
        _write_all(fd, b"complete-private-record")
    finally:
        os.close(fd)

    assert calls > 1
    assert path.read_bytes() == b"complete-private-record"


class _FakeProcess:
    returncode = 0

    def __init__(self) -> None:
        self.input: bytes | None = None

    async def communicate(self, value: bytes) -> tuple[bytes, bytes]:
        self.input = value
        return b"{}", b""

    def kill(self) -> None:
        return None

    async def wait(self) -> int:
        return 0


async def test_harness_command_has_no_egress_secrets_or_host_mounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    proc = _FakeProcess()

    async def create(*args: str, **kwargs: object) -> _FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    harness = IsolatedCodingHarness(
        docker_bin="docker", image="ditto-screener-l2-analyzer:active"
    )
    await harness.run(tmp_path, "workspace_index", {})

    args = list(captured["args"])
    assert args[:3] == ["docker", "run", "-i"]
    assert args[args.index("--network") : args.index("--network") + 2] == [
        "--network",
        "none",
    ]
    assert {"--read-only", "--cap-drop", "ALL", "no-new-privileges"} <= set(args)
    assert args[args.index("--cpus") + 1] == "0.5"
    assert f"{os.getuid()}:{os.getgid()}" in args
    assert os.getuid() != 0
    assert "/var/run/docker.sock" not in " ".join(args)
    assert "/workspace,readonly" in " ".join(args)
    env = captured["kwargs"]["env"]  # type: ignore[index]
    assert set(env) == {"PATH"}  # type: ignore[arg-type]


def test_harness_rejects_unbounded_calibration_cpu_override() -> None:
    with pytest.raises(ValueError, match="CPU limit"):
        IsolatedCodingHarness(
            docker_bin="docker",
            image="ditto-screener-l2-analyzer:active",
            cpu_limit=2.1,
        )


async def test_expired_deadline_stops_before_analyzer_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = False

    async def create(*_args: str, **_kwargs: object) -> _FakeProcess:
        nonlocal started
        started = True
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    harness = IsolatedCodingHarness(
        docker_bin="docker", image="ditto-screener-l2-analyzer:active"
    )

    with pytest.raises(ValueError, match="lease budget"):
        await harness.run(
            tmp_path,
            "workspace_index",
            {},
            deadline=asyncio.get_running_loop().time() - 0.01,
        )

    assert not started


async def test_cancelled_review_terminates_analyzer_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = asyncio.Event()

    class _BlockingProcess(_FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self.killed = False

        async def communicate(self, value: bytes) -> tuple[bytes, bytes]:
            self.input = value
            started.set()
            await asyncio.Event().wait()
            return b"{}", b""

        def kill(self) -> None:
            self.killed = True

    proc = _BlockingProcess()

    async def create(*_args: str, **_kwargs: object) -> _BlockingProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    harness = IsolatedCodingHarness(
        docker_bin="docker", image="ditto-screener-l2-analyzer:active"
    )
    task = asyncio.create_task(harness.run(tmp_path, "workspace_index", {}))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.killed


async def test_model_tool_argument_error_is_private_and_correctable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _ErrorProcess(_FakeProcess):
        returncode = 2

        async def communicate(self, value: bytes) -> tuple[bytes, bytes]:
            self.input = value
            return b'{"error":"ValueError","message":"invalid bounded argument"}', b""

    async def create(*_args: str, **_kwargs: object) -> _ErrorProcess:
        return _ErrorProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    output = await IsolatedCodingHarness(
        docker_bin="docker", image="ditto-screener-l2-analyzer:active"
    ).run(tmp_path, "read_file", {"path": "src/main.rs"})

    _require_complete_analysis(output, allow_tool_error=True)
    with pytest.raises(ValueError, match="rejected its request"):
        _require_complete_analysis(output)

    oversized = '{"error":"analyzer-output-truncated"}'
    _require_complete_analysis(oversized, allow_tool_error=True)
    with pytest.raises(L2InconclusiveError, match="output was truncated"):
        _require_complete_analysis(oversized)


def _tool_call(
    call_id: str, name: str, arguments: dict[str, object]
) -> dict[str, object]:
    return {
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "type": "function_call",
        "name": name,
        "arguments": json.dumps(arguments),
    }


def _response(
    calls: list[dict[str, object]],
    *,
    input_tokens: int = 1_000,
    output_tokens: int = 200,
    cached_tokens: int = 0,
    reasoning_tokens: int = 50,
    cost: float = 0.011,
    model: str = "openai/gpt-5.6-sol-20260709",
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "openrouter_metadata": {
                "endpoints": {
                    "available": [
                        {
                            "provider": (
                                "Moonshot AI"
                                if model.startswith("moonshotai/")
                                else "Azure"
                            ),
                            "selected": True,
                        }
                    ]
                }
            },
            "output": calls,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "input_tokens_details": {
                    "cached_tokens": cached_tokens,
                    "cache_write_tokens": 0,
                },
                "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
                "cost": cost,
            },
        },
    )


class _FakeHarness:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(
        self,
        _workspace: Path,
        command: str,
        _arguments: dict[str, object],
        *,
        deadline: float | None = None,
    ) -> str:
        del deadline
        self.calls.append(command)
        if command == "call_graph":
            return json.dumps(
                {"nodes": [], "ambiguous_calls": [], "unresolved_calls": []}
            )
        return "{}"


class _PartialHarness(_FakeHarness):
    async def run(
        self,
        _workspace: Path,
        command: str,
        _arguments: dict[str, object],
        *,
        deadline: float | None = None,
    ) -> str:
        del deadline
        self.calls.append(command)
        if command == "call_graph":
            return json.dumps(
                {"nodes": [], "ambiguous_calls": [], "unresolved_calls": []}
            )
        return json.dumps({"files": [], "truncated": True})


class _ScorerAttentionHarness(_FakeHarness):
    async def run(
        self,
        _workspace: Path,
        command: str,
        _arguments: dict[str, object],
        *,
        deadline: float | None = None,
    ) -> str:
        del deadline
        self.calls.append(command)
        if command == "call_graph":
            return json.dumps(
                {"nodes": [], "ambiguous_calls": [], "unresolved_calls": []}
            )
        if command == "scorer_field_flow":
            return json.dumps(
                {
                    "score_controls": [{"path": "src/main.rs", "line": 1}],
                    "field_clears": [{"path": "src/main.rs", "line": 1}],
                    "field_populations": [{"path": "src/main.rs", "line": 1}],
                    "same_function_candidates": [],
                    "truncated": False,
                }
            )
        return "{}"


class _TruncatedGraphHarness(_FakeHarness):
    async def run(
        self,
        _workspace: Path,
        command: str,
        _arguments: dict[str, object],
        *,
        deadline: float | None = None,
    ) -> str:
        del deadline
        self.calls.append(command)
        if command == "call_graph":
            return json.dumps(
                {
                    "nodes": [],
                    "ambiguous_calls": [],
                    "unresolved_calls": [],
                    "truncated": True,
                }
            )
        return "{}"


class _OnePartialSearchHarness(_FakeHarness):
    def __init__(self) -> None:
        super().__init__()
        self.partial_searches = 0

    async def run(
        self,
        _workspace: Path,
        command: str,
        _arguments: dict[str, object],
        *,
        deadline: float | None = None,
    ) -> str:
        del deadline
        self.calls.append(command)
        if command == "search" and self.partial_searches == 0:
            self.partial_searches += 1
            return json.dumps({"hits": [], "truncated": True})
        if command == "call_graph":
            return json.dumps(
                {"nodes": [], "ambiguous_calls": [], "unresolved_calls": []}
            )
        return "{}"


def _sol_agent(
    tmp_path: Path,
    harness: _FakeHarness,
    handler: Any,
) -> SolL2SourceReviewAgent:
    key = tmp_path / "openrouter.key"
    key.write_text("sk-test-" + "x" * 40)
    key.chmod(0o600)
    return SolL2SourceReviewAgent(
        api_key_file=str(key),
        base_url="https://openrouter.test/api/v1",
        harness=harness,  # type: ignore[arg-type]
        cache_dir=str(tmp_path / "cache"),
        audit_journal=L2AuditJournal(None, retention_days=30),
        timeout_seconds=30,
        max_steps=12,
        max_input_tokens=80_000,
        max_output_tokens=8_000,
        max_completion_tokens=2_400,
        max_cost_usd=1.5,
        cache_ttl_seconds=86_400,
        transport=httpx.MockTransport(handler),
    )


async def test_local_address_uses_a_fresh_owned_transport_per_client(
    tmp_path: Path,
) -> None:
    key = tmp_path / "openrouter.key"
    key.write_text("sk-test-" + "x" * 40)
    key.chmod(0o600)
    agent = SolL2SourceReviewAgent(
        api_key_file=str(key),
        base_url="https://openrouter.test/api/v1",
        harness=_FakeHarness(),  # type: ignore[arg-type]
        cache_dir=str(tmp_path / "cache"),
        audit_journal=L2AuditJournal(None, retention_days=30),
        timeout_seconds=30,
        max_steps=12,
        max_input_tokens=80_000,
        max_output_tokens=8_000,
        max_completion_tokens=2_400,
        max_cost_usd=1.5,
        cache_ttl_seconds=86_400,
        local_address="0.0.0.0",
    )

    first = agent._client_transport()
    second = agent._client_transport()

    assert isinstance(first, httpx.AsyncHTTPTransport)
    assert isinstance(second, httpx.AsyncHTTPTransport)
    assert first is not second
    await first.aclose()
    await second.aclose()


async def test_scorer_attention_blocks_direct_clear_and_requires_sol(
    tmp_path: Path,
) -> None:
    source = "fn main() {}"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    safe = _clearance_certificate(
        {
            "disposition": "safe",
            "risk_level": "low",
            "resolution_basis": "authoritative_model_tool_path",
            "categories": ["none"],
            "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
            "evidence": [],
            "summary": "sanitized",
        }
    )
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        calls = (
            [_tool_call("3", "read_file", {"path": "src/main.rs"})]
            if len(requests) == 3
            else [_tool_call(str(len(requests)), "submit_l2_review", safe)]
        )
        return _response(
            calls,
            model=(
                "moonshotai/kimi-k3-20260715"
                if len(requests) == 1
                else "openai/gpt-5.6-sol-20260709"
            ),
        )

    result = await _sol_agent(tmp_path, _ScorerAttentionHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1("medium"),
        deadline=None,
    )

    assert len(requests) == 4
    assert requests[2]["reasoning"] == {"effort": "medium"}
    assert requests[3]["reasoning"] == {"effort": "medium"}
    assert result.observation.risk_level == "low"
    assert result.clearance_path == "l3_adjudicated_safe"


async def test_partial_dossier_can_prove_violation_but_never_clear(
    tmp_path: Path,
) -> None:
    source = "fn main() { read_secret(); }\nfn read_secret() { send_outbound(); }"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    requests = 0
    violation = {
        "disposition": "violation",
        "risk_level": "high",
        "confidence": 0.99,
        "resolution_basis": "credential_exfiltration",
        "categories": ["credential_access"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": line,
                "file_sha256": digest,
                "category": "credential_access",
                "role": role,
            }
            for line, role in ((1, "trigger"), (2, "effect"))
        ],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 2, "role": "effect"},
        ],
        "summary": "sanitized",
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _response([_tool_call("1", "submit_l2_review", violation)])

    harness = _PartialHarness()
    result = await _sol_agent(tmp_path, harness, handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )

    assert requests == 1
    assert result.observation.ok
    assert result.observation.risk_level == "high"
    assert not result.dossier_complete
    assert result.clearance_path == "l2_violation"


async def test_partial_dossier_safe_consensus_cannot_clear(tmp_path: Path) -> None:
    source = "fn main() { serve(); }\nfn serve() {}"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    safe = _clearance_certificate(
        {
            "disposition": "safe",
            "risk_level": "low",
            "confidence": 1.0,
            "resolution_basis": "authoritative_model_tool_path",
            "categories": ["none"],
            "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
            "evidence": [],
            "summary": "sanitized",
        }
    )
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _response([_tool_call(str(requests), "submit_l2_review", safe)])

    result = await _sol_agent(tmp_path, _PartialHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )

    assert requests == 3
    assert not result.observation.ok
    assert result.observation.failure_disposition == "retryable_infra"
    assert result.observation.error_code == "l3-adjudicator-incomplete"
    assert not result.dossier_complete


async def test_sol_request_is_provider_locked_cached_and_concurrency_safe(
    tmp_path: Path,
) -> None:
    archive, artifact_sha = _tar(tmp_path, "fn main() { serve(); }\nfn serve() {}")
    source_digest = hashlib.sha256(b"fn main() { serve(); }\nfn serve() {}").hexdigest()
    key = tmp_path / "openrouter.key"
    key.write_text("sk-test-" + "x" * 40)
    key.chmod(0o600)
    requests: list[dict[str, object]] = []
    submitted = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 0.93,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": source_digest}],
        "evidence": [],
        "causal_path": [],
        "summary": "private model summary",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 3:
            calls = [_tool_call("3", "read_file", {"path": "src/main.rs"})]
        else:
            result = (
                _clearance_certificate(submitted) if len(requests) == 4 else submitted
            )
            calls = [_tool_call(str(len(requests)), "submit_l2_review", result)]
        return _response(
            calls,
            cached_tokens=800 if len(requests) == 2 else 0,
            reasoning_tokens=40 if len(requests) == 1 else 80,
            model=(
                "moonshotai/kimi-k3-20260715"
                if len(requests) == 1
                else "openai/gpt-5.6-sol-20260709"
            ),
        )

    harness = _FakeHarness()
    journal_path = tmp_path / "l2-audit.jsonl"
    agent = SolL2SourceReviewAgent(
        api_key_file=str(key),
        base_url="https://openrouter.test/api/v1",
        harness=harness,  # type: ignore[arg-type]
        cache_dir=str(tmp_path / "cache"),
        audit_journal=L2AuditJournal(str(journal_path), retention_days=30),
        timeout_seconds=30,
        max_steps=12,
        max_input_tokens=80_000,
        max_output_tokens=8_000,
        max_completion_tokens=2_400,
        max_cost_usd=1.5,
        cache_ttl_seconds=86_400,
        transport=httpx.MockTransport(handler),
    )

    first, second = await asyncio.gather(
        agent.review(
            str(archive),
            artifact_sha256=artifact_sha,
            attempt_id=ATTEMPT,
            l1_observation=_l1(),
            deadline=None,
        ),
        agent.review(
            str(archive),
            artifact_sha256=artifact_sha,
            attempt_id=ATTEMPT,
            l1_observation=_l1(),
            deadline=None,
        ),
    )

    assert first.observation.risk_level == second.observation.risk_level == "low"
    assert len(requests) == 4, "one model review must serve concurrent identical work"
    assert set(harness.calls) == {
        "workspace_index",
        "call_graph",
        "starter_diff",
        "starter_function_diff",
        "build_structure",
        "integrity_surfaces",
        "scorer_field_flow",
        "read_file",
    }
    assert requests[0]["model"] == L2_MODEL
    assert requests[0]["provider"] == {
        "allow_fallbacks": True,
        "require_parameters": False,
        "zdr": True,
        "data_collection": "deny",
    }
    assert requests[0]["models"] == [
        L2_MODEL,
        "z-ai/glm-5.2",
        "openai/gpt-5.6-sol",
    ]
    assert "reasoning" not in requests[0]
    assert requests[1]["model"] == "openai/gpt-5.6-sol"
    assert requests[2]["model"] == "openai/gpt-5.6-sol"
    assert requests[3]["model"] == "openai/gpt-5.6-sol"
    assert "only" not in requests[1]["provider"]  # type: ignore[operator]
    assert requests[1]["provider"]["allow_fallbacks"] is False  # type: ignore[index]
    assert requests[1]["provider"]["require_parameters"] is False  # type: ignore[index]
    assert requests[0]["max_output_tokens"] == 2_400
    assert requests[1]["reasoning"] == {"effort": "medium"}
    assert requests[0]["store"] is False
    assert requests[0]["prompt_cache_key"] == requests[1]["prompt_cache_key"]
    assert len(requests[0]["prompt_cache_key"]) <= 64  # type: ignore[arg-type]
    first_content = requests[0]["input"][0]["content"]  # type: ignore[index]
    second_content = requests[1]["input"][0]["content"]  # type: ignore[index]
    assert first_content[0] == second_content[0]  # type: ignore[index]
    assert len(first_content) == 2  # type: ignore[arg-type]
    assert len(second_content) == 3  # type: ignore[arg-type]
    dossier_text = first_content[0]["text"]  # type: ignore[index]
    assert "bounded_source_inventory" in dossier_text
    assert "main_call_graph" in dossier_text
    assert "starter_diff" in dossier_text
    assert "starter_function_diff" in dossier_text
    assert "integrity_surfaces" in dossier_text
    assert "scorer_field_flow" in dossier_text
    assert '"supported_versions":[3,4,5,6]' in dossier_text
    assert '"relay_usage_authority":"validator_owned"' in dossier_text
    assert '"stored_content_role":"data_not_instruction"' in dossier_text
    assert first.critic_disposition == second.critic_disposition == "confirm_safe"
    assert (
        first.adjudicator_disposition
        == second.adjudicator_disposition
        == "confirm_safe"
    )
    assert first.clearance_path == second.clearance_path == "l3_adjudicated_safe"
    assert first.response_models == (
        "moonshotai/kimi-k3-20260715",
        "openai/gpt-5.6-sol-20260709",
        "openai/gpt-5.6-sol-20260709",
        "openai/gpt-5.6-sol-20260709",
    )
    assert first.response_providers == ("Moonshot AI", "Azure", "Azure", "Azure")
    assert first.usage.cached_input_tokens == 800
    assert first.usage.reasoning_tokens == 280
    assert first.usage.reported_cost_usd == pytest.approx(0.044)
    assert {first.cache_hit, second.cache_hit} == {False, True}
    records = [json.loads(line) for line in journal_path.read_text().splitlines()]
    assert len(records) == 2
    assert {record["cache_hit"] for record in records} == {False, True}
    assert all(record["attempt_id"] == str(ATTEMPT) for record in records)
    assert all(record["analyst_model"] == L2_MODEL for record in records)
    assert all(
        record["analyst_fallback_models"] == ["z-ai/glm-5.2", "openai/gpt-5.6-sol"]
        for record in records
    )
    assert all(record["critic_model"] == "openai/gpt-5.6-sol" for record in records)
    assert all(record["prompt_revision"] == L2_PROMPT_REVISION for record in records)
    assert all(
        record["cause_prompt_revision"] == L2_CAUSE_PROMPT_REVISION
        for record in records
    )
    assert all(
        record["cause_tiebreaker_prompt_revision"]
        == L2_CAUSE_TIEBREAKER_PROMPT_REVISION
        for record in records
    )
    assert all(
        record["safety_prompt_revision"] == L2_SAFETY_PROMPT_REVISION
        for record in records
    )
    assert all(
        record["static_hold_revision"] == L2_STATIC_HOLD_REVISION for record in records
    )
    assert all(record["dossier_revision"] == L2_DOSSIER_REVISION for record in records)
    assert all(record["harness_revision"] == L2_HARNESS_REVISION for record in records)
    assert all(record["evidence_schema_version"] == 1 for record in records)
    assert all(record["authority_transition"] is None for record in records)
    assert all(record["scorer_visible_effect"] is None for record in records)
    assert all(record["causal_role_complete"] is True for record in records)
    assert all(
        record["causal_verification_reason"] == "causal-evidence-not-required"
        for record in records
    )
    assert all(len(record["starter_revisions"]) == 4 for record in records)
    assert all(record["budgets"]["max_cost_usd"] == 1.5 for record in records)
    assert all(record["budgets"]["max_analyzer_calls"] == 24 for record in records)
    assert all(
        record["budgets"]["cause_adjudicator_max_analyzer_calls"] == 16
        for record in records
    )
    assert all(
        record["budgets"]["cause_tiebreaker_max_analyzer_calls"] == 12
        for record in records
    )
    assert all(
        record["budgets"]["safety_adjudicator_max_analyzer_calls"] == 12
        for record in records
    )
    assert all(record["elapsed_ms"] >= 0 for record in records)
    assert "private model summary" not in journal_path.read_text()
    assert journal_path.stat().st_mode & 0o777 == 0o600


async def test_served_generator_attention_requires_sol_and_can_clear(
    tmp_path: Path,
) -> None:
    source = """\
const ATTRS: &[&str] = &["city", "employer", "car", "university", "instrument",
    "project", "trip", "pet", "cuisine", "diet", "color", "hobby"];
const QUESTIONS: &[&str] = &["What city?", "Where work?", "Which project?",
    "How many trips?", "List pets", "What was used before?"];
const FACTS: &[&str] = &["I moved city", "I work company", "I drive car",
    "I studied university", "I play instrument", "My project changed"];
const EVENTS: &[&str] = &["moved", "started", "switched", "changed"];
fn bridge(question: Query, query: Query, fact: Fact, memory: Memory,
    attribute: Attr, value: Value) { extract(question); retrieve(query, memory);
    match_fact(fact); keyword(attribute); use_value(value); }
fn direct_answer() -> Answer { return deterministic_answer(); }
fn run() -> Answer {
    if ready() { return direct_answer(); }
    call_model_inference()
}
"""
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    requests: list[dict[str, object]] = []
    safe = _clearance_certificate(
        {
            "disposition": "safe",
            "risk_level": "low",
            "confidence": 1.0,
            "resolution_basis": "authoritative_model_tool_path",
            "categories": ["none"],
            "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
            "evidence": [],
            "summary": "sanitized",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 3:
            return _response(
                [_tool_call("3", "read_file", {"path": "src/main.rs"})],
                model="openai/gpt-5.6-sol-20260709",
            )
        return _response(
            [_tool_call("1", "submit_l2_review", safe)],
            model=(
                "moonshotai/kimi-k3-20260715"
                if len(requests) == 1
                else "openai/gpt-5.6-sol-20260709"
            ),
        )

    agent = _sol_agent(tmp_path, _FakeHarness(), handler)
    result = await agent.review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )

    assert len(requests) == 4
    assert requests[0]["model"] == L2_MODEL
    assert requests[1]["model"] == "openai/gpt-5.6-sol"
    assert result.observation.risk_level == "low"
    assert result.clearance_path == "l3_adjudicated_safe"
    assert result.critic_disposition == "confirm_safe"


async def test_defensive_review_comment_is_attention_not_a_static_hold(
    tmp_path: Path,
) -> None:
    source = """\
fn main() {
    // Never alter model routing for a screener audit or single-call nonce probe.
    let answer = chat(request.question);
    return_response(answer);
}
"""
    archive, artifact_sha = _tar(tmp_path, source)
    repository = TarSourceRepository(str(archive))
    assert repository.review_leads()["review_adaptive_model_routing"]["candidate"]
    digest = hashlib.sha256(source.encode()).hexdigest()
    safe = _clearance_certificate(
        {
            "disposition": "safe",
            "risk_level": "low",
            "confidence": 1.0,
            "resolution_basis": "authoritative_model_tool_path",
            "categories": ["none"],
            "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
            "evidence": [],
            "summary": "sanitized",
        }
    )
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 3:
            return _response(
                [_tool_call("3", "read_file", {"path": "src/main.rs"})],
                model="openai/gpt-5.6-sol-20260709",
            )
        return _response(
            [_tool_call(str(len(requests)), "submit_l2_review", safe)],
            model=(
                "moonshotai/kimi-k3-20260715"
                if len(requests) == 1
                else "openai/gpt-5.6-sol-20260709"
            ),
        )

    result = await _sol_agent(tmp_path, _FakeHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )

    assert len(requests) == 4
    assert result.observation.risk_level == "low"
    assert result.clearance_path == "l3_adjudicated_safe"


async def test_mixed_l1_integrity_attention_gets_sol_causal_review(
    tmp_path: Path,
) -> None:
    source = """\
const ATTRS: &[&str] = &["city", "employer", "car", "university", "instrument",
    "project", "trip", "pet", "cuisine", "diet", "color", "hobby"];
const QUESTIONS: &[&str] = &["What city?", "Where work?", "Which project?",
    "How many trips?", "List pets", "What was used before?"];
const FACTS: &[&str] = &["I moved city", "I work company", "I drive car",
    "I studied university", "I play instrument", "My project changed"];
const EVENTS: &[&str] = &["moved", "started", "switched", "changed"];
fn bridge(question: Query, query: Query, fact: Fact, memory: Memory,
    attribute: Attr, value: Value) { extract(question); retrieve(query, memory);
    match_fact(fact); keyword(attribute); use_value(value); }
fn direct_answer() -> Answer { return deterministic_answer(); }
fn run() -> Answer {
    if ready() { return direct_answer(); }
    call_model_inference()
}
"""
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    requests: list[dict[str, object]] = []
    safe = _clearance_certificate(
        {
            "disposition": "safe",
            "risk_level": "low",
            "confidence": 1.0,
            "resolution_basis": "authoritative_model_tool_path",
            "categories": ["none"],
            "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
            "evidence": [],
            "summary": "sanitized",
        }
    )
    cause = {
        "disposition": "violation",
        "risk_level": "high",
        "confidence": 0.99,
        "resolution_basis": "scorer_field_manipulation",
        "categories": ["benchmark_emulation", "scorer_contract_manipulation"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": line,
                "file_sha256": digest,
                "category": category,
                "role": role,
            }
            for category in ("benchmark_emulation", "scorer_contract_manipulation")
            for line, role in ((1, "trigger"), (2, "effect"))
        ],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 2, "role": "effect"},
        ],
        "generator_components": [],
        "summary": "sanitized",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        result = safe if len(requests) == 1 else cause
        model = (
            "moonshotai/kimi-k3-20260715"
            if len(requests) == 1
            else "openai/gpt-5.6-sol-20260709"
        )
        return _response(
            [_tool_call(str(len(requests)), "submit_l2_review", result)],
            model=model,
        )

    l1 = SourceReviewObservation(
        ok=True,
        risk_level="high",
        finding_digest="a" * 64,
        categories=("benchmark_emulation", "scorer_contract_manipulation"),
        finding={
            "risk_level": "high",
            "confidence": 0.9,
            "categories": ["benchmark_emulation", "scorer_contract_manipulation"],
            "evidence": [
                {
                    "path": "src/main.rs",
                    "line": 1,
                    "category": "benchmark_emulation",
                },
                {
                    "path": "src/main.rs",
                    "line": 2,
                    "category": "scorer_contract_manipulation",
                },
            ],
            "summary": "bounded mixed routing lead",
        },
    )
    result = await _sol_agent(tmp_path, _FakeHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=l1,
        deadline=None,
    )

    assert len(requests) == 3
    assert result.resolution_basis == "scorer_field_manipulation"
    assert result.clearance_path == "l3_adjudicated_violation"
    assert result.adjudicator_disposition == "uphold_violation"


async def test_reasoning_only_turn_gets_one_bounded_tool_retry(tmp_path: Path) -> None:
    archive, artifact_sha = _tar(tmp_path, "fn main() { serve(); }\nfn serve() {}")
    source_digest = hashlib.sha256(b"fn main() { serve(); }\nfn serve() {}").hexdigest()
    requests: list[dict[str, object]] = []
    submitted = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 0.93,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": source_digest}],
        "evidence": [],
        "causal_path": [],
        "summary": "discarded",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return _response([], model="moonshotai/kimi-k3-20260715")
        if len(requests) == 4:
            return _response([_tool_call("4", "read_file", {"path": "src/main.rs"})])
        result = _clearance_certificate(submitted) if len(requests) == 5 else submitted
        return _response(
            [_tool_call(str(len(requests)), "submit_l2_review", result)],
            model=(
                "moonshotai/kimi-k3-20260715"
                if len(requests) == 2
                else "openai/gpt-5.6-sol-20260709"
            ),
        )

    result = await _sol_agent(tmp_path, _FakeHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )

    assert result.observation.risk_level == "low"
    assert len(requests) == 5
    retry = requests[1]["input"][-1]  # type: ignore[index]
    assert retry["role"] == "user"  # type: ignore[index]
    assert "Call exactly one" in retry["content"][0]["text"]  # type: ignore[index]


async def test_model_contract_failure_retains_usage_and_never_clears(
    tmp_path: Path,
) -> None:
    archive, artifact_sha = _tar(tmp_path, "fn main() {}")

    def handler(_request: httpx.Request) -> httpx.Response:
        return _response([], model="moonshotai/kimi-k3-20260715")

    result = await _sol_agent(tmp_path, _FakeHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )

    assert not result.observation.ok
    assert result.observation.failure_disposition == "retryable_infra"
    assert result.observation.error_code == "l2-model-tool-contract"
    assert result.usage.input_tokens == 2_000
    assert result.usage.output_tokens == 400
    assert result.response_models == (
        "moonshotai/kimi-k3-20260715",
        "moonshotai/kimi-k3-20260715",
    )


async def test_parallel_model_tool_calls_cannot_exceed_trajectory_cap(
    tmp_path: Path,
) -> None:
    archive, artifact_sha = _tar(tmp_path, "fn main() {}")
    calls = [_tool_call(str(index), "workspace_index", {}) for index in range(29)]

    result = await _sol_agent(
        tmp_path,
        _FakeHarness(),
        lambda _request: _response(calls, model="moonshotai/kimi-k3-20260715"),
    ).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )

    assert not result.observation.ok
    assert result.observation.error_code == "l2-model-tool-budget"
    assert result.observation.failure_disposition == "pass_inconclusive"
    assert result.observation.review_audit is not None
    assert result.observation.review_audit["reason_code"] == "l2-model-tool-budget"
    assert result.response_models == ("moonshotai/kimi-k3-20260715",)


async def test_analyst_violation_stops_before_critic(tmp_path: Path) -> None:
    source = "fn main() { bypass(); }\nfn bypass() {}"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    submitted = {
        "disposition": "violation",
        "risk_level": "medium",
        "confidence": 0.91,
        "resolution_basis": "hidden_model_bypass",
        "categories": ["provider_bypass"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 2,
                "file_sha256": digest,
                "category": "provider_bypass",
                "role": "effect",
            }
        ],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 2, "role": "effect"},
        ],
        "summary": "sanitized",
    }
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _response([_tool_call("1", "submit_l2_review", submitted)])

    result = await _sol_agent(tmp_path, _FakeHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )

    assert requests == 1
    assert result.observation.risk_level == "medium"
    assert result.critic_disposition is None


async def test_mixed_benchmark_violation_gets_sol_cause_adjudication(
    tmp_path: Path,
) -> None:
    source = "fn main() { replace(); }\nfn replace() {}"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    base = {
        "disposition": "violation",
        "risk_level": "high",
        "confidence": 0.94,
        "categories": ["benchmark_emulation", "scorer_contract_manipulation"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 1,
                "file_sha256": digest,
                "category": "benchmark_emulation",
                "role": "trigger",
            },
            {
                "path": "src/main.rs",
                "line": 2,
                "file_sha256": digest,
                "category": "benchmark_emulation",
                "role": "effect",
            },
            {
                "path": "src/main.rs",
                "line": 1,
                "file_sha256": digest,
                "category": "scorer_contract_manipulation",
                "role": "trigger",
            },
            {
                "path": "src/main.rs",
                "line": 2,
                "file_sha256": digest,
                "category": "scorer_contract_manipulation",
                "role": "effect",
            },
        ],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 2, "role": "effect"},
        ],
        "summary": "sanitized",
    }
    analyst = {**base, "resolution_basis": "benchmark_answer_replacement"}
    adjudicated = {**base, "resolution_basis": "scorer_field_manipulation"}
    requests = 0
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        payloads.append(json.loads(request.content))
        result = analyst if requests == 1 else adjudicated
        model = (
            "moonshotai/kimi-k3-20260715"
            if requests == 1
            else "openai/gpt-5.6-sol-20260709"
        )
        return _response(
            [_tool_call(str(requests), "submit_l2_review", result)], model=model
        )

    result = await _sol_agent(tmp_path, _FakeHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1("high"),
        deadline=None,
    )

    assert requests == 3
    assert payloads[1]["reasoning"] == {"effort": "medium"}
    assert payloads[2]["reasoning"] == {"effort": "medium"}
    cause_content = payloads[1]["input"][0]["content"]  # type: ignore[index]
    provisional = json.loads(cause_content[1]["text"])[  # type: ignore[index]
        "provisional_analyst_result"
    ]
    assert provisional["finding"]["evidence"]
    assert "l1_untrusted_diagnostic" in provisional
    tiebreak_content = payloads[2]["input"][0]["content"]  # type: ignore[index]
    disagreement = json.loads(tiebreak_content[1]["text"])[  # type: ignore[index]
        "provisional_analyst_result"
    ]
    assert disagreement["allowed_resolution_bases"] == [
        "benchmark_answer_replacement",
        "scorer_field_manipulation",
    ]
    assert result.observation.risk_level == "high"
    assert result.resolution_basis == "scorer_field_manipulation"
    assert result.critic_disposition == "not_required"
    assert result.adjudicator_disposition == "resolve_violation_cause_disagreement"
    assert result.clearance_path == "l3_adjudicated_violation_cause_tiebreak"


async def test_violation_adjudicator_disagreement_cannot_clear(tmp_path: Path) -> None:
    source = "fn main() { replace(); }\nfn replace() {}"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    violation = {
        "disposition": "violation",
        "risk_level": "high",
        "confidence": 0.94,
        "resolution_basis": "benchmark_answer_replacement",
        "categories": ["benchmark_emulation"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 1,
                "file_sha256": digest,
                "category": "benchmark_emulation",
                "role": "trigger",
            },
            {
                "path": "src/main.rs",
                "line": 2,
                "file_sha256": digest,
                "category": "benchmark_emulation",
                "role": "effect",
            },
        ],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 2, "role": "effect"},
        ],
        "summary": "sanitized",
    }
    safe = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 0.8,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [],
        "causal_path": [],
        "summary": "sanitized",
    }
    responses = (violation, safe)
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        response = responses[requests]
        requests += 1
        return _response([_tool_call(str(requests), "submit_l2_review", response)])

    result = await _sol_agent(tmp_path, _FakeHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1("high"),
        deadline=None,
    )

    assert requests == 2
    assert not result.observation.ok
    assert result.observation.failure_disposition == "inconclusive"
    assert result.adjudicator_disposition == "disagreement"
    assert result.clearance_path == "l3_violation_adjudicator_disagreement"


async def test_violation_adjudicator_retry_reuses_kimi_stage(tmp_path: Path) -> None:
    source = "fn main() { replace(); }\nfn replace() {}"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    violation = {
        "disposition": "violation",
        "risk_level": "high",
        "confidence": 0.94,
        "resolution_basis": "benchmark_answer_replacement",
        "categories": ["benchmark_emulation"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 1,
                "file_sha256": digest,
                "category": "benchmark_emulation",
                "role": "trigger",
            },
            {
                "path": "src/main.rs",
                "line": 2,
                "file_sha256": digest,
                "category": "benchmark_emulation",
                "role": "effect",
            },
        ],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 2, "role": "effect"},
        ],
        "summary": "sanitized",
    }
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests in {2, 3}:
            return _response([])
        return _response(
            [_tool_call(str(requests), "submit_l2_review", violation)],
            model=(
                "moonshotai/kimi-k3-20260715"
                if requests == 1
                else "openai/gpt-5.6-sol-20260709"
            ),
        )

    agent = _sol_agent(tmp_path, _FakeHarness(), handler)
    first = await agent.review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1("high"),
        deadline=None,
    )
    second = await agent.review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1("high"),
        deadline=None,
    )

    assert first.observation.failure_disposition == "retryable_infra"
    assert second.observation.risk_level == "high"
    assert second.analyst_cache_hit
    assert second.adjudicator_disposition == "confirm_violation_cause"
    assert requests == 4, "the retry must rerun only the SOL cause adjudicator"


async def test_incomplete_main_graph_allows_violation_but_never_clear(
    tmp_path: Path,
) -> None:
    source = "fn main() { bypass(); }\nfn bypass() {}"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    violation = {
        "disposition": "violation",
        "risk_level": "medium",
        "confidence": 0.99,
        "resolution_basis": "hidden_model_bypass",
        "categories": ["provider_bypass"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 2,
                "file_sha256": digest,
                "category": "provider_bypass",
                "role": "effect",
            }
        ],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 2, "role": "effect"},
        ],
        "summary": "sanitized",
    }

    def violation_handler(_request: httpx.Request) -> httpx.Response:
        return _response([_tool_call("1", "submit_l2_review", violation)])

    violation_result = await _sol_agent(
        tmp_path, _TruncatedGraphHarness(), violation_handler
    ).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )
    assert violation_result.observation.risk_level == "medium"
    assert not violation_result.dossier_complete

    safe = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 1.0,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "context"},
            {"path": "src/main.rs", "line": 1, "role": "decision"},
            {"path": "src/main.rs", "line": 1, "role": "effect"},
            {"path": "src/main.rs", "line": 1, "role": "sink"},
        ],
        "summary": "sanitized",
    }

    safe_requests = 0

    def safe_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal safe_requests
        safe_requests += 1
        if safe_requests in {1, 3, 5}:
            return _response(
                [
                    _tool_call(
                        str(safe_requests),
                        "read_file",
                        {"path": "src/main.rs", "start_line": 1, "end_line": 2},
                    )
                ]
            )
        return _response([_tool_call("1", "submit_l2_review", safe)])

    safe_dir = tmp_path / "safe"
    safe_dir.mkdir()
    safe_result = await _sol_agent(
        safe_dir, _TruncatedGraphHarness(), safe_handler
    ).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )
    assert not safe_result.observation.ok
    assert safe_result.observation.failure_disposition == "retryable_infra"
    assert safe_result.observation.error_code == "l3-adjudicator-incomplete"
    assert safe_result.clearance_path == "l3_adjudicator_incomplete"
    assert safe_requests == 6


async def test_partial_exploratory_tool_is_withheld_and_safety_still_runs(
    tmp_path: Path,
) -> None:
    source = "fn main() { let answer = bypass(); serve(answer); }"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    safe = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 0.9,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [],
        "causal_path": [],
        "summary": "sanitized",
    }
    violation = {
        **safe,
        "disposition": "violation",
        "risk_level": "high",
        "confidence": 0.99,
        "resolution_basis": "hidden_model_bypass",
        "categories": ["provider_bypass"],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 1,
                "file_sha256": digest,
                "category": "provider_bypass",
                "role": "effect",
            }
        ],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 1, "role": "effect"},
        ],
    }
    requests = 0
    request_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        request_payloads.append(json.loads(request.content))
        if requests == 2:
            output = [_tool_call("2", "search", {"query": "bypass"})]
        else:
            result = violation if requests == 4 else safe
            output = [_tool_call(str(requests), "submit_l2_review", result)]
        return _response(
            output,
            model=(
                "moonshotai/kimi-k3-20260715"
                if requests == 1
                else "openai/gpt-5.6-sol-20260709"
            ),
        )

    result = await _sol_agent(tmp_path, _OnePartialSearchHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1("high"),
        deadline=None,
    )

    assert requests == 4
    assert result.observation.risk_level == "high"
    assert result.critic_disposition == "confirm_safe"
    assert result.adjudicator_disposition == "uphold_violation"
    assert result.clearance_path == "l3_adjudicated_violation"
    assert result.dossier_complete
    assert any(
        "partial-analysis-not-admissible" in json.dumps(payload)
        for payload in request_payloads
    )


async def test_critic_challenge_keeps_quarantine(tmp_path: Path) -> None:
    source = "fn main() { bypass(); }\nfn bypass() {}"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    safe = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 0.8,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [],
        "causal_path": [],
        "summary": "sanitized",
    }
    challenge = {
        **safe,
        "disposition": "violation",
        "risk_level": "medium",
        "confidence": 0.9,
        "resolution_basis": "hidden_model_bypass",
        "categories": ["provider_bypass"],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 2,
                "file_sha256": digest,
                "category": "provider_bypass",
                "role": "effect",
            }
        ],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 2, "role": "effect"},
        ],
    }
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        result = safe if requests == 1 else challenge
        return _response([_tool_call(str(requests), "submit_l2_review", result)])

    result = await _sol_agent(tmp_path, _FakeHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )

    assert requests == 3
    assert result.observation.risk_level == "medium"
    assert result.critic_disposition == "challenge"
    assert result.adjudicator_disposition == "uphold_violation"
    assert result.clearance_path == "l3_adjudicated_violation"


async def test_agreeing_safe_models_cannot_clear_original_scorer_lead(
    tmp_path: Path,
) -> None:
    source = (
        "fn main() { let output = model_answer(); filter(output); }\n"
        "fn filter(_output: String) {}"
    )
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    safe = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 0.9,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [],
        "causal_path": [],
        "summary": "sanitized",
    }
    violation = {
        **safe,
        "disposition": "violation",
        "risk_level": "medium",
        "confidence": 0.99,
        "resolution_basis": "scorer_field_manipulation",
        "categories": ["scorer_contract_manipulation"],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 1,
                "file_sha256": digest,
                "category": "scorer_contract_manipulation",
                "role": "trigger",
            },
            {
                "path": "src/main.rs",
                "line": 2,
                "file_sha256": digest,
                "category": "scorer_contract_manipulation",
                "role": "effect",
            },
        ],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 2, "role": "effect"},
        ],
    }
    l1 = SourceReviewObservation(
        ok=True,
        risk_level="medium",
        finding_digest="e" * 64,
        categories=("benchmark_emulation", "scorer_contract_manipulation"),
        finding={
            "risk_level": "medium",
            "confidence": 0.9,
            "categories": ["benchmark_emulation", "scorer_contract_manipulation"],
            "evidence": [
                {
                    "path": "src/main.rs",
                    "line": 1,
                    "category": "benchmark_emulation",
                },
                {
                    "path": "src/main.rs",
                    "line": 2,
                    "category": "scorer_contract_manipulation",
                },
            ],
            "summary": "bounded scorer routing lead",
        },
    )
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 3:
            return _response([_tool_call("3", "read_file", {"path": "src/main.rs"})])
        result = violation if requests == 4 else safe
        return _response(
            [_tool_call(str(requests), "submit_l2_review", result)],
            model=(
                "moonshotai/kimi-k3-20260715"
                if requests == 1
                else "openai/gpt-5.6-sol-20260709"
            ),
        )

    result = await _sol_agent(tmp_path, _FakeHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=l1,
        deadline=None,
    )

    assert requests == 4
    assert result.observation.risk_level == "medium"
    assert result.resolution_basis == "scorer_field_manipulation"
    assert result.critic_disposition == "confirm_safe"
    assert result.adjudicator_disposition == "uphold_violation"
    assert result.clearance_path == "l3_adjudicated_violation"


@pytest.mark.parametrize(
    ("adjudicator_confidence", "clears"), [(1.0, True), (0.99, False)]
)
async def test_adjudicator_requires_certificate_to_overturn_false_critic_challenge(
    tmp_path: Path, adjudicator_confidence: float, clears: bool
) -> None:
    source = "fn main() { execute_real_tool(); }\nfn execute_real_tool() {}"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    safe = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 0.9,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [],
        "causal_path": [],
        "summary": "sanitized",
    }
    challenge = {
        **safe,
        "disposition": "violation",
        "risk_level": "medium",
        "resolution_basis": "fabricated_tool_trajectory",
        "categories": ["fabricated_tool_trajectory"],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 1,
                "file_sha256": digest,
                "category": "fabricated_tool_trajectory",
                "role": "effect",
            }
        ],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 1, "role": "effect"},
        ],
    }
    adjudicated_safe = {
        **safe,
        "confidence": adjudicator_confidence,
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "context"},
            {"path": "src/main.rs", "line": 1, "role": "decision"},
            {"path": "src/main.rs", "line": 1, "role": "effect"},
            {"path": "src/main.rs", "line": 1, "role": "sink"},
        ],
    }
    responses = (safe, challenge, adjudicated_safe)
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        if requests == 2:
            requests += 1
            return _response([_tool_call("3", "read_file", {"path": "src/main.rs"})])
        result = responses[requests if requests < 2 else 2]
        requests += 1
        return _response([_tool_call(str(requests), "submit_l2_review", result)])

    result = await _sol_agent(tmp_path, _FakeHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )

    assert requests == 4
    assert result.critic_disposition == "challenge"
    if clears:
        assert result.observation.risk_level == "low"
        assert result.adjudicator_disposition == "overturn_to_safe"
        assert result.clearance_path == "l3_adjudicated_safe"
    else:
        assert not result.observation.ok
        assert result.observation.failure_disposition == "retryable_infra"
        assert result.adjudicator_disposition == "inconclusive"
        assert result.clearance_path == "l3_adjudicator_clearance_unproven"


async def test_critic_failure_cannot_clear_or_reject(tmp_path: Path) -> None:
    source = "fn main() {}"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    safe = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 0.8,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [],
        "causal_path": [],
        "summary": "sanitized",
    }
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return _response([_tool_call("1", "submit_l2_review", safe)])
        return _response([])

    result = await _sol_agent(tmp_path, _FakeHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )

    assert requests == 3
    assert not result.observation.ok
    assert result.observation.failure_disposition == "retryable_infra"
    assert result.critic_disposition == "retryable_infra"


async def test_critic_retry_reuses_sanitized_analyst_stage_cache(
    tmp_path: Path,
) -> None:
    source = "fn main() {}"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    safe = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 0.9,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [],
        "causal_path": [],
        "summary": "sanitized",
    }
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests in {2, 3}:
            return _response([])
        if requests == 5:
            return _response([_tool_call("5", "read_file", {"path": "src/main.rs"})])
        result = _clearance_certificate(safe) if requests == 6 else safe
        return _response([_tool_call(str(requests), "submit_l2_review", result)])

    agent = _sol_agent(tmp_path, _FakeHarness(), handler)
    first = await agent.review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )
    second = await agent.review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )

    assert first.observation.failure_disposition == "retryable_infra"
    assert second.observation.risk_level == "low"
    assert second.analyst_cache_hit
    assert requests == 6, "the retry must resume at SOL instead of rerunning Kimi"


async def test_adjudicator_retry_reuses_analyst_and_critic_stage_caches(
    tmp_path: Path,
) -> None:
    source = "fn main() { execute_real_tool(); }\nfn execute_real_tool() {}"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    safe = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 0.9,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [],
        "causal_path": [],
        "summary": "sanitized",
    }
    challenge = {
        **safe,
        "disposition": "violation",
        "risk_level": "medium",
        "resolution_basis": "fabricated_tool_trajectory",
        "categories": ["fabricated_tool_trajectory"],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 1,
                "file_sha256": digest,
                "category": "fabricated_tool_trajectory",
                "role": "effect",
            }
        ],
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 1, "role": "effect"},
        ],
    }
    adjudicated_safe = {
        **safe,
        "confidence": 1.0,
        "causal_path": [
            {"path": "src/main.rs", "line": 1, "role": "context"},
            {"path": "src/main.rs", "line": 1, "role": "decision"},
            {"path": "src/main.rs", "line": 1, "role": "effect"},
            {"path": "src/main.rs", "line": 1, "role": "sink"},
        ],
    }
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return _response([_tool_call("1", "submit_l2_review", safe)])
        if requests == 2:
            return _response([_tool_call("2", "submit_l2_review", challenge)])
        if requests in {3, 4}:
            return _response([])
        if requests == 5:
            return _response(
                [_tool_call("5", "read_file", {"path": "src/main.rs"})],
                input_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
                cost=0,
            )
        return _response([_tool_call("6", "submit_l2_review", adjudicated_safe)])

    agent = _sol_agent(tmp_path, _FakeHarness(), handler)
    first = await agent.review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )
    second = await agent.review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )

    assert first.observation.failure_disposition == "retryable_infra"
    assert second.observation.risk_level == "low"
    assert second.analyst_cache_hit
    assert second.critic_cache_hit
    assert second.adjudicator_disposition == "overturn_to_safe"
    assert requests == 6, "the retry must rerun only the SOL adjudicator"
    assert second.usage.input_tokens == 1_000, "only adjudicator usage is new"


async def test_invalid_final_tool_result_gets_compact_correction_turn(
    tmp_path: Path,
) -> None:
    source = "fn main() {}"
    archive, artifact_sha = _tar(tmp_path, source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    safe = {
        "disposition": "safe",
        "risk_level": "low",
        "confidence": 0.8,
        "resolution_basis": "authoritative_model_tool_path",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [],
        "causal_path": [],
        "summary": "sanitized",
    }
    contradictory = {
        **safe,
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 1,
                "file_sha256": digest,
                "category": "benchmark_emulation",
                "role": "context",
            }
        ],
    }
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 4:
            return _response(
                [_tool_call("4", "read_file", {"path": "src/main.rs"})],
                model="openai/gpt-5.6-sol-20260709",
            )
        result = (
            contradictory
            if len(requests) == 1
            else _clearance_certificate(safe)
            if len(requests) == 5
            else safe
        )
        return _response(
            [_tool_call(str(len(requests)), "submit_l2_review", result)],
            model=(
                "moonshotai/kimi-k3-20260715"
                if len(requests) <= 2
                else "openai/gpt-5.6-sol-20260709"
            ),
        )

    result = await _sol_agent(tmp_path, _FakeHarness(), handler).review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=None,
    )

    assert result.observation.risk_level == "low"
    assert result.critic_disposition == "confirm_safe"
    assert len(requests) == 5
    correction_items = requests[1]["input"]  # type: ignore[index]
    correction = correction_items[-1]  # type: ignore[index]
    assert correction["type"] == "function_call_output"
    assert "safe result contains contradictory evidence" in correction["output"]


async def test_late_l2_result_is_not_accepted(tmp_path: Path) -> None:
    archive, artifact_sha = _tar(tmp_path, "fn main() {}")
    key = tmp_path / "openrouter.key"
    key.write_text("sk-test-" + "x" * 40)
    key.chmod(0o600)
    agent = SolL2SourceReviewAgent(
        api_key_file=str(key),
        base_url="https://openrouter.test/api/v1",
        harness=_FakeHarness(),  # type: ignore[arg-type]
        cache_dir=str(tmp_path / "cache"),
        audit_journal=L2AuditJournal(None, retention_days=30),
        timeout_seconds=30,
        max_steps=12,
        max_input_tokens=80_000,
        max_output_tokens=8_000,
        max_completion_tokens=2_400,
        max_cost_usd=1.5,
        cache_ttl_seconds=86_400,
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )

    result = await agent.review(
        str(archive),
        artifact_sha256=artifact_sha,
        attempt_id=ATTEMPT,
        l1_observation=_l1(),
        deadline=asyncio.get_running_loop().time() - 0.01,
    )

    assert not result.observation.ok
    assert result.observation.error_code == "l2-late-result"
    assert result.observation.failure_disposition == "retryable_infra"
    assert not list((tmp_path / "cache").glob("*.json"))


async def test_http_retry_stops_when_backoff_would_exceed_deadline(
    tmp_path: Path,
) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    agent = SolL2SourceReviewAgent(
        api_key_file=None,
        base_url="https://openrouter.test/api/v1",
        harness=_FakeHarness(),  # type: ignore[arg-type]
        cache_dir=str(tmp_path / "cache"),
        audit_journal=L2AuditJournal(None, retention_days=30),
        timeout_seconds=30,
        max_steps=12,
        max_input_tokens=80_000,
        max_output_tokens=8_000,
        max_completion_tokens=2_400,
        max_cost_usd=1.5,
        cache_ttl_seconds=86_400,
        transport=httpx.MockTransport(handler),
    )
    async with httpx.AsyncClient(transport=agent._transport) as client:
        with pytest.raises(ValueError, match="lease budget"):
            await agent._post(
                client,
                "test-key",
                [],
                artifact_sha256="d" * 64,
                reasoning_effort="low",
                model="openai/gpt-5.6-sol",
                fallback_models=(),
                provider=None,
                deadline=asyncio.get_running_loop().time() + 0.1,
            )

    assert requests == 1


def test_catalog_pricing_budget_accounts_for_long_context_tier() -> None:
    assert _cost(80_000, 8_000) == pytest.approx(0.64)
    assert _cost(272_000, 8_000) == pytest.approx(3.08)
    assert _cost(272_000, 8_000, cached_input_tokens=200_000) == pytest.approx(1.28)


def test_exact_reported_cost_precedes_conservative_fallback(
    tmp_path: Path,
) -> None:
    agent = _sol_agent(
        tmp_path,
        _FakeHarness(),
        lambda _request: _response([]),
    )
    agent._require_budget(
        L2Usage(
            input_tokens=10_000,
            output_tokens=1_000,
            estimated_cost_usd=1.6,
            reported_cost_usd=1.0,
        )
    )
    with pytest.raises(ValueError, match="token or cost budget"):
        agent._require_budget(
            L2Usage(
                input_tokens=10_000,
                output_tokens=1_000,
                estimated_cost_usd=1.0,
                reported_cost_usd=1.6,
            )
        )
    with pytest.raises(ValueError, match="token or cost budget"):
        agent._require_budget(
            L2Usage(
                input_tokens=10_000,
                output_tokens=1_000,
                estimated_cost_usd=1.6,
            )
        )


def test_private_audit_retention_prunes_expired_records(tmp_path: Path) -> None:
    path = tmp_path / "private" / "audit.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"recorded_at": 0, "expired": True}) + "\n")
    journal = L2AuditJournal(str(path), retention_days=1)

    journal.record({"recorded_at": 2_000_000_000, "disposition": "safe"})

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records == [{"recorded_at": 2_000_000_000, "disposition": "safe"}]
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_private_audit_retains_recent_records_after_four_megabytes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "audit.jsonl"
    path.parent.mkdir()
    now = 2_000_000_000
    lines = [
        json.dumps({"recorded_at": now, "sequence": index, "pad": "x" * 2_200})
        for index in range(2_005)
    ]
    path.write_text("\n".join(lines) + "\n")
    assert path.stat().st_size > 4 * 1024 * 1024
    journal = L2AuditJournal(str(path), retention_days=30)

    journal.record({"recorded_at": now, "sequence": "new"})

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 2_000
    assert records[-2]["sequence"] == 2_004
    assert records[-1]["sequence"] == "new"


def test_cache_lock_excludes_another_worker_process(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    agent = SolL2SourceReviewAgent(
        api_key_file=None,
        base_url="https://openrouter.test/api/v1",
        harness=_FakeHarness(),  # type: ignore[arg-type]
        cache_dir=str(cache),
        audit_journal=L2AuditJournal(None, retention_days=30),
        timeout_seconds=30,
        max_steps=12,
        max_input_tokens=80_000,
        max_output_tokens=8_000,
        max_completion_tokens=2_400,
        max_cost_usd=1.5,
        cache_ttl_seconds=86_400,
    )
    key = "d" * 64
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,os,sys; "
                "fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600); "
                "fcntl.flock(fd,fcntl.LOCK_EX); print('ready',flush=True); "
                "sys.stdin.read()"
            ),
            str(cache / f"{key}.lock"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "ready"
    try:
        assert agent._try_lock_cache(key) is None
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=5)
    fd = agent._try_lock_cache(key)
    assert fd is not None
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


@pytest.mark.integration
async def test_real_analyzer_container_isolated_and_canonical_starter_clean(
    tmp_path: Path,
) -> None:
    starter_raw = os.environ.get("DITTO_STARTER_KIT_DIR")
    if not starter_raw:
        pytest.skip("DITTO_STARTER_KIT_DIR is required")
    starter = Path(starter_raw).resolve()
    image = "ditto-screener-l2-analyzer:test"
    build = await asyncio.create_subprocess_exec(
        "docker",
        "build",
        "-f",
        str(ROOT / "deploy/l2-analyzer.Dockerfile"),
        "-t",
        image,
        str(ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await build.communicate()
    assert build.returncode == 0, output.decode(errors="replace")[-4_000:]
    harness = IsolatedCodingHarness(docker_bin="docker", image=image)
    diff = json.loads(await harness.run(starter, "starter_diff", {}))
    assert diff["revision"] == "106076a40e4214cda821dfd0bee5c9c6785d425c"
    assert not diff["modified"]
    assert not diff["added"]
    assert not diff["removed"]
    assert len(diff["unchanged"]) == 42
    function_diff = json.loads(await harness.run(starter, "starter_function_diff", {}))
    assert function_diff["revision"] == "106076a40e4214cda821dfd0bee5c9c6785d425c"
    assert function_diff["unchanged_count"] == 103
    assert not function_diff["modified"]
    assert not function_diff["added"]
    assert not function_diff["removed"]
    assert not function_diff["truncated"]
    changed_starter = tmp_path / "changed-starter"
    shutil.copytree(starter, changed_starter, ignore=shutil.ignore_patterns(".git"))
    changed_source = changed_starter / "src/lib.rs"
    changed_source.write_text(
        changed_source.read_text()
        + "\nfn calibration_only_added_function() -> bool { true }\n"
    )
    changed_function_diff = json.loads(
        await harness.run(changed_starter, "starter_function_diff", {})
    )
    assert changed_function_diff["added_count"] == 1
    added_function = changed_function_diff["added"][0]
    assert added_function["path"] == "src/lib.rs"
    assert added_function["name"] == "calibration_only_added_function"
    assert added_function["ordinal"] == 0
    assert added_function["start_line"] == added_function["end_line"]
    assert len(added_function["sha256"]) == 64
    surfaces = json.loads(await harness.run(starter, "integrity_surfaces", {}))
    assert not surfaces["truncated"]
    assert surfaces["surfaces"]["service_entry"]["count"] > 0
    assert surfaces["surfaces"]["model_authority"]["count"] > 0
    assert "generator_construction" in surfaces["surfaces"]
    assert all(
        set(hit) == {"path", "line", "terms"}
        for surface in surfaces["surfaces"].values()
        for hit in surface["hits"]
    )
    structure = json.loads(
        await harness.run(
            starter, "rust_structure", {"path": "src/bin/dittobench-miner.rs"}
        )
    )
    assert not structure["ast_has_error"]
    assert not any(
        structure[key]
        for key in (
            "ast_truncated",
            "functions_truncated",
            "calls_truncated",
            "routes_truncated",
        )
    )
    assert any(item["name"] == "main" for item in structure["functions"])
    graph = json.loads(await harness.run(starter, "call_graph", {"entry": "main"}))
    assert not graph["unresolved"]
    assert not graph["truncated"]
    assert graph["nodes"]

    scorer_flow = tmp_path / "scorer-flow"
    scorer_flow.mkdir()
    (scorer_flow / "main.rs").write_text(
        "fn served(mut response: RunResponse, ab_score: f64) -> RunResponse {\n"
        "    response.answer = Some(model_answer());\n"
        "    response.abstain = Some(false);\n"
        "    if ab_score > 0.9 {\n"
        "        response.answer = None;\n"
        "        response.abstain = None;\n"
        "    }\n"
        "    response\n"
        "}\n"
        "fn benign(model_text: String) -> RunResponse {\n"
        "    let debug_candidate = parse(&model_text);\n"
        "    log(debug_candidate);\n"
        "    RunResponse { final_text: model_text, answer: None, abstain: None }\n"
        "}\n"
    )
    score_flows = json.loads(await harness.run(scorer_flow, "scorer_field_flow", {}))
    assert not score_flows["truncated"]
    assert score_flows["flow_count"] == 2
    assert {flow["field"] for flow in score_flows["flows"]} == {
        "answer",
        "abstain",
    }
    assert all(flow["prior_populated_lines"] for flow in score_flows["flows"])
    assert all(flow["function"] == "served" for flow in score_flows["flows"])

    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    (incomplete / "long.rs").write_text("// " + "x" * 48_100)
    long_read = json.loads(
        await harness.run(incomplete, "read_file", {"path": "long.rs"})
    )
    assert long_read["truncated"]
    (incomplete / "functions.rs").write_text(
        "\n".join(f"fn function_{index}() {{}}" for index in range(1_001))
    )
    many_functions = json.loads(
        await harness.run(incomplete, "rust_structure", {"path": "functions.rs"})
    )
    assert many_functions["functions_truncated"]
    (incomplete / "nodes.rs").write_text(
        "fn main() {\n"
        + "\n".join(f"let value_{index} = {index};" for index in range(60_000))
        + "\n}"
    )
    many_nodes = json.loads(
        await harness.run(incomplete, "rust_structure", {"path": "nodes.rs"})
    )
    assert many_nodes["ast_truncated"]
    oversized = tmp_path / "oversized"
    oversized.mkdir()
    (oversized / "large.rs").write_bytes(b"answer" + b"x" * (2 * 1024 * 1024))
    oversized_search = json.loads(
        await harness.run(oversized, "search", {"query": "answer"})
    )
    assert oversized_search["truncated"]
    assert oversized_search["omitted_count"] == 1
    oversized_index = json.loads(await harness.run(oversized, "workspace_index", {}))
    assert not oversized_index["truncated"]
    with (oversized / "huge.bin").open("wb") as huge:
        huge.truncate(20 * 1024 * 1024 + 1)
    oversized_diff = json.loads(await harness.run(oversized, "starter_diff", {}))
    assert oversized_diff["truncated"]
    assert oversized_diff["omitted_count"] == 1
    walk_bomb = tmp_path / "walk-bomb"
    walk_bomb.mkdir()
    for index in range(1_025):
        (walk_bomb / f"d{index:04}").mkdir()
    bounded_walk = json.loads(await harness.run(walk_bomb, "workspace_index", {}))
    assert bounded_walk["truncated"]

    qualified = tmp_path / "qualified"
    qualified.mkdir()
    (qualified / "main.rs").write_text(
        "mod a { pub fn handle() {} }\n"
        "mod b { pub fn handle() {} }\n"
        "fn main() { a::handle(); }\n"
    )
    qualified_graph = json.loads(
        await harness.run(qualified, "call_graph", {"entry": "main"})
    )
    reachable = {item["qualified_name"] for item in qualified_graph["nodes"]}
    assert "crate::a::handle" in reachable
    assert "crate::b::handle" not in reachable

    unqualified = tmp_path / "unqualified"
    unqualified.mkdir()
    (unqualified / "main.rs").write_text(
        "mod decoy { pub fn handle() {} }\nfn main() { handle(); }\n"
    )
    unqualified_graph = json.loads(
        await harness.run(unqualified, "call_graph", {"entry": "main"})
    )
    unqualified_reachable = {
        item["qualified_name"] for item in unqualified_graph["nodes"]
    }
    assert "crate::decoy::handle" not in unqualified_reachable
    assert unqualified_graph["unresolved_count"] >= 1

    # sandbox_probe is intentionally unavailable to the model-facing harness.
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--mount",
        f"type=bind,src={starter},dst=/workspace,readonly",
        "--tmpfs",
        "/scratch:rw,noexec,nosuid,nodev,size=33554432,mode=1777",
        image,
        "sandbox_probe",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(b"{}")
    assert proc.returncode == 0, stderr.decode(errors="replace")
    probe = json.loads(stdout)
    assert probe == {
        "cloud_paths": False,
        "docker_socket": False,
        "egress": False,
        "gid": os.getgid(),
        "scratch_writable": True,
        "uid": os.getuid(),
        "workspace_writable": False,
    }

    cleanup = await asyncio.create_subprocess_exec("docker", "rmi", "-f", image)
    await cleanup.wait()
