"""Owner-link attestation: what gets signed, and how it is verified.

A miner who rotates keys loses the platform's same-owner copy-screening
exemption, because that exemption keys on the payment-time coldkey
(:func:`ditto.api_server.scoring_gate.evaluate_duplicate_signals`). The
principle it encodes is owner-based -- "copying is only a threat across
owners" -- but the implementation is key-based, so a miner who moves to a new
coldkey/hotkey gets copy-flagged against their own earlier work.

This module defines the cryptographic replacement for that Discord
conversation: **two hotkeys are declared to be the same operator, and each of
the two endpoints signs its own half.**

The edge is hotkey-to-hotkey
----------------------------
The claim is "hotkey A and hotkey B are the same operator", and both hotkeys
are named in the signed bytes. A coldkey signature is *evidence for* that
claim -- reaching it through the coldkey->hotkey binding the platform already
knows from payment records -- rather than defining a different kind of edge.

Keeping the edge at hotkey granularity is what makes the scope legible: an
attestation covers exactly the two hotkeys it names, and nothing else. A
coldkey-to-coldkey edge would have silently extended to hotkeys that did not
exist when it was signed, which would have needed an expiry or enumeration
bound to stay honest. At hotkey granularity that problem does not arise, so
that machinery is not built.

Either key may prove a half
---------------------------
Each endpoint proves its half with **either** the hotkey itself or the coldkey
that owns it:

- a **hotkey** signature proves control of the exact hotkey being linked --
  the claim proved directly;
- a **coldkey** signature proves ownership at the root, and reaches the claim
  through the payment-record binding, which the platform knows independently
  of anything the attestation asserts.

Both are logically sufficient; they differ in key strength, not validity.
Supporting both matters because a single required type strands exactly the
people the mechanism exists for: a miner who lost the old hotkey's key but
still holds the coldkey, or who rotated coldkeys but kept the hotkey. The key
type used for each half is recorded and graded (:func:`evidence_grade`), but
grading is reviewer context -- it does not gate the exemption. See the module
docstring of :mod:`ditto.db.queries.attestation`.

Why both halves must be signed
------------------------------
A one-sided attestation is unsafe. Anyone could mint an edge naming a hotkey
they do not control: Mallory signs a link between her hotkey ``M`` and a
victim's ``V``, and because the link suppresses copy screening between its two
endpoints, she could then resubmit the victim's work with the victim's own
identity as cover -- using only a key she already holds.

Requiring both endpoints to sign closes that completely. Mallory can produce
her own half and never the victim's, so the edge never forms. Because that
defence does not depend on which direction the edge points, the link is
symmetric: it is a statement about one operator holding both keys, and both
key holders consented to it.

What is bound into each half
----------------------------
- a **domain tag + version** so a signature minted here cannot be replayed
  into the upload or validator signing lanes, which use their own tags;
- the **netuid**, so an attestation minted on another subnet running this
  software cannot be replayed onto SN118;
- **both hotkeys in canonical (sorted) order**, so a pair has exactly one
  signable representation and the two halves cannot disagree about the pair;
- **which side** this half is for, and the **key kind** and **exact signer**
  proving it, so a half cannot be relabelled, moved to the other side, or
  passed off as a stronger key type than it is;
- a **nonce**, stored under a unique constraint, so a captured attestation
  cannot be submitted twice;
- **issued_at**, so a signature that leaks long after the fact is outside the
  acceptance window.

Signing a bare "I own X" string would satisfy none of these.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

import bittensor

if TYPE_CHECKING:
    from uuid import UUID

LINK_DOMAIN: Final = "ditto-owner-link:v1"
"""Domain tag for both halves of an owner-link attestation."""

KeyKind = Literal["hotkey", "coldkey"]
"""Which key proved a half. Recorded per side and graded, never gating."""

Side = Literal["lo", "hi"]
"""Which endpoint of the canonically-ordered pair a half belongs to."""

EvidenceGrade = Literal["coldkey-coldkey", "mixed", "hotkey-hotkey"]
"""Relative strength of the two proofs. Reviewer context only."""

MAX_ISSUED_AT_SKEW: Final = timedelta(minutes=5)
"""How far into the future ``issued_at`` may sit before it is rejected.

Non-zero because the miner's clock is not ours; small because the only
legitimate gap between minting and submitting an attestation is one CLI
invocation.
"""

MAX_ATTESTATION_AGE: Final = timedelta(hours=24)
"""How long a minted attestation stays submittable.

