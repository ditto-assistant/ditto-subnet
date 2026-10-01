"""Exact public proposal approval, separate from epoch or funding authority."""

from collections.abc import Callable
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .treasury import Block, Digest, Hash, TreasuryEmissionPolicy, TreasuryLedgerPin

SignatureVerifier = Callable[[str, bytes, bytes], bool]


class TreasuryPolicyApproval(BaseModel):
    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, revalidate_instances="always"
    )

    policy: TreasuryEmissionPolicy
    signature: Annotated[str, Field(pattern=r"^0x[0-9a-f]{128}$")]


def approval_message(policy: TreasuryEmissionPolicy) -> bytes:
    return f"ditto-treasury-emission-policy-v1:{policy.digest}".encode()


def verify_policy_approval(
    approval: TreasuryPolicyApproval,
    *,
    expected_policy_digest: str,
    expected_collector_policy_digest: str,
    verify_signature: SignatureVerifier,
) -> TreasuryEmissionPolicy:
    """Verify only the deployment-pinned known-field proposal and coldkey.

    The caller must supply a real public-key verifier, never an operator's
    verified boolean. This has no wallet, chain, file or environment access.
    Returned approval does not authorize an epoch, registration or weights.
    """
    expected = TypeAdapter(Digest).validate_python(expected_policy_digest)
    collector_expected = TypeAdapter(Digest).validate_python(
        expected_collector_policy_digest
    )
    validated = TreasuryPolicyApproval.model_validate(approval)
    policy = validated.policy
    if policy.digest != expected:
        raise ValueError("proposal differs from immutable approval pin")
    if policy.collector_policy_digest != collector_expected:
        raise ValueError("proposal differs from immutable collector policy pin")
    if (
        verify_signature(
            policy.collector_coldkey,
            approval_message(policy),
            bytes.fromhex(validated.signature[2:]),
        )
        is not True
    ):
        raise ValueError("proposal lacks offline collector approval")
    return policy


def verify_pinned_policy_approval(
    pin: TreasuryLedgerPin,
    approval: TreasuryPolicyApproval,
    *,
    expected_policy_digest: str,
    expected_collector_policy_digest: str,
    verify_signature: SignatureVerifier,
    netuid: int,
    first_block: int,
    pinned_block: int,
    pinned_block_hash: str,
) -> TreasuryLedgerPin:
    """Bind approval to the immutable epoch's observed recipient, never latest policy.

    This validates supplied shadow evidence; it does not independently fetch
    finality or attest the recipient is still registered before dispatch.
    """
    validated = TreasuryLedgerPin.model_validate(pin)
    first_block = TypeAdapter(Block).validate_python(first_block)
    pinned_block = TypeAdapter(Block).validate_python(pinned_block)
    pinned_block_hash = TypeAdapter(Hash).validate_python(pinned_block_hash)
    verified_policy = verify_policy_approval(
        approval,
        expected_policy_digest=expected_policy_digest,
        expected_collector_policy_digest=expected_collector_policy_digest,
        verify_signature=verify_signature,
    )
    if validated.policy != verified_policy:
        raise ValueError("epoch policy differs from offline approval")
    validated.require_epoch(
        netuid=netuid, first_block=first_block, pinned_block=pinned_block
    )
    if (
        validated.identity.finalized_block == pinned_block
        and validated.identity.finalized_block_hash != pinned_block_hash
    ):
        raise ValueError("approved identity hash differs from epoch pin")
    return validated


def verify_public_signature(address: str, message: bytes, signature: bytes) -> bool:
    """Runtime verifier: instantiate a public-only key, never load a wallet.

    Wallet/crypto dependencies belong to the root and Platform runtimes. Lazy
    import keeps shared wire-model consumers independent of those packages.
    """
    try:
        from bittensor_wallet import Keypair

        return Keypair(ss58_address=address).verify(message, signature) is True
    except Exception:
        # Invalid SS58 checksums/signature scalars and unavailable runtime
        # crypto fail closed, without returning or logging input material.
        return False
