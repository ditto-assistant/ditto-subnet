"""Backroom airdrop runs (SN-490): preview, idempotent execute and report.

An airdrop pays one asset to a list of recipients in one run:

- **Preview** (``dry_run``, the default) resolves every recipient, refuses
  bad rows, applies the per-run cap, checks the funding balance and returns a
  digest plus the exact confirmation phrase. It writes nothing.
- **Execute** re-derives the preview from the same input, requires the echoed
  digest and phrase, and records the run and every recipient as ``pending``
  in one committed transaction before anything is settled. Each recipient is
  then claimed (``pending`` -> ``dispatching``, committed) before its
  executor is called, and its outcome is recorded afterwards.
- **Idempotency.** The run's key is unique and its insert is serialized on
  an advisory lock. A retry with the same key and the same content resumes the
  run and settles only rows still ``pending``. A row that was claimed is
  never sent again, and a crash mid-send leaves it ``dispatching`` for
  reconciliation. The same key with different content is refused.

Which asset an airdrop really moves is SN-490's open decision. Settlement
goes through an :class:`AirdropExecutor` registered per ``(asset, mode)``.
Only :class:`SimulatedAirdropExecutor` exists, registered for ``rehearsal``,
so a ``live`` run is refused for every asset until a reviewed live executor
is wired in.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from fastapi import HTTPException
from scalecodec.utils.ss58 import ss58_decode
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.airdrop import (
    AIRDROP_ASSETS,
    AIRDROP_MAX_RECIPIENTS,
    AIRDROP_MAX_TOTAL,
    AIRDROP_RECIPIENT_KIND,
    AIRDROP_UNIT,
    AirdropAsset,
    AirdropAssetPolicy,
    AirdropBalanceCheck,
    AirdropCapCheck,
    AirdropExecuteRequest,
    AirdropMode,
    AirdropPolicy,
    AirdropPreview,
    AirdropPreviewRecipient,
    AirdropPreviewRequest,
    AirdropRecipientResult,
    AirdropRunReport,
    AirdropRunStatus,
    AirdropRunSummary,
)
from ditto.db.models import AirdropRecipient, AirdropRun, MinerDittoLink

logger = logging.getLogger(__name__)

_LOCK_NAMESPACE = "airdrop-run:"
_SS58_ALPHABET = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
_ERROR_LIMIT = 500
_SAFE_INTEGER = 2**53 - 1
# A row left dispatching this long is surfaced as needing attention: its
# executor call never recorded an outcome (a crash or a lost worker).
STALE_DISPATCH_SECONDS = 600


# ─── Settlement and funding seams ────────────────────────────────────────────


@dataclass(frozen=True)
class AirdropSettlement:
    """What an executor reports for one recipient.

    ``uncertain`` means the send may or may not have landed: it is recorded
    as such and never retried automatically.
    """

    state: Literal["settled", "failed", "uncertain"]
    receipt: str | None = None
    error: str | None = None


class AirdropExecutor(Protocol):
    """Pays one recipient. ``live`` is true only for an executor that moves funds."""

    name: str
    live: bool

    async def settle(
        self,
        *,
        run_id: UUID,
        asset: AirdropAsset,
        recipient_key: str,
        payee: str,
        amount: int,
    ) -> AirdropSettlement: ...


class SimulatedAirdropExecutor:
    """Rehearsal settlement: a deterministic receipt, nothing moves."""

    name = "simulated"
    live = False

    async def settle(
        self,
        *,
        run_id: UUID,
        asset: AirdropAsset,
        recipient_key: str,
        payee: str,
        amount: int,
    ) -> AirdropSettlement:
        del run_id, asset, payee, amount
        return AirdropSettlement("settled", receipt=f"rehearsal:{recipient_key[:16]}")


class AirdropFundingSource(Protocol):
    """Reports how much of ``asset`` the source holding the airdrop can pay."""

    name: str

    async def available(self, asset: AirdropAsset) -> int | None: ...


def default_executors() -> dict[tuple[str, str], AirdropExecutor]:
    """Rehearsal for every asset. No live executor ships (SN-490)."""
    simulated = SimulatedAirdropExecutor()
    return {(asset, "rehearsal"): simulated for asset in AIRDROP_ASSETS}


@dataclass(frozen=True)
class AirdropRuntime:
    executors: Mapping[tuple[str, str], AirdropExecutor]
    funding: Mapping[str, AirdropFundingSource]

    def executor(self, asset: str, mode: str) -> AirdropExecutor | None:
        executor = self.executors.get((asset, mode))
        if executor is None:
            return None
        # A live run needs an executor that says it is live, and a rehearsal
        # must never be handed one that is: the mode is the operator's promise.
        if executor.live != (mode == "live"):
            return None
        return executor


def runtime_from_state(state: object) -> AirdropRuntime:
    executors = getattr(state, "airdrop_executors", None)
    funding = getattr(state, "airdrop_funding", None)
    return AirdropRuntime(
        executors=executors if executors is not None else default_executors(),
        funding=funding if funding is not None else {},
    )


def policy(runtime: AirdropRuntime) -> AirdropPolicy:
    def executor_name(asset: str, mode: str) -> str | None:
        executor = runtime.executor(asset, mode)
        return executor.name if executor is not None else None

    return AirdropPolicy(
        max_recipients=AIRDROP_MAX_RECIPIENTS,
        assets=[
            AirdropAssetPolicy(
                asset=asset,
                unit=AIRDROP_UNIT[asset],
                recipient_kind=AIRDROP_RECIPIENT_KIND[asset],
                max_total_amount=AIRDROP_MAX_TOTAL[asset],
                rehearsal_executor=executor_name(asset, "rehearsal"),
                live_executor=executor_name(asset, "live"),
                funding_source=(
                    runtime.funding[asset].name if asset in runtime.funding else None
                ),
            )
            for asset in AIRDROP_ASSETS
        ],
    )


# ─── Preview ─────────────────────────────────────────────────────────────────


def _valid_ss58(address: str) -> bool:
    if not 32 <= len(address) <= 64 or not set(address) <= _SS58_ALPHABET:
        return False
    try:
        ss58_decode(address, valid_ss58_format=42)
    except (TypeError, ValueError):
        return False
    return True


def recipient_key(idempotency_key: str, asset: str, kind: str, address: str) -> str:
    """The per-recipient idempotency key: one row per run key and recipient."""
    material = json.dumps(
        [idempotency_key, asset, kind, address], separators=(",", ":")
    ).encode()
    return hashlib.sha256(material).hexdigest()


def _digest(payload: AirdropPreviewRequest, rows: list[AirdropPreviewRecipient]) -> str:
    canonical = json.dumps(
        {
            "asset": payload.asset,
            "mode": payload.mode,
            "idempotency_key": payload.idempotency_key,
            "reason": payload.reason,
            "recipients": [
                [row.kind, row.address, row.amount, row.payee, row.refused]
                for row in rows
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def format_amount(asset: str, amount: int) -> str:
    if asset == "alpha":
        whole, fraction = divmod(amount, 10**9)
        text = f"{whole}.{fraction:09d}".rstrip("0").rstrip(".")
        return f"{text} ALPHA"
    return f"{amount} DITTO CREDITS"


def confirmation_phrase(asset: str, mode: str, total: int, count: int) -> str:
    noun = "RECIPIENT" if count == 1 else "RECIPIENTS"
    phrase = f"AIRDROP {format_amount(asset, total)} TO {count} {noun}"
    return f"{phrase} AS REHEARSAL" if mode == "rehearsal" else phrase


async def _resolve_accounts(
    session: AsyncSession, hotkeys: list[str]
) -> dict[str, str]:
    if not hotkeys:
        return {}
    rows = await session.execute(
        select(MinerDittoLink.miner_hotkey, MinerDittoLink.ditto_user_id).where(
            MinerDittoLink.miner_hotkey.in_(hotkeys),
            MinerDittoLink.revoked_at.is_(None),
        )
    )
    return dict(rows.tuples().all())


async def _prior_amounts(
    session: AsyncSession,
    *,
    asset: str,
    mode: str,
    idempotency_key: str,
    payees: list[str],
) -> dict[str, int]:
    if not payees:
        return {}
    rows = await session.execute(
        select(AirdropRecipient.payee, func.sum(AirdropRecipient.amount))
        .join(AirdropRun, AirdropRun.run_id == AirdropRecipient.run_id)
        .where(
            AirdropRun.asset == asset,
            AirdropRun.mode == mode,
            AirdropRun.idempotency_key != idempotency_key,
            AirdropRecipient.payee.in_(payees),
            # In-flight rows count: the retry this exists to catch is the one
            # made under a new key while the first run is still settling.
            AirdropRecipient.status != "failed",
        )
        .group_by(AirdropRecipient.payee)
    )
    return {payee: int(total) for payee, total in rows.all()}


async def _balance(
    runtime: AirdropRuntime, asset: AirdropAsset, required: int
) -> AirdropBalanceCheck:
    source = runtime.funding.get(asset)
    if source is None:
        return AirdropBalanceCheck(
            status="unavailable",
            source=None,
            available=None,
            required=required,
            detail=f"No funding source is configured for {asset} (SN-490).",
        )
    try:
        available = await source.available(asset)
    except Exception as error:  # noqa: BLE001 - a read failure is reported, not raised
        logger.warning("airdrop funding read failed for %s: %s", asset, error)
        available = None
    if available is None:
        return AirdropBalanceCheck(
            status="unavailable",
            source=source.name,
            available=None,
            required=required,
            detail=f"{source.name} could not report an available balance.",
        )
    # Every amount crosses into Backroom as a JS number; report at most the
    # largest safe integer, which is far above any per-run cap.
    available = min(available, _SAFE_INTEGER)
    sufficient = available >= required
    return AirdropBalanceCheck(
        status="sufficient" if sufficient else "insufficient",
        source=source.name,
        available=available,
        required=required,
        detail=(
            f"{source.name} holds enough for this run."
            if sufficient
            else f"{source.name} is short by {required - available}."
        ),
    )


async def build_preview(
    session: AsyncSession, runtime: AirdropRuntime, payload: AirdropPreviewRequest
) -> AirdropPreview:
    asset: AirdropAsset = payload.asset
    expected_kind = AIRDROP_RECIPIENT_KIND[asset]
    hotkeys = [
        entry.address
        for entry in payload.recipients
        if entry.kind == "account" and expected_kind == "account"
    ]
    links = await _resolve_accounts(session, hotkeys)

    rows: list[AirdropPreviewRecipient] = []
    seen: set[tuple[str, str]] = set()
    seen_payees: set[str] = set()
    for position, entry in enumerate(payload.recipients):
        refused: str | None = None
        payee: str | None = None
        if entry.kind != expected_kind:
            refused = f"{asset} pays {expected_kind} recipients only"
        elif not _valid_ss58(entry.address):
            refused = "not a valid SS58 address"
        elif (entry.kind, entry.address) in seen:
            refused = "duplicate recipient; combine the amounts into one row"
        elif entry.kind == "wallet":
            payee = entry.address
        else:
            payee = links.get(entry.address)
            if payee is None:
                refused = "hotkey has no active Sign in with Ditto link"
            elif payee in seen_payees:
                # Two hotkeys linked to one Ditto account would credit it twice.
                refused = "another row already pays this Ditto account"
                payee = None
        seen.add((entry.kind, entry.address))
        if payee is not None:
            seen_payees.add(payee)
        rows.append(
            AirdropPreviewRecipient(
                position=position,
                kind=entry.kind,
                address=entry.address,
                amount=entry.amount,
                payee=payee,
                recipient_key=(
                    None
                    if refused
                    else recipient_key(
                        payload.idempotency_key, asset, entry.kind, entry.address
                    )
                ),
                refused=refused,
                prior_amount=0,
            )
        )

    payable = [row for row in rows if row.refused is None]
    prior = await _prior_amounts(
        session,
        asset=asset,
        mode=payload.mode,
        idempotency_key=payload.idempotency_key,
        payees=sorted({row.payee for row in payable if row.payee is not None}),
    )
    rows = [
        row.model_copy(update={"prior_amount": prior.get(row.payee or "", 0)})
        for row in rows
    ]
    total = sum(row.amount for row in payable)
    cap = AirdropCapCheck(
        max_recipients=AIRDROP_MAX_RECIPIENTS,
        max_total_amount=AIRDROP_MAX_TOTAL[asset],
        recipient_count=len(payable),
        total_amount=total,
        within_cap=(
            len(payload.recipients) <= AIRDROP_MAX_RECIPIENTS
            and total <= AIRDROP_MAX_TOTAL[asset]
        ),
    )
    existing = await session.scalar(
        select(AirdropRun).where(AirdropRun.idempotency_key == payload.idempotency_key)
    )
    digest = _digest(payload, rows)
    matches = existing is not None and existing.digest == digest
    required = total
    if existing is not None and matches:
        # Resuming: rows already claimed have left the funding source.
        required = int(
            await session.scalar(
                select(func.coalesce(func.sum(AirdropRecipient.amount), 0)).where(
                    AirdropRecipient.run_id == existing.run_id,
                    AirdropRecipient.status == "pending",
                )
            )
            or 0
        )
    balance = await _balance(runtime, asset, required)
    executor = runtime.executor(asset, payload.mode)

    blockers: list[str] = []
    refused_count = len(rows) - len(payable)
    if refused_count:
        blockers.append(f"{refused_count} recipient row(s) refused; fix the list")
    if len(payload.recipients) > AIRDROP_MAX_RECIPIENTS:
        blockers.append(
            f"{len(payload.recipients)} recipients exceed the per-run cap of "
            f"{AIRDROP_MAX_RECIPIENTS}"
        )
    if total > AIRDROP_MAX_TOTAL[asset]:
        blockers.append(
            f"total {format_amount(asset, total)} exceeds the per-run cap of "
            f"{format_amount(asset, AIRDROP_MAX_TOTAL[asset])}"
        )
    if executor is None:
        blockers.append(
            f"no {payload.mode} executor is configured for {asset}; "
            "only rehearsals can run until SN-490 is decided"
        )
    if payload.mode == "live" and balance.status != "sufficient":
        blockers.append(f"live funding balance is {balance.status}")

    if existing is not None and not matches:
        blockers.append(
            f"idempotency key already names run {existing.run_id} with different "
            "contents; choose a new key or resume that run unchanged"
        )

    return AirdropPreview(
        asset=asset,
        unit=AIRDROP_UNIT[asset],
        mode=payload.mode,
        idempotency_key=payload.idempotency_key,
        reason=payload.reason,
        executor=executor.name if executor is not None else None,
        recipients=rows,
        recipient_count=len(payable),
        refused_count=refused_count,
        total_amount=total,
        cap=cap,
        balance=balance,
        blockers=blockers,
        ready=not blockers,
        existing_run_id=existing.run_id if existing is not None else None,
        existing_run_matches=matches if existing is not None else None,
        warnings=_warnings(asset, rows),
        confirmation_digest=digest,
        confirmation=confirmation_phrase(asset, payload.mode, total, len(payable)),
    )


def _warnings(asset: str, rows: list[AirdropPreviewRecipient]) -> list[str]:
    prior = [row for row in rows if row.refused is None and row.prior_amount]
    if not prior:
        return []
    amount = format_amount(asset, sum(row.prior_amount for row in prior))
    return [
        f"{len(prior)} recipient(s) already received or are being paid {amount} "
        "under other run keys; if this is a retry, resume the original key instead"
    ]


# ─── Execute ─────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


async def _insert_run(
    maker: async_sessionmaker[AsyncSession],
    preview: AirdropPreview,
    *,
    executor: AirdropExecutor,
    actor: str,
) -> tuple[UUID, bool]:
    """Record the run and its recipients, or find the one already recorded."""
    async with maker() as session, session.begin():
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(_LOCK_NAMESPACE + preview.idempotency_key, 0)
                )
            )
        )
        existing = await session.scalar(
            select(AirdropRun).where(
                AirdropRun.idempotency_key == preview.idempotency_key
            )
        )
        if existing is not None:
            if existing.digest != preview.confirmation_digest:
                raise HTTPException(
                    409,
                    f"idempotency key already names run {existing.run_id} with "
                    "different contents; nothing was paid again",
                )
            return existing.run_id, True
        run_id = uuid4()
        session.add(
            AirdropRun(
                run_id=run_id,
                idempotency_key=preview.idempotency_key,
                asset=preview.asset,
                mode=preview.mode,
                executor=executor.name,
                digest=preview.confirmation_digest,
                reason=preview.reason,
                actor=actor,
                recipient_count=preview.recipient_count,
                total_amount=preview.total_amount,
            )
        )
        await session.flush()
        for row in preview.recipients:
            if row.payee is None or row.recipient_key is None:
                raise HTTPException(409, "a refused recipient cannot be recorded")
            session.add(
                AirdropRecipient(
                    run_id=run_id,
                    position=row.position,
                    recipient_key=row.recipient_key,
                    kind=row.kind,
                    address=row.address,
                    payee=row.payee,
                    amount=row.amount,
                    status="pending",
                )
            )
        return run_id, False


@dataclass(frozen=True)
class _Claim:
    recipient_key: str
    payee: str
    amount: int


async def _claim(
    maker: async_sessionmaker[AsyncSession], run_id: UUID, position: int
) -> _Claim | None:
    """Move one row from pending to dispatching; None if another call has it."""
    async with maker() as session, session.begin():
        row = await session.scalar(
            select(AirdropRecipient)
            .where(
                AirdropRecipient.run_id == run_id,
                AirdropRecipient.position == position,
            )
            .with_for_update()
        )
        if row is None or row.status != "pending":
            return None
        row.status = "dispatching"
        row.attempted_at = _now()
        return _Claim(row.recipient_key, row.payee, row.amount)


async def _record(
    maker: async_sessionmaker[AsyncSession],
    run_id: UUID,
    position: int,
    outcome: AirdropSettlement,
) -> None:
    async with maker() as session, session.begin():
        row = await session.scalar(
            select(AirdropRecipient)
            .where(
                AirdropRecipient.run_id == run_id,
                AirdropRecipient.position == position,
            )
            .with_for_update()
        )
        if row is None or row.status != "dispatching":
            return
        row.status = outcome.state
        row.receipt = outcome.receipt
        row.error = outcome.error[:_ERROR_LIMIT] if outcome.error else None
        if outcome.state == "settled":
            row.settled_at = _now()


async def _complete_if_done(
    maker: async_sessionmaker[AsyncSession], run_id: UUID
) -> None:
    async with maker() as session, session.begin():
        run = await session.get(AirdropRun, run_id, with_for_update=True)
        if run is None or run.completed_at is not None:
            return
        open_rows = await session.scalar(
            select(func.count())
            .select_from(AirdropRecipient)
            .where(
                AirdropRecipient.run_id == run_id,
                AirdropRecipient.status.in_(("pending", "dispatching")),
            )
        )
        if not open_rows:
            run.completed_at = _now()


async def execute(
    maker: async_sessionmaker[AsyncSession],
    runtime: AirdropRuntime,
    payload: AirdropExecuteRequest,
    actor: str,
) -> tuple[AirdropPreview, AirdropRunReport]:
    """Record and settle a confirmed run; returns the preview it executed."""
    async with maker() as session:
        preview = await build_preview(session, runtime, payload)
    if payload.confirmation_digest is None or payload.confirmation is None:
        raise HTTPException(
            422, "dry_run=false requires confirmation_digest and confirmation"
        )
    if payload.confirmation_digest != preview.confirmation_digest:
        raise HTTPException(
            409,
            "confirmation_digest does not match the current preview; preview "
            "again and confirm what it shows",
        )
    if payload.confirmation != preview.confirmation:
        raise HTTPException(
            422, f"confirmation must be exactly: {preview.confirmation}"
        )
    if not preview.ready:
        raise HTTPException(409, "; ".join(preview.blockers))
    executor = runtime.executor(preview.asset, preview.mode)
    if executor is None:  # also a preview blocker; kept explicit for the type
        raise HTTPException(409, f"no {preview.mode} executor for {preview.asset}")

    run_id, resumed = await _insert_run(maker, preview, executor=executor, actor=actor)
    for row in preview.recipients:
        claimed = await _claim(maker, run_id, row.position)
        if claimed is None:
            continue
        try:
            outcome = await executor.settle(
                run_id=run_id,
                asset=preview.asset,
                recipient_key=claimed.recipient_key,
                payee=claimed.payee,
                amount=claimed.amount,
            )
        except Exception as error:  # noqa: BLE001 - the send may have landed
            logger.exception("airdrop settlement raised for run %s", run_id)
            outcome = AirdropSettlement(
                "uncertain", error=f"{type(error).__name__}: {error}"
            )
        await _record(maker, run_id, row.position, outcome)
    await _complete_if_done(maker, run_id)

    async with maker() as session:
        report = await run_report(session, run_id)
    if report is None:
        raise HTTPException(500, f"airdrop run {run_id} vanished after recording")
    return preview, report.model_copy(update={"resumed": resumed})


# ─── Reports ─────────────────────────────────────────────────────────────────


def _status(counts: Counter[str], stale: int) -> AirdropRunStatus:
    if stale or counts["failed"] or counts["uncertain"]:
        return "needs_attention"
    if counts["pending"] or counts["dispatching"]:
        return "in_progress"
    return "settled"


def _summary(run: AirdropRun, rows: list[AirdropRecipient]) -> AirdropRunSummary:
    counts = Counter(row.status for row in rows)
    now = _now()
    stale = sum(
        1
        for row in rows
        if row.status == "dispatching"
        and row.attempted_at is not None
        and (now - row.attempted_at).total_seconds() > STALE_DISPATCH_SECONDS
    )
    return AirdropRunSummary(
        run_id=run.run_id,
        idempotency_key=run.idempotency_key,
        asset=run.asset,  # type: ignore[arg-type]
        unit=AIRDROP_UNIT[run.asset],  # type: ignore[index]
        mode=run.mode,  # type: ignore[arg-type]
        executor=run.executor,
        reason=run.reason,
        actor=run.actor,
        digest=run.digest,
        created_at=run.created_at,
        completed_at=run.completed_at,
        status=_status(counts, stale),
        stale_dispatching=stale,
        recipient_count=run.recipient_count,
        total_amount=run.total_amount,
        settled_amount=sum(row.amount for row in rows if row.status == "settled"),
        counts=dict(sorted(counts.items())),
    )


async def run_report(session: AsyncSession, run_id: UUID) -> AirdropRunReport | None:
    run = await session.get(AirdropRun, run_id)
    if run is None:
        return None
    rows = list(
        (
            await session.scalars(
                select(AirdropRecipient)
                .where(AirdropRecipient.run_id == run_id)
                .order_by(AirdropRecipient.position)
            )
        ).all()
    )
    return AirdropRunReport(
        **_summary(run, rows).model_dump(),
        recipients=[
            AirdropRecipientResult(
                position=row.position,
                kind=row.kind,  # type: ignore[arg-type]
                address=row.address,
                payee=row.payee,
                amount=row.amount,
                recipient_key=row.recipient_key,
                status=row.status,  # type: ignore[arg-type]
                receipt=row.receipt,
                error=row.error,
                attempted_at=row.attempted_at,
                settled_at=row.settled_at,
            )
            for row in rows
        ],
    )


async def list_runs(
    session: AsyncSession,
    *,
    limit: int,
    offset: int = 0,
    idempotency_key: str | None = None,
    asset: AirdropAsset | None = None,
    mode: AirdropMode | None = None,
) -> list[AirdropRunSummary]:
    statement = select(AirdropRun)
    if idempotency_key is not None:
        statement = statement.where(AirdropRun.idempotency_key == idempotency_key)
    if asset is not None:
        statement = statement.where(AirdropRun.asset == asset)
    if mode is not None:
        statement = statement.where(AirdropRun.mode == mode)
    runs = list(
        (
            await session.scalars(
                statement.order_by(AirdropRun.created_at.desc(), AirdropRun.run_id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    if not runs:
        return []
    rows = (
        await session.scalars(
            select(AirdropRecipient).where(
                AirdropRecipient.run_id.in_([run.run_id for run in runs])
            )
        )
    ).all()
    by_run: dict[UUID, list[AirdropRecipient]] = {}
    for row in rows:
        by_run.setdefault(row.run_id, []).append(row)
    return [_summary(run, by_run.get(run.run_id, [])) for run in runs]
