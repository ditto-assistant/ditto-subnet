"""Consent gate decisions, independent of payment and existing scored agents."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from ditto.db.models import ClaShadowUpload
from ditto.db.queries.contributor_identity import (
    current_cla_settings,
    current_cla_signature,
    current_cla_version,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
CLA_REQUIRED_ERROR_CODE = 1106


class ClaConsentRequired(Exception):
    """No valid receipt for the currently effective contributor agreement."""


@dataclass(frozen=True)
class ClaGateDecision:
    mode: str
    allowed: bool
    signature_id: UUID | None
    required_version: int | None
    message: str


async def evaluate_cla_gate(
    session: AsyncSession, *, netuid: int, hotkey: str
) -> ClaGateDecision:
    settings = await current_cla_settings(session)
    version = await current_cla_version(session)
    signature = await current_cla_signature(
        session, netuid=netuid, hotkey=hotkey, version=version
    )
    if settings.mode not in ("off", "shadow", "enforce"):
        raise RuntimeError("invalid CLA mode")
    message = (
        f"Contributor agreement v{version.version} is required; run `ditto cla "
        f"sign` before submitting."
        if version
        else (
            "No effective contributor agreement is published; contact the "
            "operator before submitting."
        )
    )
    return ClaGateDecision(
        mode=settings.mode,
        allowed=settings.mode != "enforce" or signature is not None,
        signature_id=signature.signature_id if signature else None,
        required_version=version.version if version else None,
        message=message,
    )


async def record_shadow_upload(
    session: AsyncSession, *, netuid: int, hotkey: str, decision: ClaGateDecision
) -> None:
    if decision.mode == "shadow" and decision.signature_id is None:
        logger.warning(
            "unsigned CLA upload in shadow mode: hotkey=%s version=%s",
            hotkey,
            decision.required_version,
        )
        session.add(
            ClaShadowUpload(
                netuid=netuid,
                hotkey=hotkey,
                cla_version=decision.required_version,
                created_at=datetime.now(UTC),
            )
        )
        await session.flush()
