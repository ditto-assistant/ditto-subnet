"""Persistence primitives for bounded LongMem confirmation bundles.

The ordinary validator ticket remains the mutable execution-slot capability.
This module owns the separate artifact/profile bundle ledger, append-only signed
dimension evidence, operator-authorized generations, and exact UTC-day spend
gate.  Callers compose these helpers inside one transaction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ditto.api_models.confirmation_bundles import (
    ConfirmationBundleMode,
    ConfirmationBundleSettings,
    ConfirmationBundleState,
    ConfirmationCompletionReport,
    ConfirmationDimension,
    ConfirmationEvidenceRoot,
    ConfirmationReservationState,
    ConfirmationResultStatus,
    supports_confirmation,
)
from ditto.api_server.attestation import verify_signature
from ditto.api_server.confirmation_bundles import confirmation_bench_version_error
from ditto.api_server.confirmation_evidence import (
    ConfirmationEvidenceError,
    ConfirmationVerificationProfile,
    VerifiedConfirmationEvidence,
    compute_subject_projection,
    confirmation_signing_message,
    rebuild_confirmation_evidence,
)
from ditto.db.models import (
    Agent,
    ConfirmationBudgetDay,
    ConfirmationBudgetReservation,
    ConfirmationBundle,
    ConfirmationBundleSettingsRevision,
    ConfirmationBundleSubject,
    ConfirmationBundleTicket,
    ConfirmationDimensionEvidence,
    ConfirmationRetestAuthorization,
    InferenceGrant,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

GLOBAL_SCOPE = "*"
CONFIRMATION_TICKET_TTL = timedelta(hours=4)


class ConfirmationBundlePersistenceError(ValueError):
    """A bundle mutation conflicts with its frozen contract or current state."""


class StaleConfirmationBudget(ConfirmationBundlePersistenceError):
    """The caller based a spend decision on an obsolete UTC-day revision."""


@dataclass(frozen=True)
class BundleResolution:
    bundle: ConfirmationBundle | None
    subject: ConfirmationBundleSubject
    reused_completed_evidence: bool


@dataclass(frozen=True)
class BudgetReservationDecision:
    budget: ConfirmationBudgetDay
    reservation: ConfirmationBudgetReservation | None
    blocked_reason: str | None
    replayed: bool = False


@dataclass(frozen=True)
class SettlementResult:
    budget: ConfirmationBudgetDay
    reservation: ConfirmationBudgetReservation
    replayed: bool


@dataclass(frozen=True)
class RetestAuthorizationResult:
    authorization: ConfirmationRetestAuthorization
    superseded_bundle: ConfirmationBundle
    bundle: ConfirmationBundle
    replayed: bool


@dataclass(frozen=True)
class ConfirmationShadowCalibration:
    """Raw settled-row aggregates for the admin shadow-calibration view."""

    base_run_count: int
    base_cost_microusd: int
    bundle_count: int
    bundle_cost_microusd: int
    # ``completed`` counts bundles that actually produced verified evidence.
    # Superseded generations are reported separately: folding them in made a
    # lane that had never once completed read as a well-populated calibration
    # window with a zero promotion rate, which is a very different diagnosis.
    completed_bundle_count: int
    superseded_bundle_count: int
    failed_bundle_count: int
    qualified_bundle_count: int
    observed_from: datetime | None
    observed_through: datetime | None


@dataclass(frozen=True)
class ActiveConfirmationSubject:
    """Public-safe identity for one subject in a live confirmation bundle."""

    agent_id: UUID
    agent_name: str


@dataclass(frozen=True)
class ActiveConfirmationWork:
    """One issued LongMem/ablation ticket, independent of ordinary slots."""

    ticket: ConfirmationBundleTicket
    bundle: ConfirmationBundle
    mode: ConfirmationBundleMode
    subjects: tuple[ActiveConfirmationSubject, ...]


async def list_active_confirmation_work(
    session: AsyncSession, *, now: datetime
) -> list[ActiveConfirmationWork]:
    """Return live confirmation tickets for the public fleet snapshot.

    Confirmation tickets intentionally use their own ``longmem-*`` capacity.
    Keeping this query separate from ordinary validator work prevents an
    expensive confirmation run from consuming or fabricating a DittoBench slot.
    """
    rows = (
        await session.execute(
            select(
                ConfirmationBundleTicket,
                ConfirmationBundle,
                ConfirmationBundleSettingsRevision,
            )
            .join(
                ConfirmationBundle,
                ConfirmationBundle.bundle_id == ConfirmationBundleTicket.bundle_id,
            )
            .join(
                ConfirmationBundleSettingsRevision,
                ConfirmationBundleSettingsRevision.revision
                == ConfirmationBundle.settings_revision,
            )
            .where(
                ConfirmationBundleTicket.status == "issued",
                ConfirmationBundleTicket.deadline > now,
                ConfirmationBundle.state == ConfirmationBundleState.LEASED.value,
            )
            .order_by(
                ConfirmationBundleTicket.validator_hotkey,
                ConfirmationBundleTicket.slot_id,
                ConfirmationBundleTicket.issued_at,
            )
        )
    ).all()
    if not rows:
        return []

    bundle_ids = [bundle.bundle_id for _, bundle, _ in rows]
    subjects_by_bundle: dict[UUID, list[ActiveConfirmationSubject]] = {}
    subject_rows = (
        await session.execute(
            select(
                ConfirmationBundleSubject.bundle_id,
                Agent.agent_id,
                Agent.name,
            )
            .join(Agent, Agent.agent_id == ConfirmationBundleSubject.agent_id)
            .where(ConfirmationBundleSubject.bundle_id.in_(bundle_ids))
            .order_by(
                ConfirmationBundleSubject.bundle_id, Agent.created_at, Agent.agent_id
            )
        )
    ).all()
    for bundle_id, agent_id, agent_name in subject_rows:
        if bundle_id is None:  # pragma: no cover - constrained by the input filter
            continue
        subjects_by_bundle.setdefault(bundle_id, []).append(
            ActiveConfirmationSubject(agent_id=agent_id, agent_name=agent_name)
        )

    active: list[ActiveConfirmationWork] = []
    for ticket, bundle, settings_row in rows:
        settings = ConfirmationBundleSettings.model_validate(settings_row.settings)
        if settings.mode == ConfirmationBundleMode.OFF:
            continue
        active.append(
            ActiveConfirmationWork(
                ticket=ticket,
                bundle=bundle,
                mode=settings.mode,
                subjects=tuple(subjects_by_bundle.get(bundle.bundle_id, ())),
            )
        )
    return active


def _count_state(state: str):
    """Count bundle rows in exactly one lifecycle state."""
    return func.coalesce(
        func.sum(case((ConfirmationBundle.state == state, 1), else_=0)), 0
    )


async def confirmation_shadow_calibration(
    session: AsyncSession,
    *,
    now: datetime,
    bench_version: int,
    profile_revision: str | None,
    profile_checksum: str | None,
) -> ConfirmationShadowCalibration:
    """Measure base and confirmation costs without price-list estimates.

    Base cost follows the public leaderboard's settled, non-empty grant rule for
    ``bench_version``.  Confirmation cost uses authoritative settled reservation
    actuals.  A qualified completed generation is a promotion; superseded and
    failed generations remain part of the immutable calibration history but are
    counted on their own axes so an execution outage can never be misread as a
    completed-but-unpromoted cohort.
    """

    base_count, base_cost, base_first, base_last = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(
                    func.sum(
                        InferenceGrant.cost_microusd
                        + InferenceGrant.embedding_cost_microusd
                    ),
                    0,
                ),
                func.min(InferenceGrant.created_at),
                func.max(InferenceGrant.created_at),
            ).where(
                InferenceGrant.bench_version == bench_version,
                or_(
                    InferenceGrant.ticket_deadline <= now,
                    InferenceGrant.status.in_(("revoked", "exhausted")),
                ),
                or_(
                    InferenceGrant.request_count > 0,
                    InferenceGrant.embedding_request_count > 0,
                ),
            )
        )
    ).one()
    if profile_revision is None or profile_checksum is None:
        bundle_count, bundle_cost, bundle_first, bundle_last = 0, 0, None, None
        completed_count, superseded_count, failed_count, qualified_count = 0, 0, 0, 0
    else:
        bundle_count, bundle_cost, bundle_first, bundle_last = (
            await session.execute(
                select(
                    func.count(func.distinct(ConfirmationBudgetReservation.bundle_id)),
                    func.coalesce(
                        func.sum(ConfirmationBudgetReservation.actual_microusd), 0
                    ),
                    func.min(ConfirmationBudgetReservation.settled_at),
                    func.max(ConfirmationBudgetReservation.settled_at),
                )
                .join(
                    ConfirmationBundle,
                    ConfirmationBundle.bundle_id
                    == ConfirmationBudgetReservation.bundle_id,
                )
                .where(
                    ConfirmationBudgetReservation.state == "settled",
                    ConfirmationBundle.profile_revision == profile_revision,
                    ConfirmationBundle.profile_checksum == profile_checksum,
                )
            )
        ).one()
        completed_count, superseded_count, failed_count, qualified_count = (
            await session.execute(
                select(
                    _count_state(ConfirmationBundleState.COMPLETED.value),
                    _count_state(ConfirmationBundleState.SUPERSEDED.value),
                    _count_state(ConfirmationBundleState.FAILED.value),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    ConfirmationBundle.qualification_status
                                    == "qualified",
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                ).where(
                    ConfirmationBundle.profile_revision == profile_revision,
                    ConfirmationBundle.profile_checksum == profile_checksum,
                )
            )
        ).one()
    observed = [
        value
        for value in (base_first, base_last, bundle_first, bundle_last)
        if value is not None
    ]
    return ConfirmationShadowCalibration(
        base_run_count=int(base_count),
        base_cost_microusd=int(base_cost),
        bundle_count=int(bundle_count),
        bundle_cost_microusd=int(bundle_cost),
        completed_bundle_count=int(completed_count),
        superseded_bundle_count=int(superseded_count),
        failed_bundle_count=int(failed_count),
        qualified_bundle_count=int(qualified_count),
        observed_from=min(observed) if observed else None,
        observed_through=max(observed) if observed else None,
    )


async def latest_confirmation_bundle_settings_revision(
    session: AsyncSession, *, scope: str = GLOBAL_SCOPE
) -> ConfirmationBundleSettingsRevision | None:
    return await session.scalar(
        select(ConfirmationBundleSettingsRevision)
        .where(ConfirmationBundleSettingsRevision.scope == scope)
        .order_by(ConfirmationBundleSettingsRevision.revision.desc())
        .limit(1)
    )


async def list_confirmation_bundle_settings_revisions(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[ConfirmationBundleSettingsRevision]:
    return list(
        await session.scalars(
            select(ConfirmationBundleSettingsRevision)
            .order_by(ConfirmationBundleSettingsRevision.revision.desc())
            .limit(limit)
        )
    )


async def insert_confirmation_bundle_settings_revision(
    session: AsyncSession,
    *,
    parent_revision: int,
    scope: str,
    settings: dict,
    checksum: str,
    reason: str,
    actor: str,
) -> ConfirmationBundleSettingsRevision:
    row = ConfirmationBundleSettingsRevision(
        parent_revision=parent_revision,
        scope=scope,
        settings=settings,
        checksum=checksum,
        reason=reason,
        actor=actor,
    )
    session.add(row)
    await session.flush()
    return row


async def record_base_only_subject(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    base_evidence_sha256: str,
    base_quality_micros: int,
    base_stderr_micros: int,
    base_model_factor_bps: int,
    base_tool_factor_bps: int,
) -> ConfirmationBundleSubject:
    """Record a typed base proof without touching canonical scoring state."""
    if not supports_confirmation(bench_version):
        raise ConfirmationBundlePersistenceError(
            confirmation_bench_version_error("subjects")
        )
    _validate_base_proof(
        base_evidence_sha256=base_evidence_sha256,
        base_quality_micros=base_quality_micros,
        base_stderr_micros=base_stderr_micros,
        base_model_factor_bps=base_model_factor_bps,
        base_tool_factor_bps=base_tool_factor_bps,
    )
    agent = await session.scalar(
        select(Agent).where(Agent.agent_id == agent_id).with_for_update()
    )
    if agent is None:
        raise ConfirmationBundlePersistenceError("agent does not exist")
    bundle_subject = await session.get(
        ConfirmationBundleSubject, (agent_id, bench_version), with_for_update=True
    )
    if bundle_subject is None:
        bundle_subject = ConfirmationBundleSubject(
            agent_id=agent_id,
            bench_version=bench_version,
            artifact_sha256=agent.sha256,
            result_status=ConfirmationResultStatus.BASE_ONLY.value,
            base_evidence_sha256=base_evidence_sha256,
            base_quality_micros=base_quality_micros,
            base_stderr_micros=base_stderr_micros,
            base_model_factor_bps=base_model_factor_bps,
            base_tool_factor_bps=base_tool_factor_bps,
        )
        session.add(bundle_subject)
    elif bundle_subject.bundle_id is not None:
        if _base_proof_tuple(bundle_subject) != (
            agent.sha256,
            base_evidence_sha256,
            base_quality_micros,
            base_stderr_micros,
            base_model_factor_bps,
            base_tool_factor_bps,
        ):
            raise ConfirmationBundlePersistenceError(
                "attached confirmation base proof cannot be replaced"
            )
        return bundle_subject
    else:
        bundle_subject.artifact_sha256 = agent.sha256
        bundle_subject.bundle_id = None
        bundle_subject.result_status = ConfirmationResultStatus.BASE_ONLY.value
        bundle_subject.base_evidence_sha256 = base_evidence_sha256
        bundle_subject.base_quality_micros = base_quality_micros
        bundle_subject.base_stderr_micros = base_stderr_micros
        bundle_subject.base_model_factor_bps = base_model_factor_bps
        bundle_subject.base_tool_factor_bps = base_tool_factor_bps
        _clear_subject_projection(bundle_subject)
    await session.flush()
    return bundle_subject


async def get_or_create_confirmation_bundle(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    base_evidence_sha256: str,
    base_quality_micros: int,
    base_stderr_micros: int,
    base_model_factor_bps: int,
    base_tool_factor_bps: int,
    settings_revision: int,
    settings: ConfirmationBundleSettings,
    verification_profile: ConfirmationVerificationProfile | None = None,
) -> BundleResolution:
    """Resolve exact-key reuse or create one generation-zero bundle.

    One advisory lock per artifact/profile prevents two renamed submissions from
    both observing absence.  The database unique key remains the final backstop.
    This creates exactly one bundle, never one bundle per seed or dimension.
    """
    if settings.mode == ConfirmationBundleMode.OFF:
        subject = await record_base_only_subject(
            session,
            agent_id=agent_id,
            bench_version=bench_version,
            base_evidence_sha256=base_evidence_sha256,
            base_quality_micros=base_quality_micros,
            base_stderr_micros=base_stderr_micros,
            base_model_factor_bps=base_model_factor_bps,
            base_tool_factor_bps=base_tool_factor_bps,
        )
        return BundleResolution(
            bundle=None, subject=subject, reused_completed_evidence=False
        )
    if not supports_confirmation(bench_version):
        raise ConfirmationBundlePersistenceError(
            confirmation_bench_version_error("bundles")
        )
    if settings.profile_revision is None or settings.profile_checksum is None:
        raise ConfirmationBundlePersistenceError("confirmation profile is unconfigured")
    if verification_profile is None:
        raise ConfirmationBundlePersistenceError(
            "confirmation verification profile is unconfigured"
        )
    try:
        profile_checksum = verification_profile.checksum()
    except ConfirmationEvidenceError as error:
        raise ConfirmationBundlePersistenceError(str(error)) from error
    if (
        verification_profile.revision != settings.profile_revision
        or profile_checksum != settings.profile_checksum
    ):
        raise ConfirmationBundlePersistenceError(
            "confirmation verification profile does not match settings"
        )
    stored_settings = await session.get(
        ConfirmationBundleSettingsRevision, settings_revision
    )
    if stored_settings is None:
        raise ConfirmationBundlePersistenceError("settings revision does not exist")
    if stored_settings.settings != settings.model_dump(mode="json"):
        raise ConfirmationBundlePersistenceError(
            "settings revision does not match the supplied frozen policy"
        )
    if stored_settings.checksum != _settings_checksum(settings):
        raise ConfirmationBundlePersistenceError("stored settings checksum is invalid")
    agent = await session.scalar(
        select(Agent).where(Agent.agent_id == agent_id).with_for_update()
    )
    if agent is None:
        raise ConfirmationBundlePersistenceError("agent does not exist")
    lock_key = ":".join((agent.sha256, str(bench_version), settings.profile_checksum))
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )
    bundle = await session.scalar(
        select(ConfirmationBundle)
        .where(
            ConfirmationBundle.artifact_sha256 == agent.sha256,
            ConfirmationBundle.bench_version == bench_version,
            ConfirmationBundle.profile_revision == settings.profile_revision,
            ConfirmationBundle.profile_checksum == settings.profile_checksum,
        )
        .order_by(ConfirmationBundle.retest_generation.desc())
        .limit(1)
        .with_for_update()
    )
    if bundle is None:
        bundle = ConfirmationBundle(
            artifact_sha256=agent.sha256,
            bench_version=bench_version,
            profile_revision=settings.profile_revision,
            profile_checksum=settings.profile_checksum,
            retest_generation=0,
            generation_reason="initial",
            settings_revision=settings_revision,
            settings_checksum=stored_settings.checksum,
            state=ConfirmationBundleState.PENDING.value,
        )
        session.add(bundle)
        await session.flush()
    elif (
        bundle.settings_revision != settings_revision
        or bundle.settings_checksum != stored_settings.checksum
    ):
        bundle = await _supersede_unspent_bundle_for_settings(
            session,
            source=bundle,
            settings_revision=settings_revision,
            settings_checksum=stored_settings.checksum,
        )
    completed = bundle.state == ConfirmationBundleState.COMPLETED.value
    resolved_subject = await session.get(
        ConfirmationBundleSubject, (agent_id, bench_version), with_for_update=True
    )
    status = ConfirmationResultStatus.PROVISIONAL
    if resolved_subject is None:
        resolved_subject = ConfirmationBundleSubject(
            agent_id=agent_id,
            bench_version=bench_version,
            artifact_sha256=agent.sha256,
            bundle_id=bundle.bundle_id,
            result_status=status.value,
            base_evidence_sha256=base_evidence_sha256,
            base_quality_micros=base_quality_micros,
            base_stderr_micros=base_stderr_micros,
            base_model_factor_bps=base_model_factor_bps,
            base_tool_factor_bps=base_tool_factor_bps,
        )
        session.add(resolved_subject)
    else:
        if resolved_subject.bundle_id is not None and _base_proof_tuple(
            resolved_subject
        ) != (
            agent.sha256,
            base_evidence_sha256,
            base_quality_micros,
            base_stderr_micros,
            base_model_factor_bps,
            base_tool_factor_bps,
        ):
            raise ConfirmationBundlePersistenceError(
                "attached confirmation base proof cannot be replaced"
            )
        resolved_subject.artifact_sha256 = agent.sha256
        resolved_subject.bundle_id = bundle.bundle_id
        resolved_subject.result_status = status.value
        resolved_subject.base_evidence_sha256 = base_evidence_sha256
        resolved_subject.base_quality_micros = base_quality_micros
        resolved_subject.base_stderr_micros = base_stderr_micros
        resolved_subject.base_model_factor_bps = base_model_factor_bps
        resolved_subject.base_tool_factor_bps = base_tool_factor_bps
        _clear_subject_projection(resolved_subject)
    if completed:
        verified, frozen_mode = _replay_stored_bundle(bundle, verification_profile)
        _apply_projection(
            resolved_subject,
            verified=verified,
            mode=frozen_mode,
            profile=verification_profile,
        )
    await session.flush()
    return BundleResolution(
        bundle=bundle,
        subject=resolved_subject,
        reused_completed_evidence=completed,
    )


async def _supersede_unspent_bundle_for_settings(
    session: AsyncSession,
    *,
    source: ConfirmationBundle,
    settings_revision: int,
    settings_checksum: str,
) -> ConfirmationBundle:
    """Replace a stale-policy generation only when no work remains live.

    Untouched pending work and budget-blocked work with no issued attempt may
    advance after a settings revision. A failed bundle consumed its one
    automatic attempt and can advance only through an operator-authorized
    retest generation.
    """
    if source.state not in {
        ConfirmationBundleState.PENDING.value,
        ConfirmationBundleState.BLOCKED_BUDGET.value,
    }:
        return source
    evidence_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ConfirmationDimensionEvidence)
            .where(ConfirmationDimensionEvidence.bundle_id == source.bundle_id)
        )
        or 0
    )
    if evidence_count:
        return source
    ticket_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ConfirmationBundleTicket)
            .where(ConfirmationBundleTicket.bundle_id == source.bundle_id)
        )
        or 0
    )
    reservation_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ConfirmationBudgetReservation)
            .where(ConfirmationBudgetReservation.bundle_id == source.bundle_id)
        )
        or 0
    )
    if ticket_count or reservation_count:
        return source
    source.state = ConfirmationBundleState.SUPERSEDED.value
    replacement = ConfirmationBundle(
        artifact_sha256=source.artifact_sha256,
        bench_version=source.bench_version,
        profile_revision=source.profile_revision,
        profile_checksum=source.profile_checksum,
        retest_generation=source.retest_generation + 1,
        retest_authorization_id=None,
        generation_reason="settings_supersession",
        source_bundle_id=source.bundle_id,
        settings_revision=settings_revision,
        settings_checksum=settings_checksum,
        state=ConfirmationBundleState.PENDING.value,
    )
    session.add(replacement)
    await session.flush()
    return replacement


def _validate_base_proof(
    *,
    base_evidence_sha256: str,
    base_quality_micros: int,
    base_stderr_micros: int,
    base_model_factor_bps: int,
    base_tool_factor_bps: int,
) -> None:
    if (
        len(base_evidence_sha256) != 64
        or base_evidence_sha256.lower() != base_evidence_sha256
    ):
        raise ConfirmationBundlePersistenceError(
            "base evidence digest must be lowercase SHA-256"
        )
    try:
        bytes.fromhex(base_evidence_sha256)
    except ValueError as error:
        raise ConfirmationBundlePersistenceError(
            "base evidence digest must be lowercase SHA-256"
        ) from error
    if not 0 <= base_quality_micros <= 1_000_000:
        raise ConfirmationBundlePersistenceError("base quality is out of range")
    if not 0 <= base_stderr_micros <= 1_000_000:
        raise ConfirmationBundlePersistenceError("base stderr is out of range")
    if base_model_factor_bps not in {0, 10_000}:
        raise ConfirmationBundlePersistenceError("base model gate must be binary")
    if base_tool_factor_bps not in {0, 10_000}:
        raise ConfirmationBundlePersistenceError("base tool gate must be binary")


def _base_proof_tuple(
    subject: ConfirmationBundleSubject,
) -> tuple[str, str, int, int, int, int]:
    return (
        subject.artifact_sha256,
        subject.base_evidence_sha256,
        subject.base_quality_micros,
        subject.base_stderr_micros,
        subject.base_model_factor_bps,
        subject.base_tool_factor_bps,
    )


def _clear_subject_projection(subject: ConfirmationBundleSubject) -> None:
    subject.full_quality_micros = None
    subject.full_stderr_micros = None
    subject.semantic_factor_bps = None
    subject.applied_factor_bps = None
    subject.full_effective_micros = None


def _apply_projection(
    subject: ConfirmationBundleSubject,
    *,
    verified: VerifiedConfirmationEvidence,
    mode: ConfirmationBundleMode,
    profile: ConfirmationVerificationProfile,
) -> None:
    try:
        projection = compute_subject_projection(
            mode=mode,
            base_quality_micros=subject.base_quality_micros,
            base_stderr_micros=subject.base_stderr_micros,
            base_model_factor_bps=subject.base_model_factor_bps,
            base_tool_factor_bps=subject.base_tool_factor_bps,
            verified=verified,
            composite=profile.composite,
        )
    except ConfirmationEvidenceError as error:
        raise ConfirmationBundlePersistenceError(str(error)) from error
    subject.result_status = projection.result_status
    subject.full_quality_micros = projection.full_quality_micros
    subject.full_stderr_micros = projection.full_stderr_micros
    subject.semantic_factor_bps = projection.semantic_factor_bps
    subject.applied_factor_bps = projection.applied_factor_bps
    subject.full_effective_micros = projection.full_effective_micros


def _replay_stored_bundle(
    bundle: ConfirmationBundle,
    profile: ConfirmationVerificationProfile,
) -> tuple[VerifiedConfirmationEvidence, ConfirmationBundleMode]:
    if (
        bundle.evidence_root is None
        or bundle.evidence_sha256 is None
        or bundle.bundle_signature is None
        or bundle.completion_mode is None
    ):
        raise ConfirmationBundlePersistenceError(
            "completed confirmation bundle lacks verified evidence"
        )
    try:
        root = ConfirmationEvidenceRoot.model_validate(bundle.evidence_root)
        mode = ConfirmationBundleMode(bundle.completion_mode)
        report = ConfirmationCompletionReport(
            ablation_coordinator_latency_ms=root.ablation_coordinator_latency_ms,
            longmemeval=root.longmemeval,
            inference_ablation=root.inference_ablation,
            embedding_ablation=root.embedding_ablation,
            bundle_signature=bundle.bundle_signature,
        )
        verified = rebuild_confirmation_evidence(
            report,
            artifact_sha256=bundle.artifact_sha256,
            profile_revision=bundle.profile_revision,
            profile_checksum=bundle.profile_checksum,
            settings_revision=bundle.settings_revision,
            settings_checksum=bundle.settings_checksum,
            retest_generation=bundle.retest_generation,
            mode=mode,
            profile=profile,
        )
    except (ConfirmationEvidenceError, ValueError) as error:
        raise ConfirmationBundlePersistenceError(
            "stored confirmation evidence failed replay"
        ) from error
    if (
        verified.evidence_sha256 != bundle.evidence_sha256
        or verified.root.model_dump(mode="json") != bundle.evidence_root
    ):
        raise ConfirmationBundlePersistenceError(
            "stored confirmation evidence digest is invalid"
        )
    return verified, mode


def _settings_checksum(settings: ConfirmationBundleSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _utc_day(now: datetime) -> date:
    if now.tzinfo is None:
        raise ConfirmationBundlePersistenceError("budget time must be timezone-aware")
    return now.astimezone(UTC).date()


async def lock_confirmation_budget_day(
    session: AsyncSession, *, utc_day: date
) -> ConfirmationBudgetDay:
    """Create if needed and lock one UTC budget row before any bundle lock."""
    await session.execute(
        pg_insert(ConfirmationBudgetDay)
        .values(
            utc_day=utc_day,
            revision=0,
            issued_attempts=0,
            outstanding_reserved_microusd=0,
            settled_microusd=0,
        )
        .on_conflict_do_nothing(index_elements=[ConfirmationBudgetDay.utc_day])
    )
    budget = await session.scalar(
        select(ConfirmationBudgetDay)
        .where(ConfirmationBudgetDay.utc_day == utc_day)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if budget is None:  # pragma: no cover - INSERT + SELECT is one transaction
        raise ConfirmationBundlePersistenceError("could not lock budget day")
    return budget


async def reserve_confirmation_bundle_budget(
    session: AsyncSession,
    *,
    bundle_id: UUID,
    reservation_id: UUID,
    now: datetime,
    expected_revision: int,
    settings_revision: int,
    settings: ConfirmationBundleSettings,
    reserve_microusd: int,
) -> BudgetReservationDecision:
    """Reserve one attempt under exact daily bundle and dollar caps."""
    if settings.mode == ConfirmationBundleMode.OFF:
        raise ConfirmationBundlePersistenceError("confirmation bundle policy is off")
    if reserve_microusd <= 0:
        raise ConfirmationBundlePersistenceError("reserve_microusd must be positive")
    day = _utc_day(now)
    budget = await lock_confirmation_budget_day(session, utc_day=day)
    if budget.revision != expected_revision:
        raise StaleConfirmationBudget(
            f"budget revision changed: expected {expected_revision}, "
            f"found {budget.revision}"
        )
    bundle = await session.scalar(
        select(ConfirmationBundle)
        .where(ConfirmationBundle.bundle_id == bundle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if bundle is None:
        raise ConfirmationBundlePersistenceError("bundle does not exist")
    if (
        bundle.settings_revision != settings_revision
        or bundle.settings_checksum != _settings_checksum(settings)
    ):
        raise ConfirmationBundlePersistenceError(
            "budget policy does not match the bundle's frozen settings"
        )
    existing = await session.get(ConfirmationBudgetReservation, reservation_id)
    if existing is not None:
        if (
            existing.bundle_id != bundle_id
            or existing.settings_revision != settings_revision
            or existing.reserved_microusd != reserve_microusd
        ):
            raise ConfirmationBundlePersistenceError(
                "reservation id was already used for different input"
            )
        return BudgetReservationDecision(
            budget=budget,
            reservation=existing,
            blocked_reason=None,
            replayed=True,
        )
    if bundle.state not in {
        ConfirmationBundleState.PENDING.value,
        ConfirmationBundleState.BLOCKED_BUDGET.value,
    }:
        raise ConfirmationBundlePersistenceError(
            f"bundle in {bundle.state} cannot reserve a new attempt"
        )
    prior_reservation = await session.scalar(
        select(ConfirmationBudgetReservation)
        .where(
            ConfirmationBudgetReservation.bundle_id == bundle_id,
        )
        .limit(1)
        .with_for_update()
    )
    if prior_reservation is not None:
        raise ConfirmationBundlePersistenceError(
            "confirmation bundle already consumed its automatic attempt; "
            "authorize a retest generation"
        )
    blocked_reason: str | None = None
    if budget.issued_attempts + 1 > settings.daily_bundle_cap:
        blocked_reason = "bundle_cap"
    elif (
        budget.outstanding_reserved_microusd
        + budget.settled_microusd
        + reserve_microusd
        > settings.daily_dollar_cap_microusd
    ):
        blocked_reason = "dollar_cap"
    if blocked_reason is not None:
        bundle.state = ConfirmationBundleState.BLOCKED_BUDGET.value
        await session.flush()
        return BudgetReservationDecision(
            budget=budget, reservation=None, blocked_reason=blocked_reason
        )
    max_attempt = await session.scalar(
        select(func.max(ConfirmationBudgetReservation.attempt)).where(
            ConfirmationBudgetReservation.bundle_id == bundle_id
        )
    )
    attempt = int(max_attempt or 0) + 1
    reservation = ConfirmationBudgetReservation(
        reservation_id=reservation_id,
        bundle_id=bundle_id,
        attempt=attempt,
        utc_day=day,
        settings_revision=settings_revision,
        reserved_microusd=reserve_microusd,
        state=ConfirmationReservationState.RESERVED.value,
    )
    session.add(reservation)
    budget.revision += 1
    budget.issued_attempts += 1
    budget.outstanding_reserved_microusd += reserve_microusd
    if bundle.state == ConfirmationBundleState.BLOCKED_BUDGET.value:
        bundle.state = ConfirmationBundleState.PENDING.value
    await session.flush()
    return BudgetReservationDecision(
        budget=budget, reservation=reservation, blocked_reason=None
    )


async def issue_confirmation_bundle_ticket(
    session: AsyncSession,
    *,
    bundle_id: UUID,
    reservation_id: UUID,
    validator_hotkey: str,
    slot_id: str,
    now: datetime,
    ttl: timedelta = CONFIRMATION_TICKET_TTL,
) -> ConfirmationBundleTicket:
    """Issue or resume the bundle's one append-only four-hour lease attempt."""
    if now.tzinfo is None:
        raise ConfirmationBundlePersistenceError("ticket time must be timezone-aware")
    if ttl != CONFIRMATION_TICKET_TTL:
        raise ConfirmationBundlePersistenceError(
            "confirmation ticket TTL must remain exactly four hours"
        )
    bundle = await session.scalar(
        select(ConfirmationBundle)
        .where(ConfirmationBundle.bundle_id == bundle_id)
        .with_for_update()
    )
    reservation = await session.scalar(
        select(ConfirmationBudgetReservation)
        .where(ConfirmationBudgetReservation.reservation_id == reservation_id)
        .with_for_update()
    )
    if bundle is None or reservation is None or reservation.bundle_id != bundle_id:
        raise ConfirmationBundlePersistenceError("bundle reservation does not exist")
    if reservation.state != ConfirmationReservationState.RESERVED.value:
        raise ConfirmationBundlePersistenceError("reservation was already settled")
    existing = await session.scalar(
        select(ConfirmationBundleTicket)
        .where(
            ConfirmationBundleTicket.bundle_id == bundle_id,
            ConfirmationBundleTicket.attempt == reservation.attempt,
        )
        .with_for_update()
    )
    if existing is not None:
        if (
            existing.status == "issued"
            and existing.deadline > now
            and existing.validator_hotkey == validator_hotkey
            and existing.slot_id == slot_id
        ):
            return existing
        raise ConfirmationBundlePersistenceError(
            "reservation attempt already has a different or closed ticket"
        )
    if bundle.state not in {
        ConfirmationBundleState.PENDING.value,
        ConfirmationBundleState.FAILED.value,
    }:
        raise ConfirmationBundlePersistenceError(
            f"bundle in {bundle.state} cannot be leased"
        )
    ticket = ConfirmationBundleTicket(
        bundle_id=bundle_id,
        validator_hotkey=validator_hotkey,
        slot_id=slot_id,
        status="issued",
        attempt=reservation.attempt,
        issued_at=now,
        deadline=now + ttl,
    )
    session.add(ticket)
    bundle.state = ConfirmationBundleState.LEASED.value
    await session.flush()
    return ticket


