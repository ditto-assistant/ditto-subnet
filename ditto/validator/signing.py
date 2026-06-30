"""Validator hotkey loading + score signing.

The worker signs each score submission so the platform can verify the report
came from the claimed validator hotkey (the platform's ``/validator/.../score``
verifies an sr25519 signature over ``f"{validator_hotkey}:{run_id}"``).

WIP / ops decision: the signing private key comes from a bittensor wallet on the
host or a mnemonic secret. We only hold the public hotkey
(``5CZq6Mdanx...``) in config; the secret half must be provisioned on the VM
(Secret Manager -> wallet file, or VALIDATOR_MNEMONIC). Never log the key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ditto.validator.errors import ValidatorConfigError

if TYPE_CHECKING:
    from ditto.validator.config import ValidatorConfig


def load_validator_keypair(config: ValidatorConfig) -> Any:
    """Load the signing keypair and assert it matches ``config.validator_hotkey``.

    Prefers an explicit mnemonic (``VALIDATOR_MNEMONIC``); otherwise loads the
    named bittensor wallet hotkey. Raises if neither is usable or the loaded
    ss58 does not match the configured hotkey (guards against signing weights
    with the wrong key).
    """
    import bittensor

    keypair: Any
    if config.validator_mnemonic:
        keypair = bittensor.Keypair.create_from_mnemonic(config.validator_mnemonic)
    elif config.wallet_name and config.wallet_hotkey:
        wallet = bittensor.Wallet(name=config.wallet_name, hotkey=config.wallet_hotkey)
        keypair = wallet.hotkey
    else:  # pragma: no cover - guarded earlier by config parsing
        raise ValidatorConfigError("no signing key source configured")

    if keypair.ss58_address != config.validator_hotkey:
        raise ValidatorConfigError(
            "loaded signing key ss58 does not match VALIDATOR_HOTKEY "
            f"({keypair.ss58_address} != {config.validator_hotkey})"
        )
    return keypair


def sign_score(keypair: Any, validator_hotkey: str, run_id: str) -> str:
    """Return the hex sr25519 signature over ``f"{validator_hotkey}:{run_id}"``."""
    message = f"{validator_hotkey}:{run_id}".encode()
    signature: bytes = keypair.sign(message)
    return signature.hex()
