"""Reconcile ordinary quorum proofs with bounded LongMem confirmation work.

The score ledger remains authoritative for ordinary scoring.  This module only
projects its signature-bound lower-median base proof into the separate
confirmation ledger, then applies the pure owner/top-N/challenger policy.  It
never reserves budget, issues a lease, runs a provider, or mutates canonical
scores, rollouts, or rewards.

One reconciliation converges exactly one benchmark's cohort.  Callers pass the
live benchmark, so the lane follows the network forward instead of accumulating
work against an epoch that no longer ranks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.confirmation_bundles import (
    ConfirmationBundleMode,
    ConfirmationBundleSettings,
    supports_confirmation,
)
from ditto.api_models.validator import V9BaseEvidence
from ditto.api_server.confirmation_bundles import (
    ConfirmationCandidate,
    ConfirmationEligibilityMode,
    ConfirmationState,
    select_confirmation_candidates,
)
from ditto.api_server.confirmation_evidence import (
    ConfirmationEvidenceError,
    ConfirmationVerificationProfile,
    compute_subject_projection,
)
from ditto.db.models import (
    Agent,
    ConfirmationBundle,
    ConfirmationBundleSubject,
    EvaluationPayment,
    Score,
)
from ditto.db.queries.confirmation_bundles import (
    ConfirmationBundlePersistenceError,
    _replay_stored_bundle,
    get_or_create_confirmation_bundle,
    latest_confirmation_bundle_settings_revision,
    record_base_only_subject,
)
from ditto.db.queries.confirmation_policy_lock import try_lock_confirmation_policy
from ditto.db.queries.scores import (
    MIN_ELIGIBLE_CASES,
    SCORING_QUORUM,
    attested_emission_owner_roots,
    emission_owner,
    list_eligible_ledger,
    quorum_score_rows,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ConfirmationBaseProof:
    evidence_sha256: str
    quality_micros: int
    stderr_micros: int
    model_factor_bps: int
    tool_factor_bps: int


@dataclass(frozen=True)
class ConfirmationReconciliation:
    base_subjects: int = 0
    selected_subjects: int = 0
    resolved_bundles: int = 0
    reused_bundles: int = 0
    issuance_active: bool = False


def lower_median_base_proof(
    scores: Sequence[Score], *, artifact_sha256: str, bench_version: int
) -> ConfirmationBaseProof:
    """Return the physical lower-median row and its verified signed base root.

    Choosing a row, rather than averaging even-sized quorums, preserves a proof
    whose digest and validator signature can be independently checked.  The
    ordering exactly matches the canonical ledger's score/hotkey ordering.
    """
    if len(scores) < SCORING_QUORUM:
        raise ConfirmationBundlePersistenceError(
            "confirmation base proof requires a completed scoring quorum"
        )
    ordered = sorted(scores, key=lambda row: (row.composite, row.validator_hotkey))
    return _base_proof_from_score(
        ordered[(len(ordered) - 1) // 2],
        artifact_sha256=artifact_sha256,
        bench_version=bench_version,
    )


def _base_proof_from_score(
    score: Score, *, artifact_sha256: str, bench_version: int
) -> ConfirmationBaseProof:
    details = score.details if isinstance(score.details, dict) else {}
    raw = details.get("v9_base")
    digest = details.get("base_evidence_sha256")
    if not isinstance(raw, dict) or not isinstance(digest, str):
        raise ConfirmationBundlePersistenceError(
            "quorum median lacks signature-bound base evidence"
        )
    try:
        evidence = V9BaseEvidence.model_validate(raw)
    except ValidationError as error:
        raise ConfirmationBundlePersistenceError(
            "quorum median base evidence is invalid"
        ) from error
    if evidence.artifact_sha256 != artifact_sha256:
        raise ConfirmationBundlePersistenceError(
            "quorum median base evidence has the wrong artifact digest"
        )
    # The cohort is single-version by construction. Re-checking here keeps a
    # proof carried on a row from another epoch from ever anchoring a bundle.
    if evidence.bench_version != bench_version:
        raise ConfirmationBundlePersistenceError(
            "quorum median base evidence has the wrong bench version"
        )
    if digest != evidence.digest_hex():
        raise ConfirmationBundlePersistenceError(
            "quorum median base evidence digest is invalid"
        )
    if round(score.composite * 1_000_000) != evidence.effective_composite_micros:
        raise ConfirmationBundlePersistenceError(
            "quorum median score contradicts its base evidence"
        )
    return ConfirmationBaseProof(
        evidence_sha256=digest,
        quality_micros=evidence.ordinary_composite_micros,
        stderr_micros=evidence.ordinary_stderr_micros,
        model_factor_bps=evidence.score_gates.model_use.factor_bps,
        tool_factor_bps=evidence.score_gates.authoritative_tool.factor_bps,
    )


def registered_confirmation_profile(
    registry: object,
    *,
    revision: str | None,
    checksum: str | None,
) -> ConfirmationVerificationProfile | None:
    """Resolve one exact, process-owned profile or fail closed."""
    if revision is None or checksum is None or not isinstance(registry, Mapping):
        return None
    profile = registry.get((revision, checksum))
    if not isinstance(profile, ConfirmationVerificationProfile):
        return None
    try:
        if profile.revision != revision or profile.checksum() != checksum:
            return None
    except ConfirmationEvidenceError:
        return None
    return profile


async def reconcile_confirmation_candidates(
    session: AsyncSession,
    *,
    bench_version: int,
    verification_profiles: object,
    finalized_agent: Agent | None = None,
    finalized_scores: Sequence[Score] = (),
) -> ConfirmationReconciliation:
    """Persist a just-finalized proof and converge one benchmark's cohort.

    The optional finalized row is recorded even when moderation keeps it off
    the eligible leaderboard.  Candidate selection itself reads the canonical
    ledger for ``bench_version``, which already enforces full-run eligibility
    and one slot per attested owner.
    """
    if not supports_confirmation(bench_version):
        return ConfirmationReconciliation()

    recorded_agent_ids: set[UUID] = set()
    if finalized_agent is not None:
        proof = lower_median_base_proof(
            finalized_scores,
            artifact_sha256=finalized_agent.sha256,
            bench_version=bench_version,
        )
        await _record_base_subject(
            session,
            agent_id=finalized_agent.agent_id,
            bench_version=bench_version,
            proof=proof,
        )
        recorded_agent_ids.add(finalized_agent.agent_id)

    # Settings appends, candidate reconciliation, and claims share one policy
    # lock, so a bundle can never be created against a revision that ceased to
    # be latest while selection was running. Background reconciliation must
    # never queue on that lock, however: score finalization and confirmation
    # submission already own agent/ticket/bundle rows, while an operator write
    # owns the policy lock before reconciling those same rows. Waiting here
    # would invert that order and can put the audited policy write behind an
    # unbounded FIFO of score transactions. A caller that already owns the
    # lock (settings write or fresh claim) reacquires the transaction-scoped
    # advisory lock successfully. Every other caller preserves any just-recorded
    # base proof and defers cohort convergence to the next claim, score,
    # completion, or settings write.
    if not await try_lock_confirmation_policy(session):
        return ConfirmationReconciliation(base_subjects=len(recorded_agent_ids))

    settings_row = await latest_confirmation_bundle_settings_revision(session)
    settings = (
        ConfirmationBundleSettings.model_validate(settings_row.settings)
        if settings_row is not None
        else ConfirmationBundleSettings()
    )
    profile = registered_confirmation_profile(
        verification_profiles,
        revision=settings.profile_revision,
        checksum=settings.profile_checksum,
    )
    issuance_active = (
        settings_row is not None
        and settings.mode != ConfirmationBundleMode.OFF
        and profile is not None
    )

    # Rank confirmation follows the live board's owner representative
    # (``official`` / continual when that lane is active). Canonical medians
    # can keep an older family version in front after the board has moved on,
    # and those are not the rows operators rank as the current kings.
    ledger = await list_eligible_ledger(
        session,
        bench_version=bench_version,
        owner_score="official",
        apply_v9_confirmation_policy=False,
        include_fingerprints=False,
        details_keys=("v9_base", "base_evidence_sha256"),
    )
    ledger_owner_roots = await attested_emission_owner_roots(
        session,
        [
            (
                row.miner_hotkey,
                emission_owner(
                    miner_hotkey=row.miner_hotkey,
                    miner_coldkey=row.miner_coldkey,
                ),
            )
            for row in ledger
        ],
    )
    candidates: list[ConfirmationCandidate] = []
    proofs: dict[str, ConfirmationBaseProof] = {}
    agent_ids: dict[str, UUID] = {}
    for owner_root, row in zip(ledger_owner_roots, ledger, strict=True):
        if not row.eligible:
            continue
        # list_eligible_ledger returns the physical lower-median row, preserving
        # its signature and details. A lightweight Score-shaped carrier reuses
        # the exact proof validator from the direct post-quorum path.
        carrier = Score(
            agent_id=row.agent_id,
            validator_hotkey=row.validator_hotkey,
            bench_version=bench_version,
            run_id=row.run_id,
            signature=row.signature,
            seed=row.seed,
            composite=row.composite,
            tool_mean=row.tool_mean,
            memory_mean=row.memory_mean,
            median_ms=row.median_ms,
            n=row.n,
            details=row.details,
            generated_at=row.first_seen,
        )
        proof = _base_proof_from_score(
            carrier, artifact_sha256=row.sha256, bench_version=bench_version
        )
        await _record_base_subject(
            session,
            agent_id=row.agent_id,
            bench_version=bench_version,
            proof=proof,
        )
        recorded_agent_ids.add(row.agent_id)
        subject = await session.get(
            ConfirmationBundleSubject, (row.agent_id, bench_version)
        )
        if subject is None:  # pragma: no cover - record + flush is authoritative
            raise ConfirmationBundlePersistenceError(
                "confirmation subject was not persisted"
            )
        bundle = (
            await session.get(ConfirmationBundle, subject.bundle_id)
            if subject.bundle_id is not None
            else None
        )
        state = (
            ConfirmationState(bundle.state)
            if bundle is not None
            else ConfirmationState.BASE_ONLY
        )
        key = str(row.agent_id)
        proofs[key] = proof
        agent_ids[key] = row.agent_id
        candidates.append(
            ConfirmationCandidate(
                agent_id=key,
                # The ledger has already selected one representative per
                # attested owner graph, but persisted-subject fallback rows are
                # merged below. Keep both sources in the graph's canonical key
                # space or one owner can occupy a slot through each source.
                owner_id=owner_root,
                artifact_sha256=row.sha256,
                bench_version=bench_version,
                base_composite=proof.quality_micros / 1_000_000,
                base_stderr=proof.stderr_micros / 1_000_000,
                first_seen=row.first_seen,
                confirmation_state=state,
                full_composite=(
                    subject.full_effective_micros / 1_000_000
                    if subject.full_effective_micros is not None
                    else None
                ),
            )
        )

    ledger_owner_ids = {candidate.owner_id for candidate in candidates}

    # Once a subject has been persisted it remains the durable reconciliation
    # input. This matters after completion and settings changes: those triggers
    # must be able to re-evaluate a candidate even when a concurrent rollout
    # temporarily removes explicit rows from the live ledger read. It must not
    # let a higher-base older sibling steal the slot from the owner already
    # represented on that ledger.
    persisted_rows = (
        await session.execute(
            select(
                ConfirmationBundleSubject,
                Agent,
                EvaluationPayment.miner_coldkey,
            )
            .join(Agent, Agent.agent_id == ConfirmationBundleSubject.agent_id)
            .outerjoin(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
            .where(
                ConfirmationBundleSubject.bench_version == bench_version,
                Agent.status == AgentStatus.SCORED,
            )
            .order_by(Agent.created_at, Agent.agent_id)
        )
    ).all()
    missing_rows = [
        (subject, agent, coldkey)
        for subject, agent, coldkey in persisted_rows
        if str(agent.agent_id) not in proofs
    ]
    if missing_rows:
        score_rows = await quorum_score_rows(
            session,
            [agent.agent_id for _, agent, _ in missing_rows],
            bench_versions={
                agent.agent_id: bench_version for _, agent, _ in missing_rows
            },
        )
        roots = await attested_emission_owner_roots(
            session,
            [
                (
                    agent.miner_hotkey,
                    emission_owner(
                        miner_hotkey=agent.miner_hotkey, miner_coldkey=coldkey
                    ),
                )
                for _, agent, coldkey in missing_rows
            ],
        )
        for root, (subject, agent, _coldkey) in zip(roots, missing_rows, strict=True):
            if root in ledger_owner_ids:
                continue
            rows = score_rows.get(agent.agent_id, [])
            if len(rows) < SCORING_QUORUM:
                continue
            ordered = sorted(
                rows, key=lambda row: (row.composite, row.validator_hotkey)
            )
            representative = ordered[(len(ordered) - 1) // 2]
            if representative.n < MIN_ELIGIBLE_CASES or representative.composite <= 0:
                continue
            proof = _base_proof_from_score(
                representative,
                artifact_sha256=agent.sha256,
                bench_version=bench_version,
            )
            bundle = (
                await session.get(ConfirmationBundle, subject.bundle_id)
                if subject.bundle_id is not None
                else None
            )
            key = str(agent.agent_id)
            proofs[key] = proof
            agent_ids[key] = agent.agent_id
            candidates.append(
                ConfirmationCandidate(
                    agent_id=key,
                    owner_id=root,
                    artifact_sha256=agent.sha256,
                    bench_version=bench_version,
                    base_composite=proof.quality_micros / 1_000_000,
                    base_stderr=proof.stderr_micros / 1_000_000,
                    first_seen=agent.created_at,
                    confirmation_state=(
                        ConfirmationState(bundle.state)
                        if bundle is not None
                        else ConfirmationState.BASE_ONLY
                    ),
                    full_composite=(
                        subject.full_effective_micros / 1_000_000
                        if subject.full_effective_micros is not None
                        else None
                    ),
                )
            )

    selection = select_confirmation_candidates(
        candidates,
        top_n=settings.top_n,
        challenger_z=settings.challenger_z,
        eligibility_mode=ConfirmationEligibilityMode(settings.eligibility_mode),
        min_base_score_micros=settings.min_base_score_micros,
    )
    if not issuance_active or settings_row is None or profile is None:
        return ConfirmationReconciliation(
            base_subjects=len(recorded_agent_ids),
            selected_subjects=len(selection.selected),
            issuance_active=False,
        )

    resolved = 0
    reused = 0
    for candidate in selection.selected:
        proof = proofs[candidate.agent_id]
        resolution = await get_or_create_confirmation_bundle(
            session,
            agent_id=agent_ids[candidate.agent_id],
            bench_version=bench_version,
            base_evidence_sha256=proof.evidence_sha256,
            base_quality_micros=proof.quality_micros,
            base_stderr_micros=proof.stderr_micros,
            base_model_factor_bps=proof.model_factor_bps,
            base_tool_factor_bps=proof.tool_factor_bps,
            settings_revision=settings_row.revision,
            settings=settings,
            verification_profile=profile,
        )
        if resolution.bundle is not None:
            resolved += 1
        if resolution.reused_completed_evidence:
            reused += 1
            _reproject_completed_subject_for_current_mode(
                resolution.subject,
                bundle=resolution.bundle,
                profile=profile,
                mode=settings.mode,
            )
    await session.flush()
    return ConfirmationReconciliation(
        base_subjects=len(recorded_agent_ids),
        selected_subjects=len(selection.selected),
        resolved_bundles=resolved,
        reused_bundles=reused,
        issuance_active=True,
    )


async def _record_base_subject(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    proof: ConfirmationBaseProof,
) -> None:
    await record_base_only_subject(
        session,
        agent_id=agent_id,
        bench_version=bench_version,
        base_evidence_sha256=proof.evidence_sha256,
        base_quality_micros=proof.quality_micros,
        base_stderr_micros=proof.stderr_micros,
        base_model_factor_bps=proof.model_factor_bps,
        base_tool_factor_bps=proof.tool_factor_bps,
    )


def _reproject_completed_subject_for_current_mode(
    subject: ConfirmationBundleSubject,
    *,
    bundle: ConfirmationBundle | None,
    profile: ConfirmationVerificationProfile,
    mode: ConfirmationBundleMode,
) -> None:
    """Reuse immutable signed evidence while applying today's operator mode."""
    if bundle is None:
        raise ConfirmationBundlePersistenceError(
            "reused confirmation evidence is missing its bundle"
        )
    verified, _frozen_mode = _replay_stored_bundle(bundle, profile)
    projection = compute_subject_projection(
        mode=mode,
        base_quality_micros=subject.base_quality_micros,
        base_stderr_micros=subject.base_stderr_micros,
        base_model_factor_bps=subject.base_model_factor_bps,
        base_tool_factor_bps=subject.base_tool_factor_bps,
        verified=verified,
        composite=profile.composite,
    )
    subject.result_status = projection.result_status
    subject.full_quality_micros = projection.full_quality_micros
    subject.full_stderr_micros = projection.full_stderr_micros
    subject.semantic_factor_bps = projection.semantic_factor_bps
    subject.applied_factor_bps = projection.applied_factor_bps
    subject.full_effective_micros = projection.full_effective_micros


__all__ = [
    "ConfirmationReconciliation",
    "ConfirmationBaseProof",
    "lower_median_base_proof",
    "reconcile_confirmation_candidates",
    "registered_confirmation_profile",
]
