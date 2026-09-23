from __future__ import annotations

import importlib.util
import json
from argparse import Namespace
from pathlib import Path
from typing import Any

import httpx
import pytest

from ditto_screener.policy import SourceReviewObservation
from ditto_screener.source_review import OpenRouterSourceReviewAgent
from ditto_screening_protocol.models import (
    SourceReviewFinding,
    source_review_invariants_for_policy,
)


def _calibration_module() -> Any:
    script = Path(__file__).parents[1] / "scripts" / "run_single_sol_calibration.py"
    spec = importlib.util.spec_from_file_location("run_single_sol_calibration", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reviewer() -> Any:
    return _calibration_module().MeteredSingleSolReviewer(
        api_key_file=None,
        model="openai/gpt-5.6-sol",
        base_url="https://openrouter.ai/api/v1",
        timeout_seconds=1,
        max_steps=1,
    )


@pytest.mark.asyncio
async def test_single_sol_usage_counts_reported_cost_and_unmetered_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = iter(
        [
            httpx.Response(
                200,
                json={
                    "model": "openai/gpt-5.6-sol",
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 3,
                        "cost": 0.1,
                    },
                },
            ),
            httpx.Response(200, json={"model": "openai/gpt-5.6-sol"}),
        ]
    )

    async def fake_post(*_args: object, **_kwargs: object) -> httpx.Response:
        return next(replies)

    monkeypatch.setattr(OpenRouterSourceReviewAgent, "_post_completion", fake_post)
    reviewer = _reviewer()
    async with httpx.AsyncClient() as client:
        for _ in range(2):
            await reviewer._post_completion(
                client, "placeholder", [], reasoning_effort="high"
            )

    assert reviewer.usage == {
        "input_tokens": 12,
        "cached_input_tokens": 0,
        "output_tokens": 3,
        "reasoning_tokens": 0,
        "reported_cost_usd": 0.1,
        "requests": 2,
        "responses": 2,
        "request_failures": 0,
        "unmetered_responses": 1,
    }


@pytest.mark.asyncio
async def test_single_sol_usage_counts_failed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_post(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ReadTimeout("test timeout")

    monkeypatch.setattr(OpenRouterSourceReviewAgent, "_post_completion", fake_post)
    reviewer = _reviewer()
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ReadTimeout):
            await reviewer._post_completion(
                client, "placeholder", [], reasoning_effort="high"
            )

    assert reviewer.usage["requests"] == 1
    assert reviewer.usage["responses"] == 0
    assert reviewer.usage["request_failures"] == 1


def test_invariant_assessment_exports_only_bounded_decisions() -> None:
    finding = {
        "artifact_sha256": "a" * 64,
        "prompt_revision": "source-review-v13",
        "risk_level": "low",
        "confidence": 0.8,
        "categories": ["none"],
        "evidence": [{"path": "src/main.py", "line": 42, "category": "none"}],
        "summary": "No causal policy breach established.",
        "invariant_assessment": {
            "schema_version": 2,
            "decisions": [
                {
                    "invariant": invariant.value,
                    "disposition": "breach" if index == 0 else "inconclusive",
                    "summary": "Not established in this fixture.",
                    "evidence_indices": [0] if index == 0 else [],
                }
                for index, invariant in enumerate(
                    source_review_invariants_for_policy(13)
                )
            ],
        },
        "unexpected_secret": "must not be exported",
    }

    result = _calibration_module()._sanitized_invariant_assessment(finding)

    assert result is not None
    assert result["schema_version"] == 2
    assert len(result["decisions"]) == 8
    assert result["decisions"][0]["disposition"] == "breach"
    assert result["decisions"][0]["evidence"] == [
        {"path": "src/main.py", "line": 42, "category": "none"}
    ]
    assert "summary" not in result["decisions"][0]
    assert len(result["decisions"][0]["summary_sha256"]) == 64
    assert all(
        decision["disposition"] == "inconclusive"
        for decision in result["decisions"][1:]
    )
    assert "must not be exported" not in str(result)
    assert _calibration_module()._sanitized_invariant_assessment(None) is None


def _identity() -> dict[str, object]:
    return {
        "agent_id": "11111111-1111-4111-8111-111111111111",
        "attempt_id": "22222222-2222-4222-8222-222222222222",
        "artifact_sha256": "a" * 64,
        "policy_version": 13,
        "manifest_digest": "b" * 64,
        "review_settings_revision": 119,
    }


