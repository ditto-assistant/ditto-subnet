"""Admission timing preserves owner iteration and never creates a copy verdict."""

import tarfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID, uuid4

import pytest

from ditto.api_models.submission_attempts import AttemptControlSettings
from ditto.api_server.source_inspect import SourceInspectError
from ditto.api_server.submission_attempts import (
    PriorAttempt,
    artifact_profile,
    decide_attempt,
    settings_digest,
)

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
ROOT = uuid4()


def archive(files: dict[str, bytes], *, mtime: int = 0) -> bytes:
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for path, contents in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(contents)
            info.mtime = mtime
            tar.addfile(info, BytesIO(contents))
    return stream.getvalue()


def profile(
    *, runtime: str = "a", packaging: str = "b", start: int = 0, corpus: str = "one"
) -> dict:
    return {
        "version": 1,
        "sha256": "f" * 64,
        "runtime_hash": runtime * 64,
        "packaging_hash": packaging * 64,
        "runtime_files": 1,
        "fingerprint": {
            "v": 2,
            "corpus": corpus,
            "k": 256,
            "card": 100,
            "m": [f"{index:016x}" for index in range(start, start + 100)],
        },
    }


def prior(
    *,
    index: int = 0,
    outcome: str = "completed",
    kind: str = "small_source_delta",
    fast: bool = False,
    lineage: UUID = ROOT,
    source: dict | None = None,
) -> PriorAttempt:
    return PriorAttempt(
        agent_id=ROOT if index == 0 else uuid4(),
        lineage_agent_id=lineage,
        profile=source or profile(),
        classification=kind,
        submitted_at=NOW - timedelta(minutes=20 - index),
        feedback_at=NOW - timedelta(minutes=10 - index)
        if outcome != "pending"
        else None,
        outcome=outcome,
        reason_code="docker-build" if outcome == "repairable" else None,
        fast_repair=fast,
    )


def decide(source: dict | None, history: list[PriorAttempt], **settings):
    return decide_attempt(
        source,
        history,
        settings=AttemptControlSettings(**settings),
        revision=4,
        now=NOW,
    )


def test_repack_and_source_rename_keep_runtime_identity() -> None:
    original = {
        "Dockerfile": b"FROM scratch\n",
        "src/main.rs": b"fn main() {}\n",
        "prompt.txt": b"reason carefully",
    }
    repack = artifact_profile(archive(original, mtime=123))
    first = artifact_profile(archive(original))
    renamed = artifact_profile(
        archive(
            {
                "Dockerfile": original["Dockerfile"],
                "engine.rs": original["src/main.rs"],
                "prompt.txt": original["prompt.txt"],
            }
        )
    )
    assert first["sha256"] != repack["sha256"]
    assert first["runtime_hash"] == repack["runtime_hash"] == renamed["runtime_hash"]


def test_prompts_binaries_and_manifests_are_runtime_inputs() -> None:
    base = {
        "Dockerfile": b"FROM scratch",
        "Cargo.toml": b"[package]\nname='agent'",
        "prompt.txt": b"one",
        "weights.bin": b"\x00\x01",
    }
    before = artifact_profile(archive(base))
    for path in ("Cargo.toml", "prompt.txt", "weights.bin"):
        changed = artifact_profile(archive({**base, path: base[path] + b"new"}))
        assert changed["runtime_hash"] != before["runtime_hash"]
    docker = artifact_profile(archive({**base, "Dockerfile": b"FROM alpine"}))
    assert docker["runtime_hash"] == before["runtime_hash"]
    assert docker["packaging_hash"] != before["packaging_hash"]


def test_unsafe_archive_never_becomes_comparison_evidence() -> None:
    with pytest.raises(SourceInspectError):
        artifact_profile(archive({"Dockerfile": b"FROM scratch", "../evil": b"bad"}))


def test_known_build_repair_has_a_bounded_fast_retry() -> None:
    result = decide(
        profile(packaging="c"),
        [prior(outcome="repairable", kind="first_submission")],
        mode="enforce",
    )
    assert result.classification == "packaging_only_repair"
    assert result.fast_repair is True
    assert result.fast_repairs_remaining == 1
    assert result.retry_at is None


