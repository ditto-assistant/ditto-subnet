"""Mandatory two-phase identity/consent guard for bounty claims.

Prepare GitHub evidence without an acceptance transaction, then revalidate it
under locks in the transaction that records the claim. Neither phase chooses a
payee; the signed coldkey and finalized Owner(hotkey) remain payment authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from ditto.api_server.cla import ClaConsentRequired
from ditto.api_server.github_link import (
    GithubLinkRejected,
    GithubProviderUnavailable,
)
from ditto.db.queries.contributor_identity import (
    active_github_link,
    current_cla_signature,
    current_cla_version,
    lock_identity_writes,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.api_server.github_link import GithubLinkClient

_PREPARED = object()
_MAX_EVIDENCE_AGE = timedelta(minutes=2)


class BountyIdentityRejected(Exception):
    pass


class BountyIdentityUnavailable(Exception):
    """GitHub could not confirm the identity now; retry. Never a rejection."""


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
    cla_signature_id: UUID


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
    except GithubProviderUnavailable as exc:
        raise BountyIdentityUnavailable(str(exc)) from exc
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
    # Claims only read the link, so they share the lock; link writers take it
    # exclusively and wait for them.
    await lock_identity_writes(session, namespace="github-link", shared=True)
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
    # Bounty work is a code contribution to this repository, so a claim
    # always needs the current CLA, whatever the upload gate's rollout mode.
    version = await current_cla_version(session)
    signature = await current_cla_signature(
        session, netuid=netuid, hotkey=hotkey, version=version
    )
    if signature is None:
        raise ClaConsentRequired(
            f"Contributor agreement v{version.version} is required; run `ditto "
            "cla sign` before claiming a bounty."
            if version is not None
            else "No effective contributor agreement is published; bounty claims "
            "open once one is."
        )
    return VerifiedBountyContributor(
        hotkey=hotkey,
        github_link_id=link.link_id,
        github_user_id=link.github_user_id,
        github_login=evidence.github_login,
        cla_signature_id=signature.signature_id,
    )
