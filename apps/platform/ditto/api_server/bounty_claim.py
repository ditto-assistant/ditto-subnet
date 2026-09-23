"""Hotkey-signed bounty claims, reservations, and contributor identity verification."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from ditto.api_models.bounty_claim import BountySpec, KeyKind
from ditto.api_server.attestation import verify_signature

CLAIM_DOMAIN = "ditto-bounty-claim:v1"
RENEW_DOMAIN = "ditto-bounty-renew:v1"
SUBMIT_DOMAIN = "ditto-bounty-submit:v1"
HANDOFF_DOMAIN = "ditto-bounty-handoff:v1"
WITHDRAW_DOMAIN = "ditto-bounty-withdraw:v1"
REBIND_DOMAIN = "ditto-bounty-rebind-payee:v1"
APPEAL_DOMAIN = "ditto-bounty-appeal:v1"
TEAM_DOMAIN = "ditto-bounty-team:v1"

MAX_ISSUED_AT_SKEW = timedelta(minutes=5)
MAX_ATTESTATION_AGE = timedelta(hours=24)


class BountyClaimRejected(Exception):
    """A bounty claim action failed verification or policy validation."""


def format_timestamp(dt: datetime) -> str:
    """Format datetime into canonical UTC ISO-8601 string."""
    return dt.astimezone(UTC).isoformat()


def compute_spec_digest(spec: BountySpec | dict[str, Any]) -> str:
    """Compute SHA-256 digest over canonical JSON representation of bounty terms."""
    if isinstance(spec, BountySpec):
        raw_dict = spec.model_dump(mode="json")
    else:
        raw_dict = dict(spec)

    canonical_json = json.dumps(
        raw_dict,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def compute_team_digest(members: list[str], payee_coldkey: str) -> str:
    """Compute SHA-256 digest over team member hotkeys and designated payee."""
    sorted_members = sorted(set(members))
    payload = {
        "members": sorted_members,
        "payee_coldkey": payee_coldkey,
    }
    canonical_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def bounty_claim_message(
    *,
    netuid: int,
    repo: str,
    issue: int,
    bounty_revision: int,
    spec_digest: str,
    claimant_hotkey: str,
    payee_coldkey: str,
    github_login: str,
    team_digest: str,
    expires_at: datetime,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    """Exact UTF-8 bytes signed by a claimant to initiate a reservation."""
    exp = format_timestamp(expires_at)
    issued = format_timestamp(issued_at)
    return (
        f"{CLAIM_DOMAIN}:{netuid}:{repo}:{issue}:{bounty_revision}:{spec_digest}:"
        f"{claimant_hotkey}:{payee_coldkey}:{github_login}:{team_digest}:{exp}:{nonce}:{issued}:"
        f"{key_kind}:{signer}"
    ).encode()


def bounty_renew_message(
    *,
    netuid: int,
    claim_id: UUID,
    claimant_hotkey: str,
    bounty_revision: int,
    spec_digest: str,
    new_expires_at: datetime,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    """Exact UTF-8 bytes signed by a claimant to extend an active reservation."""
    new_exp = format_timestamp(new_expires_at)
    issued = format_timestamp(issued_at)
    return (
        f"{RENEW_DOMAIN}:{netuid}:{claim_id}:{claimant_hotkey}:{bounty_revision}:{spec_digest}:"
        f"{new_exp}:{nonce}:{issued}:{key_kind}:{signer}"
    ).encode()


def bounty_submit_message(
    *,
    netuid: int,
    claim_id: UUID,
    claimant_hotkey: str,
    pr_number: int,
    head_sha: str,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    """Exact UTF-8 bytes signed by a claimant to submit PR deliverables."""
    issued = format_timestamp(issued_at)
    return (
        f"{SUBMIT_DOMAIN}:{netuid}:{claim_id}:{claimant_hotkey}:{pr_number}:{head_sha}:"
        f"{nonce}:{issued}:{key_kind}:{signer}"
    ).encode()


def bounty_handoff_message(
    *,
    netuid: int,
    claim_id: UUID,
    side: str,
    from_hotkey: str,
    to_hotkey: str,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    """Exact UTF-8 bytes signed by a party during a reservation handoff."""
    issued = format_timestamp(issued_at)
    return (
        f"{HANDOFF_DOMAIN}:{netuid}:{claim_id}:{side}:{from_hotkey}:{to_hotkey}:"
        f"{nonce}:{issued}:{key_kind}:{signer}"
    ).encode()


def bounty_withdraw_message(
    *,
    netuid: int,
    claim_id: UUID,
    claimant_hotkey: str,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    """Exact UTF-8 bytes signed by a claimant to voluntarily release a reservation."""
    issued = format_timestamp(issued_at)
    return (
        f"{WITHDRAW_DOMAIN}:{netuid}:{claim_id}:{claimant_hotkey}:"
        f"{nonce}:{issued}:{key_kind}:{signer}"
    ).encode()


def bounty_rebind_payee_message(
    *,
    netuid: int,
    claim_id: UUID,
    claimant_hotkey: str,
    old_payee_coldkey: str,
    new_payee_coldkey: str,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    """Exact UTF-8 bytes signed to rebind payout destination to a rotated coldkey."""
    issued = format_timestamp(issued_at)
    return (
        f"{REBIND_DOMAIN}:{netuid}:{claim_id}:{claimant_hotkey}:{old_payee_coldkey}:{new_payee_coldkey}:"
        f"{nonce}:{issued}:{key_kind}:{signer}"
    ).encode()


def bounty_appeal_message(
    *,
    netuid: int,
    claim_id: UUID,
    revocation_entry_hash: str,
    reason_digest: str,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    """Exact UTF-8 bytes signed to appeal an administrative revocation."""
    issued = format_timestamp(issued_at)
    return (
        f"{APPEAL_DOMAIN}:{netuid}:{claim_id}:{revocation_entry_hash}:{reason_digest}:"
        f"{nonce}:{issued}:{key_kind}:{signer}"
    ).encode()


def bounty_team_member_message(
    *,
    netuid: int,
    team_digest: str,
    member_hotkey: str,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    """Exact UTF-8 bytes signed by a collaborator binding them to team digest."""
    issued = format_timestamp(issued_at)
    return (
        f"{TEAM_DOMAIN}:{netuid}:{team_digest}:{member_hotkey}:"
        f"{nonce}:{issued}:{key_kind}:{signer}"
    ).encode()


def check_bounty_freshness(*, issued_at: datetime, now: datetime) -> None:
    """Reject actions with timestamps outside allowable skew or age limits."""
    issued = issued_at.astimezone(UTC)
    if issued > now + MAX_ISSUED_AT_SKEW:
        raise BountyClaimRejected(
            "bounty action issued_at is in the future beyond allowed skew"
        )
    if issued < now - MAX_ATTESTATION_AGE:
        raise BountyClaimRejected(
            "bounty action signature has expired; mint a fresh signature"
        )


def verify_signed_bounty_action(
    *,
    payload: bytes,
    key_kind: KeyKind,
    signer: str,
    signature: str,
    expected_hotkey: str,
    expected_coldkey: str | None = None,
) -> None:
    """Verify signature and validate that signer matches authorized key kind."""
    if key_kind == "hotkey":
        if signer != expected_hotkey:
            raise BountyClaimRejected(
                "hotkey proof signer must match the claimant hotkey"
            )
    elif key_kind == "coldkey":
        if expected_coldkey is None:
            raise BountyClaimRejected(
                "coldkey proof requires an authorized on-chain owner coldkey"
            )
        if signer != expected_coldkey:
            raise BountyClaimRejected(
                "coldkey proof signer must match the authorized on-chain owner coldkey"
            )
    else:
        raise BountyClaimRejected(f"unrecognized key_kind: {key_kind}")

    if not verify_signature(signer=signer, payload=payload, signature_hex=signature):
        raise BountyClaimRejected("signature did not verify")


def verify_claimant_payee_binding(
    *,
    claimant_hotkey: str,
    payee_coldkey: str,
    on_chain_owner: str,
) -> None:
    """Assert payee coldkey matches Subtensor owner of claimant hotkey."""
    if payee_coldkey != on_chain_owner:
        msg = (
            f"payee coldkey {payee_coldkey} does not match on-chain owner "
            f"{on_chain_owner} for hotkey {claimant_hotkey}"
        )
        raise BountyClaimRejected(msg)