def test_single_sol_handoff_matches_l4_contract_and_fails_closed_on_metering() -> None:
    module = _calibration_module()
    identity = _identity()
    finding = SourceReviewFinding.model_validate(
        {
            "artifact_sha256": identity["artifact_sha256"],
            "prompt_revision": "source-review-v26-policy-v13",
            "risk_level": "low",
            "confidence": 0.8,
            "categories": ["none"],
            "evidence": [{"path": "src/main.py", "line": 42, "category": "none"}],
            "summary": "No causal policy breach established.",
        }
    )
    observation = SourceReviewObservation(
        ok=True,
        risk_level="low",
        finding_digest=finding.canonical_digest(),
        categories=("none",),
        finding=finding.model_dump(mode="json", exclude_none=True),
        notes=(
            {
                "kind": "cleared",
                "category": "none",
                "summary": "Served path checked",
                "stage": "l1",
                "path": "src/main.py",
                "line": 42,
            },
        ),
    )
    reviewer = _reviewer()
    reviewer._compaction_count = 0
    reviewer.response_models.add(module.MODEL)
    reviewer.usage.update(requests=1, responses=1, reported_cost_usd=0.25)
    exported = module._sol_handoff(identity, observation, reviewer, 1234)

    assert exported["agent_id"] == identity["agent_id"]
    assert exported["attempt_id"] == identity["attempt_id"]
    assert exported["review_settings_revision"] == 119
    assert exported["reported_cost_usd"] == 0.25
    assert exported["notes_payload_sha256"] == module._canonical_json_sha256(
        exported["notes"]
    )
    digest = SourceReviewFinding.model_validate(exported["finding"]).canonical_digest()
    assert digest == exported["finding_digest"]
    assert "src/main.py" in json.dumps(exported)
    assert "source_file" not in exported
    assert "prompt_messages" not in exported

    reviewer.usage["unmetered_responses"] = 1
    assert (
        module._sol_handoff(identity, observation, reviewer, 1234)["reported_cost_usd"]
        is None
    )
    reviewer.usage["unmetered_responses"] = 0
    reviewer.response_models.add("unexpected/model")
    assert (
        module._sol_handoff(identity, observation, reviewer, 1234)["reported_cost_usd"]
        is None
    )


def test_single_sol_handoff_rejects_wrong_finding_and_missing_identity() -> None:
    module = _calibration_module()
    identity = _identity()
    assert module._handoff_identity(identity, 13) == identity
    for field, bad in (
        ("agent_id", "not-a-uuid"),
        ("attempt_id", "not-a-uuid"),
        ("artifact_sha256", "a" * 63),
        ("manifest_digest", "b" * 63),
        ("review_settings_revision", True),
    ):
        with pytest.raises(ValueError):
            module._handoff_identity({**identity, field: bad}, 13)

    observation = SourceReviewObservation(
        ok=True,
        risk_level="low",
        finding_digest="f" * 64,
        categories=("none",),
        finding={
            "artifact_sha256": "a" * 64,
            "prompt_revision": "source-review-v26-policy-v13",
            "risk_level": "low",
            "confidence": 0.8,
            "categories": ["none"],
            "summary": "No causal policy breach established.",
        },
    )
    with pytest.raises(ValueError, match="finding digest mismatch"):
        module._sol_handoff(identity, observation, _reviewer(), 1)


def test_private_writer_does_not_chmod_shared_parent(tmp_path: Path) -> None:
    module = _calibration_module()
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(ValueError, match="mode 0700"):
        module._write_private_json(shared / "handoff.json", {"private": True})
    assert shared.stat().st_mode & 0o777 == 0o755
    assert not (shared / "handoff.json").exists()

    private = tmp_path / "private"
    module._write_private_json(private / "handoff.json", {"private": True})
    assert private.stat().st_mode & 0o777 == 0o700
    assert (private / "handoff.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_handoff_identity_preflight_precedes_any_model_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _calibration_module()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "revision": "corpus-v1",
                "items": [
                    {
                        **_identity(),
                        "attempt_id": "not-a-uuid",
                        "archive": "artifact.tar.gz",
                        "expected_disposition": "safe",
                    }
                ],
            }
        )
    )
    args = Namespace(
        manifest=manifest,
        artifact_root=tmp_path / "artifacts",
        api_key_file=tmp_path / "absent-key",
        results_file=tmp_path / "results.json",
        handoff_file=tmp_path / "handoff.json",
        concurrency=1,
        policy_version=13,
        artifact_sha256s=None,
    )
    monkeypatch.setattr(module, "_arguments", lambda: args)
    with pytest.raises(ValueError, match="attempt_id must be a UUID"):
        await module._main()
    assert not args.results_file.exists()
    assert not args.handoff_file.exists()


@pytest.mark.asyncio
async def test_all_handoff_archives_are_checked_before_model_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _calibration_module()
    root = tmp_path / "artifacts"
    root.mkdir()
    good = root / "good.tar.gz"
    good.write_bytes(b"first exact artifact")
    bad = root / "bad.tar.gz"
    bad.write_bytes(b"second wrong artifact")
    identity = _identity()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "revision": "corpus-v1",
                "items": [
                    {
                        **identity,
                        "archive": good.name,
                        "artifact_sha256": module._sha256(good),
                        "expected_disposition": "safe",
                    },
                    {
                        **identity,
                        "attempt_id": "33333333-3333-4333-8333-333333333333",
                        "archive": bad.name,
                        "artifact_sha256": "c" * 64,
                        "expected_disposition": "violation",
                    },
                ],
            }
        )
    )
    args = Namespace(
        manifest=manifest,
        artifact_root=root,
        api_key_file=tmp_path / "absent-key",
        results_file=tmp_path / "results.json",
        handoff_file=tmp_path / "handoff.json",
        concurrency=1,
        policy_version=13,
        artifact_sha256s=None,
    )
    monkeypatch.setattr(module, "_arguments", lambda: args)
    with pytest.raises(ValueError, match="artifact digest mismatch"):
        await module._main()
    assert not args.results_file.exists()
    assert not args.handoff_file.exists()
