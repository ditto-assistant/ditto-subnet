"""Atomic k=3 validator ticket sets for one shadow coding run."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.chain.models import NeuronInfo
from ditto.db.models import CodingShadowRun, CodingShadowTicket
from ditto.db.queries.coding_evaluations import (
    CodingShadowConflictError,
    CodingShadowNotQualifiedError,
    issue_coding_shadow_ticket,
)


class CodingTicketSetUnavailableError(Exception):
    """Current validator-permit authority could not be resolved."""


class CodingValidatorPermitSource(Protocol):
    async def get_recent_neurons(self, netuid: int) -> Sequence[NeuronInfo]:
        """Return current neurons with ``hotkey`` and ``validator_permit`` fields."""


@dataclass(frozen=True)
class CodingTicketSetPolicy:
    validator_count: int = 3
    lease_seconds: int = 60 * 60

    def __post_init__(self) -> None:
        if self.validator_count != 3:
            raise ValueError("coding contract v1 requires exactly three validators")
        if not 60 <= self.lease_seconds <= 2 * 60 * 60:
            raise ValueError("coding ticket-set lease must be between 1m and 2h")


@dataclass(frozen=True)
class CodingTicketSetResult:
    tickets: tuple[CodingShadowTicket, CodingShadowTicket, CodingShadowTicket]
    idempotent: bool
    weight_eligible: bool = False


_DEFAULT_TICKET_SET_POLICY = CodingTicketSetPolicy()


def coding_shadow_ticket_id(
    *, ticket_set_id: UUID, run_row_id: UUID, validator_hotkey: str
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "\x00".join(
            (
                "dittobench-coding-ticket-set:v1",
                str(ticket_set_id),
                str(run_row_id),
                validator_hotkey,
            )
        ),
    )


def _aware(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _existing_set_authority(
    existing: Sequence[CodingShadowTicket],
    *,
    validators: tuple[str, ...],
    ticket_ids: tuple[UUID, ...],
    policy: CodingTicketSetPolicy,
) -> tuple[datetime, datetime] | None:
    if (
        len(existing) != 3
        or tuple(ticket.validator_hotkey for ticket in existing) != validators
        or tuple(ticket.ticket_id for ticket in existing) != ticket_ids
    ):
        return None
    issued_at = _aware(existing[0].issued_at)
    deadline = _aware(existing[0].deadline)
    if (
        deadline != issued_at + timedelta(seconds=policy.lease_seconds)
        or any(_aware(ticket.issued_at) != issued_at for ticket in existing)
        or any(_aware(ticket.deadline) != deadline for ticket in existing)
    ):
        return None
    return issued_at, deadline


async def issue_coding_shadow_ticket_set(
    session: AsyncSession,
    *,
    permit_source: CodingValidatorPermitSource,
    netuid: int,
    run_row_id: UUID,
    ticket_set_id: UUID,
    validator_hotkeys: Sequence[str],
    policy: CodingTicketSetPolicy = _DEFAULT_TICKET_SET_POLICY,
) -> CodingTicketSetResult:
    """Issue one all-or-nothing validator set over an already exposed run."""

    validators = tuple(validator_hotkeys)
    if len(validators) != policy.validator_count:
        raise ValueError("coding ticket set must contain exactly three validators")
    if validators != tuple(sorted(validators)) or len(set(validators)) != len(
        validators
    ):
        raise ValueError("coding ticket validators must be unique and sorted")
    if netuid < 1:
        raise ValueError("coding ticket set requires a positive netuid")

    ticket_ids = tuple(
        coding_shadow_ticket_id(
            ticket_set_id=ticket_set_id,
            run_row_id=run_row_id,
            validator_hotkey=hotkey,
        )
        for hotkey in validators
    )
    existing = list(
        await session.scalars(
            select(CodingShadowTicket)
            .where(CodingShadowTicket.run_row_id == run_row_id)
            .order_by(CodingShadowTicket.validator_hotkey)
        )
    )
    if existing:
        stored_authority = _existing_set_authority(
            existing,
            validators=validators,
            ticket_ids=ticket_ids,
            policy=policy,
        )
        if stored_authority is None:
            raise CodingShadowConflictError(
                "coding run already has a partial or different validator ticket set"
            )
        issued_at, deadline = stored_authority
        replay_rows: list[CodingShadowTicket] = []
        async with session.begin_nested():
            for hotkey, ticket_id in zip(validators, ticket_ids, strict=True):
                replayed = await issue_coding_shadow_ticket(
                    session,
                    run_row_id=run_row_id,
                    ticket_id=ticket_id,
                    validator_hotkey=hotkey,
                    issued_at=issued_at,
                    deadline=deadline,
                )
                if not isinstance(replayed.row, CodingShadowTicket):
                    raise RuntimeError("coding ticket issuer returned another row type")
                if not replayed.idempotent:
                    raise CodingShadowConflictError(
                        "stored coding ticket set did not replay idempotently"
                    )
                replay_rows.append(replayed.row)
        return CodingTicketSetResult(
            tickets=(replay_rows[0], replay_rows[1], replay_rows[2]),
            idempotent=True,
        )

    try:
        neurons = await permit_source.get_recent_neurons(netuid)
    except Exception as error:
        raise CodingTicketSetUnavailableError(
            "validator permit snapshot is unavailable"
        ) from error
    permitted = {neuron.hotkey for neuron in neurons if neuron.validator_permit}
    if not set(validators).issubset(permitted):
        raise CodingShadowNotQualifiedError(
            "coding ticket set contains an unpermitted validator"
        )

    run = await session.get(CodingShadowRun, run_row_id, with_for_update=True)
    if run is None:
        raise CodingShadowNotQualifiedError("coding shadow run does not exist")
    existing = list(
        await session.scalars(
            select(CodingShadowTicket)
            .where(CodingShadowTicket.run_row_id == run_row_id)
            .order_by(CodingShadowTicket.validator_hotkey)
        )
    )
    if existing:
        stored_authority = _existing_set_authority(
            existing,
            validators=validators,
            ticket_ids=ticket_ids,
            policy=policy,
        )
        if stored_authority is None:
            raise CodingShadowConflictError(
                "coding run already has a partial or different validator ticket set"
            )
        issued_at, deadline = stored_authority
    else:
        database_now = await session.scalar(select(func.clock_timestamp()))
        if not isinstance(database_now, datetime):  # pragma: no cover - DB invariant
            raise RuntimeError("database clock did not return a timestamp")
        issued_at = _aware(database_now)
        deadline = issued_at + timedelta(seconds=policy.lease_seconds)

    rows: list[CodingShadowTicket] = []
    idempotent = True
    async with session.begin_nested():
        for hotkey, ticket_id in zip(validators, ticket_ids, strict=True):
            issued = await issue_coding_shadow_ticket(
                session,
                run_row_id=run_row_id,
                ticket_id=ticket_id,
                validator_hotkey=hotkey,
                issued_at=issued_at,
                deadline=deadline,
            )
            if not isinstance(issued.row, CodingShadowTicket):
                raise RuntimeError("coding ticket issuer returned another row type")
            rows.append(issued.row)
            idempotent = idempotent and issued.idempotent
    if len(rows) != 3:  # pragma: no cover - fixed loop invariant
        raise RuntimeError("coding ticket set is incomplete")
    return CodingTicketSetResult(
        tickets=(rows[0], rows[1], rows[2]),
        idempotent=idempotent,
    )
