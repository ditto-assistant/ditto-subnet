from __future__ import annotations

from pathlib import Path

from ditto.validator.updater_status import collect_updater_status

_CURRENT = "ghcr.io/ditto-assistant/ditto-subnet-stack@sha256:" + "a" * 64
_CANDIDATE = "ghcr.io/ditto-assistant/ditto-subnet-stack@sha256:" + "b" * 64


def _managed(monkeypatch) -> None:
    monkeypatch.setenv("VALIDATOR_STACK_MODE", "managed")
    monkeypatch.setenv("VALIDATOR_STACK_UPDATER", "true")
    monkeypatch.setenv("VALIDATOR_STACK_DESCRIPTOR_REF", _CURRENT)
    monkeypatch.setenv("VALIDATOR_STACK_VERSION", "0.63.1")


def test_managed_defaults_enabled_when_updater_env_is_omitted(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("VALIDATOR_STACK_MODE", "managed")
    monkeypatch.delenv("VALIDATOR_STACK_UPDATER", raising=False)
    monkeypatch.setenv("VALIDATOR_STACK_DESCRIPTOR_REF", _CURRENT)
    monkeypatch.setenv("VALIDATOR_STACK_VERSION", "0.63.1")

    status = collect_updater_status(observed_at=100, state_dir=tmp_path)

    assert status.enabled is True
    assert status.state == "idle"
    assert status.channel == "compat-2"


def test_managed_explicit_false_still_reports_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("VALIDATOR_STACK_MODE", "managed")
    monkeypatch.setenv("VALIDATOR_STACK_UPDATER", "false")
    monkeypatch.setenv("VALIDATOR_STACK_DESCRIPTOR_REF", _CURRENT)
    monkeypatch.setenv("VALIDATOR_STACK_VERSION", "0.63.1")

    status = collect_updater_status(observed_at=100, state_dir=tmp_path)

    assert status.enabled is False
    assert status.state == "disabled"


def test_source_mode_reports_not_managed_without_reading_host_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("VALIDATOR_STACK_MODE", "source")
    (tmp_path / "failed-candidate").write_text("SECRET=must-not-leak\n")

    status = collect_updater_status(observed_at=100, state_dir=tmp_path)

    assert status.model_dump(exclude_none=True) == {
        "enabled": False,
        "state": "not_managed",
        "failed_candidate_count": 0,
        "suppressed": False,
        "observed_at": 100,
    }


def test_managed_backoff_is_bounded_and_structured(tmp_path: Path, monkeypatch) -> None:
    _managed(monkeypatch)
    (tmp_path / "failed-candidate").write_text(
        f"CANDIDATE={_CANDIDATE}\n"
        "FAILURES=2\nRETRY_AFTER=200\nFAILED_AT=90\n"
        "REASON=candidate_readiness_failed\nSUPPRESSED=false\n"
    )
    (tmp_path / "last-update.env").write_text(
        f"PREVIOUS_RELEASE={_CANDIDATE}\nCURRENT_RELEASE={_CURRENT}\n"
        "CURRENT_VERSION=0.63.1\nUPDATED_AT=80\n"
    )

    status = collect_updater_status(observed_at=100, state_dir=tmp_path)

    assert status.state == "backoff"
    assert status.current_descriptor == _CURRENT
    assert status.candidate_descriptor == _CANDIDATE
    assert status.failed_candidate_count == 2
    assert status.retry_after == 200
    assert status.last_failure_reason == "candidate_readiness_failed"
    assert status.last_failure_at == 90
    assert status.last_success_at == 80


def test_transaction_phase_wins_over_backoff(tmp_path: Path, monkeypatch) -> None:
    _managed(monkeypatch)
    (tmp_path / "transaction.env").write_text(
        f"PHASE=candidate_started\nPREVIOUS_RELEASE={_CURRENT}\n"
        f"CANDIDATE_RELEASE={_CANDIDATE}\n"
    )

    status = collect_updater_status(observed_at=100, state_dir=tmp_path)

    assert status.state == "verifying"
    assert status.transaction_phase == "candidate_started"
    assert status.candidate_descriptor == _CANDIDATE


def test_malformed_or_untrusted_files_fail_closed_without_echoing_values(
    tmp_path: Path, monkeypatch
) -> None:
    _managed(monkeypatch)
    secret = "operator-token-must-never-appear"
    (tmp_path / "failed-candidate").write_text(f"ERROR={secret}\n")

    status = collect_updater_status(observed_at=100, state_dir=tmp_path)

    assert status.state == "unavailable"
    assert secret not in status.model_dump_json()


def test_symlinked_state_file_is_not_followed(tmp_path: Path, monkeypatch) -> None:
    _managed(monkeypatch)
    outside = tmp_path / "outside"
    outside.write_text(f"CANDIDATE={_CANDIDATE}\nFAILURES=1\nRETRY_AFTER=200\n")
    (tmp_path / "failed-candidate").symlink_to(outside)

    assert collect_updater_status(observed_at=100, state_dir=tmp_path).state == (
        "unavailable"
    )
