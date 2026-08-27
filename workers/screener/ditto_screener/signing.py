"""Screener hotkey loading + verdict signing.

The screener signs each verdict so the platform can verify it came from the
claimed hotkey and that the ``passed`` boolean was not flipped in transit. The
The current signature also binds the platform-issued screening attempt id, so a
verdict cannot be replayed against another lease. Never log the key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ditto_screener.errors import ScreenerConfigError
from ditto_screener.heartbeat import (
    ReviewSettingsStatus,
    ScreenerProgress,
    SystemMetrics,
    review_settings_signing_token,
    screener_progress_signing_token,
    system_metrics_signing_token,
)
from ditto_screening_protocol import (
    SCREENING_POLICY_VERSION,
    ScreenResultOutcome,
    verdict_signing_message,
)

if TYPE_CHECKING:
    from uuid import UUID

    from ditto_screener.config import ScreenerConfig


def load_screener_keypair(config: ScreenerConfig) -> Any:
    """Load the signing keypair and assert it matches ``config.screener_hotkey``.

    Prefers an explicit mnemonic (``SCREENER_MNEMONIC``); otherwise loads the
    named bittensor wallet hotkey. Raises if neither is usable or the loaded
    ss58 does not match the configured hotkey (guards against signing verdicts
    with the wrong key).
    """
    import bittensor

    keypair: Any
    if config.screener_mnemonic:
        keypair = bittensor.Keypair.create_from_mnemonic(config.screener_mnemonic)
    elif config.wallet_name and config.wallet_hotkey:
        wallet = bittensor.Wallet(name=config.wallet_name, hotkey=config.wallet_hotkey)
        keypair = wallet.hotkey
    else:  # pragma: no cover - guarded earlier by config parsing
        raise ScreenerConfigError("no signing key source configured")

    if keypair.ss58_address != config.screener_hotkey:
        raise ScreenerConfigError(
            "loaded signing key ss58 does not match SCREENER_HOTKEY "
            f"({keypair.ss58_address} != {config.screener_hotkey})"
        )
    return keypair


def sign_verdict(
    keypair: Any,
    *,
    screener_hotkey: str,
    agent_id: UUID,
    passed: bool,
    policy_version: int = SCREENING_POLICY_VERSION,
    attempt_id: UUID | None = None,
    outcome: ScreenResultOutcome | None = None,
    manifest_digest: str | None = None,
    finding_digest: str | None = None,
    review_audit_digest: str | None = None,
    deferred_source_review: bool = False,
    review_settings_revision: int | None = None,
    review_settings_instance_id: str | None = None,
    review_settings_scope: str | None = None,
    review_settings_checksum: str | None = None,
    reason_code: str | None = None,
    image_sha256: str | None = None,
    image_size_bytes: int | None = None,
    image_id: str | None = None,
    image_ref: str | None = None,
    image_upload_id: UUID | None = None,
) -> str:
    """Return the hex sr25519 signature over the canonical verdict payload."""
    message = verdict_signing_message(
        screener_hotkey=screener_hotkey,
        agent_id=agent_id,
        passed=passed,
        policy_version=policy_version,
        attempt_id=attempt_id,
        outcome=outcome,
        manifest_digest=manifest_digest,
        finding_digest=finding_digest,
        review_audit_digest=review_audit_digest,
        deferred_source_review=deferred_source_review,
        review_settings_revision=review_settings_revision,
        review_settings_instance_id=review_settings_instance_id,
        review_settings_scope=review_settings_scope,
        review_settings_checksum=review_settings_checksum,
        reason_code=reason_code,
        image_sha256=image_sha256,
        image_size_bytes=image_size_bytes,
        image_id=image_id,
        image_ref=image_ref,
        image_upload_id=image_upload_id,
    )
    signature: bytes = keypair.sign(message)
    return signature.hex()


def heartbeat_signing_message(
    *,
    screener_hotkey: str,
    software_version: str,
    protocol_version: int,
    policy_version: int,
    state: str,
    active_agent_id: UUID | None,
    instance_id: str | None = None,
    progress: ScreenerProgress | None = None,
    system_metrics: SystemMetrics | None,
    review_settings: ReviewSettingsStatus | None = None,
    timestamp: int,
) -> bytes:
    """Build the versioned heartbeat payload mirrored by the platform."""
    if protocol_version == 1:
        if progress is not None:
            raise ValueError("heartbeat protocol v1 cannot sign progress")
        return (
            "ditto-screener-heartbeat:v1:"
            f"{screener_hotkey}:{software_version}:{protocol_version}:{policy_version}:"
            f"{state}:{active_agent_id or ''}:"
            f"{system_metrics_signing_token(system_metrics)}:{timestamp}"
        ).encode()
    if protocol_version >= 4:
        if not instance_id or review_settings is None:
            raise ValueError(
                "heartbeat protocol v4 requires instance_id and review settings"
            )
        review_settings_token = review_settings_signing_token(
            review_settings, protocol_version=protocol_version
        )
        return (
            "ditto-screener-heartbeat:v4:"
            f"{screener_hotkey}:{software_version}:{protocol_version}:{policy_version}:"
            f"{state}:{active_agent_id or ''}:{instance_id}:"
            f"{screener_progress_signing_token(progress)}:"
            f"{system_metrics_signing_token(system_metrics)}:"
            f"{review_settings_token}:"
            f"{timestamp}"
        ).encode()
    if protocol_version >= 3:
        # v3 adds the per-instance identity so the fleet's shared hotkey no
        # longer collapses every worker into one heartbeat row. instance_id is
        # signed to keep it non-spoofable; it never contains ':' (the field
        # delimiter) — see ScreenerHeartbeatRequest's pattern.
        if not instance_id:
            raise ValueError("heartbeat protocol v3 requires an instance_id")
        return (
            "ditto-screener-heartbeat:v3:"
            f"{screener_hotkey}:{software_version}:{protocol_version}:{policy_version}:"
            f"{state}:{active_agent_id or ''}:{instance_id}:"
            f"{screener_progress_signing_token(progress)}:"
            f"{system_metrics_signing_token(system_metrics)}:{timestamp}"
        ).encode()
    return (
        "ditto-screener-heartbeat:v2:"
        f"{screener_hotkey}:{software_version}:{protocol_version}:{policy_version}:"
        f"{state}:{active_agent_id or ''}:"
        f"{screener_progress_signing_token(progress)}:"
        f"{system_metrics_signing_token(system_metrics)}:{timestamp}"
    ).encode()


def sign_heartbeat(
    keypair: Any,
    *,
    screener_hotkey: str,
    software_version: str,
    protocol_version: int,
    policy_version: int,
    state: str,
    active_agent_id: UUID | None,
    instance_id: str | None = None,
    progress: ScreenerProgress | None = None,
    system_metrics: SystemMetrics | None,
    review_settings: ReviewSettingsStatus | None = None,
    timestamp: int,
) -> str:
    message = heartbeat_signing_message(
        screener_hotkey=screener_hotkey,
        software_version=software_version,
        protocol_version=protocol_version,
        policy_version=policy_version,
        state=state,
        active_agent_id=active_agent_id,
        instance_id=instance_id,
        progress=progress,
        system_metrics=system_metrics,
        review_settings=review_settings,
        timestamp=timestamp,
    )
    signature: bytes = keypair.sign(message)
    return signature.hex()
