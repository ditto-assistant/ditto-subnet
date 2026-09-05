"""Append-only persistence for private Coding v2 release authorities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ditto.api_models.coding_private_v2_registry import (
    CodingPrivateV2PublicationReceipt,
    CodingPrivateV2RegistrationAuthority,
    private_v2_release_event_digest,
)
from ditto.db.models import CodingPrivateV2Release, CodingPrivateV2ReleaseEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

ReleaseAction = Literal["quarantined", "retired"]


class CodingPrivateV2ReleaseConflictError(Exception):
    """An immutable release or lifecycle identity changed."""


class CodingPrivateV2ReleaseInactiveError(Exception):
    """The named release is absent or terminal for the requested transition."""


@dataclass(frozen=True)
class CodingPrivateV2ReleaseInsertResult:
    row: CodingPrivateV2Release
    idempotent: bool


@dataclass(frozen=True)
class CodingPrivateV2ReleaseEventResult:
    row: CodingPrivateV2ReleaseEvent
    idempotent: bool


@dataclass(frozen=True)
class CodingPrivateV2ReleaseBundle:
    release: CodingPrivateV2Release
    events: tuple[CodingPrivateV2ReleaseEvent, ...]

    @property
    def status(self) -> Literal["registered", "quarantined", "retired"]:
        actions = {event.action for event in self.events}
        if "retired" in actions:
            return "retired"
        if "quarantined" in actions:
            return "quarantined"
        return "registered"

    @property
    def latest_event(self) -> CodingPrivateV2ReleaseEvent | None:
        return self.events[-1] if self.events else None


async def get_private_v2_release(
    session: AsyncSession,
    *,
    corpus_release_id: str,
    for_update: bool = False,
) -> CodingPrivateV2Release | None:
    statement = select(CodingPrivateV2Release).where(
        CodingPrivateV2Release.corpus_release_id == corpus_release_id
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


def private_v2_release_matches(
    row: CodingPrivateV2Release,
    *,
    registration: CodingPrivateV2RegistrationAuthority,
    receipt: CodingPrivateV2PublicationReceipt,
) -> bool:
    return (
        row.corpus_release_id == registration.corpus_release_id
        and row.coding_contract_version == 2
        and row.private_release_sha256 == registration.private_release_sha256
        and row.catalog_sha256 == registration.catalog_sha256
        and row.catalog_merkle_root == registration.catalog_merkle_root
        and row.payload_sha256 == registration.payload_sha256
        and row.transport_sha256 == registration.transport_sha256
        and row.wrapping_key_sha256 == registration.wrapping_key_sha256
        and row.publication_receipt_sha256 == registration.publication_receipt_sha256
        and row.provider_probe_receipt_sha256 == receipt.probe_receipt_payload_sha256
        and row.private_input_authority_sha256 == receipt.private_input_authority_sha256
        and row.curator_signing_key_sha256 == receipt.curator_signing_key_sha256
        and row.publication_source_sha == receipt.source_sha
        and row.publication_object_count == receipt.object_count
        and row.previous_registration_sha256 is None
        and row.registration_sha256 == registration.registration_sha256
        and row.registration_authority
        == registration.model_dump(mode="json", by_alias=True)
        and row.shadow_only is True
        and row.selectable is False
        and row.weight_eligible is False
    )


async def insert_private_v2_release(
    session: AsyncSession,
    *,
    registration: CodingPrivateV2RegistrationAuthority,
    receipt: CodingPrivateV2PublicationReceipt,
    reason: str,
    actor: str,
) -> CodingPrivateV2ReleaseInsertResult:
    values = {
        "release_row_id": uuid4(),
        "corpus_release_id": registration.corpus_release_id,
        "coding_contract_version": 2,
        "private_release_sha256": registration.private_release_sha256,
        "catalog_sha256": registration.catalog_sha256,
        "catalog_merkle_root": registration.catalog_merkle_root,
        "payload_sha256": registration.payload_sha256,
        "transport_sha256": registration.transport_sha256,
        "wrapping_key_sha256": registration.wrapping_key_sha256,
        "publication_receipt_sha256": registration.publication_receipt_sha256,
        "provider_probe_receipt_sha256": receipt.probe_receipt_payload_sha256,
        "private_input_authority_sha256": receipt.private_input_authority_sha256,
        "curator_signing_key_sha256": receipt.curator_signing_key_sha256,
        "publication_source_sha": receipt.source_sha,
        "publication_object_count": receipt.object_count,
        "previous_registration_sha256": None,
        "registration_sha256": registration.registration_sha256,
        "registration_authority": registration.model_dump(mode="json", by_alias=True),
        "shadow_only": True,
        "selectable": False,
        "weight_eligible": False,
        "reason": reason.strip(),
        "actor": actor.strip(),
    }
    inserted_id = await session.scalar(
        pg_insert(CodingPrivateV2Release)
        .values(**values)
        .on_conflict_do_nothing()
        .returning(CodingPrivateV2Release.release_row_id)
    )
    if inserted_id is not None:
        row = await session.get(CodingPrivateV2Release, inserted_id)
        if row is None:  # pragma: no cover
            raise RuntimeError("inserted private v2 release was not readable")
        return CodingPrivateV2ReleaseInsertResult(row=row, idempotent=False)
    row = await get_private_v2_release(
        session, corpus_release_id=registration.corpus_release_id
    )
    if row is None or not private_v2_release_matches(
        row, registration=registration, receipt=receipt
    ):
        raise CodingPrivateV2ReleaseConflictError(
            "private v2 release identity already names different authority"
        )
    return CodingPrivateV2ReleaseInsertResult(row=row, idempotent=True)


async def append_private_v2_release_event(
    session: AsyncSession,
    *,
    corpus_release_id: str,
    expected_registration_sha256: str,
    action: ReleaseAction,
    reason: str,
    actor: str,
) -> CodingPrivateV2ReleaseEventResult:
    release = await get_private_v2_release(
        session,
        corpus_release_id=corpus_release_id,
        for_update=True,
    )
    if release is None:
        raise CodingPrivateV2ReleaseInactiveError("private v2 release does not exist")
    if release.registration_sha256 != expected_registration_sha256:
        raise CodingPrivateV2ReleaseConflictError(
            "private v2 registration changed; re-read before transition"
        )
    existing_events = tuple(
        (
            await session.scalars(
                select(CodingPrivateV2ReleaseEvent)
                .where(
                    CodingPrivateV2ReleaseEvent.release_row_id == release.release_row_id
                )
                .order_by(
                    CodingPrivateV2ReleaseEvent.created_at,
                    CodingPrivateV2ReleaseEvent.event_id,
                )
            )
        ).all()
    )
    existing = next(
        (event for event in existing_events if event.action == action), None
    )
    event_sha256 = private_v2_release_event_digest(
        registration_sha256=expected_registration_sha256,
        action=action,
        reason=reason,
        actor=actor,
    )
    if existing is not None:
        if (
            existing.event_sha256 != event_sha256
            or existing.reason != reason.strip()
            or existing.actor != actor.strip()
        ):
            raise CodingPrivateV2ReleaseConflictError(
                "private v2 lifecycle event already has different audit authority"
            )
        return CodingPrivateV2ReleaseEventResult(row=existing, idempotent=True)
    if action == "quarantined" and any(
        event.action == "retired" for event in existing_events
    ):
        raise CodingPrivateV2ReleaseInactiveError(
            "retired private v2 release cannot be quarantined"
        )
    inserted_id = await session.scalar(
        pg_insert(CodingPrivateV2ReleaseEvent)
        .values(
            event_id=uuid4(),
            release_row_id=release.release_row_id,
            expected_registration_sha256=expected_registration_sha256,
            action=action,
            event_sha256=event_sha256,
            shadow_only=True,
            selectable=False,
            weight_eligible=False,
            reason=reason.strip(),
            actor=actor.strip(),
        )
        .on_conflict_do_nothing()
        .returning(CodingPrivateV2ReleaseEvent.event_id)
    )
    if inserted_id is not None:
        row = await session.get(CodingPrivateV2ReleaseEvent, inserted_id)
        if row is None:  # pragma: no cover
            raise RuntimeError("inserted private v2 event was not readable")
        return CodingPrivateV2ReleaseEventResult(row=row, idempotent=False)
    concurrent = await session.scalar(
        select(CodingPrivateV2ReleaseEvent).where(
            CodingPrivateV2ReleaseEvent.release_row_id == release.release_row_id,
            CodingPrivateV2ReleaseEvent.action == action,
        )
    )
    if concurrent is None or concurrent.event_sha256 != event_sha256:
        raise CodingPrivateV2ReleaseConflictError(
            "private v2 lifecycle transition changed concurrently"
        )
    return CodingPrivateV2ReleaseEventResult(row=concurrent, idempotent=True)


async def list_private_v2_releases(
    session: AsyncSession,
    *,
    limit: int,
) -> tuple[list[CodingPrivateV2ReleaseBundle], int]:
    total = int(
        await session.scalar(select(func.count()).select_from(CodingPrivateV2Release))
        or 0
    )
    releases = list(
        (
            await session.scalars(
                select(CodingPrivateV2Release)
                .order_by(
                    CodingPrivateV2Release.created_at.desc(),
                    CodingPrivateV2Release.release_row_id.desc(),
                )
                .limit(limit)
            )
        ).all()
    )
    if not releases:
        return [], total
    release_ids = [release.release_row_id for release in releases]
    events = list(
        (
            await session.scalars(
                select(CodingPrivateV2ReleaseEvent)
                .where(CodingPrivateV2ReleaseEvent.release_row_id.in_(release_ids))
                .order_by(
                    CodingPrivateV2ReleaseEvent.created_at,
                    CodingPrivateV2ReleaseEvent.event_id,
                )
            )
        ).all()
    )
    by_release: dict[object, list[CodingPrivateV2ReleaseEvent]] = {
        release_id: [] for release_id in release_ids
    }
    for event in events:
        by_release[event.release_row_id].append(event)
    return [
        CodingPrivateV2ReleaseBundle(
            release=release,
            events=tuple(by_release[release.release_row_id]),
        )
        for release in releases
    ], total


__all__ = [
    "CodingPrivateV2ReleaseBundle",
    "CodingPrivateV2ReleaseConflictError",
    "CodingPrivateV2ReleaseEventResult",
    "CodingPrivateV2ReleaseInactiveError",
    "CodingPrivateV2ReleaseInsertResult",
    "append_private_v2_release_event",
    "get_private_v2_release",
    "insert_private_v2_release",
    "list_private_v2_releases",
    "private_v2_release_matches",
]
