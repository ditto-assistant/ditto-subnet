"""Reads and mutations against ``name_claims`` / ``name_claim_endorsements``.

The cryptographic checks live in :mod:`ditto.api_server.name_claim`. This
module persists verified claims, records endorsements, flips a claim to
upheld once the threshold is met, and answers "may this owner upload
this name?"
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.name_claim import (
    ENDORSEMENT_THRESHOLD,
    ENTRENCHMENT_AGE,
    normalize_name_stem,
    owner_root_for,
)
from ditto.db.models import (
    Agent,
    EvaluationPayment,
    NameClaim,
    NameClaimEndorsement,
    Score,
)
from ditto.db.queries.scores import (
    MIN_ELIGIBLE_CASES,
    attested_emission_owner_roots,
    emission_owner,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


class NameClaimReplayedError(Exception):
    """The nonce on this name-claim action has already been recorded."""


class NameClaimConflictError(Exception):
    """The stem already has a live (pending or upheld) reservation."""


@dataclass(frozen=True)
class ActiveHandleClaim:
    """One live claim, ready for leaderboard annotation."""

    claim_id: UUID
    name_stem: str
    claimant_hotkey: str
    status: str
    claimant_owner_root: str


async def owner_root_for_hotkey(session: AsyncSession, *, hotkey: str) -> str:
    """Payment-owner root for a hotkey, folded with active attestations."""
    from ditto.db.queries.attestation import get_bound_coldkey_for_hotkey

    coldkey = await get_bound_coldkey_for_hotkey(session, hotkey=hotkey)
    raw = owner_root_for(miner_hotkey=hotkey, miner_coldkey=coldkey)
    roots = await attested_emission_owner_roots(session, [(hotkey, raw)])
    return roots[0] if roots else raw


async def claimant_has_used_stem(
    session: AsyncSession, *, claimant_hotkey: str, name_stem: str
) -> bool:
    """True iff the claimant's owner family already uploaded that stem."""
    owner_root = await owner_root_for_hotkey(session, hotkey=claimant_hotkey)
    rows = (
        (
            await session.execute(
                select(Agent.miner_hotkey, Agent.name, EvaluationPayment.miner_coldkey)
                .select_from(Agent)
                .outerjoin(
                    EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id
                )
            )
        )
        .tuples()
        .all()
    )
    identities: list[tuple[str, str]] = []
    for hotkey, name, coldkey in rows:
        if normalize_name_stem(name) != name_stem:
            continue
        identities.append(
            (hotkey, emission_owner(miner_hotkey=hotkey, miner_coldkey=coldkey))
        )
    if not identities:
        return False
    roots = await attested_emission_owner_roots(session, identities)
    return owner_root in set(roots)


