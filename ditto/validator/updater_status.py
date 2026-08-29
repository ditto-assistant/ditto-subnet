"""Collect bounded managed-updater state without exposing the host control plane."""

from __future__ import annotations

import os
import re
import stat
import time
from pathlib import Path

from ditto.api_models.validator_updater import (
    ValidatorUpdaterFailureReason,
    ValidatorUpdaterPhase,
    ValidatorUpdaterState,
    ValidatorUpdaterStatus,
)

UPDATER_STATE_DIR = Path("/var/lib/ditto-validator-updater")
_MAX_STATE_BYTES = 4096
_DESCRIPTOR = re.compile(
    r"^ghcr\.io/ditto-assistant/ditto-subnet-stack@sha256:[0-9a-f]{64}$"
)
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_PHASE_STATES: dict[ValidatorUpdaterPhase, ValidatorUpdaterState] = {
    "prepared": "draining",
    "drained": "draining",
    "old_stopped": "replacing",
    "candidate_started": "verifying",
    "committed": "verifying",
    "rollback_pending": "rollback",
    "rollback_ready": "rollback",
}
_FAILURE_REASONS: set[str] = {
    "candidate_deploy_failed",
    "candidate_readiness_failed",
    "transaction_interrupted",
    "unknown",
}


class _MalformedState(ValueError):
    pass


