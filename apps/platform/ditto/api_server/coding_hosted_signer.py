"""Lifecycle-bound SR25519 signer for native-v2 public status/result envelopes."""

from __future__ import annotations

from dataclasses import dataclass, field

import bittensor

from ditto.api_models.coding_hosted import (
    HostedCodingResult,
    HostedCodingStatus,
    hosted_signing_bytes,
)
from ditto.api_models.coding_inference import _decode_json_document
from ditto.api_server.coding_hosted_runtime_io import read_private
from ditto.api_server.coding_hosted_signer_config import (
    HostedControlSignerConfig,
    check_hosted_signer_config,
)
from ditto.api_server.errors import ApiServerConfigError

_ERROR = "hosted Coding signer unavailable"


@dataclass(repr=False)
class HostedControlSigner:
    """No wallet, seed URI, arbitrary-message signing or rotation fallback."""

    _key: bittensor.Keypair | None = field(repr=False)
    _hotkey: str

    @property
    def ss58_address(self) -> str:
        return self._hotkey

    def sign(self, data: bytes) -> bytes:
        try:
            key = self._key
            if key is None or type(data) is not bytes or len(data) > 8192:
                raise ValueError(_ERROR)
            document = _decode_json_document(data, maximum_bytes=8192)
            if not isinstance(document, dict) or "signature" in document:
                raise ValueError(_ERROR)
            schema = document.get("schema")
            value: HostedCodingStatus | HostedCodingResult
            if schema == "dittobench-coding-hosted-status-v2":
                value = HostedCodingStatus.model_validate(
                    {**document, "signature": "0" * 128}
                )
            elif schema == "dittobench-coding-hosted-result-v2":
                value = HostedCodingResult.model_validate(
                    {**document, "signature": "0" * 128}
                )
            else:
                raise ValueError(_ERROR)
            if (
                value.platform_hotkey != self._hotkey
                or hosted_signing_bytes(value) != data
            ):
                raise ValueError(_ERROR)
            signature = key.sign(data)
            if len(signature) != 64 or not bittensor.Keypair(
                ss58_address=self._hotkey
            ).verify(data, signature):
                raise ValueError(_ERROR)
            return signature
        except Exception:
            raise ValueError(_ERROR) from None

    def close(self) -> None:
        # Refuse retained references too. This does not claim memory zeroization
        # in Python or the native keypair implementation.
        self._key = None


def load_hosted_control_signer(
    config: HostedControlSignerConfig, *, process_role: str
) -> HostedControlSigner | None:
    # Relay processes do not even inspect the protected seed path.
    if process_role != "platform" or not config.enabled:
        return None
    check_hosted_signer_config(config)
    seed = bytearray()
    try:
        assert config.seed_file is not None
        seed.extend(read_private(config.seed_file, 32))
        if len(seed) != 32 or not any(seed):
            raise ValueError(_ERROR)
        key = bittensor.Keypair.create_from_seed(bytes(seed).hex())
        if key.crypto_type != 1 or key.ss58_address != config.expected_hotkey:
            raise ValueError(_ERROR)
        challenge = b"dittobench-coding-hosted-signer-startup-v2\n"
        proof = key.sign(challenge)
        if len(proof) != 64 or not bittensor.Keypair(
            ss58_address=config.expected_hotkey
        ).verify(challenge, proof):
            raise ValueError(_ERROR)
        return HostedControlSigner(key, key.ss58_address)
    except Exception:
        raise ApiServerConfigError(_ERROR) from None
    finally:
        seed[:] = bytes(len(seed))