async def list_entrenched_owner_roots(
    session: AsyncSession, *, now: datetime
) -> set[str]:
    """Payment-owner families that may endorse a handle claim.

    Entrenched means: at least one ``scored`` agent with a full-benchmark
    score, and that family's earliest upload is at least
    :data:`ENTRENCHMENT_AGE` old. Brand-new miners cannot endorse.
    """
    cutoff = now - ENTRENCHMENT_AGE
    stmt = (
        select(
            Agent.miner_hotkey,
            EvaluationPayment.miner_coldkey,
            func.min(Agent.created_at).label("first_seen"),
        )
        .select_from(Agent)
        .join(Score, Score.agent_id == Agent.agent_id)
        .outerjoin(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
        .where(
            Agent.status == AgentStatus.SCORED,
            Score.n >= MIN_ELIGIBLE_CASES,
            Score.composite > 0,
        )
        .group_by(Agent.miner_hotkey, EvaluationPayment.miner_coldkey)
        .having(func.min(Agent.created_at) <= cutoff)
    )
    rows = (await session.execute(stmt)).all()
    identities = [
        (
            str(row.miner_hotkey),
            emission_owner(
                miner_hotkey=str(row.miner_hotkey),
                miner_coldkey=row.miner_coldkey,
            ),
        )
        for row in rows
    ]
    if not identities:
        return set()
    return set(await attested_emission_owner_roots(session, identities))


async def record_name_claim(
    session: AsyncSession,
    *,
    netuid: int,
    name_stem: str,
    claimant_hotkey: str,
    claimant_key_kind: str,
    claimant_signer: str,
    claimant_signature: str,
    nonce: UUID,
    issued_at: datetime,
) -> NameClaim:
    """Insert a pending claim. Raises on replay or live-stem conflict."""
    row = NameClaim(
        claim_id=uuid4(),
        netuid=netuid,
        name_stem=name_stem,
        claimant_hotkey=claimant_hotkey,
        claimant_key_kind=claimant_key_kind,
        claimant_signer=claimant_signer,
        claimant_signature=claimant_signature,
        nonce=nonce,
        issued_at=issued_at,
        status="pending",
    )
    session.add(row)
    try:
        await session.flush()
    except SAIntegrityError as exc:
        message = str(exc.orig) if getattr(exc, "orig", None) is not None else str(exc)
        if "name_claims_nonce_key" in message:
            raise NameClaimReplayedError(
                "name-claim nonce has already been used"
            ) from exc
        if "name_claims_active_stem_idx" in message:
            raise NameClaimConflictError(
                f"handle {name_stem!r} already has a live claim"
            ) from exc
        raise
    return row


async def get_name_claim(session: AsyncSession, *, claim_id: UUID) -> NameClaim | None:
    return await session.get(NameClaim, claim_id)


async def list_name_claims(
    session: AsyncSession,
    *,
    netuid: int,
    include_withdrawn: bool = False,
) -> list[NameClaim]:
    stmt = select(NameClaim).where(NameClaim.netuid == netuid)
    if not include_withdrawn:
        stmt = stmt.where(NameClaim.status.in_(("pending", "upheld")))
    stmt = stmt.order_by(NameClaim.created_at.asc())
    return list((await session.execute(stmt)).scalars().all())


async def list_endorsements(
    session: AsyncSession, *, claim_ids: Sequence[UUID]
) -> dict[UUID, list[NameClaimEndorsement]]:
    if not claim_ids:
        return {}
    rows = list(
        (
            await session.execute(
                select(NameClaimEndorsement)
                .where(NameClaimEndorsement.claim_id.in_(claim_ids))
                .order_by(NameClaimEndorsement.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    by_claim: dict[UUID, list[NameClaimEndorsement]] = {
        claim_id: [] for claim_id in claim_ids
    }
    for row in rows:
        by_claim.setdefault(row.claim_id, []).append(row)
    return by_claim


async def record_endorsement(
    session: AsyncSession,
    *,
    claim: NameClaim,
    endorser_hotkey: str,
    endorser_owner_root: str,
    endorser_key_kind: str,
    endorser_signer: str,
    endorser_signature: str,
    nonce: UUID,
    issued_at: datetime,
    now: datetime,
) -> tuple[NameClaimEndorsement, NameClaim]:
    """Insert an endorsement and uphold the claim if the threshold is met."""
    endorsement = NameClaimEndorsement(
        endorsement_id=uuid4(),
        claim_id=claim.claim_id,
        endorser_hotkey=endorser_hotkey,
        endorser_owner_root=endorser_owner_root,
        endorser_key_kind=endorser_key_kind,
        endorser_signer=endorser_signer,
        endorser_signature=endorser_signature,
        nonce=nonce,
        issued_at=issued_at,
    )
    session.add(endorsement)
    try:
        await session.flush()
    except SAIntegrityError as exc:
        message = str(exc.orig) if getattr(exc, "orig", None) is not None else str(exc)
        if "name_claim_endorsements_nonce_key" in message:
            raise NameClaimReplayedError(
                "name-claim endorsement nonce has already been used"
            ) from exc
        if "name_claim_endorsements_owner_key" in message:
            raise NameClaimConflictError(
                "this owner family has already endorsed this claim"
            ) from exc
        raise
    count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(NameClaimEndorsement)
                .where(NameClaimEndorsement.claim_id == claim.claim_id)
            )
        ).scalar_one()
    )
    if claim.status == "pending" and count >= ENDORSEMENT_THRESHOLD:
        claim.status = "upheld"
        claim.upheld_at = now
        await session.flush()
    return endorsement, claim


async def withdraw_name_claim(
    session: AsyncSession,
    *,
    claim: NameClaim,
    withdrawn_signer: str,
    withdrawn_signature: str,
    now: datetime,
) -> NameClaim:
    claim.status = "withdrawn"
    claim.withdrawn_at = now
    claim.upheld_at = None
    claim.withdrawn_signer = withdrawn_signer
    claim.withdrawn_signature = withdrawn_signature
    await session.flush()
    return claim


async def reserved_stem_owner_root(
    session: AsyncSession, *, netuid: int, name_stem: str
) -> str | None:
    """Owner root that currently holds an upheld reservation for ``name_stem``."""
    result = await session.execute(
        select(NameClaim).where(
            NameClaim.netuid == netuid,
            NameClaim.name_stem == name_stem,
            NameClaim.status == "upheld",
        )
    )
    claim = result.scalar_one_or_none() if result is not None else None
    if claim is None:
        return None
    return await owner_root_for_hotkey(session, hotkey=claim.claimant_hotkey)


async def upload_name_is_reserved(
    session: AsyncSession,
    *,
    netuid: int,
    agent_name: str,
    miner_hotkey: str,
    miner_coldkey: str | None,
) -> str | None:
    """Return a rejection message if ``agent_name`` is reserved to someone else."""
    stem = normalize_name_stem(agent_name)
    if not stem:
        return None
    reserved_root = await reserved_stem_owner_root(
        session, netuid=netuid, name_stem=stem
    )
    if reserved_root is None:
        return None
    raw = emission_owner(miner_hotkey=miner_hotkey, miner_coldkey=miner_coldkey)
    uploader_root = (
        await attested_emission_owner_roots(session, [(miner_hotkey, raw)])
    )[0]
    if uploader_root == reserved_root:
        return None
    return (
        f"handle {stem!r} is reserved to another miner family after a "
        "signed, endorsed name claim; upload under a different name"
    )


async def active_handle_claims(
    session: AsyncSession, *, netuid: int
) -> dict[str, ActiveHandleClaim]:
    """Live claims keyed by stem, with the claimant's current owner root."""
    claims = await list_name_claims(session, netuid=netuid, include_withdrawn=False)
    out: dict[str, ActiveHandleClaim] = {}
    for claim in claims:
        root = await owner_root_for_hotkey(session, hotkey=claim.claimant_hotkey)
        out[claim.name_stem] = ActiveHandleClaim(
            claim_id=claim.claim_id,
            name_stem=claim.name_stem,
            claimant_hotkey=claim.claimant_hotkey,
            status=claim.status,
            claimant_owner_root=root,
        )
    return out