def _read_env(path: Path, allowed: set[str]) -> dict[str, str] | None:
    """Read a tiny regular file without following a final-component symlink."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _MalformedState from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_STATE_BYTES:
            raise _MalformedState
        payload = os.read(descriptor, _MAX_STATE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > _MAX_STATE_BYTES:
        raise _MalformedState
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise _MalformedState from error
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or "=" not in line:
            raise _MalformedState
        key, value = line.split("=", 1)
        if (
            key not in allowed
            or key in values
            or not value
            or any(character.isspace() for character in value)
        ):
            raise _MalformedState
        values[key] = value
    return values


def _descriptor(value: str | None) -> str | None:
    if value is None:
        return None
    if not _DESCRIPTOR.fullmatch(value):
        raise _MalformedState
    return value


def _version(value: str | None) -> str | None:
    if value is None:
        return None
    if not _VERSION.fullmatch(value):
        raise _MalformedState
    return value


def _revision(value: str | None) -> str | None:
    if value is None:
        return None
    if not _REVISION.fullmatch(value):
        raise _MalformedState
    return value


def _integer(value: str | None, *, maximum: int | None = None) -> int | None:
    if value is None:
        return None
    if not value.isascii() or not value.isdigit():
        raise _MalformedState
    parsed = int(value)
    if maximum is not None and parsed > maximum:
        raise _MalformedState
    return parsed


def _unavailable(observed_at: int) -> ValidatorUpdaterStatus:
    return ValidatorUpdaterStatus(
        enabled=True,
        channel="compat-2",
        state="unavailable",
        self_refresh_installed=False,
        observed_at=observed_at,
    )


def collect_updater_status(
    *,
    observed_at: int | None = None,
    state_dir: Path = UPDATER_STATE_DIR,
) -> ValidatorUpdaterStatus:
    """Return only schema-validated state derived from updater-owned files.

    The collector never reads systemd, Docker, the environment file, or logs.
    Missing/malformed managed state becomes ``unavailable`` rather than raw data.
    Source/self-managed installs explicitly report ``not_managed``.
    """

    now = int(time.time()) if observed_at is None else observed_at
    managed = os.environ.get("VALIDATOR_STACK_MODE") == "managed"
    enabled = os.environ.get("VALIDATOR_STACK_UPDATER", "true").lower() in {
        "1",
        "true",
        "yes",
    }
    if not managed:
        return ValidatorUpdaterStatus(
            enabled=False,
            state="not_managed",
            self_refresh_installed=False,
            observed_at=now,
        )

    try:
        current_descriptor = _descriptor(
            os.environ.get("VALIDATOR_STACK_DESCRIPTOR_REF")
        )
        current_version = _version(os.environ.get("VALIDATOR_STACK_VERSION"))
        refresh_install = _read_env(
            state_dir / "updater-refresh-installed.env",
            {"INSTALLED_REVISION", "INSTALLED_AT"},
        )
        self_refresh_installed = refresh_install is not None
        if refresh_install is not None:
            _revision(refresh_install.get("INSTALLED_REVISION"))
            _integer(refresh_install.get("INSTALLED_AT"))
        last_refresh = _read_env(
            state_dir / "last-updater-refresh.env",
            {
                "UPDATER_PREVIOUS_REVISION",
                "UPDATER_CURRENT_REVISION",
                "UPDATER_REFRESHED_AT",
            },
        )
        self_refresh_revision = None
        self_refresh_last_success_at = None
        if last_refresh is not None:
            if not self_refresh_installed:
                raise _MalformedState
            _revision(last_refresh.get("UPDATER_PREVIOUS_REVISION"))
            self_refresh_revision = _revision(
                last_refresh.get("UPDATER_CURRENT_REVISION")
            )
            self_refresh_last_success_at = _integer(
                last_refresh.get("UPDATER_REFRESHED_AT")
            )
            if self_refresh_revision is None or self_refresh_last_success_at is None:
                raise _MalformedState
        if not enabled:
            return ValidatorUpdaterStatus(
                enabled=False,
                channel="compat-2",
                state="disabled",
                current_descriptor=current_descriptor,
                current_version=current_version,
                self_refresh_installed=self_refresh_installed,
                self_refresh_revision=self_refresh_revision,
                self_refresh_last_success_at=self_refresh_last_success_at,
                observed_at=now,
            )
        transaction = _read_env(
            state_dir / "transaction.env",
            {"PHASE", "PREVIOUS_RELEASE", "CANDIDATE_RELEASE"},
        )
        failed = _read_env(
            state_dir / "failed-candidate",
            {
                "CANDIDATE",
                "FAILURES",
                "RETRY_AFTER",
                "FAILED_AT",
                "REASON",
                "SUPPRESSED",
            },
        )
        prefetched = _read_env(
            state_dir / "prefetched-release.env",
            {"CANDIDATE_RELEASE", "CANDIDATE_VERSION"},
        )
        last_update = _read_env(
            state_dir / "last-update.env",
            {"PREVIOUS_RELEASE", "CURRENT_RELEASE", "CURRENT_VERSION", "UPDATED_AT"},
        )

        phase: ValidatorUpdaterPhase | None = None
        state: ValidatorUpdaterState = "idle"
        candidate_descriptor = None
        candidate_version = None
        if transaction is not None:
            raw_phase = transaction.get("PHASE")
            if raw_phase not in _PHASE_STATES:
                raise _MalformedState
            phase = raw_phase  # type: ignore[assignment]
            state = _PHASE_STATES[phase]
            candidate_descriptor = _descriptor(transaction.get("CANDIDATE_RELEASE"))
            _descriptor(transaction.get("PREVIOUS_RELEASE"))
            if prefetched is not None:
                prefetched_descriptor = _descriptor(prefetched.get("CANDIDATE_RELEASE"))
                prefetched_version = _version(prefetched.get("CANDIDATE_VERSION"))
                if prefetched_descriptor == candidate_descriptor:
                    candidate_version = prefetched_version
        elif prefetched is not None:
            candidate_descriptor = _descriptor(prefetched.get("CANDIDATE_RELEASE"))
            candidate_version = _version(prefetched.get("CANDIDATE_VERSION"))
            if candidate_descriptor is None or candidate_version is None:
                raise _MalformedState
            state = "prefetched"

        failures = 0
        retry_after = None
        suppressed = False
        last_failure_at = None
        last_failure_reason: ValidatorUpdaterFailureReason | None = None
        if failed is not None:
            failed_descriptor = _descriptor(failed.get("CANDIDATE"))
            failures = _integer(failed.get("FAILURES"), maximum=100) or 0
            retry_after = _integer(failed.get("RETRY_AFTER"))
            last_failure_at = _integer(failed.get("FAILED_AT"))
            raw_reason = failed.get("REASON", "unknown")
            if raw_reason not in _FAILURE_REASONS:
                raw_reason = "unknown"
            last_failure_reason = raw_reason  # type: ignore[assignment]
            raw_suppressed = failed.get("SUPPRESSED", "false")
            if raw_suppressed not in {"true", "false"}:
                raise _MalformedState
            suppressed = raw_suppressed == "true"
            if failures < 1 or retry_after is None or failed_descriptor is None:
                raise _MalformedState
            if candidate_descriptor is None:
                candidate_descriptor = failed_descriptor
            if phase is None:
                if suppressed:
                    state = "suppressed"
                elif now < retry_after:
                    state = "backoff"
                else:
                    state = "retry_ready"

        last_success_at = None
        if last_update is not None:
            _descriptor(last_update.get("PREVIOUS_RELEASE"))
            _descriptor(last_update.get("CURRENT_RELEASE"))
            _version(last_update.get("CURRENT_VERSION"))
            last_success_at = _integer(last_update.get("UPDATED_AT"))

        return ValidatorUpdaterStatus(
            enabled=True,
            channel="compat-2",
            state=state,
            transaction_phase=phase,
            current_descriptor=current_descriptor,
            current_version=current_version,
            candidate_descriptor=candidate_descriptor,
            candidate_version=candidate_version,
            failed_candidate_count=failures,
            retry_after=retry_after,
            suppressed=suppressed,
            last_success_at=last_success_at,
            last_failure_at=last_failure_at,
            last_failure_reason=last_failure_reason,
            self_refresh_installed=self_refresh_installed,
            self_refresh_revision=self_refresh_revision,
            self_refresh_last_success_at=self_refresh_last_success_at,
            observed_at=now,
        )
    except (OSError, _MalformedState, ValueError):
        return _unavailable(now)


__all__ = ["UPDATER_STATE_DIR", "collect_updater_status"]
