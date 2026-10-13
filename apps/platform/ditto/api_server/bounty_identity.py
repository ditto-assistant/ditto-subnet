"""Mandatory identity/consent guard for the proposed bounty-claim engine.

This module does not choose a payee. The claim's signed coldkey and its
finalized Owner(hotkey) binding remain the payment authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from ditto.api_server.cla import ClaConsentRequired, evaluate_cla_gate
from ditto.db.queries.contributor_identity import (
    active_github_link,
    lock_identity_writes,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_server.github_link import GithubLinkClient


class BountyIdentityRejected(Exception):
    pass


@dataclass(frozen=True)
class VerifiedBountyContributor:
    hotkey: str
    github_link_id: UUID
    github_user_id: int
    github_login: str
    cla_signature_id: UUID | None


async def require_verified_bounty_contributor(
    session: AsyncSession,
    *,
    netuid: int,
    hotkey: str,
    github_login: str,
    github: GithubLinkClient,
) -> VerifiedBountyContributor:
    link = await active_github_link(session, netuid=netuid, hotkey=hotkey)
    if link is None or link.github_login.casefold() != github_login.casefold():
        raise BountyIdentityRejected(
            "github_login must match the hotkey's active verified link; "
            "run `ditto link-github`"
        )
    identity = await github.public_identity(link.github_login)
    if (
        identity.user_id != link.github_user_id
        or identity.login.casefold() != github_login.casefold()
    ):
        raise BountyIdentityRejected(
            "GitHub login was renamed or reassigned; re-link before claiming"
        )
    # Do not block link writers on the external request. Caller records the
    # claim in this transaction, after revalidating the exact proof under lock.
    await lock_identity_writes(session, namespace="github-link")
    current_link = await active_github_link(session, netuid=netuid, hotkey=hotkey)
    if current_link is None or (
        current_link.link_id,
        current_link.github_user_id,
        current_link.github_login,
    ) != (link.link_id, link.github_user_id, link.github_login):
        raise BountyIdentityRejected(
            "verified GitHub link changed during verification; re-link or retry"
        )
    link = current_link
    await lock_identity_writes(session, namespace="cla-policy")
    consent = await evaluate_cla_gate(session, netuid=netuid, hotkey=hotkey)
    if not consent.allowed:
        raise ClaConsentRequired(consent.message)
    return VerifiedBountyContributor(
        hotkey=hotkey,
        github_link_id=link.link_id,
        github_user_id=link.github_user_id,
        github_login=identity.login,
        cla_signature_id=consent.signature_id,
    )
