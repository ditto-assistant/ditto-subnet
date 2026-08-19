"""DB-backed reconciliation tests for ordinary quorum -> confirmation work."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.confirmation_bundles import (
    ConfirmationBundleMode,
    ConfirmationBundleSettings,
    ConfirmationResultStatus,
)
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.validator import V9BaseEvidence
from ditto.api_server.confirmation_candidate_reconciliation import (
    ConfirmationReconciliation,
    lower_median_base_proof,
    reconcile_confirmation_candidates,
)
from ditto.db.models import (
    Agent,
    ConfirmationBudgetReservation,
    ConfirmationBundle,
    ConfirmationBundleSettingsRevision,
    ConfirmationBundleSubject,
    EvaluationPayment,
    Score,
)
from ditto.db.queries.confirmation_bundles import (
    complete_confirmation_bundle,
    insert_confirmation_bundle_settings_revision,
    issue_confirmation_bundle_ticket,
    reserve_confirmation_bundle_budget,
    settle_confirmation_bundle_budget,
)
from ditto.db.queries.confirmation_policy_lock import lock_confirmation_policy
from ditto.tests.confirmation_evidence_fixtures import (
    VALIDATOR_KEYPAIR,
    active_settings,
    signed_report,
    verification_profile,
)
from ditto_screening_protocol.bench_v9 import (
    CONFIRMATION_BENCH_VERSIONS,
    V9ScoreGateEvidence,
)

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)
_VECTOR_PATH = (
    Path(__file__).resolve().parents[5]
    / "services/dittobench-api/testdata/v9_base_contract_vectors.json"
)
_VECTOR = json.loads(_VECTOR_PATH.read_text())["vectors"][0]["details"]


def _checksum(settings: ConfirmationBundleSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _settings(
    session: AsyncSession,
    *,
    mode: ConfirmationBundleMode = ConfirmationBundleMode.SHADOW,
    top_n: int = 5,
    parent_revision: int = 0,
    daily_bundle_cap: int | None = None,
) -> tuple[ConfirmationBundleSettingsRevision, ConfirmationBundleSettings]:
    settings = active_settings(mode=mode).model_copy(update={"top_n": top_n})
    if daily_bundle_cap is not None:
        settings = settings.model_copy(update={"daily_bundle_cap": daily_bundle_cap})
    row = await insert_confirmation_bundle_settings_revision(
        session,
        parent_revision=parent_revision,
        scope="*",
        settings=settings.model_dump(mode="json"),
        checksum=_checksum(settings),
        reason="operator approved candidate reconciliation test",
        actor="operator@example.com",
    )
    return row, settings


def _score(
    agent_id: UUID,
    *,
    artifact_sha256: str,
    composite_micros: int,
    stderr_micros: int,
    validator_index: int,
    bench_version: int = 9,
) -> Score:
    raw = copy.deepcopy(_VECTOR)
    raw.update(
        {
            "run_id": f"run-{agent_id}-{validator_index}",
            "artifact_sha256": artifact_sha256,
            "ordinary_composite_micros": composite_micros,
            "ordinary_stderr_micros": stderr_micros,
            "effective_composite_micros": composite_micros,
            "effective_stderr_micros": stderr_micros,
        }
    )
    # The gate stack is digest-bound, so moving the epoch means re-deriving the
    # digest exactly as a scorer would rather than editing the field in place.
    # v12 also requires model_dependence; a version bump that only rewrites
    # bench_version is not a valid digest.
    gate_payload = {**raw["score_gates"], "bench_version": bench_version}
    if bench_version >= 12:
        gate_payload["model_dependence"] = {
            "administered_cases": 10,
            "eligible_cases": 10,
            "dependent_cases": 10,
            "independent_cases": 0,
            "slice_attribution_complete": True,
            "dependence_bps": 10000,
            "threshold_bps": 1,
            "result": "passed",
            "factor_bps": 10000,
        }
    gates = V9ScoreGateEvidence.model_validate(gate_payload)
    raw["bench_version"] = bench_version
    raw["score_gates"] = gates.model_dump(mode="json")
    raw["score_gates_sha256"] = gates.digest_hex()
    evidence = V9BaseEvidence.model_validate(raw)
    digest = evidence.digest_hex()
    return Score(
        agent_id=agent_id,
        validator_hotkey=f"5Validator-{validator_index:02d}",
        bench_version=bench_version,
        run_id=evidence.run_id,
        signature=f"{validator_index + 1:02x}",
        seed=validator_index,
        composite=composite_micros / 1_000_000,
        tool_mean=composite_micros / 1_000_000,
        memory_mean=composite_micros / 1_000_000,
        median_ms=100,
        n=114,
        details={
            "v9_base": evidence.model_dump(mode="json"),
            "base_evidence_sha256": digest,
        },
        generated_at=_NOW + timedelta(seconds=validator_index),
    )


async def _agent_with_quorum(
    session: AsyncSession,
    *,
    index: int,
    composites: tuple[int, ...],
    stderr_micros: int = 10_000,
    artifact_sha256: str | None = None,
    bench_version: int = 9,
) -> tuple[Agent, list[Score]]:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=f"5Miner-{index:02d}",
        name=f"candidate-{index}",
        sha256=artifact_sha256 or f"{index + 1:064x}",
        status=AgentStatus.SCORED,
        screening_policy_version=SCREENING_POLICY_VERSION,
        created_at=_NOW + timedelta(minutes=index),
    )
    scores = [
        _score(
            agent.agent_id,
            artifact_sha256=agent.sha256,
            composite_micros=value,
            stderr_micros=stderr_micros,
            validator_index=position,
            bench_version=bench_version,
        )
        for position, value in enumerate(composites)
    ]
    session.add(agent)
    session.add_all(scores)
    await session.flush()
    return agent, scores


def _registry() -> dict[tuple[str, str], object]:
    profile = verification_profile()
    return {(profile.revision, profile.checksum()): profile}


async def test_no_settings_persists_physical_lower_median_base_proof_only(
    session: AsyncSession,
) -> None:
    async with session.begin():
        agent, scores = await _agent_with_quorum(
            session,
            index=0,
            composites=(900_000, 600_000, 800_000, 700_000),
        )
        expected = lower_median_base_proof(
            scores, artifact_sha256=agent.sha256, bench_version=9
        )
        result = await reconcile_confirmation_candidates(
            session,
            bench_version=9,
            verification_profiles={},
            finalized_agent=agent,
            finalized_scores=scores,
        )

    subject = await session.get(ConfirmationBundleSubject, (agent.agent_id, 9))
    assert subject is not None
    assert subject.base_evidence_sha256 == expected.evidence_sha256
    assert subject.base_quality_micros == 700_000
    assert subject.result_status == ConfirmationResultStatus.BASE_ONLY.value
    assert subject.bundle_id is None
    assert result.issuance_active is False
    bundle_count = await session.scalar(
        select(func.count()).select_from(ConfirmationBundle)
    )
    assert bundle_count == 0


async def test_background_reconciliation_yields_to_contended_policy_write(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        await _settings(session)
        agent, _scores = await _agent_with_quorum(
            session,
            index=0,
            composites=(700_000, 800_000, 900_000),
        )
        agent_id = agent.agent_id

    async def reconcile() -> ConfirmationReconciliation:
        async with session_maker() as worker, worker.begin():
            finalized_agent = await worker.get(Agent, agent_id)
            assert finalized_agent is not None
            finalized_scores = list(
                await worker.scalars(
                    select(Score)
                    .where(Score.agent_id == agent_id, Score.bench_version == 9)
                    .order_by(Score.validator_hotkey)
                )
            )
            return await reconcile_confirmation_candidates(
                worker,
                bench_version=9,
                verification_profiles=_registry(),
                finalized_agent=finalized_agent,
                finalized_scores=finalized_scores,
            )

    # This is the exact transaction-scoped advisory lock an audited settings
    # write owns. Score finalization must preserve its base proof and return,
    # not wait behind the operator or join a FIFO of background reconcilers.
    async with session_maker() as holder, holder.begin():
        await lock_confirmation_policy(holder)
        result = await asyncio.wait_for(reconcile(), timeout=2)

    assert result.base_subjects == 1
    assert result.selected_subjects == 0
    assert result.resolved_bundles == 0
    async with session_maker() as session:
        subject = await session.get(ConfirmationBundleSubject, (agent_id, 9))
        assert subject is not None
        assert subject.result_status == ConfirmationResultStatus.BASE_ONLY.value
        assert subject.bundle_id is None
        assert (
            await session.scalar(select(func.count()).select_from(ConfirmationBundle))
            == 0
        )


# The evidence stack itself is pinned by ``V9EvidenceBenchVersion``; these are
# every epoch that currently carries it.
@pytest.mark.parametrize("live_version", CONFIRMATION_BENCH_VERSIONS)
async def test_confirmation_follows_the_live_benchmark(
    session: AsyncSession, live_version: int
) -> None:
    """LongMemEval is a permanent fixture, so every live epoch gets a cohort.

    This is the regression that matters most: the lane used to accept only
    bench 9, so the moment the network activated a later epoch it stopped
    producing candidates entirely and kept re-superseding a frozen cohort of
    submissions that no longer ranked.
    """
    async with session.begin():
        await _settings(session)
        agent, scores = await _agent_with_quorum(
            session,
            index=0,
            composites=(970_000, 980_000, 990_000),
            bench_version=live_version,
        )
        session.add(
            EvaluationPayment(
                block_hash=f"0x{0:064x}",
                extrinsic_index=0,
                agent_id=agent.agent_id,
                miner_hotkey=agent.miner_hotkey,
                miner_coldkey="5LiveBenchOwner",
                amount_rao=1,
                dest_address="5ConfirmationTreasury",
                timestamp=_NOW,
            )
        )
        result = await reconcile_confirmation_candidates(
            session,
            bench_version=live_version,
            verification_profiles=_registry(),
            finalized_agent=agent,
            finalized_scores=scores,
        )

    assert result.base_subjects == 1
    subject = await session.get(
        ConfirmationBundleSubject, (agent.agent_id, live_version)
    )
    assert subject is not None
    assert subject.bench_version == live_version
    # The subject must not be filed under the epoch the lane happened to ship
    # in; that aliasing is what made the stale cohort look populated.
    if live_version != 9:
        assert await session.get(ConfirmationBundleSubject, (agent.agent_id, 9)) is None


@pytest.mark.parametrize("pre_contract_version", [1, 8])
async def test_pre_contract_bench_versions_never_enter_the_ledger(
    session: AsyncSession, pre_contract_version: int
) -> None:
    """Below the first version carrying a typed base root there is nothing to
    project, so those closed contracts stay exactly as they were."""
    async with session.begin():
        await _settings(session)
        agent, scores = await _agent_with_quorum(
            session, index=0, composites=(700_000, 800_000, 900_000)
        )
        result = await reconcile_confirmation_candidates(
            session,
            bench_version=pre_contract_version,
            verification_profiles=_registry(),
            finalized_agent=agent,
            finalized_scores=scores,
        )
    assert result == ConfirmationReconciliation()
    assert (
        await session.get(
            ConfirmationBundleSubject, (agent.agent_id, pre_contract_version)
        )
        is None
    )
    assert (
        await session.scalar(select(func.count()).select_from(ConfirmationBundle)) == 0
    )


async def test_active_settings_without_exact_registered_profile_fail_base_only(
    session: AsyncSession,
) -> None:
    async with session.begin():
        await _settings(session)
        agent, scores = await _agent_with_quorum(
            session, index=0, composites=(700_000, 800_000, 900_000)
        )
        result = await reconcile_confirmation_candidates(
            session,
            bench_version=9,
            verification_profiles={},
            finalized_agent=agent,
            finalized_scores=scores,
        )
    subject = await session.get(ConfirmationBundleSubject, (agent.agent_id, 9))
    assert subject is not None and subject.bundle_id is None
    assert result.issuance_active is False


async def test_off_settings_never_create_work_even_with_registered_profile(
    session: AsyncSession,
) -> None:
    async with session.begin():
        await _settings(session, mode=ConfirmationBundleMode.OFF)
        agent, scores = await _agent_with_quorum(
            session, index=0, composites=(700_000, 800_000, 900_000)
        )
        result = await reconcile_confirmation_candidates(
            session,
            bench_version=9,
            verification_profiles=_registry(),
            finalized_agent=agent,
            finalized_scores=scores,
        )
    subject = await session.get(ConfirmationBundleSubject, (agent.agent_id, 9))
    assert subject is not None and subject.bundle_id is None
    assert result.issuance_active is False
    bundle_count = await session.scalar(
        select(func.count()).select_from(ConfirmationBundle)
    )
    assert bundle_count == 0


async def test_top_n_challenger_digest_reuse_and_replay_are_deterministic(
    session: AsyncSession,
) -> None:
    shared_digest = "a" * 64
    async with session.begin():
        await _settings(session, top_n=2)
        first, _ = await _agent_with_quorum(
            session,
            index=0,
            composites=(900_000,) * 3,
            artifact_sha256=shared_digest,
        )
        renamed, _ = await _agent_with_quorum(
            session,
            index=1,
            composites=(800_000,) * 3,
            artifact_sha256=shared_digest,
        )
        challenger, _ = await _agent_with_quorum(
            session,
            index=2,
            composites=(790_000,) * 3,
            stderr_micros=20_000,
        )
        outside, _ = await _agent_with_quorum(
            session,
            index=3,
            composites=(700_000,) * 3,
        )
        first_pass = await reconcile_confirmation_candidates(
            session, bench_version=9, verification_profiles=_registry()
        )
        second_pass = await reconcile_confirmation_candidates(
            session, bench_version=9, verification_profiles=_registry()
        )

    subjects = {
        agent.agent_id: await session.get(
            ConfirmationBundleSubject, (agent.agent_id, 9)
        )
        for agent in (first, renamed, challenger, outside)
    }
    first_subject = subjects[first.agent_id]
    renamed_subject = subjects[renamed.agent_id]
    challenger_subject = subjects[challenger.agent_id]
    outside_subject = subjects[outside.agent_id]
    assert first_subject is not None
    assert renamed_subject is not None
    assert challenger_subject is not None
    assert outside_subject is not None
    assert first_subject.bundle_id == renamed_subject.bundle_id
    assert challenger_subject.bundle_id is not None
    assert outside_subject.bundle_id is None
    assert first_pass.selected_subjects == second_pass.selected_subjects == 3
    bundle_count = await session.scalar(
        select(func.count()).select_from(ConfirmationBundle)
    )
    assert bundle_count == 2


async def test_ledger_and_persisted_fallback_share_attested_owner_key_space(
    session: AsyncSession,
) -> None:
    coldkey = "5SharedConfirmationOwner"

    def payment(agent: Agent, *, index: int) -> EvaluationPayment:
        return EvaluationPayment(
            block_hash=f"0x{index:064x}",
            extrinsic_index=index,
            agent_id=agent.agent_id,
            miner_hotkey=agent.miner_hotkey,
            miner_coldkey=coldkey,
            amount_rao=1,
            dest_address="5ConfirmationTreasury",
            timestamp=_NOW + timedelta(minutes=index),
        )

    async with session.begin():
        older, older_scores = await _agent_with_quorum(
            session,
            index=0,
            composites=(700_000,) * 3,
        )
        session.add(payment(older, index=0))
        await session.flush()
        # Persist the older subject while issuance is disabled, reproducing a
        # durable fallback row without creating spendable work.
        await reconcile_confirmation_candidates(
            session,
            bench_version=9,
            verification_profiles={},
            finalized_agent=older,
            finalized_scores=older_scores,
        )

        await _settings(session, top_n=2)
        winner, _ = await _agent_with_quorum(
            session,
            index=1,
            composites=(900_000,) * 3,
        )
        session.add(payment(winner, index=1))
        await session.flush()
        result = await reconcile_confirmation_candidates(
            session, bench_version=9, verification_profiles=_registry()
        )

    older_subject = await session.get(ConfirmationBundleSubject, (older.agent_id, 9))
    winner_subject = await session.get(ConfirmationBundleSubject, (winner.agent_id, 9))
    assert older_subject is not None and older_subject.bundle_id is None
    assert winner_subject is not None and winner_subject.bundle_id is not None
    assert result.selected_subjects == 1
    assert (
        await session.scalar(select(func.count()).select_from(ConfirmationBundle)) == 1
    )


async def test_settings_change_supersedes_only_zero_spend_pending_generation(
    session: AsyncSession,
) -> None:
    registry = _registry()
    async with session.begin():
        shadow_revision, _ = await _settings(session, top_n=1)
        agent, _ = await _agent_with_quorum(session, index=0, composites=(800_000,) * 3)
        await reconcile_confirmation_candidates(
            session, bench_version=9, verification_profiles=registry
        )
        subject = await session.get(ConfirmationBundleSubject, (agent.agent_id, 9))
        assert subject is not None and subject.bundle_id is not None
        original = await session.get(ConfirmationBundle, subject.bundle_id)
        assert original is not None

        enforce_revision, _ = await _settings(
            session,
            mode=ConfirmationBundleMode.ENFORCE,
            top_n=1,
            parent_revision=shadow_revision.revision,
        )
        first = await reconcile_confirmation_candidates(
            session, bench_version=9, verification_profiles=registry
        )
        replay = await reconcile_confirmation_candidates(
            session, bench_version=9, verification_profiles=registry
        )

    refreshed = await session.get(ConfirmationBundleSubject, (agent.agent_id, 9))
    assert refreshed is not None and refreshed.bundle_id is not None
    replacement = await session.get(ConfirmationBundle, refreshed.bundle_id)
    assert replacement is not None
    assert original.state == "superseded"
    assert original.evidence_sha256 is None
    assert replacement.bundle_id != original.bundle_id
    assert replacement.retest_generation == original.retest_generation + 1
    assert replacement.generation_reason == "settings_supersession"
    assert replacement.source_bundle_id == original.bundle_id
    assert replacement.settings_revision == enforce_revision.revision
    assert first.resolved_bundles == replay.resolved_bundles == 1
    bundle_count = await session.scalar(
        select(func.count()).select_from(ConfirmationBundle)
    )
    assert bundle_count == 2


async def test_settings_change_recovers_failed_spent_generation(
    session: AsyncSession,
) -> None:
    registry = _registry()
    async with session.begin():
        shadow_revision, shadow = await _settings(session, top_n=1)
        agent, _ = await _agent_with_quorum(session, index=0, composites=(800_000,) * 3)
        await reconcile_confirmation_candidates(
            session, bench_version=9, verification_profiles=registry
        )
        subject = await session.get(ConfirmationBundleSubject, (agent.agent_id, 9))
        assert subject is not None and subject.bundle_id is not None
        source = await session.get(ConfirmationBundle, subject.bundle_id)
        assert source is not None
        decision = await reserve_confirmation_bundle_budget(
            session,
            bundle_id=source.bundle_id,
            reservation_id=uuid4(),
            now=_NOW,
            expected_revision=0,
            settings_revision=shadow_revision.revision,
            settings=shadow,
            reserve_microusd=50_000,
        )
        assert decision.reservation is not None
        ticket = await issue_confirmation_bundle_ticket(
            session,
            bundle_id=source.bundle_id,
            reservation_id=decision.reservation.reservation_id,
            validator_hotkey=VALIDATOR_KEYPAIR.ss58_address,
            slot_id="longmem-0",
            now=_NOW,
        )
        await settle_confirmation_bundle_budget(
            session,
            reservation_id=decision.reservation.reservation_id,
            expected_revision=1,
            actual_microusd=42_000,
            failed_attempt=True,
            settled_at=_NOW + timedelta(minutes=1),
        )
        assert source.state == "failed"

        enforce_revision, _ = await _settings(
            session,
            mode=ConfirmationBundleMode.ENFORCE,
            top_n=1,
            parent_revision=shadow_revision.revision,
        )
        await reconcile_confirmation_candidates(
            session, bench_version=9, verification_profiles=registry
        )

    refreshed = await session.get(ConfirmationBundleSubject, (agent.agent_id, 9))
    assert refreshed is not None and refreshed.bundle_id is not None
    replacement = await session.get(ConfirmationBundle, refreshed.bundle_id)
    assert replacement is not None
    assert source.state == "superseded"
    assert replacement.source_bundle_id == source.bundle_id
    assert replacement.generation_reason == "settings_supersession"
    assert replacement.settings_revision == enforce_revision.revision
    assert replacement.state == "pending"
    assert decision.reservation.state == "settled"
    assert decision.reservation.actual_microusd == 42_000
    assert ticket.status == "expired"


async def test_settings_change_recovers_budget_blocked_spent_generation(
    session: AsyncSession,
) -> None:
    registry = _registry()
    async with session.begin():
        shadow_revision, shadow = await _settings(session, top_n=1, daily_bundle_cap=1)
        agent, _ = await _agent_with_quorum(session, index=0, composites=(800_000,) * 3)
        await reconcile_confirmation_candidates(
            session, bench_version=9, verification_profiles=registry
        )
        subject = await session.get(ConfirmationBundleSubject, (agent.agent_id, 9))
        assert subject is not None and subject.bundle_id is not None
        source = await session.get(ConfirmationBundle, subject.bundle_id)
        assert source is not None
        first = await reserve_confirmation_bundle_budget(
            session,
            bundle_id=source.bundle_id,
            reservation_id=uuid4(),
            now=_NOW,
            expected_revision=0,
            settings_revision=shadow_revision.revision,
            settings=shadow,
            reserve_microusd=50_000,
        )
        assert first.reservation is not None
        await issue_confirmation_bundle_ticket(
            session,
            bundle_id=source.bundle_id,
            reservation_id=first.reservation.reservation_id,
            validator_hotkey=VALIDATOR_KEYPAIR.ss58_address,
            slot_id="longmem-1",
            now=_NOW,
        )
        await settle_confirmation_bundle_budget(
            session,
            reservation_id=first.reservation.reservation_id,
            expected_revision=1,
            actual_microusd=50_000,
            failed_attempt=True,
            settled_at=_NOW + timedelta(minutes=1),
        )
        blocked = await reserve_confirmation_bundle_budget(
            session,
            bundle_id=source.bundle_id,
            reservation_id=uuid4(),
            now=_NOW + timedelta(minutes=2),
            expected_revision=2,
            settings_revision=shadow_revision.revision,
            settings=shadow,
            reserve_microusd=50_000,
        )
        assert blocked.reservation is None
        assert blocked.blocked_reason == "bundle_cap"
        assert source.state == "blocked_budget"

        enforce_revision, _ = await _settings(
            session,
            mode=ConfirmationBundleMode.ENFORCE,
            top_n=1,
            parent_revision=shadow_revision.revision,
            daily_bundle_cap=2,
        )
        await reconcile_confirmation_candidates(
            session, bench_version=9, verification_profiles=registry
        )

    refreshed = await session.get(ConfirmationBundleSubject, (agent.agent_id, 9))
    assert refreshed is not None and refreshed.bundle_id is not None
    replacement = await session.get(ConfirmationBundle, refreshed.bundle_id)
    assert replacement is not None
    assert source.state == "superseded"
    assert replacement.source_bundle_id == source.bundle_id
    assert replacement.settings_revision == enforce_revision.revision
    assert replacement.state == "pending"
    assert first.reservation.state == "settled"
    assert first.reservation.actual_microusd == 50_000


async def test_reserved_pending_generation_requires_operator_recovery(
    session: AsyncSession,
) -> None:
    registry = _registry()
    async with session.begin():
        shadow_revision, shadow = await _settings(session, top_n=1)
        agent, _ = await _agent_with_quorum(session, index=0, composites=(800_000,) * 3)
        await reconcile_confirmation_candidates(
            session, bench_version=9, verification_profiles=registry
        )
        subject = await session.get(ConfirmationBundleSubject, (agent.agent_id, 9))
        assert subject is not None and subject.bundle_id is not None
        original = await session.get(ConfirmationBundle, subject.bundle_id)
        assert original is not None
        decision = await reserve_confirmation_bundle_budget(
            session,
            bundle_id=original.bundle_id,
            reservation_id=uuid4(),
            now=_NOW,
            expected_revision=0,
            settings_revision=shadow_revision.revision,
            settings=shadow,
            reserve_microusd=50_000,
        )
        assert decision.reservation is not None

        await _settings(
            session,
            mode=ConfirmationBundleMode.ENFORCE,
            top_n=1,
            parent_revision=shadow_revision.revision,
        )
        await reconcile_confirmation_candidates(
            session, bench_version=9, verification_profiles=registry
        )

    refreshed = await session.get(ConfirmationBundleSubject, (agent.agent_id, 9))
    assert refreshed is not None
    assert refreshed.bundle_id == original.bundle_id
    assert original.state == "pending"
    bundle_count = await session.scalar(
        select(func.count()).select_from(ConfirmationBundle)
    )
    assert bundle_count == 1


async def test_shadow_evidence_reprojects_under_enforce_without_new_spend(
    session: AsyncSession,
) -> None:
    profile = verification_profile()
    registry = _registry()
    async with session.begin():
        shadow_revision, shadow = await _settings(session, top_n=1)
        agent, _ = await _agent_with_quorum(session, index=0, composites=(800_000,) * 3)
        await reconcile_confirmation_candidates(
            session, bench_version=9, verification_profiles=registry
        )
        subject = await session.get(ConfirmationBundleSubject, (agent.agent_id, 9))
        assert subject is not None and subject.bundle_id is not None
        bundle = await session.get(ConfirmationBundle, subject.bundle_id)
        assert bundle is not None
        reservation = await reserve_confirmation_bundle_budget(
            session,
            bundle_id=bundle.bundle_id,
            reservation_id=uuid4(),
            now=_NOW,
            expected_revision=0,
            settings_revision=shadow_revision.revision,
            settings=shadow,
            reserve_microusd=50_000,
        )
        assert reservation.reservation is not None
        ticket = await issue_confirmation_bundle_ticket(
            session,
            bundle_id=bundle.bundle_id,
            reservation_id=reservation.reservation.reservation_id,
            validator_hotkey=VALIDATOR_KEYPAIR.ss58_address,
            slot_id="longmem-0",
            now=_NOW,
        )
        await settle_confirmation_bundle_budget(
            session,
            reservation_id=reservation.reservation.reservation_id,
            expected_revision=1,
            actual_microusd=15_000,
            failed_attempt=False,
            settled_at=_NOW + timedelta(minutes=4),
        )
        await complete_confirmation_bundle(
            session,
            bundle_id=bundle.bundle_id,
            ticket_id=ticket.ticket_id,
            report=signed_report(
                bundle=bundle,
                ticket=ticket,
                mode=ConfirmationBundleMode.SHADOW,
            ),
            verification_profile=profile,
            now=_NOW + timedelta(minutes=5),
        )
        frozen_evidence = bundle.evidence_sha256
        enforce_revision, _ = await _settings(
            session,
            mode=ConfirmationBundleMode.ENFORCE,
            top_n=1,
            parent_revision=shadow_revision.revision,
        )
        enforce_result = await reconcile_confirmation_candidates(
            session, bench_version=9, verification_profiles=registry
        )
        assert enforce_result.reused_bundles == 1
        assert subject.result_status == ConfirmationResultStatus.FULL_CONFIRMED.value

    refreshed = await session.get(ConfirmationBundleSubject, (agent.agent_id, 9))
    assert refreshed is not None
    assert refreshed.result_status == ConfirmationResultStatus.FULL_CONFIRMED.value
    assert refreshed.bundle_id == bundle.bundle_id
    assert bundle.evidence_sha256 == frozen_evidence
    assert bundle.settings_revision == shadow_revision.revision
    assert enforce_revision.revision != bundle.settings_revision
    assert (
        await session.scalar(
            select(func.count()).select_from(ConfirmationBudgetReservation)
        )
        == 1
    )
