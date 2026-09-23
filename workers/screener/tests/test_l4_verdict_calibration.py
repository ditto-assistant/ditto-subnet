"""Report-only paired L4 replay guards and coverage accounting."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.run_l4_verdict_calibration import (
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
        },
    ]
    summary = _summary(rows)
    assert summary["fully_paired_cases"] == 0
    assert summary["models"]["openai/gpt-5.6-sol"]["completed"] == 0
    assert summary["models"]["openai/gpt-5.6-sol"]["incomplete"] == 1


def test_private_result_writer_does_not_chmod_an_existing_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(ValueError, match="results directory must be private"):
        _write_private_json(directory / "results.json", {"items": []})
    assert directory.stat().st_mode & 0o777 == 0o755
