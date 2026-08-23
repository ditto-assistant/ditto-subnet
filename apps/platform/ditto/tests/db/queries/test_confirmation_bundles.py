"""Real-Postgres tests for v9 confirmation bundle persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.confirmation_bundles import (
    ConfirmationBundleMode,
    ConfirmationBundleSettings,
    ConfirmationBundleState,
    ConfirmationDimension,
    ConfirmationResultStatus,
)
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    ConfirmationBudgetDay,
    ConfirmationBudgetReservation,
    ConfirmationBundle,
    ConfirmationBundleSettingsRevision,
    ConfirmationBundleSubject,
    ConfirmationBundleTicket,
    ConfirmationDimensionEvidence,
    ConfirmationRetestAuthorization,
    ConfirmationScore,
    EvaluationPayment,
    Score,
)
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.confirmation_bundles import (
    CONFIRMATION_TICKET_TTL,
    ConfirmationBundlePersistenceError,
    StaleConfirmationBudget,
    authorize_confirmation_bundle_retest,
    complete_confirmation_bundle,
    confirmation_bundle_dimensions,
    confirmation_bundle_subjects,
    confirmation_bundle_tickets,
    get_or_create_confirmation_bundle,
    insert_confirmation_bundle_settings_revision,
    issue_confirmation_bundle_ticket,
    latest_confirmation_bundle_settings_revision,
    list_active_confirmation_work,
    list_confirmation_bundle_settings_revisions,
    record_base_only_subject,
    reserve_confirmation_bundle_budget,
    settle_confirmation_bundle_budget,
)
from ditto.db.queries.scores import (
    list_eligible_ledger,
    ranked_quorum_agent_ids,
    v9_confirmation_public_projections,
)
from ditto.tests.confirmation_evidence_fixtures import (
    ARTIFACT_SHA256,
    VALIDATOR_KEYPAIR,
    active_settings,
    base_proof_kwargs,
    signed_report,
    verification_profile,
)

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def settings(**overrides: object) -> ConfirmationBundleSettings:
    payload = active_settings().model_dump(mode="json")
    payload.update(
        {
            "daily_bundle_cap": 2,
            "daily_dollar_cap_microusd": 100_000,
            "per_bundle_request_cap": 20,
            "per_bundle_token_cap": 2_000,
        }
    )
    payload.update(overrides)
    return ConfirmationBundleSettings.model_validate_json(json.dumps(payload))


def checksum(policy: ConfirmationBundleSettings) -> str:
    encoded = json.dumps(
        policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def seed_agent(
    session: AsyncSession,
    *,
    name: str = "agent",
    artifact_sha256: str = ARTIFACT_SHA256,
) -> UUID:
    agent_id = uuid4()
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey=f"5Miner-{name}",
            name=name,
            sha256=artifact_sha256,
            status=AgentStatus.SCORED,
            screening_policy_version=SCREENING_POLICY_VERSION,
            created_at=_NOW,
        )
    )
    await session.flush()
    return agent_id


async def seed_settings(
    session: AsyncSession,
    policy: ConfirmationBundleSettings | None = None,
    *,
    parent_revision: int = 0,
) -> tuple[ConfirmationBundleSettingsRevision, ConfirmationBundleSettings]:
    resolved = policy or settings()
    row = await insert_confirmation_bundle_settings_revision(
        session,
        parent_revision=parent_revision,
        scope="*",
        settings=resolved.model_dump(mode="json"),
        checksum=checksum(resolved),
        reason="operator approved confirmation bundle settings",
        actor="operator@example.com",
    )
    return row, resolved


async def seed_bundle(
    session: AsyncSession,
    *,
    policy: ConfirmationBundleSettings | None = None,
    name: str = "agent",
    artifact_sha256: str = ARTIFACT_SHA256,
) -> tuple[
    UUID,
    ConfirmationBundleSettingsRevision,
    ConfirmationBundleSettings,
    ConfirmationBundle,
]:
    agent_id = await seed_agent(session, name=name, artifact_sha256=artifact_sha256)
    revision, resolved = await seed_settings(session, policy)
    result = await get_or_create_confirmation_bundle(
        session,
        agent_id=agent_id,
        bench_version=9,
        **base_proof_kwargs(quality_micros=750_000, stderr_micros=20_000),
        settings_revision=revision.revision,
        settings=resolved,
        verification_profile=verification_profile(),
    )
    assert result.bundle is not None
    return agent_id, revision, resolved, result.bundle


async def reserve_and_issue(
    session: AsyncSession,
    *,
    bundle: ConfirmationBundle,
    revision: ConfirmationBundleSettingsRevision,
    policy: ConfirmationBundleSettings,
    reserve_microusd: int = 50_000,
) -> tuple[ConfirmationBudgetReservation, ConfirmationBundleTicket]:
    decision = await reserve_confirmation_bundle_budget(
        session,
        bundle_id=bundle.bundle_id,
        reservation_id=uuid4(),
        now=_NOW,
        expected_revision=0,
        settings_revision=revision.revision,
        settings=policy,
        reserve_microusd=reserve_microusd,
    )
    assert decision.reservation is not None
    ticket = await issue_confirmation_bundle_ticket(
        session,
        bundle_id=bundle.bundle_id,
        reservation_id=decision.reservation.reservation_id,
        validator_hotkey=VALIDATOR_KEYPAIR.ss58_address,
        slot_id="longmem-0",
        now=_NOW,
    )
    return decision.reservation, ticket


class TestActiveConfirmationWork:
    async def test_live_ticket_reports_all_subjects_without_ordinary_slot_state(
        self, session: AsyncSession
    ) -> None:
        policy = settings(mode="shadow")
        async with session.begin():
            first_id, revision, policy, bundle = await seed_bundle(
                session, policy=policy, name="first"
            )
            second_id = await seed_agent(session, name="second")
            second = await get_or_create_confirmation_bundle(
                session,
                agent_id=second_id,
                bench_version=9,
                **base_proof_kwargs(quality_micros=800_000, stderr_micros=30_000),
                settings_revision=revision.revision,
                settings=policy,
                verification_profile=verification_profile(),
            )
            assert second.bundle is not None
            assert second.bundle.bundle_id == bundle.bundle_id
            _, ticket = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )

        active = await list_active_confirmation_work(
            session, now=_NOW + timedelta(minutes=1)
        )
        assert len(active) == 1
        [work] = active
        assert work.ticket.ticket_id == ticket.ticket_id
        assert work.ticket.slot_id == "longmem-0"
        assert work.mode == ConfirmationBundleMode.SHADOW
        assert {(item.agent_id, item.agent_name) for item in work.subjects} == {
            (first_id, "first"),
            (second_id, "second"),
        }

        assert (
            await list_active_confirmation_work(
                session, now=ticket.deadline + timedelta(seconds=1)
            )
            == []
        )


class TestSettingsLedger:
    async def test_empty_settings_ledger(self, session: AsyncSession) -> None:
        assert await latest_confirmation_bundle_settings_revision(session) is None
        assert await list_confirmation_bundle_settings_revisions(session) == []

    async def test_latest_and_history_are_append_only(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            first, _ = await seed_settings(session)
            second, _ = await seed_settings(
                session,
                settings(mode="enforce"),
                parent_revision=first.revision,
            )
        latest = await latest_confirmation_bundle_settings_revision(session)
        history = await list_confirmation_bundle_settings_revisions(session)
        assert latest is not None and latest.revision == second.revision
        assert [row.revision for row in history] == [second.revision, first.revision]
        assert history[1].settings["mode"] == "shadow"

    async def test_duplicate_parent_is_database_rejected(
        self, session: AsyncSession
    ) -> None:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await seed_settings(session)
                await seed_settings(session, settings(mode="enforce"))

    @pytest.mark.parametrize(
        ("field", "value"),
        [("scope", "tenant"), ("checksum", "short"), ("reason", "tiny")],
    )
    async def test_settings_constraints_reject_malformed_audit_rows(
        self, session: AsyncSession, field: str, value: object
    ) -> None:
        policy = settings()
        values = {
            "parent_revision": 0,
            "scope": "*",
            "settings": policy.model_dump(mode="json"),
            "checksum": checksum(policy),
            "reason": "operator approved confirmation settings",
            "actor": "operator@example.com",
        }
        values[field] = value
        with pytest.raises(IntegrityError):
            async with session.begin():
                session.add(ConfirmationBundleSettingsRevision(**values))


class TestBundleResolution:
    async def test_off_policy_records_only_base_state(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_id = await seed_agent(session)
            result = await get_or_create_confirmation_bundle(
                session,
                agent_id=agent_id,
                bench_version=9,
                **base_proof_kwargs(quality_micros=700_000, stderr_micros=10_000),
                settings_revision=0,
                settings=ConfirmationBundleSettings(),
            )
        assert result.bundle is None
        assert result.subject.result_status == ConfirmationResultStatus.BASE_ONLY.value
        assert result.subject.bundle_id is None
        assert (
            await session.scalar(select(func.count()).select_from(ConfirmationBundle))
            == 0
        )

    async def test_base_only_update_never_downgrades_confirmed_evidence(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_id = await seed_agent(session)
            subject = ConfirmationBundleSubject(
                agent_id=agent_id,
                bench_version=9,
                artifact_sha256="b" * 64,
                bundle_id=None,
                result_status=ConfirmationResultStatus.BASE_ONLY.value,
                **base_proof_kwargs(quality_micros=700_000, stderr_micros=20_000),
            )
            session.add(subject)
        # A base-only row can be refreshed without minting a bundle.
        async with session.begin():
            refreshed = await record_base_only_subject(
                session,
                agent_id=agent_id,
                bench_version=9,
                **base_proof_kwargs(quality_micros=750_000, stderr_micros=20_000),
            )
        assert refreshed.base_quality_micros == 750_000
        assert refreshed.result_status == "base_only"

    async def test_active_policy_creates_one_provisional_bundle(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_id, revision, policy, bundle = await seed_bundle(session)
        assert bundle.retest_generation == 0
        assert bundle.state == ConfirmationBundleState.PENDING.value
        assert bundle.settings_revision == revision.revision
        subject = await session.get(ConfirmationBundleSubject, (agent_id, 9))
        assert subject is not None
        assert subject.bundle_id == bundle.bundle_id
        assert subject.result_status == ConfirmationResultStatus.PROVISIONAL.value
        assert policy.profile_checksum == bundle.profile_checksum

    async def test_same_agent_reuses_exact_pending_bundle(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_id, revision, policy, first = await seed_bundle(session)
        async with session.begin():
            second = await get_or_create_confirmation_bundle(
                session,
                agent_id=agent_id,
                bench_version=9,
                **base_proof_kwargs(quality_micros=750_000, stderr_micros=20_000),
                settings_revision=revision.revision,
                settings=policy,
                verification_profile=verification_profile(),
            )
        assert second.bundle is not None
        assert second.bundle.bundle_id == first.bundle_id
        assert not second.reused_completed_evidence
        assert (
            await session.scalar(select(func.count()).select_from(ConfirmationBundle))
            == 1
        )

    async def test_renamed_same_digest_reuses_exact_bundle(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, revision, policy, first = await seed_bundle(
                session, name="original", artifact_sha256="f" * 64
            )
            renamed_id = await seed_agent(
                session, name="renamed", artifact_sha256="f" * 64
            )
        async with session.begin():
            reused = await get_or_create_confirmation_bundle(
                session,
                agent_id=renamed_id,
                bench_version=9,
                **base_proof_kwargs(quality_micros=770_000, stderr_micros=10_000),
                settings_revision=revision.revision,
                settings=policy,
                verification_profile=verification_profile(),
            )
        assert reused.bundle is not None
        assert reused.bundle.bundle_id == first.bundle_id
        assert (
            len(await confirmation_bundle_subjects(session, bundle_id=first.bundle_id))
            == 2
        )

    async def test_changed_digest_does_not_reuse(self, session: AsyncSession) -> None:
        async with session.begin():
            _, revision, policy, first = await seed_bundle(
                session, name="one", artifact_sha256="1" * 64
            )
            other_id = await seed_agent(session, name="two", artifact_sha256="2" * 64)
        async with session.begin():
            second = await get_or_create_confirmation_bundle(
                session,
                agent_id=other_id,
                bench_version=9,
                **base_proof_kwargs(quality_micros=800_000, stderr_micros=10_000),
                settings_revision=revision.revision,
                settings=policy,
                verification_profile=verification_profile(),
            )
        assert second.bundle is not None
        assert second.bundle.bundle_id != first.bundle_id

    async def test_changed_profile_does_not_reuse(self, session: AsyncSession) -> None:
        async with session.begin():
            agent_id, _, _, first = await seed_bundle(session)
            changed_profile = replace(
                verification_profile(), revision="confirmation-v9-test-2"
            )
            changed = settings(
                profile_revision=changed_profile.revision,
                profile_checksum=changed_profile.checksum(),
            )
            revision, changed = await seed_settings(session, changed, parent_revision=1)
        async with session.begin():
            second = await get_or_create_confirmation_bundle(
                session,
                agent_id=agent_id,
                bench_version=9,
                **base_proof_kwargs(quality_micros=750_000, stderr_micros=20_000),
                settings_revision=revision.revision,
                settings=changed,
                verification_profile=changed_profile,
            )
        assert second.bundle is not None
        assert second.bundle.bundle_id != first.bundle_id

    async def test_pre_contract_bundle_is_rejected_before_write(
        self, session: AsyncSession
    ) -> None:
        """Only versions below the base-evidence contract are refused.

        Versions at or above it are accepted so the lane can follow the live
        benchmark; it used to accept bench 9 alone, which stranded it the
        moment a later epoch was activated.
        """
        async with session.begin():
            agent_id = await seed_agent(session)
            revision, policy = await seed_settings(session)
            with pytest.raises(
                ConfirmationBundlePersistenceError, match="carrying base evidence"
            ):
                await get_or_create_confirmation_bundle(
                    session,
                    agent_id=agent_id,
                    bench_version=8,
                    **base_proof_kwargs(quality_micros=800_000, stderr_micros=10_000),
                    settings_revision=revision.revision,
                    settings=policy,
                    verification_profile=verification_profile(),
                )

    @pytest.mark.parametrize("live_version", [10, 11])
    async def test_bundle_is_written_for_a_later_live_benchmark(
        self, session: AsyncSession, live_version: int
    ) -> None:
        async with session.begin():
            agent_id = await seed_agent(session)
            revision, policy = await seed_settings(session)
            resolution = await get_or_create_confirmation_bundle(
                session,
                agent_id=agent_id,
                bench_version=live_version,
                **base_proof_kwargs(quality_micros=800_000, stderr_micros=10_000),
                settings_revision=revision.revision,
                settings=policy,
                verification_profile=verification_profile(),
            )
        assert resolution.bundle is not None
        assert resolution.bundle.bench_version == live_version
        assert resolution.subject.bench_version == live_version

    async def test_supplied_settings_must_match_frozen_revision(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_id = await seed_agent(session)
            revision, _ = await seed_settings(session)
            with pytest.raises(
                ConfirmationBundlePersistenceError, match="frozen policy"
            ):
                await get_or_create_confirmation_bundle(
                    session,
                    agent_id=agent_id,
                    bench_version=9,
                    **base_proof_kwargs(quality_micros=800_000, stderr_micros=10_000),
                    settings_revision=revision.revision,
                    settings=settings(top_n=6),
                    verification_profile=verification_profile(),
                )


class TestBudgetAndTicketLifecycle:
    async def test_reservation_charges_exact_integer_budget(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            decision = await reserve_confirmation_bundle_budget(
                session,
                bundle_id=bundle.bundle_id,
                reservation_id=uuid4(),
                now=_NOW,
                expected_revision=0,
                settings_revision=revision.revision,
                settings=policy,
                reserve_microusd=40_000,
            )
        assert decision.blocked_reason is None
        assert decision.budget.revision == 1
        assert decision.budget.issued_attempts == 1
        assert decision.budget.outstanding_reserved_microusd == 40_000
        assert decision.reservation is not None
        assert decision.reservation.attempt == 1

    @pytest.mark.parametrize("amount", [0, -1])
    async def test_reservation_rejects_nonpositive_amount(
        self, session: AsyncSession, amount: int
    ) -> None:
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            with pytest.raises(ConfirmationBundlePersistenceError, match="positive"):
                await reserve_confirmation_bundle_budget(
                    session,
                    bundle_id=bundle.bundle_id,
                    reservation_id=uuid4(),
                    now=_NOW,
                    expected_revision=0,
                    settings_revision=revision.revision,
                    settings=policy,
                    reserve_microusd=amount,
                )

    async def test_stale_budget_revision_is_rejected(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            await reserve_confirmation_bundle_budget(
                session,
                bundle_id=bundle.bundle_id,
                reservation_id=uuid4(),
                now=_NOW,
                expected_revision=0,
                settings_revision=revision.revision,
                settings=policy,
                reserve_microusd=40_000,
            )
        async with session.begin():
            with pytest.raises(StaleConfirmationBudget, match="found 1"):
                await reserve_confirmation_bundle_budget(
                    session,
                    bundle_id=bundle.bundle_id,
                    reservation_id=uuid4(),
                    now=_NOW,
                    expected_revision=0,
                    settings_revision=revision.revision,
                    settings=policy,
                    reserve_microusd=40_000,
                )

    async def test_bundle_cap_pauses_new_issuance_visibly(
        self, session: AsyncSession
    ) -> None:
        policy = settings(daily_bundle_cap=1)
        async with session.begin():
            _, revision, policy, first = await seed_bundle(
                session, policy=policy, name="one", artifact_sha256="1" * 64
            )
            second_id = await seed_agent(session, name="two", artifact_sha256="2" * 64)
            second_resolution = await get_or_create_confirmation_bundle(
                session,
                agent_id=second_id,
                bench_version=9,
                **base_proof_kwargs(quality_micros=700_000, stderr_micros=20_000),
                settings_revision=revision.revision,
                settings=policy,
                verification_profile=verification_profile(),
            )
            assert second_resolution.bundle is not None
            await reserve_confirmation_bundle_budget(
                session,
                bundle_id=first.bundle_id,
                reservation_id=uuid4(),
                now=_NOW,
                expected_revision=0,
                settings_revision=revision.revision,
                settings=policy,
                reserve_microusd=40_000,
            )
        async with session.begin():
            blocked = await reserve_confirmation_bundle_budget(
                session,
                bundle_id=second_resolution.bundle.bundle_id,
                reservation_id=uuid4(),
                now=_NOW,
                expected_revision=1,
                settings_revision=revision.revision,
                settings=policy,
                reserve_microusd=40_000,
            )
        assert blocked.reservation is None
        assert blocked.blocked_reason == "bundle_cap"
        assert (
            second_resolution.bundle.state
            == ConfirmationBundleState.BLOCKED_BUDGET.value
        )

    async def test_dollar_cap_counts_reserved_plus_settled(
        self, session: AsyncSession
    ) -> None:
        policy = settings(daily_dollar_cap_microusd=70_000)
        async with session.begin():
            _, revision, policy, first = await seed_bundle(
                session, policy=policy, name="one", artifact_sha256="1" * 64
            )
            other_id = await seed_agent(session, name="two", artifact_sha256="2" * 64)
            other = await get_or_create_confirmation_bundle(
                session,
                agent_id=other_id,
                bench_version=9,
                **base_proof_kwargs(quality_micros=700_000, stderr_micros=20_000),
                settings_revision=revision.revision,
                settings=policy,
                verification_profile=verification_profile(),
            )
            assert other.bundle is not None
            await reserve_confirmation_bundle_budget(
                session,
                bundle_id=first.bundle_id,
                reservation_id=uuid4(),
                now=_NOW,
                expected_revision=0,
                settings_revision=revision.revision,
                settings=policy,
                reserve_microusd=40_000,
            )
            blocked = await reserve_confirmation_bundle_budget(
                session,
                bundle_id=other.bundle.bundle_id,
                reservation_id=uuid4(),
                now=_NOW,
                expected_revision=1,
                settings_revision=revision.revision,
                settings=policy,
                reserve_microusd=40_000,
            )
        assert blocked.blocked_reason == "dollar_cap"
        assert blocked.budget.outstanding_reserved_microusd == 40_000

    async def test_reservation_id_replay_is_idempotent(
        self, session: AsyncSession
    ) -> None:
        request_id = uuid4()
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            first = await reserve_confirmation_bundle_budget(
                session,
                bundle_id=bundle.bundle_id,
                reservation_id=request_id,
                now=_NOW,
                expected_revision=0,
                settings_revision=revision.revision,
                settings=policy,
                reserve_microusd=40_000,
            )
            replay = await reserve_confirmation_bundle_budget(
                session,
                bundle_id=bundle.bundle_id,
                reservation_id=request_id,
                now=_NOW,
                expected_revision=1,
                settings_revision=revision.revision,
                settings=policy,
                reserve_microusd=40_000,
            )
        assert first.reservation is not None
        assert replay.reservation is not None
        assert replay.reservation.reservation_id == request_id
        assert replay.replayed
        assert replay.budget.issued_attempts == 1

    async def test_ticket_uses_exact_current_lifecycle_ttl(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            reservation, ticket = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )
        assert ticket.status == "issued"
        assert ticket.deadline - ticket.issued_at == CONFIRMATION_TICKET_TTL
        assert ticket.attempt == reservation.attempt == 1
        assert bundle.state == ConfirmationBundleState.LEASED.value

    async def test_ticket_refuses_noncanonical_ttl(self, session: AsyncSession) -> None:
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            decision = await reserve_confirmation_bundle_budget(
                session,
                bundle_id=bundle.bundle_id,
                reservation_id=uuid4(),
                now=_NOW,
                expected_revision=0,
                settings_revision=revision.revision,
                settings=policy,
                reserve_microusd=40_000,
            )
            assert decision.reservation is not None
            with pytest.raises(ConfirmationBundlePersistenceError, match="90 minutes"):
                await issue_confirmation_bundle_ticket(
                    session,
                    bundle_id=bundle.bundle_id,
                    reservation_id=decision.reservation.reservation_id,
                    validator_hotkey="5Validator",
                    slot_id="longmem-0",
                    now=_NOW,
                    ttl=timedelta(minutes=89),
                )

    async def test_successful_settlement_is_idempotent(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            reservation, _ = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )
            first = await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
                expected_revision=1,
                actual_microusd=40_000,
                failed_attempt=False,
                settled_at=_NOW + timedelta(minutes=10),
            )
            replay = await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
                expected_revision=2,
                actual_microusd=40_000,
                failed_attempt=False,
                settled_at=_NOW + timedelta(minutes=11),
            )
        assert not first.replayed
        assert replay.replayed
        assert replay.budget.settled_microusd == 40_000
        assert replay.budget.outstanding_reserved_microusd == 0

    async def test_settlement_over_cap_is_accepted_and_blocks_future_work(
        self, session: AsyncSession
    ) -> None:
        policy = settings(daily_dollar_cap_microusd=50_000)
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session, policy=policy)
            reservation, _ = await reserve_and_issue(
                session,
                bundle=bundle,
                revision=revision,
                policy=policy,
                reserve_microusd=40_000,
            )
            settled = await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
                expected_revision=1,
                actual_microusd=60_000,
                failed_attempt=False,
                settled_at=_NOW + timedelta(minutes=10),
            )
        assert settled.budget.settled_microusd == 60_000

    async def test_failed_attempt_is_charged_and_remains_auditable(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            reservation, ticket = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )
            settled = await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
                expected_revision=1,
                actual_microusd=12_345,
                failed_attempt=True,
                settled_at=_NOW + timedelta(minutes=10),
            )
        assert settled.reservation.failed_attempt is True
        assert bundle.state == ConfirmationBundleState.FAILED.value
        assert ticket.status == "expired"
        assert ticket.failure_reason == "confirmation_failed"
        assert settled.budget.issued_attempts == 1
        assert settled.budget.settled_microusd == 12_345

    async def test_utc_rollover_uses_independent_budget_rows(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, revision, policy, first = await seed_bundle(
                session, name="one", artifact_sha256="1" * 64
            )
            other_id = await seed_agent(session, name="two", artifact_sha256="2" * 64)
            other = await get_or_create_confirmation_bundle(
                session,
                agent_id=other_id,
                bench_version=9,
                **base_proof_kwargs(quality_micros=700_000, stderr_micros=20_000),
                settings_revision=revision.revision,
                settings=policy,
                verification_profile=verification_profile(),
            )
            assert other.bundle is not None
            yesterday = await reserve_confirmation_bundle_budget(
                session,
                bundle_id=first.bundle_id,
                reservation_id=uuid4(),
                now=datetime(2026, 8, 8, 23, 59, tzinfo=UTC),
                expected_revision=0,
                settings_revision=revision.revision,
                settings=policy,
                reserve_microusd=40_000,
            )
            today = await reserve_confirmation_bundle_budget(
                session,
                bundle_id=other.bundle.bundle_id,
                reservation_id=uuid4(),
                now=datetime(2026, 8, 9, 0, 1, tzinfo=UTC),
                expected_revision=0,
                settings_revision=revision.revision,
                settings=policy,
                reserve_microusd=40_000,
            )
        assert yesterday.budget.utc_day.isoformat() == "2026-08-08"
        assert today.budget.utc_day.isoformat() == "2026-08-09"
        assert today.budget.revision == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(ConfirmationBudgetDay)
            )
            == 2
        )


class TestCompletionAndRetest:
    async def completed_bundle(
        self,
        session: AsyncSession,
        *,
        mode: ConfirmationBundleMode = ConfirmationBundleMode.SHADOW,
        inference_status: Literal[
            "passed", "failed", "unavailable", "not_run"
        ] = "passed",
        embedding_status: Literal[
            "passed", "failed", "unavailable", "not_run"
        ] = "passed",
        observational_drop: bool = False,
        actual_cost: int = 15_000,
    ) -> tuple[
        UUID,
        ConfirmationBundleSettingsRevision,
        ConfirmationBundleSettings,
        ConfirmationBundle,
        ConfirmationBundleTicket,
    ]:
        policy = settings(mode=mode.value)
        agent_id, revision, policy, bundle = await seed_bundle(session, policy=policy)
        reservation, ticket = await reserve_and_issue(
            session, bundle=bundle, revision=revision, policy=policy
        )
        await settle_confirmation_bundle_budget(
            session,
            reservation_id=reservation.reservation_id,
            expected_revision=1,
            actual_microusd=actual_cost,
            failed_attempt=False,
            settled_at=_NOW + timedelta(minutes=4),
        )
        if actual_cost == 15_000:
            await complete_confirmation_bundle(
                session,
                bundle_id=bundle.bundle_id,
                ticket_id=ticket.ticket_id,
                report=signed_report(
                    bundle=bundle,
                    ticket=ticket,
                    mode=mode,
                    inference_status=inference_status,
                    embedding_status=embedding_status,
                    observational_drop=observational_drop,
                ),
                verification_profile=verification_profile(),
                now=_NOW + timedelta(minutes=5),
            )
        return agent_id, revision, policy, bundle, ticket

    async def test_shadow_persists_one_shared_root_but_never_confirms_subject(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_id, _, _, bundle, ticket = await self.completed_bundle(session)
        assert bundle.state == ConfirmationBundleState.COMPLETED.value
        assert bundle.qualification_status == "qualified"
        assert bundle.completion_mode == "shadow"
        assert bundle.evidence_sha256 is not None
        assert bundle.bundle_signature is not None
        assert bundle.completion_ticket_id == ticket.ticket_id
        assert not hasattr(bundle, "full_composite")
        subject = await session.get(ConfirmationBundleSubject, (agent_id, 9))
        assert subject is not None
        assert subject.result_status == ConfirmationResultStatus.PROVISIONAL.value
        assert subject.full_quality_micros == 650_000
        assert subject.full_effective_micros == 650_000
        assert subject.applied_factor_bps == 10_000
        stored = await confirmation_bundle_dimensions(
            session, bundle_id=bundle.bundle_id
        )
        assert {row.dimension for row in stored} == {
            dimension.value for dimension in ConfirmationDimension
        }
        assert sum(row.provider_cost_microusd for row in stored) == 15_000
        assert ticket.status == "scored"

    async def test_enforce_computes_subject_authority_server_side(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_id, _, _, _, _ = await self.completed_bundle(
                session, mode=ConfirmationBundleMode.ENFORCE
            )
        subject = await session.get(ConfirmationBundleSubject, (agent_id, 9))
        assert subject is not None
        assert subject.result_status == "full_confirmed"
        assert subject.full_quality_micros == 650_000
        assert subject.full_effective_micros == 650_000
        assert subject.semantic_factor_bps == 10_000

    async def test_two_subjects_share_evidence_but_not_full_quality(
        self, session: AsyncSession
    ) -> None:
        policy = settings(mode="enforce")
        async with session.begin():
            first_id, revision, policy, bundle = await seed_bundle(
                session, policy=policy
            )
            second_id = await seed_agent(session, name="rename")
            second = await get_or_create_confirmation_bundle(
                session,
                agent_id=second_id,
                bench_version=9,
                **base_proof_kwargs(quality_micros=900_000, stderr_micros=80_000),
                settings_revision=revision.revision,
                settings=policy,
                verification_profile=verification_profile(),
            )
            assert second.bundle is not None
            assert second.bundle.bundle_id == bundle.bundle_id
            reservation, ticket = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )
            await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
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
                    mode=ConfirmationBundleMode.ENFORCE,
                ),
                verification_profile=verification_profile(),
                now=_NOW + timedelta(minutes=5),
            )
        first = await session.get(ConfirmationBundleSubject, (first_id, 9))
        second_subject = await session.get(ConfirmationBundleSubject, (second_id, 9))
        assert first is not None and second_subject is not None
        assert first.full_quality_micros == 650_000
        assert second_subject.full_quality_micros == 740_000
        assert first.full_stderr_micros != second_subject.full_stderr_micros
        assert first.bundle_id == second_subject.bundle_id == bundle.bundle_id

    async def test_unavailable_ablation_completes_shared_audit_but_not_subject(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_id, _, _, bundle, _ = await self.completed_bundle(
                session,
                mode=ConfirmationBundleMode.ENFORCE,
                embedding_status="unavailable",
            )
        assert bundle.state == "completed"
        assert bundle.qualification_status == "unqualified"
        subject = await session.get(ConfirmationBundleSubject, (agent_id, 9))
        assert subject is not None
        assert subject.result_status == "provisional"
        assert subject.full_quality_micros is None
        assert subject.full_effective_micros is None

    async def test_failed_enforce_ablation_is_complete_and_keeps_the_mix(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_id, _, _, bundle, _ = await self.completed_bundle(
                session,
                mode=ConfirmationBundleMode.ENFORCE,
                inference_status="failed",
            )
        assert bundle.qualification_status == "qualified"
        subject = await session.get(ConfirmationBundleSubject, (agent_id, 9))
        assert subject is not None
        assert subject.result_status == "full_confirmed"
        assert subject.full_quality_micros == 650_000
        assert subject.semantic_factor_bps == 0
        assert subject.applied_factor_bps == 10_000
        assert subject.full_effective_micros == 650_000

    async def test_enforce_observational_drop_is_complete_and_keeps_the_mix(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_id, _, _, bundle, _ = await self.completed_bundle(
                session,
                mode=ConfirmationBundleMode.ENFORCE,
                inference_status="failed",
                embedding_status="failed",
                observational_drop=True,
            )
        assert bundle.qualification_status == "qualified"
        subject = await session.get(ConfirmationBundleSubject, (agent_id, 9))
        assert subject is not None
        assert subject.result_status == "full_confirmed"
        assert subject.full_quality_micros == 650_000
        assert subject.semantic_factor_bps == 0
        assert subject.applied_factor_bps == 10_000
        assert subject.full_effective_micros == 650_000
        stored = await confirmation_bundle_dimensions(
            session, bundle_id=bundle.bundle_id
        )
        reasons = {
            row.dimension: row.evidence["reason"]
            for row in stored
            if row.dimension in {"inference_ablation", "embedding_ablation"}
        }
        assert reasons == {
            "inference_ablation": "observational_drop_not_causal",
            "embedding_ablation": "observational_drop_not_causal",
        }

    async def test_completion_rejects_cost_mismatch(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            reservation, ticket = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )
            await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
                expected_revision=1,
                actual_microusd=14_999,
                failed_attempt=False,
                settled_at=_NOW + timedelta(minutes=4),
            )
            with pytest.raises(ConfirmationBundlePersistenceError, match="cost"):
                await complete_confirmation_bundle(
                    session,
                    bundle_id=bundle.bundle_id,
                    ticket_id=ticket.ticket_id,
                    report=signed_report(
                        bundle=bundle,
                        ticket=ticket,
                        mode=ConfirmationBundleMode.SHADOW,
                    ),
                    verification_profile=verification_profile(),
                    now=_NOW + timedelta(minutes=5),
                )

    async def test_forged_bundle_signature_is_rejected_before_persistence(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            reservation, ticket = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )
            await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
                expected_revision=1,
                actual_microusd=15_000,
                failed_attempt=False,
                settled_at=_NOW + timedelta(minutes=4),
            )
            forged = signed_report(
                bundle=bundle,
                ticket=ticket,
                mode=ConfirmationBundleMode.SHADOW,
            ).model_copy(update={"bundle_signature": "00"})
            with pytest.raises(ConfirmationBundlePersistenceError, match="signature"):
                await complete_confirmation_bundle(
                    session,
                    bundle_id=bundle.bundle_id,
                    ticket_id=ticket.ticket_id,
                    report=forged,
                    verification_profile=verification_profile(),
                    now=_NOW + timedelta(minutes=5),
                )

    async def test_exact_signed_replay_is_idempotent(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            reservation, ticket = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )
            await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
                expected_revision=1,
                actual_microusd=15_000,
                failed_attempt=False,
                settled_at=_NOW + timedelta(minutes=4),
            )
            report = signed_report(
                bundle=bundle,
                ticket=ticket,
                mode=ConfirmationBundleMode.SHADOW,
            )
            first = await complete_confirmation_bundle(
                session,
                bundle_id=bundle.bundle_id,
                ticket_id=ticket.ticket_id,
                report=report,
                verification_profile=verification_profile(),
                now=_NOW + timedelta(minutes=5),
            )
            replay = await complete_confirmation_bundle(
                session,
                bundle_id=bundle.bundle_id,
                ticket_id=ticket.ticket_id,
                report=report,
                verification_profile=verification_profile(),
                now=_NOW + timedelta(minutes=6),
            )
        assert replay.bundle_id == first.bundle_id
        assert (
            await session.scalar(
                select(func.count()).select_from(ConfirmationDimensionEvidence)
            )
            == 3
        )

    async def test_completion_uses_frozen_mode_after_latest_policy_turns_off(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            reservation, ticket = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )
            await seed_settings(
                session, ConfirmationBundleSettings(), parent_revision=revision.revision
            )
            await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
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
                verification_profile=verification_profile(),
                now=_NOW + timedelta(minutes=5),
            )
        assert bundle.state == "completed"
        assert bundle.completion_mode == "shadow"

    async def test_retest_requires_completed_evidence(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, _, _, bundle = await seed_bundle(session)
            with pytest.raises(ConfirmationBundlePersistenceError, match="completed"):
                await authorize_confirmation_bundle_retest(
                    session,
                    source_bundle_id=bundle.bundle_id,
                    authorization_id=uuid4(),
                    expected_generation=0,
                    actor="operator@example.com",
                    reason="operator approved a fresh confirmation run",
                )

    async def test_authorized_retest_creates_exact_next_generation(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_id, revision, policy, bundle = await seed_bundle(session)
            reservation, ticket = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )
            await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
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
                verification_profile=verification_profile(),
                now=_NOW + timedelta(minutes=5),
            )
            request_id = uuid4()
            result = await authorize_confirmation_bundle_retest(
                session,
                source_bundle_id=bundle.bundle_id,
                authorization_id=request_id,
                expected_generation=0,
                actor="operator@example.com",
                reason="operator approved a fresh confirmation run",
            )
        assert result.bundle.retest_generation == 1
        assert result.bundle.retest_authorization_id == request_id
        assert result.superseded_bundle.state == "superseded"
        assert not result.replayed
        subject = await session.get(ConfirmationBundleSubject, (agent_id, 9))
        assert subject is not None
        assert subject.bundle_id == result.bundle.bundle_id
        assert subject.result_status == "provisional"
        authorization = await session.get(ConfirmationRetestAuthorization, request_id)
        assert authorization is not None
        assert authorization.from_generation == 0
        assert authorization.authorized_generation == 1


class TestDatabaseBackstops:
    async def test_bundle_identity_is_unique(self, session: AsyncSession) -> None:
        with pytest.raises(IntegrityError):
            async with session.begin():
                _, revision, _, bundle = await seed_bundle(session)
                session.add(
                    ConfirmationBundle(
                        artifact_sha256=bundle.artifact_sha256,
                        bench_version=9,
                        profile_revision=bundle.profile_revision,
                        profile_checksum=bundle.profile_checksum,
                        retest_generation=0,
                        settings_revision=revision.revision,
                        settings_checksum=bundle.settings_checksum,
                        state="pending",
                    )
                )

    async def test_bundle_identity_fields_cannot_be_updated(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, _, _, bundle = await seed_bundle(session)
        with pytest.raises(DBAPIError, match="identity is immutable"):
            async with session.begin():
                await session.execute(
                    update(ConfirmationBundle)
                    .where(ConfirmationBundle.bundle_id == bundle.bundle_id)
                    .values(artifact_sha256="9" * 64)
                )

    async def test_dimension_evidence_cannot_be_updated(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            reservation, ticket = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )
            await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
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
                verification_profile=verification_profile(),
                now=_NOW + timedelta(minutes=5),
            )
        with pytest.raises(DBAPIError, match="append-only"):
            async with session.begin():
                await session.execute(
                    update(ConfirmationDimensionEvidence)
                    .where(ConfirmationDimensionEvidence.bundle_id == bundle.bundle_id)
                    .values(provider_cost_microusd=0)
                )

    async def test_dimension_evidence_cannot_be_deleted(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            reservation, ticket = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )
            await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
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
                verification_profile=verification_profile(),
                now=_NOW + timedelta(minutes=5),
            )
        with pytest.raises(DBAPIError, match="append-only"):
            async with session.begin():
                await session.execute(
                    delete(ConfirmationDimensionEvidence).where(
                        ConfirmationDimensionEvidence.bundle_id == bundle.bundle_id
                    )
                )

    @pytest.mark.parametrize(
        ("dimension", "synthetic", "cost"),
        [
            ("longmemeval", True, 0),
            ("inference_ablation", False, 0),
            ("embedding_ablation", True, 1),
        ],
    )
    async def test_synthetic_cost_constraints_are_database_enforced(
        self,
        session: AsyncSession,
        dimension: str,
        synthetic: bool,
        cost: int,
    ) -> None:
        with pytest.raises(IntegrityError):
            async with session.begin():
                _, _, _, bundle = await seed_bundle(session)
                session.add(
                    ConfirmationDimensionEvidence(
                        bundle_id=bundle.bundle_id,
                        dimension=dimension,
                        status="completed",
                        evidence_sha256="a" * 64,
                        request_count=0,
                        input_tokens=0,
                        output_tokens=0,
                        provider_cost_microusd=cost,
                        latency_ms=0,
                        synthetic=synthetic,
                        evidence={},
                    )
                )

    async def test_query_projection_order_is_stable(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            _, revision, policy, bundle = await seed_bundle(session)
            reservation, ticket = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )
            await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
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
                verification_profile=verification_profile(),
                now=_NOW + timedelta(minutes=5),
            )
        assert [
            row.attempt
            for row in await confirmation_bundle_tickets(
                session, bundle_id=bundle.bundle_id
            )
        ] == [1]
        assert [
            row.dimension
            for row in await confirmation_bundle_dimensions(
                session, bundle_id=bundle.bundle_id
            )
        ] == sorted(d.value for d in ConfirmationDimension)


class TestCanonicalPathIsolation:
    async def test_base_only_subject_cannot_mutate_canonical_state(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            before_version = await active_bench_version(session)
        async with session.begin():
            agent_id = await seed_agent(session)
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            original_status = agent.status
            await record_base_only_subject(
                session,
                agent_id=agent_id,
                bench_version=9,
                **base_proof_kwargs(quality_micros=800_000, stderr_micros=10_000),
            )
        refreshed = await session.get(Agent, agent_id)
        assert refreshed is not None
        assert refreshed.status == original_status == AgentStatus.SCORED
        assert await session.scalar(select(func.count()).select_from(Score)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(ConfirmationScore))
            == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(BenchmarkRollout))
            == 0
        )
        assert await active_bench_version(session) == before_version
        assert await list_eligible_ledger(session, include_details=False) == []


class TestV9RewardProjection:
    async def test_enforce_excludes_unconfirmed_owner_sibling_and_ranks_full_score(
        self, session: AsyncSession
    ) -> None:
        """Real Postgres locks the reward cut before owner-family reduction."""

        policy = settings(mode="enforce")
        async with session.begin():
            confirmed_id, revision, policy, bundle = await seed_bundle(
                session,
                policy=policy,
                name="confirmed",
                artifact_sha256=ARTIFACT_SHA256,
            )
            reservation, ticket = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )
            await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
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
                    mode=ConfirmationBundleMode.ENFORCE,
                ),
                verification_profile=verification_profile(),
                now=_NOW + timedelta(minutes=5),
            )
            unconfirmed_id = await seed_agent(
                session, name="unconfirmed", artifact_sha256="9" * 64
            )
            owner = "5OwnerColdkey" + "X" * 32
            for index, agent_id in enumerate((confirmed_id, unconfirmed_id)):
                agent = await session.get(Agent, agent_id)
                assert agent is not None
                session.add(
                    EvaluationPayment(
                        block_hash=f"0x{index:064x}",
                        extrinsic_index=index,
                        agent_id=agent_id,
                        miner_hotkey=agent.miner_hotkey,
                        miner_coldkey=owner,
                        amount_rao=1,
                        dest_address="5Destination",
                        timestamp=_NOW,
                    )
                )
                raw = 0.75 if agent_id == confirmed_id else 0.99
                for validator_index in range(3):
                    session.add(
                        Score(
                            agent_id=agent_id,
                            validator_hotkey=f"5Validator{validator_index}",
                            bench_version=9,
                            run_id=f"run-{agent_id}-{validator_index}",
                            signature="ab" * 64,
                            seed=validator_index,
                            composite=raw,
                            tool_mean=raw,
                            memory_mean=raw,
                            median_ms=500,
                            n=114,
                            generated_at=_NOW,
                        )
                    )

        rows = await list_eligible_ledger(
            session,
            bench_version=9,
            include_fingerprints=False,
            include_details=False,
        )
        assert [row.agent_id for row in rows] == [confirmed_id]
        assert rows[0].composite == pytest.approx(0.75)
        # Frozen test policy is 60% base quality (0.75) + 40% LongMem (0.50).
        assert rows[0].official_composite == pytest.approx(0.65)
        assert rows[0].v9_confirmation is not None
        assert rows[0].v9_confirmation["full_effective_micros"] == 650_000
        public = await v9_confirmation_public_projections(
            session, agent_ids=[confirmed_id, unconfirmed_id]
        )
        assert public[confirmed_id].result_status == "full_confirmed"
        assert public[confirmed_id].full_confirmed_composite == pytest.approx(0.65)
        assert public[confirmed_id].receipt is not None
        # No subject means the endpoint maps this v9 score to explicit base_only.
        assert unconfirmed_id not in public

    async def test_enforce_reuses_qualified_shadow_evidence_for_every_reward_read(
        self, session: AsyncSession
    ) -> None:
        """Promotion reuses matching immutable evidence and rejects profile drift."""

        async with session.begin():
            agent_id, shadow_revision, shadow, bundle = await seed_bundle(session)
            reservation, ticket = await reserve_and_issue(
                session,
                bundle=bundle,
                revision=shadow_revision,
                policy=shadow,
            )
            await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
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
                verification_profile=verification_profile(),
                now=_NOW + timedelta(minutes=5),
            )
            for validator_index, composite in enumerate((0.7, 0.75, 0.8)):
                session.add(
                    Score(
                        agent_id=agent_id,
                        validator_hotkey=f"5PromotedValidator{validator_index}",
                        bench_version=9,
                        run_id=f"promoted-shadow-{validator_index}",
                        signature="ab" * 64,
                        seed=validator_index,
                        composite=composite,
                        tool_mean=composite,
                        memory_mean=composite,
                        median_ms=500,
                        n=114,
                        generated_at=_NOW,
                    )
                )
            enforce_revision, _ = await seed_settings(
                session,
                settings(mode="enforce"),
                parent_revision=shadow_revision.revision,
            )
            subject = await session.get(ConfirmationBundleSubject, (agent_id, 9))
            assert subject is not None
            # Mirrors reconciliation's projection after a shadow -> enforce flip;
            # the real Postgres authority trigger also validates this transition.
            subject.result_status = "full_confirmed"
            await session.flush()
            enforce_revision_number = enforce_revision.revision

        assert bundle.completion_mode == "shadow"
        assert bundle.settings_revision == shadow_revision.revision
        assert enforce_revision.revision != bundle.settings_revision
        (row,) = await list_eligible_ledger(
            session,
            bench_version=9,
            include_fingerprints=False,
            include_details=False,
        )
        assert row.agent_id == agent_id
        assert row.official_composite == pytest.approx(0.65)
        assert await ranked_quorum_agent_ids(session, bench_version=9) == {agent_id}
        public = await v9_confirmation_public_projections(session, agent_ids=[agent_id])
        assert public[agent_id].result_status == "full_confirmed"
        assert public[agent_id].full_confirmed_composite == pytest.approx(0.65)
        receipt = public[agent_id].receipt
        assert receipt is not None
        assert receipt["mode"] == "enforce"

        await session.rollback()  # close the read-only autobegun transaction
        changed_profile = replace(
            verification_profile(), revision="confirmation-v9-test-drift"
        )
        async with session.begin():
            await seed_settings(
                session,
                settings(
                    mode="enforce",
                    profile_revision=changed_profile.revision,
                    profile_checksum=changed_profile.checksum(),
                ),
                parent_revision=enforce_revision_number,
            )

        assert (
            await list_eligible_ledger(
                session,
                bench_version=9,
                include_fingerprints=False,
                include_details=False,
            )
            == []
        )
        assert await ranked_quorum_agent_ids(session, bench_version=9) == set()
        drifted_public = await v9_confirmation_public_projections(
            session, agent_ids=[agent_id]
        )
        assert drifted_public[agent_id].result_status == "provisional"
        assert drifted_public[agent_id].full_confirmed_composite is None
        assert drifted_public[agent_id].receipt is None

    async def test_shadow_keeps_base_ledger_byte_semantics(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_id, _revision, _policy, _bundle = await seed_bundle(session)
            for validator_index, composite in enumerate((0.6, 0.7, 0.8)):
                session.add(
                    Score(
                        agent_id=agent_id,
                        validator_hotkey=f"5ShadowValidator{validator_index}",
                        bench_version=9,
                        run_id=f"shadow-{validator_index}",
                        signature="ab" * 64,
                        seed=validator_index,
                        composite=composite,
                        tool_mean=composite,
                        memory_mean=composite,
                        median_ms=500,
                        n=114,
                        generated_at=_NOW,
                    )
                )
        (row,) = await list_eligible_ledger(
            session,
            bench_version=9,
            include_fingerprints=False,
            include_details=False,
        )
        assert row.composite == pytest.approx(0.7)
        assert row.official_composite == pytest.approx(0.7)
        assert row.v9_confirmation is None
        public = await v9_confirmation_public_projections(session, agent_ids=[agent_id])
        assert public[agent_id].result_status == "provisional"
        assert public[agent_id].full_confirmed_composite is None
        assert public[agent_id].receipt is None

    async def test_shadow_bundle_completion_cannot_satisfy_canonical_quorum(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            before_version = await active_bench_version(session)
        async with session.begin():
            agent_id, revision, policy, bundle = await seed_bundle(session)
            original = await session.get(Agent, agent_id)
            assert original is not None
            original_status = original.status
            reservation, ticket = await reserve_and_issue(
                session, bundle=bundle, revision=revision, policy=policy
            )
            await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
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
                verification_profile=verification_profile(),
                now=_NOW + timedelta(minutes=5),
            )
        refreshed = await session.get(Agent, agent_id)
        assert refreshed is not None
        assert refreshed.status == original_status == AgentStatus.SCORED
        assert await session.scalar(select(func.count()).select_from(Score)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(ConfirmationScore))
            == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(BenchmarkRollout))
            == 0
        )
        assert await active_bench_version(session) == before_version
        assert await list_eligible_ledger(session, include_details=False) == []