A day is generous for "run the command, then post it" while keeping the window
in which a leaked-but-unsubmitted signature is useful short. The link it
creates is permanent once recorded; this bounds only the mint-to-submit gap.
"""

MAX_LINK_DEPTH: Final = 1
"""Attestation hops honoured when resolving linked hotkeys. Direct edges only.

Deliberately reduced from the transitive walk an earlier revision of this
design used. Once the link became symmetric, transitivity acquired a failure
mode it did not have while directional: ``A--B`` plus ``B--C`` would link
``A`` and ``C``, two owners who never signed anything with each other, letting
an intermediary bridge them. Consent here is pairwise and the relation stays
pairwise.

The cost is small and falls on the rare heavy rotator: a miner on their third
hotkey attests the pairs that actually collide rather than relying on a chain.
Each such attestation requires the real key holders to sign, which is the
point. Kept as a named constant so the choice is visible and testable rather
than implied by an absent loop.
"""


def expected_netuid() -> int:
    """The netuid this deployment will accept attestations for.

    Mirrors :func:`ditto.chain.models.parse_chain_config_from_env` rather than
    importing it, so signature verification does not drag in the chain client.
    """
    return int(os.environ.get("NETUID", "118"))


def canonical_pair(hotkey_a: str, hotkey_b: str) -> tuple[str, str]:
    """Return the two hotkeys in canonical (sorted) order as ``(lo, hi)``.

    The link is symmetric, so a pair must have exactly one signable
    representation. Sorting gives that for free and means the same two miners
    cannot create two distinct edges by submitting the pair in either order.
    """
    return (hotkey_a, hotkey_b) if hotkey_a <= hotkey_b else (hotkey_b, hotkey_a)


def link_message(
    *,
    netuid: int,
    hotkey_lo: str,
    hotkey_hi: str,
    nonce: UUID,
    issued_at: datetime,
    side: Side,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    """Exact UTF-8 bytes one endpoint signs.

    Called twice per attestation -- once per side -- with the same pair, nonce
    and timestamp. ``side``, ``key_kind`` and ``signer`` are inside the signed
    bytes so a half cannot be relabelled, replayed onto the other endpoint, or
    presented as a stronger key type than the one that actually signed it.

    Pure, so the miner CLI and this verifier can be tested against identical
    vectors; ``ditto-subnet`` carries a byte-for-byte copy of this builder.
    """
    issued = issued_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (
        f"{LINK_DOMAIN}:{netuid}:{hotkey_lo}:{hotkey_hi}:{nonce}:{issued}"
        f":{side}:{key_kind}:{signer}"
    ).encode()


def evidence_grade(lo_key_kind: KeyKind, hi_key_kind: KeyKind) -> EvidenceGrade:
    """Grade the pair of proofs backing a link.

    Reviewer context, not a gate. Sufficiency is binary: any two valid halves
    establish the link. See :mod:`ditto.db.queries.attestation` for why.
    """
    kinds = {lo_key_kind, hi_key_kind}
    if kinds == {"coldkey"}:
        return "coldkey-coldkey"
    if kinds == {"hotkey"}:
        return "hotkey-hotkey"
    return "mixed"


def verify_signature(*, signer: str, payload: bytes, signature_hex: str) -> bool:
    """Return True iff ``signature_hex`` is a valid sr25519 sig over ``payload``.

    Deliberately identical in behaviour to the upload endpoint's
    ``_verify_signature``: narrow catch on ``ValueError`` (malformed hex,
    malformed SS58) and ``TypeError`` (wrong-shape input from the wallet
    library), so anything else crashes the handler as the bug it is rather
    than being reported as "signature did not verify".
    """
    try:
        keypair = bittensor.Keypair(ss58_address=signer)
        return bool(keypair.verify(payload, bytes.fromhex(signature_hex)))
    except (ValueError, TypeError):
        return False


class AttestationRejected(Exception):
    """An attestation failed verification or policy checks.

    Carries an operator-readable reason; the endpoint maps it to a 400.
    """


def check_freshness(*, issued_at: datetime, now: datetime) -> None:
    """Reject attestations minted too far in the past or in the future.

    Raises:
        AttestationRejected: When ``issued_at`` is outside the window.
    """
    issued = issued_at.astimezone(UTC)
    if issued > now + MAX_ISSUED_AT_SKEW:
        raise AttestationRejected(
            "attestation issued_at is in the future beyond the allowed skew"
        )
    if issued < now - MAX_ATTESTATION_AGE:
        raise AttestationRejected(
            "attestation has expired; mint a fresh one and submit it again"
        )


def verify_half(
    *,
    netuid: int,
    hotkey_lo: str,
    hotkey_hi: str,
    nonce: UUID,
    issued_at: datetime,
    side: Side,
    key_kind: KeyKind,
    signer: str,
    signature: str,
    bound_coldkey: str | None,
) -> None:
    """Verify one endpoint's proof, or raise.

    ``bound_coldkey`` is the coldkey the platform independently associates with
    this side's hotkey (its most recent payment-time coldkey), or ``None`` when
    that hotkey has never funded a submission. It is consulted only for a
    ``coldkey`` half: the signer must *be* that coldkey. This is what makes a
    coldkey proof meaningful -- it is checked against a binding the platform
    learned from on-chain payment proofs, never from the attestation itself.

    Raises:
        AttestationRejected: On any failed check.
    """
    hotkey = hotkey_lo if side == "lo" else hotkey_hi

    if key_kind == "hotkey":
        if signer != hotkey:
            raise AttestationRejected(
                f"the {side} half declares a hotkey proof but its signer is not "
                f"{hotkey}"
            )
    else:
        if bound_coldkey is None:
            raise AttestationRejected(
                f"cannot verify a coldkey proof for {hotkey}: that hotkey has no "
                "payment record binding it to a coldkey. Sign this half with the "
                "hotkey itself."
            )
        if signer != bound_coldkey:
            raise AttestationRejected(
                f"the {side} half is signed by a coldkey that does not own "
                f"{hotkey} on its most recent payment record"
            )

    if not verify_signature(
        signer=signer,
        payload=link_message(
            netuid=netuid,
            hotkey_lo=hotkey_lo,
            hotkey_hi=hotkey_hi,
            nonce=nonce,
            issued_at=issued_at,
            side=side,
            key_kind=key_kind,
            signer=signer,
        ),
        signature_hex=signature,
    ):
        raise AttestationRejected(f"the {side} half's signature did not verify")


def verify_link(
    *,
    netuid: int,
    hotkey_lo: str,
    hotkey_hi: str,
    nonce: UUID,
    issued_at: datetime,
    lo_key_kind: KeyKind,
    lo_signer: str,
    lo_signature: str,
    hi_key_kind: KeyKind,
    hi_signer: str,
    hi_signature: str,
    lo_bound_coldkey: str | None,
    hi_bound_coldkey: str | None,
    now: datetime,
) -> None:
    """Verify both halves of an owner-link attestation, or raise.

    Neither hotkey needs to be currently registered on the subnet. Requiring
    chain presence would break the mechanism in exactly the case it exists for
    -- a miner who has already abandoned a key -- and it would add nothing,
    because sr25519 verification is pure mathematics over a public key. What
    the exemption grants is scoped to submissions those hotkeys *already made*,
    which are immutable historical rows; a deregistration cannot retroactively
    make those someone else's work.

    Raises:
        AttestationRejected: On any failed check, with a reason safe to return
            to the miner.
    """
    if hotkey_lo == hotkey_hi:
        raise AttestationRejected(
            "the two hotkeys must differ; a hotkey cannot be linked to itself"
        )
    if (hotkey_lo, hotkey_hi) != canonical_pair(hotkey_lo, hotkey_hi):
        raise AttestationRejected("hotkeys are not in canonical order")
    if netuid != expected_netuid():
        raise AttestationRejected(
            f"attestation is for netuid {netuid}, "
            f"this platform serves netuid {expected_netuid()}"
        )
    check_freshness(issued_at=issued_at, now=now)

    verify_half(
        netuid=netuid,
        hotkey_lo=hotkey_lo,
        hotkey_hi=hotkey_hi,
        nonce=nonce,
        issued_at=issued_at,
        side="lo",
        key_kind=lo_key_kind,
        signer=lo_signer,
        signature=lo_signature,
        bound_coldkey=lo_bound_coldkey,
    )
    verify_half(
        netuid=netuid,
        hotkey_lo=hotkey_lo,
        hotkey_hi=hotkey_hi,
        nonce=nonce,
        issued_at=issued_at,
        side="hi",
        key_kind=hi_key_kind,
        signer=hi_signer,
        signature=hi_signature,
        bound_coldkey=hi_bound_coldkey,
    )
