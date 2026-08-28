"""Platform-side lease control for relay-owned provider outage circuits.

The relay is the only writer that opens or closes a circuit. Platform consumes
that durable state under a row lock so every inference-dependent workload sees
one capacity decision: zero while cooling down, then exactly one half-open
probe. Parking preserves attempt counters and never mints a retry grant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import ProviderOutageCircuit, ValidatorTicket
from ditto.db.queries.inference import revoke_ticket_inference

OPENROUTER_PROVIDER = "openrouter"
PROVIDER_PROBE_TTL = timedelta(minutes=10)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def scoring_probe_key(*, validator_hotkey: str, slot_id: str) -> str:
    return f"{validator_hotkey}:{slot_id}"


@dataclass(frozen=True)
class ProviderWorkGate:
    circuit: ProviderOutageCircuit | None
    admitted: bool
    probe_required: bool = False


async def lock_provider_work_gate(
    session: AsyncSession,
    *,
    now: datetime,
    kind: str,
    key: str,
    provider: str = OPENROUTER_PROVIDER,
) -> ProviderWorkGate:
    """Lock the circuit and decide whether one workload may run.

    A missing/closed circuit is unlimited. An open circuit admits nothing
    before ``retry_at``. Afterwards the first caller may register one probe;
    the same probe may resume, while every sibling workload stays parked.
    """
    circuit = await session.scalar(
        select(ProviderOutageCircuit)
        .where(ProviderOutageCircuit.provider == provider)
        .with_for_update()
    )
    if circuit is None or circuit.state == "closed":
        return ProviderWorkGate(circuit=circuit, admitted=True)

    if circuit.probe_expires_at is not None and _aware(circuit.probe_expires_at) <= now:
        circuit.probe_kind = None
        circuit.probe_key = None
        circuit.probe_expires_at = None

    if _aware(circuit.retry_at) > now:
        return ProviderWorkGate(circuit=circuit, admitted=False)
    if circuit.probe_kind is None:
        return ProviderWorkGate(circuit=circuit, admitted=True, probe_required=True)
    return ProviderWorkGate(
        circuit=circuit,
        admitted=circuit.probe_kind == kind and circuit.probe_key == key,
    )


def register_provider_probe(
    gate: ProviderWorkGate,
    *,
    now: datetime,
    kind: str,
    key: str,
) -> None:
    """Claim the single half-open slot on the already-locked circuit row."""
    if not gate.probe_required:
        return
    circuit = gate.circuit
    if circuit is None or circuit.state != "open":
        return
    circuit.probe_kind = kind
    circuit.probe_key = key
    circuit.probe_expires_at = now + PROVIDER_PROBE_TTL
    circuit.updated_at = now


async def park_scoring_leases(
    session: AsyncSession,
    *,
    circuit: ProviderOutageCircuit,
    now: datetime,
) -> int:
    """Expire all non-probe scoring leases without charging an attempt."""
    if circuit.state != "open":
        return 0
    probe_key = (
        circuit.probe_key
        if circuit.probe_kind == "scoring"
        and circuit.probe_expires_at is not None
        and _aware(circuit.probe_expires_at) > now
        else None
    )
    tickets = list(
        await session.scalars(
            select(ValidatorTicket)
            .where(ValidatorTicket.status == TicketStatus.ISSUED)
            .with_for_update(skip_locked=True)
        )
    )
    parked = 0
    for ticket in tickets:
        if probe_key == scoring_probe_key(
            validator_hotkey=ticket.validator_hotkey, slot_id=ticket.slot_id
        ):
            continue
        await revoke_ticket_inference(session, ticket=ticket, now=now)
        ticket.status = TicketStatus.EXPIRED
        ticket.deadline = now
        ticket.retry_after = max(now, _aware(circuit.retry_at))
        # One logical ticket may receive at most one no-fault outage resume.
        # A new circuit epoch is fresh fleet evidence, not a fresh spend
        # allowance for this ticket.  Once attempted_epoch is set, ordinary
        # finite retry policy (or an operator) owns every later retry.
        ticket.provider_outage_epoch = (
            circuit.epoch if ticket.provider_outage_attempted_epoch is None else None
        )
        ticket.failure_reason = "infrastructure"
        ticket.failure_detail = "provider_outage_parked"
        ticket.failed_at = now
        parked += 1
    return parked


__all__ = [
    "OPENROUTER_PROVIDER",
    "ProviderWorkGate",
    "lock_provider_work_gate",
    "park_scoring_leases",
    "register_provider_probe",
    "scoring_probe_key",
]
