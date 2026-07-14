"""Screener hotkey loading + verdict signing.

The screener signs each verdict so the platform can verify it came from the
claimed hotkey and that the ``passed`` boolean was not flipped in transit. The
signature binds ``{screener_hotkey}:{agent_id}:{passed}:{policy_version}``
— the exact string the platform's ``/screener/agent/{id}/result`` rebuilds and
verifies. Never log the key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ditto.screener.errors import ScreenerConfigError
from ditto_screening_protocol import (
    SCREENING_POLICY_VERSION,
    verdict_signing_message,
)

if TYPE_CHECKING:
    from uuid import UUID

    from ditto.screener.config import ScreenerConfig


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
) -> str:
    """Return the hex sr25519 signature over the canonical verdict payload."""
    message = verdict_signing_message(
        screener_hotkey=screener_hotkey,
        agent_id=agent_id,
        passed=passed,
        policy_version=policy_version,
    )
    signature: bytes = keypair.sign(message)
    return signature.hex()