async def settle_confirmation_bundle_budget(
    session: AsyncSession,
    *,
    reservation_id: UUID,
    expected_revision: int,
    actual_microusd: int,
    failed_attempt: bool,
    settled_at: datetime,
) -> SettlementResult:
    """Settle accepted work even when actual cost crossed the issuance cap."""
    if actual_microusd < 0:
        raise ConfirmationBundlePersistenceError("actual_microusd cannot be negative")
    probe = await session.get(ConfirmationBudgetReservation, reservation_id)
    if probe is None:
        raise ConfirmationBundlePersistenceError("reservation does not exist")
    if _utc_day(settled_at) < probe.utc_day:
        raise ConfirmationBundlePersistenceError("settlement predates reservation day")
    budget = await lock_confirmation_budget_day(session, utc_day=probe.utc_day)
    if budget.revision != expected_revision:
        raise StaleConfirmationBudget(
            f"budget revision changed: expected {expected_revision}, "
            f"found {budget.revision}"
        )
    reservation = await session.scalar(
        select(ConfirmationBudgetReservation)
        .where(ConfirmationBudgetReservation.reservation_id == reservation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if reservation is None:  # pragma: no cover - locked after a successful probe
        raise ConfirmationBundlePersistenceError("reservation disappeared")
    bundle = await session.scalar(
        select(ConfirmationBundle)
        .where(ConfirmationBundle.bundle_id == reservation.bundle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if bundle is None:  # pragma: no cover - protected by foreign key
        raise ConfirmationBundlePersistenceError("bundle disappeared")
    if reservation.state == ConfirmationReservationState.SETTLED.value:
        if (
            reservation.actual_microusd == actual_microusd
            and reservation.failed_attempt == failed_attempt
        ):
            return SettlementResult(
                budget=budget, reservation=reservation, replayed=True
            )
        raise ConfirmationBundlePersistenceError(
            "reservation was settled with different accounting"
        )
    if budget.outstanding_reserved_microusd < reservation.reserved_microusd:
        raise ConfirmationBundlePersistenceError(
            "budget day is missing the reservation amount"
        )
    budget.revision += 1
    budget.outstanding_reserved_microusd -= reservation.reserved_microusd
    budget.settled_microusd += actual_microusd
    reservation.state = ConfirmationReservationState.SETTLED.value
    reservation.actual_microusd = actual_microusd
    reservation.failed_attempt = failed_attempt
    reservation.settled_at = settled_at
    if failed_attempt:
        bundle.state = ConfirmationBundleState.FAILED.value
        ticket = await session.scalar(
            select(ConfirmationBundleTicket)
            .where(
                ConfirmationBundleTicket.bundle_id == bundle.bundle_id,
                ConfirmationBundleTicket.attempt == reservation.attempt,
            )
            .with_for_update()
        )
        if ticket is not None and ticket.status == "issued":
            ticket.status = "expired"
            ticket.failure_reason = "confirmation_failed"
            ticket.failed_at = settled_at
    await session.flush()
    return SettlementResult(budget=budget, reservation=reservation, replayed=False)


async def complete_confirmation_bundle(
    session: AsyncSession,
    *,
    bundle_id: UUID,
    ticket_id: UUID,
    report: ConfirmationCompletionReport,
    verification_profile: ConfirmationVerificationProfile,
    now: datetime,
) -> ConfirmationBundle:
    """Verify one signed typed root and derive every subject independently."""
    if now.tzinfo is None:
        raise ConfirmationBundlePersistenceError(
            "completion time must be timezone-aware"
        )
    bundle = await session.scalar(
        select(ConfirmationBundle)
        .where(ConfirmationBundle.bundle_id == bundle_id)
        .with_for_update()
    )
    if bundle is None:
        raise ConfirmationBundlePersistenceError("bundle does not exist")
    if bundle.state not in {
        ConfirmationBundleState.LEASED.value,
        ConfirmationBundleState.COMPLETED.value,
    }:
        raise ConfirmationBundlePersistenceError(
            f"bundle in {bundle.state} cannot complete"
        )
    ticket = await session.scalar(
        select(ConfirmationBundleTicket)
        .where(ConfirmationBundleTicket.ticket_id == ticket_id)
        .with_for_update()
    )
    if ticket is None or ticket.bundle_id != bundle_id:
        raise ConfirmationBundlePersistenceError("ticket does not belong to bundle")
    if bundle.state == ConfirmationBundleState.LEASED.value:
        if ticket.status != "issued":
            raise ConfirmationBundlePersistenceError(
                "ticket is not the live bundle lease"
            )
        if ticket.deadline <= now:
            raise ConfirmationBundlePersistenceError("confirmation ticket expired")
    elif ticket.status != "scored" or bundle.completion_ticket_id != ticket_id:
        raise ConfirmationBundlePersistenceError(
            "completed replay does not use the accepted ticket"
        )
    settings_row = await session.get(
        ConfirmationBundleSettingsRevision, bundle.settings_revision
    )
    if settings_row is None:  # pragma: no cover - protected by foreign key
        raise ConfirmationBundlePersistenceError("frozen settings disappeared")
    settings = ConfirmationBundleSettings.model_validate_json(
        json.dumps(settings_row.settings)
    )
    if settings.mode == ConfirmationBundleMode.OFF:
        raise ConfirmationBundlePersistenceError(
            "off policy cannot own an issued confirmation bundle"
        )
    settings_checksum = _settings_checksum(settings)
    if (
        settings_row.checksum != settings_checksum
        or bundle.settings_checksum != settings_checksum
    ):
        raise ConfirmationBundlePersistenceError("frozen settings checksum is invalid")
    try:
        verified = rebuild_confirmation_evidence(
            report,
            artifact_sha256=bundle.artifact_sha256,
            profile_revision=bundle.profile_revision,
            profile_checksum=bundle.profile_checksum,
            settings_revision=bundle.settings_revision,
            settings_checksum=bundle.settings_checksum,
            retest_generation=bundle.retest_generation,
            mode=settings.mode,
            profile=verification_profile,
        )
    except ConfirmationEvidenceError as error:
        raise ConfirmationBundlePersistenceError(str(error)) from error
    totals = verified.root.totals
    if totals.request_count > settings.per_bundle_request_cap:
        raise ConfirmationBundlePersistenceError("bundle request cap exceeded")
    if totals.input_tokens + totals.output_tokens > settings.per_bundle_token_cap:
        raise ConfirmationBundlePersistenceError("bundle token cap exceeded")
    signing_message = confirmation_signing_message(
        reporter_hotkey=ticket.validator_hotkey,
        bundle_id=bundle.bundle_id,
        ticket_id=ticket.ticket_id,
        deadline=ticket.deadline,
        artifact_sha256=bundle.artifact_sha256,
        profile_revision=bundle.profile_revision,
        profile_checksum=bundle.profile_checksum,
        settings_revision=bundle.settings_revision,
        settings_checksum=bundle.settings_checksum,
        retest_generation=bundle.retest_generation,
        evidence_sha256=verified.evidence_sha256,
    )
    if not verify_signature(
        signer=ticket.validator_hotkey,
        payload=signing_message,
        signature_hex=report.bundle_signature,
    ):
        raise ConfirmationBundlePersistenceError(
            "confirmation bundle signature did not verify"
        )
    if bundle.state == ConfirmationBundleState.COMPLETED.value:
        if (
            bundle.evidence_sha256 == verified.evidence_sha256
            and bundle.evidence_root == verified.root.model_dump(mode="json")
            and bundle.bundle_signature == report.bundle_signature
            and bundle.reporter_hotkey == ticket.validator_hotkey
        ):
            return bundle
        raise ConfirmationBundlePersistenceError(
            "completed evidence cannot be replaced"
        )
    reservation = await session.scalar(
        select(ConfirmationBudgetReservation)
        .where(
            ConfirmationBudgetReservation.bundle_id == bundle_id,
            ConfirmationBudgetReservation.attempt == ticket.attempt,
        )
        .with_for_update()
    )
    if (
        reservation is None
        or reservation.state != ConfirmationReservationState.SETTLED.value
        or reservation.failed_attempt is not False
    ):
        raise ConfirmationBundlePersistenceError(
            "successful accounting must settle before evidence completion"
        )
    if reservation.actual_microusd != totals.provider_cost_microusd:
        raise ConfirmationBundlePersistenceError(
            "signed dimension cost does not match settled actual cost"
        )
    dimension_envelopes = (
        (ConfirmationDimension.LONGMEMEVAL, verified.root.longmemeval),
        (
            ConfirmationDimension.INFERENCE_ABLATION,
            verified.root.inference_ablation,
        ),
        (
            ConfirmationDimension.EMBEDDING_ABLATION,
            verified.root.embedding_ablation,
        ),
    )
    for dimension, envelope in dimension_envelopes:
        session.add(
            ConfirmationDimensionEvidence(
                bundle_id=bundle_id,
                dimension=dimension.value,
                status=envelope.status,
                evidence_sha256=envelope.evidence_sha256,
                request_count=envelope.request_count,
                input_tokens=envelope.input_tokens,
                output_tokens=envelope.output_tokens,
                provider_cost_microusd=envelope.provider_cost_microusd,
                latency_ms=envelope.latency_ms,
                synthetic=envelope.synthetic,
                evidence=envelope.evidence.model_dump(mode="json"),
            )
        )
    # The bundle transition trigger proves all three typed children exist.
    await session.flush()
    bundle.state = ConfirmationBundleState.COMPLETED.value
    bundle.qualification_status = (
        "qualified" if verified.ablations_complete else "unqualified"
    )
    bundle.completion_mode = settings.mode.value
    bundle.completion_ticket_id = ticket.ticket_id
    bundle.evidence_root = verified.root.model_dump(mode="json")
    bundle.evidence_sha256 = verified.evidence_sha256
    bundle.reporter_hotkey = ticket.validator_hotkey
    bundle.bundle_signature = report.bundle_signature
    bundle.verified_at = now
    bundle.completed_at = now
    await session.flush()
    ticket.status = "scored"
    subjects = list(
        await session.scalars(
            select(ConfirmationBundleSubject)
            .where(ConfirmationBundleSubject.bundle_id == bundle_id)
            .with_for_update()
        )
    )
    for subject in subjects:
        _apply_projection(
            subject,
            verified=verified,
            mode=settings.mode,
            profile=verification_profile,
        )
    await session.flush()
    return bundle


async def authorize_confirmation_bundle_retest(
    session: AsyncSession,
    *,
    source_bundle_id: UUID,
    authorization_id: UUID,
    expected_generation: int,
    actor: str,
    reason: str,
) -> RetestAuthorizationResult:
    """Supersede one terminal bundle with an audited next generation."""
    existing_auth = await session.get(ConfirmationRetestAuthorization, authorization_id)
    if existing_auth is not None:
        if (
            existing_auth.source_bundle_id != source_bundle_id
            or existing_auth.from_generation != expected_generation
            or existing_auth.actor != actor
            or existing_auth.reason != reason
        ):
            raise ConfirmationBundlePersistenceError(
                "retest request id was already used for different input"
            )
        new_bundle = await session.scalar(
            select(ConfirmationBundle).where(
                ConfirmationBundle.retest_authorization_id == authorization_id
            )
        )
        source = await session.get(ConfirmationBundle, source_bundle_id)
        if new_bundle is None or source is None:  # pragma: no cover - one transaction
            raise ConfirmationBundlePersistenceError("retest replay is incomplete")
        return RetestAuthorizationResult(
            authorization=existing_auth,
            superseded_bundle=source,
            bundle=new_bundle,
            replayed=True,
        )
    source = await session.scalar(
        select(ConfirmationBundle)
        .where(ConfirmationBundle.bundle_id == source_bundle_id)
        .with_for_update()
    )
    if source is None:
        raise ConfirmationBundlePersistenceError("source bundle does not exist")
    if source.state not in {
        ConfirmationBundleState.COMPLETED.value,
        ConfirmationBundleState.FAILED.value,
    }:
        raise ConfirmationBundlePersistenceError(
            "only completed or failed confirmation bundles can be retested"
        )
    latest_settings = await latest_confirmation_bundle_settings_revision(session)
    if latest_settings is None:
        raise ConfirmationBundlePersistenceError(
            "confirmation settings are unconfigured"
        )
    active_settings = ConfirmationBundleSettings.model_validate(
        latest_settings.settings
    )
    if active_settings.mode == ConfirmationBundleMode.OFF:
        raise ConfirmationBundlePersistenceError(
            "confirmation policy is off; enable issuance before authorizing a retest"
        )
    if (
        active_settings.profile_revision != source.profile_revision
        or active_settings.profile_checksum != source.profile_checksum
    ):
        raise ConfirmationBundlePersistenceError(
            "active confirmation profile does not match the source bundle"
        )
    latest_generation = await session.scalar(
        select(func.max(ConfirmationBundle.retest_generation)).where(
            ConfirmationBundle.artifact_sha256 == source.artifact_sha256,
            ConfirmationBundle.bench_version == source.bench_version,
            ConfirmationBundle.profile_revision == source.profile_revision,
            ConfirmationBundle.profile_checksum == source.profile_checksum,
        )
    )
    if int(latest_generation or 0) != expected_generation:
        raise ConfirmationBundlePersistenceError(
            "confirmation generation changed; refresh before authorizing retest"
        )
    generation = expected_generation + 1
    authorization = ConfirmationRetestAuthorization(
        authorization_id=authorization_id,
        source_bundle_id=source_bundle_id,
        artifact_sha256=source.artifact_sha256,
        bench_version=source.bench_version,
        profile_revision=source.profile_revision,
        profile_checksum=source.profile_checksum,
        from_generation=expected_generation,
        authorized_generation=generation,
        reason=reason,
        actor=actor,
    )
    session.add(authorization)
    await session.flush()
    new_bundle = ConfirmationBundle(
        artifact_sha256=source.artifact_sha256,
        bench_version=source.bench_version,
        profile_revision=source.profile_revision,
        profile_checksum=source.profile_checksum,
        retest_generation=generation,
        retest_authorization_id=authorization_id,
        generation_reason="operator_retest",
        source_bundle_id=source.bundle_id,
        settings_revision=latest_settings.revision,
        settings_checksum=latest_settings.checksum,
        state=ConfirmationBundleState.PENDING.value,
    )
    session.add(new_bundle)
    await session.flush()
    source.state = ConfirmationBundleState.SUPERSEDED.value
    subjects = list(
        await session.scalars(
            select(ConfirmationBundleSubject)
            .where(ConfirmationBundleSubject.bundle_id == source_bundle_id)
            .with_for_update()
        )
    )
    for subject in subjects:
        subject.bundle_id = new_bundle.bundle_id
        subject.result_status = ConfirmationResultStatus.PROVISIONAL.value
        _clear_subject_projection(subject)
    await session.flush()
    return RetestAuthorizationResult(
        authorization=authorization,
        superseded_bundle=source,
        bundle=new_bundle,
        replayed=False,
    )


async def list_confirmation_bundles(
    session: AsyncSession,
    *,
    state: str | None = None,
    minimum_bench_version: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> Sequence[ConfirmationBundle]:
    statement = select(ConfirmationBundle).order_by(
        ConfirmationBundle.created_at.desc(), ConfirmationBundle.bundle_id
    )
    if state is not None:
        statement = statement.where(ConfirmationBundle.state == state)
    if minimum_bench_version is not None:
        statement = statement.where(
            ConfirmationBundle.bench_version >= minimum_bench_version
        )
    return list(await session.scalars(statement.limit(limit).offset(offset)))


async def count_confirmation_bundles(
    session: AsyncSession,
    *,
    state: str | None = None,
    minimum_bench_version: int | None = None,
) -> int:
    statement = select(func.count()).select_from(ConfirmationBundle)
    if state is not None:
        statement = statement.where(ConfirmationBundle.state == state)
    if minimum_bench_version is not None:
        statement = statement.where(
            ConfirmationBundle.bench_version >= minimum_bench_version
        )
    return int(await session.scalar(statement) or 0)


async def confirmation_bundle_subjects(
    session: AsyncSession, *, bundle_id: UUID
) -> Sequence[ConfirmationBundleSubject]:
    return list(
        await session.scalars(
            select(ConfirmationBundleSubject)
            .where(ConfirmationBundleSubject.bundle_id == bundle_id)
            .order_by(
                ConfirmationBundleSubject.created_at, ConfirmationBundleSubject.agent_id
            )
        )
    )


async def confirmation_bundle_dimensions(
    session: AsyncSession, *, bundle_id: UUID
) -> Sequence[ConfirmationDimensionEvidence]:
    return list(
        await session.scalars(
            select(ConfirmationDimensionEvidence)
            .where(ConfirmationDimensionEvidence.bundle_id == bundle_id)
            .order_by(ConfirmationDimensionEvidence.dimension)
        )
    )


async def confirmation_bundle_tickets(
    session: AsyncSession, *, bundle_id: UUID
) -> Sequence[ConfirmationBundleTicket]:
    return list(
        await session.scalars(
            select(ConfirmationBundleTicket)
            .where(ConfirmationBundleTicket.bundle_id == bundle_id)
            .order_by(ConfirmationBundleTicket.attempt)
        )
    )


async def confirmation_budget_day(
    session: AsyncSession, *, utc_day: date
) -> ConfirmationBudgetDay | None:
    return await session.get(ConfirmationBudgetDay, utc_day)


__all__ = [
    "ActiveConfirmationSubject",
    "ActiveConfirmationWork",
    "BudgetReservationDecision",
    "BundleResolution",
    "CONFIRMATION_TICKET_TTL",
    "ConfirmationBundlePersistenceError",
    "ConfirmationShadowCalibration",
    "GLOBAL_SCOPE",
    "RetestAuthorizationResult",
    "SettlementResult",
    "StaleConfirmationBudget",
    "authorize_confirmation_bundle_retest",
    "complete_confirmation_bundle",
    "count_confirmation_bundles",
    "confirmation_budget_day",
    "confirmation_bundle_dimensions",
    "confirmation_bundle_subjects",
    "confirmation_bundle_tickets",
    "confirmation_shadow_calibration",
    "get_or_create_confirmation_bundle",
    "insert_confirmation_bundle_settings_revision",
    "issue_confirmation_bundle_ticket",
    "latest_confirmation_bundle_settings_revision",
    "lock_confirmation_budget_day",
    "list_active_confirmation_work",
    "list_confirmation_bundle_settings_revisions",
    "list_confirmation_bundles",
    "record_base_only_subject",
    "reserve_confirmation_bundle_budget",
    "settle_confirmation_bundle_budget",
]
