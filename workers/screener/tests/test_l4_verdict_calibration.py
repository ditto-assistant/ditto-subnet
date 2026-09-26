"""Report-only paired L4 replay guards and coverage accounting."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ditto_screening_protocol.models import (
    AdjudicationCompletionReceipt,
    AdjudicationRequestAttemptDiagnostic,
    AdjudicationRunDiagnostic,
)
from scripts import run_l4_verdict_calibration as replay
from scripts.run_l4_verdict_calibration import (
    _execute,
    _load_manifest,
    _Meter,
    _summary,
    _write_private_json,
)


def _manifest(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "artifacts"
    root.mkdir()
    archive = root / "agent.tar.gz"
    archive.write_bytes(b"private exact artifact")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    notes = [{"kind": "concern", "path": "src/main.rs", "line": 5}]
    notes_digest = hashlib.sha256(
        json.dumps(notes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {
        "revision": "independent-v13-test-v1",
        "policy_version": 13,
        "cases": [
            {
                "agent_id": "1914239c-9279-4a71-9fe4-0dd676063c33",
                "attempt_id": "77274c4d-ed6f-4181-aee5-47f1f2f6696a",
                "artifact_sha256": digest,
                "manifest_digest": "a" * 64,
                "review_settings_revision": 119,
                "baseline_worker_release": "v0.295.0",
                "baseline_prompt_revisions": {
                    "l1": "source-review-v21-policy-v13",
                    "l2": "l2-review-v1-policy-v13",
                    "l3": "l3-review-v1-policy-v13",
                    "l4": "adjudicator-v7-policy-v13",
                },
                "review_notes_digest": "b" * 64,
                "notes_payload_sha256": notes_digest,
                "policy_version": 13,
                "archive": "agent.tar.gz",
                "notes": notes,
                "finding": None,
                "error_code": "source-review-inconclusive",
                "cohort": "safe-harbor",
                "label": {
                    "decision": "clear",
                    "provenance": "independent-v13-source-review",
                    "evidence_references": ["src/main.rs:5"],
                },
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path, root


def test_manifest_requires_exact_archive_and_independent_label(tmp_path: Path) -> None:
    path, root = _manifest(tmp_path)
    _, cases = _load_manifest(path, root)
    assert len(cases) == 1
    assert cases[0]["cohort"] == "safe-harbor"

    manifest = json.loads(path.read_text())
    manifest["cases"][0]["label"]["provenance"] = "automated-rescreen"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="independent-v13"):
        _load_manifest(path, root)

    manifest["cases"][0]["label"]["provenance"] = "independent-v13-source-review"
    manifest["cases"][0]["artifact_sha256"] = "0" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="artifact digest mismatch"):
        _load_manifest(path, root)

    manifest["cases"][0]["artifact_sha256"] = hashlib.sha256(
        (root / "agent.tar.gz").read_bytes()
    ).hexdigest()
    manifest["cases"][0]["baseline_prompt_revisions"].pop("l2")
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="exact L1-L4 baseline prompt"):
        _load_manifest(path, root)


def test_meter_stops_on_missing_cost_or_wrong_model() -> None:
    spent = [0.0]
    meter = _Meter("openai/gpt-5.6-sol", 1.0, spent)
    with pytest.raises(ValueError, match="exact metering"):
        meter.observe(
            {
                "model": "openai/gpt-5.6-sol",
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "cost_usd": None,
            }
        )
    assert meter.error == "unmetered-or-route-mismatch"
    assert spent == [0.0]

    other = _Meter("openai/gpt-5.6-sol", 1.0, spent)
    with pytest.raises(ValueError, match="exact metering"):
        other.observe(
            {
                "model": "different-model",
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "cost_usd": 0.1,
            }
        )
    negative = _Meter("openai/gpt-5.6-sol", 1.0, spent)
    with pytest.raises(ValueError, match="exact metering"):
        negative.observe(
            {
                "model": "openai/gpt-5.6-sol",
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "cost_usd": -0.1,
            }
        )


def test_summary_excludes_incomplete_cases_from_accuracy() -> None:
    rows = [
        {
            "agent_id": "a",
            "attempt_id": "1",
            "requested_model": "z-ai/glm-5.3-flash",
            "complete": True,
            "label_match": True,
            "label_decision": "reject",
            "decision": "reject",
            "reject_invariant_match": True,
            "reported_cost_usd": 0.1,
            "reported_cost_lower_bound": False,
            "baseline_worker_release": "v0.295.0",
            "baseline_prompt_revisions": {"l1": "a", "l2": "b", "l3": "c", "l4": "d"},
        },
        {
            "agent_id": "a",
            "attempt_id": "1",
            "requested_model": "openai/gpt-5.6-sol",
            "complete": False,
            "label_match": None,
            "label_decision": "reject",
            "decision": "escalate",
            "reject_invariant_match": None,
            "reported_cost_usd": 0.2,
            "reported_cost_lower_bound": True,
            "baseline_worker_release": "v0.295.0",
            "baseline_prompt_revisions": {"l1": "a", "l2": "b", "l3": "c", "l4": "d"},
        },
    ]
    summary = _summary(rows)
    assert summary["fully_paired_cases"] == 0
    assert summary["models"]["openai/gpt-5.6-sol"]["completed"] == 0
    assert summary["models"]["openai/gpt-5.6-sol"]["incomplete"] == 1
    assert summary["mixed_baseline_releases"] is False
    rows[1]["baseline_worker_release"] = "v0.294.0"
    assert _summary(rows)["mixed_baseline_releases"] is True


def test_private_result_writer_does_not_chmod_an_existing_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(ValueError, match="results directory must be private"):
        _write_private_json(directory / "results.json", {"items": []})
    assert directory.stat().st_mode & 0o777 == 0o755


async def test_timeout_arm_is_recorded_before_paired_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, root = _manifest(tmp_path)
    manifest, cases = _load_manifest(manifest_path, root)
    key = tmp_path / "dedicated-key"
    key.write_text("test-only")
    results_dir = tmp_path / "private-results"
    results_dir.mkdir(mode=0o700)
    results_dir.chmod(0o700)
    results_file = results_dir / "results.json"
    invoked: list[str] = []

    class FakeCourt:
        def __init__(self, **kwargs: object) -> None:
            self.model = str(kwargs["model"])
            self.observe = kwargs["completion_observer"]

        async def adjudicate(self, *_args: object, **_kwargs: object) -> object:
            invoked.append(self.model)
            if self.model == replay.MODELS[0]:
                raise TimeoutError("private provider text must not be persisted")
            self.observe(
                {
                    "model": self.model,
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "cost_usd": 0.01,
                    "upstream": "test-upstream",
                }
            )
            return SimpleNamespace(
                decision="clear",
                reject_invariant=None,
                clear_clause=SimpleNamespace(value="genuine_model_result"),
                citations=[],
                reason="independent safe conclusion",
                escalation_code=None,
                run_diagnostic=None,
            )

    monkeypatch.setattr(replay, "SourceReviewAdjudicator", FakeCourt)
    args = argparse.Namespace(
        api_key_file=key,
        base_url="https://openrouter.ai/api/v1",
        max_reported_cost_usd=1.0,
        external_route_cap_usd=2.0,
        results_file=results_file,
    )
    report = await _execute(args, manifest, cases)
    assert invoked == list(replay.MODELS)
    first, second = report["items"]
    assert first["complete"] is False
    assert first["error_class"] == "TimeoutError"
    assert first["error_code"] == "call-timeout"
    assert first["reported_cost_lower_bound"] is True
    assert second["complete"] is True
    assert first["run_diagnostic"] is None
    assert second["completion_receipt"] is None
    assert report["summary"]["fully_paired_cases"] == 0
    assert "private provider text" not in results_file.read_text()


async def test_report_preserves_bounded_escalation_and_completion_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, root = _manifest(tmp_path)
    manifest, cases = _load_manifest(manifest_path, root)
    key = tmp_path / "dedicated-key"
    key.write_text("test-only")
    results_dir = tmp_path / "private-results"
    results_dir.mkdir(mode=0o700)
    results_dir.chmod(0o700)
    results_file = results_dir / "results.json"

    class FakeCourt:
        def __init__(self, **kwargs: object) -> None:
            self.model = str(kwargs["model"])
            self.observe = kwargs["completion_observer"]

        async def adjudicate(self, *_args: object, **_kwargs: object) -> object:
            if self.model == replay.MODELS[0]:
                return SimpleNamespace(
                    decision="escalate",
                    reject_invariant=None,
                    clear_clause=None,
                    citations=[],
                    reason="private model text must not be persisted",
                    escalation_code="adjudicator-failed",
                    run_diagnostic=AdjudicationRunDiagnostic(
                        error_class="TimeoutError",
                        failure_code="stream-no-tool-progress",
                        elapsed_ms=180_000,
                        model=self.model,
                        provider="openrouter",
                        request_count=1,
                        request_attempts=[
                            AdjudicationRequestAttemptDiagnostic(
                                ordinal=1,
                                started_ms=0,
                                elapsed_ms=180_000,
                                stage="event",
                                stream_requested=True,
                                prompt_bytes=1234,
                                http_status=200,
                                first_byte_ms=200,
                                first_event_ms=250,
                                event_count=14,
                                wire_bytes=4096,
                                upstream="provider-a",
                            )
                        ],
                    ),
                    completion_receipt=None,
                )
            self.observe(
                {
                    "model": self.model,
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "cost_usd": 0.01,
                    "upstream": "provider-b",
                }
            )
            return SimpleNamespace(
                decision="clear",
                reject_invariant=None,
                clear_clause=SimpleNamespace(value="genuine_model_result"),
                citations=[],
                reason="private successful verdict text",
                escalation_code=None,
                run_diagnostic=None,
                completion_receipt=AdjudicationCompletionReceipt(
                    elapsed_ms=9_000,
                    first_tool_call_ms=8_000,
                    first_tool_observation="stream_delta",
                    observed_model=self.model,
                    gateway_provider="openrouter",
                    observed_upstream="provider-b",
                    request_count=2,
                    final_request_prompt_bytes=2222,
                    final_request_wire_bytes=3333,
                    final_request_event_count=4,
                    prompt_tokens=10,
                    completion_tokens=5,
                ),
            )

    monkeypatch.setattr(replay, "SourceReviewAdjudicator", FakeCourt)
    args = argparse.Namespace(
        api_key_file=key,
        base_url="https://openrouter.ai/api/v1",
        max_reported_cost_usd=1.0,
        external_route_cap_usd=2.0,
        results_file=results_file,
    )
    report = await _execute(args, manifest, cases)
    failed, completed = report["items"]
    assert failed["complete"] is False
    assert failed["run_diagnostic"]["request_attempts"][0]["wire_bytes"] == 4096
    assert failed["run_diagnostic"]["request_attempts"][0]["event_count"] == 14
    assert failed["run_diagnostic"]["request_attempts"][0]["upstream"] == "provider-a"
    assert failed["completion_receipt"] is None
    assert completed["complete"] is True
    assert completed["run_diagnostic"] is None
    assert completed["completion_receipt"]["first_tool_call_ms"] == 8_000
    assert completed["completion_receipt"]["observed_upstream"] == "provider-b"
    assert completed["completion_receipt"]["final_request_wire_bytes"] == 3333
    assert completed["completion_receipt"]["final_request_event_count"] == 4
    saved = results_file.read_text()
    assert json.loads(saved)["items"] == report["items"]
    assert "private model text" not in saved
    assert "private successful verdict text" not in saved


async def test_route_mismatch_persists_then_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, root = _manifest(tmp_path)
    manifest, cases = _load_manifest(manifest_path, root)
    key = tmp_path / "dedicated-key"
    key.write_text("test-only")
    results_dir = tmp_path / "private-results"
    results_dir.mkdir(mode=0o700)
    results_dir.chmod(0o700)
    results_file = results_dir / "results.json"
    invoked: list[str] = []

    class WrongRouteCourt:
        def __init__(self, **kwargs: object) -> None:
            self.model = str(kwargs["model"])
            self.observe = kwargs["completion_observer"]

        async def adjudicate(self, *_args: object, **_kwargs: object) -> object:
            invoked.append(self.model)
            self.observe(
                {
                    "model": "other-model",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "cost_usd": 0.01,
                }
            )
            raise AssertionError("unreachable")

    monkeypatch.setattr(replay, "SourceReviewAdjudicator", WrongRouteCourt)
    args = argparse.Namespace(
        api_key_file=key,
        base_url="https://openrouter.ai/api/v1",
        max_reported_cost_usd=1.0,
        external_route_cap_usd=2.0,
        results_file=results_file,
    )
    with pytest.raises(ValueError, match="unmetered-or-route-mismatch"):
        await _execute(args, manifest, cases)
    assert invoked == [replay.MODELS[0]]
    report = json.loads(results_file.read_text())
    assert report["items"][0]["complete"] is False
    assert report["items"][0]["meter_error"] == "unmetered-or-route-mismatch"


def _add_sol_handoff(path: Path, *, host_evidence: bool) -> None:
    manifest = json.loads(path.read_text())
    case = manifest["cases"][0]
    notes = [{"kind": "concern", "path": "src/sol.rs", "line": 9}]
    case["sol_investigator"] = {
        "agent_id": case["agent_id"],
        "attempt_id": case["attempt_id"],
        "artifact_sha256": case["artifact_sha256"],
        "policy_version": case["policy_version"],
        "manifest_digest": case["manifest_digest"],
        "review_settings_revision": case["review_settings_revision"],
        "model": "openai/gpt-5.6-sol",
        "prompt_revision": "single-sol-v1-policy-v13",
        "notes": notes,
        "notes_payload_sha256": hashlib.sha256(
            json.dumps(notes, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "finding": None,
        "finding_digest": None,
        "error_code": "source-review-inconclusive",
        "latency_ms": 100,
        "reported_cost_usd": 0.02,
        "compactions": 1,
    }
    if host_evidence:
        case["mandatory_host_evidence"] = {
            "agent_id": case["agent_id"],
            "attempt_id": case["attempt_id"],
            "artifact_sha256": case["artifact_sha256"],
            "image_identity_digest": "c" * 64,
            "runtime_receipt_digest": "d" * 64,
            "private_verification_or_nonapplicability_digest": "e" * 64,
            "i1_i8_s1_s3_sweep_digest": "f" * 64,
        }
    path.write_text(json.dumps(manifest))


async def test_two_layer_missing_host_evidence_is_incomplete_without_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, root = _manifest(tmp_path)
    _add_sol_handoff(manifest_path, host_evidence=False)
    manifest, cases = _load_manifest(manifest_path, root)
    key = tmp_path / "dedicated-key"
    key.write_text("test-only")
    results_dir = tmp_path / "private-results"
    results_dir.mkdir(mode=0o700)
    results_dir.chmod(0o700)

    def unexpected_court(**_kwargs: object) -> object:
        pytest.fail("missing mandatory host evidence must not spend model tokens")

    monkeypatch.setattr(replay, "SourceReviewAdjudicator", unexpected_court)
    args = argparse.Namespace(
        api_key_file=key,
        base_url="https://openrouter.ai/api/v1",
        max_reported_cost_usd=1.0,
        external_route_cap_usd=2.0,
        results_file=results_dir / "results.json",
        two_layer=True,
    )
    report = await _execute(args, manifest, cases)
    assert len(report["items"]) == 1
    row = report["items"][0]
    assert row["complete"] is False
    assert row["error_code"] == "mandatory-host-evidence-missing"
    assert "runtime_receipt_digest" in row["missing_host_evidence_fields"]
    assert row["policy_ready"] is False


async def test_two_layer_uses_sol_handoff_for_model_diverse_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, root = _manifest(tmp_path)
    _add_sol_handoff(manifest_path, host_evidence=True)
    manifest, cases = _load_manifest(manifest_path, root)
    key = tmp_path / "dedicated-key"
    key.write_text("test-only")
    results_dir = tmp_path / "private-results"
    results_dir.mkdir(mode=0o700)
    results_dir.chmod(0o700)
    seen: list[object] = []

    class FakeCourt:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["model"] == "z-ai/glm-5.3-flash"
            self.observe = kwargs["completion_observer"]

        async def adjudicate(self, _archive: str, **kwargs: object) -> object:
            seen.append(kwargs["notes"])
            assert kwargs["finding"] is None
            assert kwargs["error_code"] == "source-review-inconclusive"
            assert kwargs["ledger_final"] is True
            self.observe(
                {
                    "model": "z-ai/glm-5.3-flash",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "cost_usd": 0.01,
                }
            )
            return SimpleNamespace(
                decision="clear",
                reject_invariant=None,
                clear_clause=SimpleNamespace(value="genuine_model_result"),
                citations=[],
                reason="safe",
                escalation_code=None,
                run_diagnostic=None,
            )

    monkeypatch.setattr(replay, "SourceReviewAdjudicator", FakeCourt)
    args = argparse.Namespace(
        api_key_file=key,
        base_url="https://openrouter.ai/api/v1",
        max_reported_cost_usd=1.0,
        external_route_cap_usd=2.0,
        results_file=results_dir / "results.json",
        two_layer=True,
    )
    report = await _execute(args, manifest, cases)
    assert seen == [cases[0]["sol_investigator"]["notes"]]
    row = report["items"][0]
    assert row["complete"] is True
    assert row["host_evidence_status"] == "references_supplied_unverified"
    assert row["host_evidence_reference_digests"]["runtime_receipt_digest"] == (
        "d" * 64
    )
    assert row["policy_ready"] is False
    assert row["investigator_compactions"] == 1
    assert row["latency_ms"] >= 100
    assert row["reported_cost_usd"] == 0.03
