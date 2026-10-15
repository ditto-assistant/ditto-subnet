"""Mandatory two-phase identity/consent guard for the proposed claim engine.

Prepare GitHub evidence without an acceptance transaction, then revalidate it
under locks in the transaction that records the claim. Neither phase chooses a
payee; the signed coldkey and finalized Owner(hotkey) remain payment authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from ditto.api_server.cla import ClaConsentRequired, evaluate_cla_gate
from ditto.api_server.github_link import GithubLinkRejected
from ditto.db.queries.contributor_identity import (
    active_github_link,
    lock_identity_writes,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.api_server.github_link import GithubLinkClient

_PREPARED = object()
_MAX_EVIDENCE_AGE = timedelta(minutes=2)


class BountyIdentityRejected(Exception):
    pass


@dataclass(frozen=True)
class PreparedBountyIdentity:
    netuid: int
    hotkey: str
    github_link_id: UUID
    github_user_id: int
    recorded_login: str
    github_login: str
    verified_at: datetime
    _seal: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class VerifiedBountyContributor:
    hotkey: str
    github_link_id: UUID
    github_user_id: int
    github_login: str
    cla_signature_id: UUID | None


async def prepare_bounty_identity(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    netuid: int,
    hotkey: str,
    github_login: str,
    github: GithubLinkClient,
) -> PreparedBountyIdentity:
    """Read the immutable proof, release its connection, then query GitHub."""
    async with session_maker() as session:
        link = await active_github_link(session, hotkey=hotkey)
        if link is None or link.github_login.casefold() != github_login.casefold():
            raise BountyIdentityRejected(
                "github_login must match the hotkey's active verified link; "
                "run `ditto link-github`"
            )
        link_id, user_id, recorded_login = (
            link.link_id,
            link.github_user_id,
            link.github_login,
        )
    try:
        identity = await github.public_identity(recorded_login)
    except GithubLinkRejected as exc:
        raise BountyIdentityRejected(str(exc)) from exc
    if (
        identity.user_id != user_id
        or identity.login.casefold() != github_login.casefold()
    ):
        raise BountyIdentityRejected(
            "GitHub login was renamed or reassigned; re-link before claiming"
        )
    return PreparedBountyIdentity(
        netuid=netuid,
        hotkey=hotkey,
        github_link_id=link_id,
        github_user_id=user_id,
        recorded_login=recorded_login,
        github_login=identity.login,
        verified_at=datetime.now(UTC),
        _seal=_PREPARED,
    )


async def require_verified_bounty_contributor(
    session: AsyncSession,
    *,
    netuid: int,
    hotkey: str,
    github_login: str,
    evidence: PreparedBountyIdentity,
) -> VerifiedBountyContributor:
    """Use only locally prepared evidence, never a client-supplied JSON object."""
    now = datetime.now(UTC)
    if (
        # Check the type first: a client-supplied object must be rejected,
        # not fail with AttributeError while its fields are read.
        type(evidence) is not PreparedBountyIdentity
        or evidence._seal is not _PREPARED
        or evidence.netuid != netuid
        or evidence.hotkey != hotkey
        or evidence.github_login.casefold() != github_login.casefold()
        or evidence.verified_at.utcoffset() is None
        or evidence.verified_at > now
        or now - evidence.verified_at > _MAX_EVIDENCE_AGE
    ):
        raise BountyIdentityRejected("prepare fresh GitHub evidence for this claim")
    await lock_identity_writes(session, namespace="github-link")
    link = await active_github_link(session, hotkey=hotkey)
    if link is None or (
        link.link_id,
        link.github_user_id,
        link.github_login,
    ) != (evidence.github_link_id, evidence.github_user_id, evidence.recorded_login):
        raise BountyIdentityRejected(
            "verified GitHub link changed during verification; re-link or retry"
        )
    await lock_identity_writes(session, namespace="cla-policy", shared=True)
    consent = await evaluate_cla_gate(session, netuid=netuid, hotkey=hotkey)
    if not consent.allowed:
        raise ClaConsentRequired(consent.message)
    return VerifiedBountyContributor(
        hotkey=hotkey,
        github_link_id=link.link_id,
        github_user_id=link.github_user_id,
        github_login=evidence.github_login,
        cla_signature_id=consent.signature_id,
    )
