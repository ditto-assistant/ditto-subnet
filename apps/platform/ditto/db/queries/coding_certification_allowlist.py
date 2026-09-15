"""Strict, append-only operator allowlist for the shadow certification path.

Refuse-all is the default. With no revision, a refuse-all (``enabled=false``)
revision, or a stored revision that cannot be parsed or whose checksum
disagrees, Platform refuses every certification lease issue, claim, harness
launch, inference grant, and receipt. Only an intact enabled revision admits
anything, and then only its exact ``(agent_id, artifact_sha256,
screened_image_sha256, validator_hotkey)`` tuples. The screened-image digest is
the agent's verified ``agents.screened_image_sha256`` (the digest every lease,
receipt, and core-qualification observation binds), so a rebuilt image never
matches a tuple written for the previous one. No revision can reopen global access.

Lock order: every transaction that authorizes on the allowlist takes the shared
transaction advisory lock before it locks any lease, agent, or grant row, and a
revision write takes it exclusively before it locks leases and grants. That
serializes writes with authorizing reads and cannot deadlock against them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_certification_admin import (
    CODING_CERTIFICATION_ALLOWLIST_MAX_ENTRIES,
    CodingCertificationAllowlistEntry,
    CodingCertificationAllowlistKey,
    CodingCertificationAllowlistRevision,
    canonical_coding_certification_allowlist_entries,
    coding_certification_allowlist_checksum,
    coding_certification_allowlist_entry_json,
)
from ditto.db.models import (
    CodingCertificationAllowlistRevision as CodingCertificationAllowlistRevisionRow,
)

CODING_CERTIFICATION_NOT_ALLOWLISTED = "coding certification is not allowlisted"
_LOCK_KEY = "ditto:coding-certification-allowlist"


class CodingCertificationAllowlistRefusedError(RuntimeError):
    """The current allowlist does not admit this exact tuple."""

    def __init__(self) -> None:
        super().__init__(CODING_CERTIFICATION_NOT_ALLOWLISTED)


class CodingCertificationAllowlistRevisionConflictError(RuntimeError):
    """The caller's expected revision is no longer current."""

    def __init__(self, current_revision: int) -> None:
        super().__init__(
            "coding certification allowlist changed; re-read it and submit "
            f"expected_revision={current_revision}"
        )
        self.current_revision = current_revision


@dataclass(frozen=True)
class CodingCertificationAllowlist:
    """The enforced allowlist for the rest of one transaction."""

    revision: int
    """Latest stored revision, or ``0`` when none exists (refuse all)."""
    tuples: frozenset[CodingCertificationAllowlistKey]

    def admits(
        self,
        *,
        agent_id: UUID,
        artifact_sha256: str,
        screened_image_sha256: str | None,
        validator_hotkey: str,
    ) -> bool:
        if screened_image_sha256 is None:
            return False
        return (
            str(agent_id),
            artifact_sha256,
            screened_image_sha256,
            validator_hotkey,
        ) in self.tuples


async def _lock(session: AsyncSession, *, shared: bool) -> None:
    """Serialize allowlist writes with every read that authorizes on it."""

    if session.get_bind().dialect.name != "postgresql":  # pragma: no cover
        return
    lock = func.pg_advisory_xact_lock_shared if shared else func.pg_advisory_xact_lock
    await session.execute(select(lock(func.hashtextextended(_LOCK_KEY, 0))))


async def latest_coding_certification_allowlist(
    session: AsyncSession,
) -> CodingCertificationAllowlistRevisionRow | None:
    return await session.scalar(
        select(CodingCertificationAllowlistRevisionRow)
        .order_by(CodingCertificationAllowlistRevisionRow.revision.desc())
        .limit(1)
    )


async def list_coding_certification_allowlist_revisions(
    session: AsyncSession,
    *,
    limit: int,
) -> Sequence[CodingCertificationAllowlistRevisionRow]:
    return list(
        await session.scalars(
            select(CodingCertificationAllowlistRevisionRow)
            .order_by(CodingCertificationAllowlistRevisionRow.revision.desc())
            .limit(limit)
        )
    )