def test_repair_allowance_does_not_reset_after_each_failed_build() -> None:
    history = [
        prior(kind="first_submission", outcome="repairable"),
        prior(index=1, outcome="repairable", fast=True),
        prior(index=2, outcome="repairable", fast=True),
    ]
    result = decide(profile(packaging="c"), history)
    assert result.fast_repair is False
    assert result.fast_repairs_remaining == 0


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_infrastructure_retry_burns_no_budget_even_after_exhaustion(mode: str) -> None:
    history = [prior(index=index) for index in range(3)]
    history.append(prior(index=3, outcome="infrastructure"))
    result = decide(profile(packaging="c"), history, mode=mode)
    assert result.classification == "infrastructure_retry"
    assert result.retry_at is None
    assert result.completed_low_information_attempts == 3


def test_platform_failure_releases_tentative_attempt_capacity() -> None:
    completed = prior(kind="first_submission")
    pending = [prior(index=index, outcome="pending") for index in range(1, 4)]
    blocked = decide(profile(), [completed, *pending])
    assert blocked.retry_at is not None
    failed = [
        replace(item, outcome="infrastructure", feedback_at=NOW - timedelta(minutes=1))
        for item in pending
    ]
    recovered = decide(profile(), [completed, *failed])
    assert recovered.retry_at is None
    assert recovered.completed_low_information_attempts == 0
    assert recovered.reserved_low_information_attempts == 0


def test_frequency_without_completed_feedback_does_not_establish_throttle() -> None:
    result = decide(
        profile(), [prior(index=index, outcome="pending") for index in range(8)]
    )
    assert result.retry_at is None
    assert result.completed_low_information_attempts == 0


def test_completed_low_information_feedback_establishes_retry_time() -> None:
    history = [prior(index=index) for index in range(3)]
    result = decide(profile(), history)
    feedback = history[-1].feedback_at
    assert feedback is not None
    assert result.retry_at == feedback + timedelta(hours=1)
    assert result.completed_low_information_attempts == 3
    assert result.reference_agent_id == history[-1].agent_id


def test_changed_opaque_runtime_does_not_inherit_a_lexical_budget() -> None:
    previous = profile()
    previous["opaque_hash"] = "a" * 64
    changed = profile(runtime="b")
    changed["opaque_hash"] = "b" * 64
    result = decide(changed, [prior(index=i, source=previous) for i in range(3)])
    assert result.classification == "inconclusive"
    assert result.retry_at is None


def test_docker_only_programs_do_not_have_proven_runtime_equality() -> None:
    original = profile()
    original["runtime_files"] = 0
    changed = profile(start=4, packaging="c")
    changed["runtime_files"] = 0
    result = decide(changed, [prior(source=original)])
    assert result.classification == "material_new_work"
    assert result.lineage_agent_id is None


def test_pending_attempt_reserves_capacity_after_clear_feedback() -> None:
    result = decide(
        profile(), [prior(), prior(index=1), prior(index=2, outcome="pending")]
    )
    assert result.retry_at is not None
    assert result.completed_low_information_attempts == 2
    assert result.reserved_low_information_attempts == 1


def test_material_changes_receive_fresh_lineage_without_old_budget() -> None:
    history = [prior(index=index) for index in range(5)]
    result = decide(profile(runtime="c", start=60), history)
    assert result.classification == "material_new_work"
    assert result.lineage_agent_id is None
    assert result.retry_at is None


def test_accumulated_small_edits_are_compared_with_exact_lineage_anchor() -> None:
    anchor = prior(kind="first_submission", source=profile(runtime="a", start=0))
    predecessor = prior(index=1, source=profile(runtime="b", start=5))
    result = decide(profile(runtime="c", start=5), [anchor, predecessor])
    assert result.classification == "material_new_work"
    assert result.lineage_agent_id is None


def test_incompatible_or_missing_fingerprints_fail_open() -> None:
    changed = profile(runtime="c", corpus="new-generation")
    result = decide(changed, [prior()])
    assert result.classification == "inconclusive"
    assert result.retry_at is None
    assert decide(None, [prior()]).classification == "inconclusive"


def test_expired_budget_window_does_not_erase_lineage_identity() -> None:
    old = [
        replace(
            prior(index=index),
            submitted_at=NOW - timedelta(days=2),
            feedback_at=NOW - timedelta(days=1, minutes=1),
        )
        for index in range(3)
    ]
    result = decide(profile(), old)
    assert result.lineage_agent_id == ROOT
    assert result.completed_low_information_attempts == 0
    assert result.retry_at is None


def test_calibration_digest_ignores_mode_but_binds_every_tuning_parameter() -> None:
    default = AttemptControlSettings()
    assert settings_digest(default) == settings_digest(
        default.model_copy(update={"mode": "enforce"})
    )
    assert settings_digest(default) != settings_digest(
        default.model_copy(update={"low_information_limit": 4})
    )
