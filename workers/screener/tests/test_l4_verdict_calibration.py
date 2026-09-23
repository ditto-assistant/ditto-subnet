"""Report-only paired L4 replay guards and coverage accounting."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    assert report["summary"]["fully_paired_cases"] == 0
    assert "private provider text" not in results_file.read_text()


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