def allowlist_entries_from_row(
    row: CodingCertificationAllowlistRevisionRow,
) -> list[CodingCertificationAllowlistEntry] | None:
    """Parse and verify one stored revision, or ``None`` if it is not intact."""

    if (
        not isinstance(row.entries, list)
        or len(row.entries) > CODING_CERTIFICATION_ALLOWLIST_MAX_ENTRIES
    ):
        return None
    try:
        entries = [
            CodingCertificationAllowlistEntry.model_validate(item)
            for item in row.entries
        ]
    except (TypeError, ValidationError):
        return None
    if len({entry.key() for entry in entries}) != len(entries):
        return None
    if not row.enabled and entries:
        return None
    if (
        coding_certification_allowlist_checksum(enabled=row.enabled, entries=entries)
        != row.checksum
    ):
        return None
    return entries


def _admitted_entries(
    row: CodingCertificationAllowlistRevisionRow,
) -> list[CodingCertificationAllowlistEntry]:
    """The tuples a stored revision admits; anything not intact admits none."""

    entries = allowlist_entries_from_row(row)
    if entries is None or not row.enabled:
        return []
    return entries


def allowlist_revision_from_row(
    row: CodingCertificationAllowlistRevisionRow,
) -> CodingCertificationAllowlistRevision:
    intact = allowlist_entries_from_row(row) is not None
    admitted = _admitted_entries(row)
    return CodingCertificationAllowlistRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        enabled=row.enabled,
        integrity="valid" if intact else "invalid",
        effective="exact_tuples" if admitted else "refuse_all",
        entries=admitted,
        checksum=row.checksum,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


def default_coding_certification_allowlist() -> CodingCertificationAllowlistRevision:
    return CodingCertificationAllowlistRevision(
        revision=0,
        parent_revision=0,
        enabled=False,
        integrity="valid",
        effective="refuse_all",
        entries=[],
        checksum=coding_certification_allowlist_checksum(enabled=False, entries=[]),
        reason="Built-in default: coding certification refused for every tuple",
        actor="platform",
        created_at=None,
    )


async def active_coding_certification_allowlist(
    session: AsyncSession,
) -> CodingCertificationAllowlist:
    """Return the enforced allowlist, refusing everything unless intact and enabled.

    Takes the shared allowlist lock for the rest of the transaction, so a
    concurrent revision cannot commit between this read and the caller's write.
    Call it before locking any lease, agent, or grant row.
    """

    await _lock(session, shared=True)
    row = await latest_coding_certification_allowlist(session)
    if row is None:
        return CodingCertificationAllowlist(revision=0, tuples=frozenset())
    return CodingCertificationAllowlist(
        revision=row.revision,
        tuples=frozenset(entry.key() for entry in _admitted_entries(row)),
    )


async def insert_coding_certification_allowlist_revision(
    session: AsyncSession,
    *,
    expected_revision: int,
    enabled: bool,
    entries: list[CodingCertificationAllowlistEntry],
    reason: str,
    actor: str,
) -> CodingCertificationAllowlistRevisionRow:
    """Append one complete revision under the exclusive allowlist lock.

    The caller must, in the same transaction, abort the in-flight leases and
    revoke the live grants the new revision refuses.
    """

    await _lock(session, shared=False)
    current = await latest_coding_certification_allowlist(session)
    current_revision = current.revision if current is not None else 0
    if expected_revision != current_revision:
        raise CodingCertificationAllowlistRevisionConflictError(current_revision)
    canonical = canonical_coding_certification_allowlist_entries(entries)
    row = CodingCertificationAllowlistRevisionRow(
        parent_revision=current_revision,
        enabled=enabled,
        entries=[
            coding_certification_allowlist_entry_json(entry) for entry in canonical
        ],
        checksum=coding_certification_allowlist_checksum(
            enabled=enabled, entries=canonical
        ),
        reason=reason,
        actor=actor,
    )
    session.add(row)
    await session.flush()
    return row
