"""Hostile transport tests for the private Bench v9 confirmation lane.

These tests deliberately use the real PostgreSQL harness.  The endpoint owns
three pieces of durable state -- its lease, its spend reservation, and its
typed completion evidence -- so a mock session would miss the cross-table and
rollback properties this protocol exists to provide.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.confirmation_bundles import (
    ConfirmationBundleMode,
    ConfirmationBundleSettings,
    ConfirmationCompletionReport,
)
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_models.validator import V9BaseEvidence
from ditto.api_models.validator_confirmation import V9ConfirmationClaimRequest
from ditto.api_server.confirmation_candidate_reconciliation import (
    reconcile_v9_confirmation_candidates,
)
from ditto.api_server.confirmation_evidence import (
    ConfirmationVerificationProfile,
    confirmation_signing_message,
    rebuild_confirmation_evidence,
)
from ditto.api_server.confirmation_wire import completion_report_from_go_dimensions
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints import validator_confirmation as confirmation_mod
from ditto.api_server.endpoints.validator_confirmation import (
    v9_confirmation_claim_signing_message,
    v9_confirmation_fail_signing_message,
    v9_confirmation_prepare_signing_message,
    v9_confirmation_prepare_wire_sha256,
)
from ditto.chain.models import NeuronInfo
from ditto.db.models import (
    Agent,
    ConfirmationBudgetDay,
    ConfirmationBudgetReservation,
    ConfirmationBundle,
    ConfirmationBundleSettingsRevision,
    ConfirmationBundleSubject,
    ConfirmationBundleTicket,
    ConfirmationDimensionEvidence,
    ConfirmationScore,
    Score,
    ValidatorSlotSettingsRevision,
    ValidatorTicket,
)
from ditto.db.queries.confirmation_attempt_lock import lock_confirmation_attempt
from ditto.db.queries.confirmation_bundles import (
    complete_confirmation_bundle,
    get_or_create_confirmation_bundle,
    settle_confirmation_bundle_budget,
)
from ditto.db.queries.confirmation_policy_lock import lock_confirmation_policy
from ditto.db.queries.confirmation_ticket_recovery import (
    expire_overdue_confirmation_bundle_tickets,
)
from ditto.tests.confirmation_evidence_fixtures import (
    ARTIFACT_SHA256,
    VALIDATOR_KEYPAIR,
    active_settings,
    base_proof_kwargs,
    go_verification_profile,
    signed_report,
    verification_profile,
)

pytestmark = pytest.mark.asyncio

_JOB_URL = "/api/v1/validator/v9-confirmation/job"
_REPORT_URL = "/api/v1/validator/v9-confirmation/bundle/{bundle_id}/report"
_PREPARE_URL = "/api/v1/validator/v9-confirmation/bundle/{bundle_id}/prepare-report"
_FAIL_URL = "/api/v1/validator/v9-confirmation/bundle/{bundle_id}/fail"
_OTHER_KEYPAIR = bittensor.Keypair.create_from_uri("//Bob")
_GO_FIXTURE_PATH = (
    Path(__file__).parents[6]
    / "services"
    / "dittobench-api"
    / "internal"
    / "confirmationwire"
    / "testdata"
    / "go_confirmation_evidence_v9.json"
)
_V9_BASE_VECTOR_PATH = (
    Path(__file__).parents[6]
    / "services"
    / "dittobench-api"
    / "testdata"
    / "v9_base_contract_vectors.json"
)
_ABLATION_COORDINATOR_LATENCY_MS = 333


@dataclass(frozen=True)
class SeededBundle:
    agent_id: UUID
    bundle_id: UUID
    settings_revision: int
    settings: ConfirmationBundleSettings


def _settings_checksum(settings: ConfirmationBundleSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _install_transport(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    *,
    register_profile: bool = True,
    profile: ConfirmationVerificationProfile | None = None,
) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    neurons = [
        NeuronInfo(
            hotkey=VALIDATOR_KEYPAIR.ss58_address,
            coldkey="5GReceiverColdkeyPlaceholderXXXXXXXXXXXXXXXXXXX",
            uid=1,
            stake=1_000.0,
            validator_permit=True,
        ),
        NeuronInfo(
            hotkey=_OTHER_KEYPAIR.ss58_address,
            coldkey="5GOtherColdkeyPlaceholderXXXXXXXXXXXXXXXXXXXXXX",
            uid=2,
            stake=1_000.0,
            validator_permit=True,
        ),
    ]

    async def _chain() -> MagicMock:
        chain = MagicMock()
        chain.get_recent_neurons = AsyncMock(return_value=neurons)
        return chain

    installed_profile = profile or verification_profile()
    app.state.confirmation_verification_profiles = (
        {
            (
                installed_profile.revision,
                installed_profile.checksum(),
            ): installed_profile
        }
        if register_profile
        else {}
    )
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_chain_client] = _chain


async def _seed_bundle(
    maker: async_sessionmaker[AsyncSession],
    *,
    settings: ConfirmationBundleSettings | None = None,
    agent_status: AgentStatus = AgentStatus.SCORED,
    artifact_sha256: str = ARTIFACT_SHA256,
    verification_profile_override: ConfirmationVerificationProfile | None = None,
) -> SeededBundle:
    frozen = settings or active_settings(mode=ConfirmationBundleMode.SHADOW)
    profile = verification_profile_override or verification_profile()
    agent_id = uuid4()
    async with maker() as session, session.begin():
        revision = ConfirmationBundleSettingsRevision(
            parent_revision=0,
            scope="*",
            settings=frozen.model_dump(mode="json"),
            checksum=_settings_checksum(frozen),
            reason="test exact-profile private confirmation transport",
            actor="pytest@example.com",
        )
        session.add(revision)
        await session.flush()
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"5Miner-{agent_id}",
                name="v9-confirmation-subject",
                sha256=artifact_sha256,
                status=agent_status,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await session.flush()
        resolution = await get_or_create_confirmation_bundle(
            session,
            agent_id=agent_id,
            bench_version=9,
            **base_proof_kwargs(),
            settings_revision=revision.revision,
            settings=frozen,
            verification_profile=profile,
        )
        assert resolution.bundle is not None
        bundle_id = resolution.bundle.bundle_id
        revision_number = revision.revision
    return SeededBundle(
        agent_id=agent_id,
        bundle_id=bundle_id,
        settings_revision=revision_number,
        settings=frozen,
    )


async def _pause_validator_issuance(
    maker: async_sessionmaker[AsyncSession], *, parent_revision: int = 0
) -> None:
    async with maker() as session, session.begin():
        session.add(
            ValidatorSlotSettingsRevision(
                parent_revision=parent_revision,
                scope="*",
                settings={
                    "max_concurrent_slots": 2,
                    "disk_percent_ceiling": 90,
                    "memory_percent_ceiling": 90,
                    "cpu_percent_ceiling": 0,
                    "resource_block_percent_ceiling": 95,
                    "paused_validator_hotkeys": [VALIDATOR_KEYPAIR.ss58_address],
                },
                checksum="f" * 64,
                reason="drain confirmation issuance for this validator",
                actor="pytest@example.com",
            )
        )


async def _seed_pending_bundle_on_revision(
    maker: async_sessionmaker[AsyncSession],
    *,
    parent: SeededBundle,
    artifact_sha256: str,
    profile: ConfirmationVerificationProfile,
) -> SeededBundle:
    """Add a second candidate without introducing a competing policy revision."""
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"5Miner-{agent_id}",
                name="v9-confirmation-lock-order-candidate",
                sha256=artifact_sha256,
                status=AgentStatus.SCORED,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=datetime.now(UTC),
            )
        )
        await session.flush()
        resolution = await get_or_create_confirmation_bundle(
            session,
            agent_id=agent_id,
            bench_version=9,
            **base_proof_kwargs(quality_micros=790_000),
            settings_revision=parent.settings_revision,
            settings=parent.settings,
            verification_profile=profile,
        )
        assert resolution.bundle is not None
        bundle_id = resolution.bundle.bundle_id
    return SeededBundle(
        agent_id=agent_id,
        bundle_id=bundle_id,
        settings_revision=parent.settings_revision,
        settings=parent.settings,
    )


async def _wait_for_budget_lock_waiter(
    maker: async_sessionmaker[AsyncSession], *, owner_pid: int
) -> None:
    """Wait until the claim is blocked at the budget boundary, not by a sleep."""
    await _wait_for_table_lock_waiters(
        maker,
        owner_pid=owner_pid,
        table_name="confirmation_budget_days",
    )


async def _wait_for_table_lock_waiters(
    maker: async_sessionmaker[AsyncSession],
    *,
    owner_pid: int,
    table_name: str,
    minimum: int = 1,
) -> None:
    """Observe real PostgreSQL lock waiters in this worker's cloned database."""
    for _ in range(200):
        async with maker() as observer:
            waiter_count = int(
                await observer.scalar(
                    text(
                        """
                        SELECT count(*)
                        FROM pg_stat_activity
                        WHERE pid <> :owner_pid
                          AND pid <> pg_backend_pid()
                          AND datname = current_database()
                          AND wait_event_type = 'Lock'
                          AND query ILIKE :query_pattern
                        """
                    ),
                    {
                        "owner_pid": owner_pid,
                        "query_pattern": f"%{table_name}%",
                    },
                )
                or 0
            )
        if waiter_count >= minimum:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"expected {minimum} waiter(s) on {table_name}, found {waiter_count}"
    )


async def _seed_reconcilable_bundle(
    maker: async_sessionmaker[AsyncSession],
) -> SeededBundle:
    settings = active_settings(mode=ConfirmationBundleMode.SHADOW).model_copy(
        update={"top_n": 1}
    )
    agent_id = uuid4()
    artifact_sha256 = ARTIFACT_SHA256
    vector_payload = json.loads(_V9_BASE_VECTOR_PATH.read_text())
    vector = vector_payload["vectors"][0]["details"]
    scores: list[Score] = []
    for index in range(3):
        raw = copy.deepcopy(vector)
        raw.update(
            {
                "run_id": f"policy-race-{agent_id}-{index}",
                "artifact_sha256": artifact_sha256,
                "ordinary_composite_micros": 800_000,
                "ordinary_stderr_micros": 10_000,
                "effective_composite_micros": 800_000,
                "effective_stderr_micros": 10_000,
            }
        )
        evidence = V9BaseEvidence.model_validate(raw)
        scores.append(
            Score(
                agent_id=agent_id,
                validator_hotkey=f"5PolicyRaceValidator-{index}",
                bench_version=9,
                run_id=evidence.run_id,
                signature=f"{index + 1:02x}",
                seed=index,
                composite=0.8,
                tool_mean=0.8,
                memory_mean=0.8,
                median_ms=100,
                n=114,
                details={
                    "v9_base": evidence.model_dump(mode="json"),
                    "base_evidence_sha256": evidence.digest_hex(),
                },
                generated_at=datetime.now(UTC) + timedelta(seconds=index),
            )
        )
    async with maker() as session, session.begin():
        revision = ConfirmationBundleSettingsRevision(
            parent_revision=0,
            scope="*",
            settings=settings.model_dump(mode="json"),
            checksum=_settings_checksum(settings),
            reason="seed policy race against claim issuance",
            actor="pytest@example.com",
        )
        session.add(revision)
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"5PolicyRaceMiner-{agent_id}",
                name="v9-confirmation-policy-race",
                sha256=artifact_sha256,
                status=AgentStatus.SCORED,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        session.add_all(scores)
        await session.flush()
        await reconcile_v9_confirmation_candidates(
            session,
            verification_profiles={
                (
                    verification_profile().revision,
                    verification_profile().checksum(),
                ): verification_profile()
            },
        )
        subject = await session.get(ConfirmationBundleSubject, (agent_id, 9))
        assert subject is not None and subject.bundle_id is not None
        bundle_id = subject.bundle_id
        revision_number = revision.revision
    return SeededBundle(
        agent_id=agent_id,
        bundle_id=bundle_id,
        settings_revision=revision_number,
        settings=settings,
    )


async def _append_off_revision(session: AsyncSession, *, parent: SeededBundle) -> None:
    off = parent.settings.model_copy(update={"mode": ConfirmationBundleMode.OFF})
    session.add(
        ConfirmationBundleSettingsRevision(
            parent_revision=parent.settings_revision,
            scope="*",
            settings=off.model_dump(mode="json"),
            checksum=_settings_checksum(off),
            reason="disable costly confirmation issuance during transport test",
            actor="pytest@example.com",
        )
    )
    await session.flush()


async def _append_enforce_revision(
    session: AsyncSession, *, parent: SeededBundle
) -> ConfirmationBundleSettingsRevision:
    enforce = parent.settings.model_copy(
        update={"mode": ConfirmationBundleMode.ENFORCE}
    )
    revision = ConfirmationBundleSettingsRevision(
        parent_revision=parent.settings_revision,
        scope="*",
        settings=enforce.model_dump(mode="json"),
        checksum=_settings_checksum(enforce),
        reason="activate new policy while a validator claim races",
        actor="pytest@example.com",
    )
    session.add(revision)
    await session.flush()
    return revision


def _claim_payload(
    *,
    slot_id: str = "longmem-0",
    nonce: UUID | None = None,
    requested_at: datetime | None = None,
    keypair: bittensor.Keypair = VALIDATOR_KEYPAIR,
    profile_revision: str | None = None,
    profile_checksum: str | None = None,
) -> dict[str, Any]:
    profile = verification_profile()
    revision = profile_revision or profile.revision
    checksum = profile_checksum or profile.checksum()
    claim_nonce = nonce or uuid4()
    claimed_at = requested_at or datetime.now(UTC)
    broker_public_key = "A" * 43
    signature = keypair.sign(
        v9_confirmation_claim_signing_message(
            validator_hotkey=keypair.ss58_address,
            slot_id=slot_id,
            profile_revision=revision,
            profile_checksum=checksum,
            broker_public_key=broker_public_key,
            nonce=claim_nonce,
            requested_at=claimed_at,
        )
    ).hex()
    return V9ConfirmationClaimRequest(
        validator_hotkey=keypair.ss58_address,
        slot_id=slot_id,
        profile_revision=revision,
        profile_checksum=checksum,
        broker_public_key=broker_public_key,
        nonce=claim_nonce,
        requested_at=claimed_at,
        signature=signature,
    ).model_dump(mode="json")


async def _claim(
    client: httpx.AsyncClient,
    *,
    payload: dict[str, Any] | None = None,
    header_hotkey: str | None = None,
) -> httpx.Response:
    body = payload or _claim_payload()
    return await client.post(
        _JOB_URL,
        json=body,
        headers={"X-Validator-Hotkey": header_hotkey or str(body["validator_hotkey"])},
    )


async def _claimed_rows(
    maker: async_sessionmaker[AsyncSession], *, bundle_id: UUID
) -> tuple[
    ConfirmationBundle,
    ConfirmationBundleTicket,
    ConfirmationBudgetReservation,
    ConfirmationBudgetDay,
]:
    async with maker() as session:
        bundle = await session.get(ConfirmationBundle, bundle_id)
        ticket = await session.scalar(
            select(ConfirmationBundleTicket).where(
                ConfirmationBundleTicket.bundle_id == bundle_id
            )
        )
        reservation = await session.scalar(
            select(ConfirmationBudgetReservation).where(
                ConfirmationBudgetReservation.bundle_id == bundle_id
            )
        )
        assert bundle is not None
        assert ticket is not None
        assert reservation is not None
        budget = await session.get(ConfirmationBudgetDay, reservation.utc_day)
        assert budget is not None
        return bundle, ticket, reservation, budget


async def _canonical_counts(
    maker: async_sessionmaker[AsyncSession], *, agent_id: UUID
) -> tuple[AgentStatus, int, int]:
    async with maker() as session:
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        score_count = int(
            await session.scalar(
                select(func.count())
                .select_from(Score)
                .where(Score.agent_id == agent_id)
            )
            or 0
        )
        continual_count = int(
            await session.scalar(
                select(func.count())
                .select_from(ConfirmationScore)
                .where(ConfirmationScore.agent_id == agent_id)
            )
            or 0
        )
        return agent.status, score_count, continual_count


async def _assert_unsettled(
    maker: async_sessionmaker[AsyncSession], *, seeded: SeededBundle
) -> None:
    bundle, ticket, reservation, budget = await _claimed_rows(
        maker, bundle_id=seeded.bundle_id
    )
    assert bundle.state == "leased"
    assert bundle.evidence_sha256 is None
    assert bundle.completion_ticket_id is None
    assert ticket.status == "issued"
    assert reservation.state == "reserved"
    assert reservation.actual_microusd is None
    assert budget.revision == 1
    assert budget.issued_attempts == 1
    assert budget.outstanding_reserved_microusd == reservation.reserved_microusd
    assert budget.settled_microusd == 0
    async with maker() as session:
        evidence_count = int(
            await session.scalar(
                select(func.count())
                .select_from(ConfirmationDimensionEvidence)
                .where(ConfirmationDimensionEvidence.bundle_id == seeded.bundle_id)
            )
            or 0
        )
    assert evidence_count == 0
    assert await _canonical_counts(maker, agent_id=seeded.agent_id) == (
        AgentStatus.SCORED,
        0,
        0,
    )


def _report_payload(
    *,
    bundle: ConfirmationBundle,
    ticket: ConfirmationBundleTicket,
    mode: ConfirmationBundleMode = ConfirmationBundleMode.SHADOW,
) -> dict[str, Any]:
    report = signed_report(bundle=bundle, ticket=ticket, mode=mode)
    return {
        "validator_hotkey": VALIDATOR_KEYPAIR.ss58_address,
        "ticket_id": str(ticket.ticket_id),
        "report": report.model_dump(mode="json"),
    }


def _fail_payload(
    *,
    bundle_id: UUID,
    ticket_id: UUID,
    reason: str = "execution_failed",
    nonce: UUID | None = None,
    requested_at: datetime | None = None,
    keypair: bittensor.Keypair = VALIDATOR_KEYPAIR,
) -> dict[str, Any]:
    failure_nonce = nonce or uuid4()
    failed_at = requested_at or datetime.now(UTC)
    signature = keypair.sign(
        v9_confirmation_fail_signing_message(
            validator_hotkey=keypair.ss58_address,
            bundle_id=bundle_id,
            ticket_id=ticket_id,
            reason=reason,
            nonce=failure_nonce,
            requested_at=failed_at,
        )
    ).hex()
    return {
        "validator_hotkey": keypair.ss58_address,
        "ticket_id": str(ticket_id),
        "reason": reason,
        "nonce": str(failure_nonce),
        "requested_at": failed_at.isoformat(),
        "signature": signature,
    }


async def _fail(
    client: httpx.AsyncClient,
    *,
    bundle_id: UUID,
    payload: dict[str, Any],
    header_hotkey: str | None = None,
) -> httpx.Response:
    return await client.post(
        _FAIL_URL.format(bundle_id=bundle_id),
        json=payload,
        headers={
            "X-Validator-Hotkey": header_hotkey or str(payload["validator_hotkey"])
        },
    )


def _go_fixture() -> dict[str, object]:
    return json.loads(_GO_FIXTURE_PATH.read_text())


def _go_settings() -> ConfirmationBundleSettings:
    profile = go_verification_profile()
    return active_settings(mode=ConfirmationBundleMode.SHADOW).model_copy(
        update={
            "profile_revision": profile.revision,
            "profile_checksum": profile.checksum(),
        }
    )


async def _seed_claimed_go_case(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> tuple[SeededBundle, ConfirmationBundle, ConfirmationBundleTicket]:
    profile = go_verification_profile()
    seeded = await _seed_bundle(
        maker,
        settings=_go_settings(),
        verification_profile_override=profile,
    )
    _install_transport(app, maker, profile=profile)
    claim = await _claim(
        client,
        payload=_claim_payload(
            profile_revision=profile.revision,
            profile_checksum=profile.checksum(),
        ),
    )
    assert claim.status_code == 200, claim.text
    bundle, ticket, _, _ = await _claimed_rows(maker, bundle_id=seeded.bundle_id)
    return seeded, bundle, ticket


def _prepare_payload(
    *,
    bundle_id: UUID,
    ticket_id: UUID,
    fixture: dict[str, object] | None = None,
    nonce: UUID | None = None,
    requested_at: datetime | None = None,
    keypair: bittensor.Keypair = VALIDATOR_KEYPAIR,
    wire_sha256: str | None = None,
) -> dict[str, Any]:
    raw = copy.deepcopy(fixture or _go_fixture())
    longmemeval = raw["longmemeval"]
    inference_ablation = raw["inference_ablation"]
    embedding_ablation = raw["embedding_ablation"]
    assert isinstance(longmemeval, dict)
    assert isinstance(inference_ablation, dict)
    assert isinstance(embedding_ablation, dict)
    prepare_nonce = nonce or uuid4()
    prepared_at = requested_at or datetime.now(UTC)
    digest = wire_sha256 or v9_confirmation_prepare_wire_sha256(
        ablation_coordinator_latency_ms=_ABLATION_COORDINATOR_LATENCY_MS,
        longmemeval=longmemeval,
        inference_ablation=inference_ablation,
        embedding_ablation=embedding_ablation,
    )
    signature = keypair.sign(
        v9_confirmation_prepare_signing_message(
            validator_hotkey=keypair.ss58_address,
            bundle_id=bundle_id,
            ticket_id=ticket_id,
            wire_sha256=digest,
            nonce=prepare_nonce,
            requested_at=prepared_at,
        )
    ).hex()
    return {
        "validator_hotkey": keypair.ss58_address,
        "ticket_id": str(ticket_id),
        "nonce": str(prepare_nonce),
        "requested_at": prepared_at.isoformat(),
        "wire_sha256": digest,
        "ablation_coordinator_latency_ms": _ABLATION_COORDINATOR_LATENCY_MS,
        "longmemeval": longmemeval,
        "inference_ablation": inference_ablation,
        "embedding_ablation": embedding_ablation,
        "signature": signature,
    }


async def _prepare(
    client: httpx.AsyncClient,
    *,
    bundle_id: UUID,
    payload: dict[str, Any],
    header_hotkey: str | None = None,
) -> httpx.Response:
    return await client.post(
        _PREPARE_URL.format(bundle_id=bundle_id),
        json=payload,
        headers={
            "X-Validator-Hotkey": header_hotkey or str(payload["validator_hotkey"])
        },
    )


def _signed_prepared_report(
    *,
    prepared: dict[str, Any],
    bundle: ConfirmationBundle,
    ticket: ConfirmationBundleTicket,
) -> ConfirmationCompletionReport:
    signature = VALIDATOR_KEYPAIR.sign(
        confirmation_signing_message(
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
            evidence_sha256=str(prepared["evidence_sha256"]),
        )
    ).hex()
    return ConfirmationCompletionReport.model_validate(
        {
            "ablation_coordinator_latency_ms": prepared[
                "ablation_coordinator_latency_ms"
            ],
            "longmemeval": prepared["longmemeval"],
            "inference_ablation": prepared["inference_ablation"],
            "embedding_ablation": prepared["embedding_ablation"],
            "bundle_signature": signature,
        }
    )


class TestV9ConfirmationClaimAdmission:
    async def test_policy_lock_contention_returns_no_work_without_queueing(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        _install_transport(app, session_maker)

        # Hold the exact policy lock used by the admin write and prove a poll
        # fails open to no work instead of occupying a request and DB session.
        async with session_maker() as session, session.begin():
            await lock_confirmation_policy(session)
            claim_task = asyncio.create_task(_claim(client))
            response = await asyncio.wait_for(claim_task, timeout=2)

        assert response.status_code == 204, response.text
        async with session_maker() as session:
            bundle = await session.get(ConfirmationBundle, seeded.bundle_id)
            assert bundle is not None and bundle.state == "pending"
            assert (
                await session.scalar(
                    select(func.count()).select_from(ConfirmationBundleTicket)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(ConfirmationBudgetReservation)
                )
                == 0
            )

    async def test_claim_retries_after_contended_policy_update(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_reconcilable_bundle(session_maker)
        _install_transport(app, session_maker)

        async with session_maker() as session, session.begin():
            await lock_confirmation_policy(session)
            claim_task = asyncio.create_task(_claim(client))
            blocked = await asyncio.wait_for(claim_task, timeout=2)
            revision = await _append_enforce_revision(session, parent=seeded)

        assert blocked.status_code == 204, blocked.text

        response = await _claim(client)

        assert response.status_code == 200, response.text
        replacement_id = UUID(response.json()["bundle_id"])
        assert replacement_id != seeded.bundle_id
        assert response.json()["settings_revision"] == revision.revision
        assert response.json()["mode"] == "enforce"
        async with session_maker() as session:
            stale = await session.get(ConfirmationBundle, seeded.bundle_id)
            replacement = await session.get(ConfirmationBundle, replacement_id)
            subject = await session.get(ConfirmationBundleSubject, (seeded.agent_id, 9))
            assert stale is not None and stale.state == "superseded"
            assert replacement is not None and replacement.state == "leased"
            assert replacement.generation_reason == "settings_supersession"
            assert replacement.source_bundle_id == stale.bundle_id
            assert replacement.retest_generation == stale.retest_generation + 1
            assert replacement.settings_revision == revision.revision
            assert subject is not None and subject.bundle_id == replacement_id
            stale_ticket_count = await session.scalar(
                select(func.count())
                .select_from(ConfirmationBundleTicket)
                .where(ConfirmationBundleTicket.bundle_id == stale.bundle_id)
            )
            stale_reservation_count = await session.scalar(
                select(func.count())
                .select_from(ConfirmationBudgetReservation)
                .where(ConfirmationBudgetReservation.bundle_id == stale.bundle_id)
            )
            assert stale_ticket_count == 0
            assert stale_reservation_count == 0

    async def test_exhausted_daily_cap_skips_candidate_reconciliation(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        _install_transport(app, session_maker)
        async with session_maker() as session, session.begin():
            session.add(
                ConfirmationBudgetDay(
                    utc_day=datetime.now(UTC).date(),
                    revision=seeded.settings.daily_bundle_cap,
                    issued_attempts=seeded.settings.daily_bundle_cap,
                    outstanding_reserved_microusd=0,
                    settled_microusd=0,
                )
            )
        reconcile = AsyncMock(
            side_effect=AssertionError("exhausted budget must not reconcile")
        )
        monkeypatch.setattr(
            confirmation_mod, "reconcile_v9_confirmation_candidates", reconcile
        )

        response = await _claim(client)

        assert response.status_code == 204, response.text
        reconcile.assert_not_awaited()
        async with session_maker() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(ConfirmationBundleTicket)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(ConfirmationBudgetReservation)
                )
                == 0
            )

    async def test_claim_commits_first_and_live_generation_stays_frozen_completable(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_reconcilable_bundle(session_maker)
        _install_transport(app, session_maker)
        issued = await _claim(client)
        assert issued.status_code == 200, issued.text
        assert issued.json()["bundle_id"] == str(seeded.bundle_id)
        assert issued.json()["settings_revision"] == seeded.settings_revision
        assert issued.json()["mode"] == "shadow"
        bundle, ticket, _, _ = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )

        async with session_maker() as session, session.begin():
            await lock_confirmation_policy(session)
            revision = await _append_enforce_revision(session, parent=seeded)
            await reconcile_v9_confirmation_candidates(
                session,
                verification_profiles={
                    (
                        verification_profile().revision,
                        verification_profile().checksum(),
                    ): verification_profile()
                },
            )

        resumed = await _claim(client)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["bundle_id"] == str(seeded.bundle_id)
        assert resumed.json()["ticket_id"] == str(ticket.ticket_id)
        assert resumed.json()["settings_revision"] == seeded.settings_revision
        assert resumed.json()["settings_revision"] != revision.revision
        assert resumed.json()["mode"] == "shadow"
        submitted = await client.post(
            _REPORT_URL.format(bundle_id=seeded.bundle_id),
            json=_report_payload(bundle=bundle, ticket=ticket),
            headers={"X-Validator-Hotkey": VALIDATOR_KEYPAIR.ss58_address},
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["accepted"] is True
        async with session_maker() as session:
            stored = await session.get(ConfirmationBundle, seeded.bundle_id)
            subject = await session.get(ConfirmationBundleSubject, (seeded.agent_id, 9))
            bundle_count = await session.scalar(
                select(func.count()).select_from(ConfirmationBundle)
            )
            assert stored is not None and stored.state == "completed"
            assert stored.generation_reason == "initial"
            assert stored.source_bundle_id is None
            assert stored.settings_revision == seeded.settings_revision
            assert subject is not None and subject.bundle_id == seeded.bundle_id
            assert bundle_count == 1

    async def test_profile_rotation_cannot_issue_a_second_live_ticket_for_slot(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        old_profile = verification_profile()
        new_profile = go_verification_profile()
        assert (old_profile.revision, old_profile.checksum()) != (
            new_profile.revision,
            new_profile.checksum(),
        )
        _install_transport(app, session_maker, profile=old_profile)
        app.state.confirmation_verification_profiles[
            (new_profile.revision, new_profile.checksum())
        ] = new_profile

        issued = await _claim(client)
        assert issued.status_code == 200, issued.text
        old_ticket_id = issued.json()["ticket_id"]

        rotated = seeded.settings.model_copy(
            update={
                "profile_revision": new_profile.revision,
                "profile_checksum": new_profile.checksum(),
            }
        )
        async with session_maker() as session, session.begin():
            session.add(
                ConfirmationBundleSettingsRevision(
                    parent_revision=seeded.settings_revision,
                    scope="*",
                    settings=rotated.model_dump(mode="json"),
                    checksum=_settings_checksum(rotated),
                    reason="rotate profile while the old slot lease remains live",
                    actor="pytest@example.com",
                )
            )

        blocked = await _claim(
            client,
            payload=_claim_payload(
                profile_revision=new_profile.revision,
                profile_checksum=new_profile.checksum(),
            ),
        )

        assert blocked.status_code == 204, blocked.text
        async with session_maker() as session:
            tickets = list(await session.scalars(select(ConfirmationBundleTicket)))
            bundles = list(await session.scalars(select(ConfirmationBundle)))
        assert len(tickets) == 1
        assert str(tickets[0].ticket_id) == old_ticket_id
        assert tickets[0].status == "issued"
        assert len(bundles) == 1
        assert bundles[0].profile_revision == old_profile.revision

    async def test_default_empty_profile_registry_returns_204_without_leasing(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        _install_transport(app, session_maker, register_profile=False)

        response = await _claim(client)

        assert response.status_code == 204, response.text
        assert response.headers["cache-control"] == "no-store"
        async with session_maker() as session:
            bundle = await session.get(ConfirmationBundle, seeded.bundle_id)
            assert bundle is not None
            assert bundle.state == "pending"
            assert (
                await session.scalar(
                    select(func.count()).select_from(ConfirmationBundleTicket)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(ConfirmationBudgetReservation)
                )
                == 0
            )
        assert await _canonical_counts(session_maker, agent_id=seeded.agent_id) == (
            AgentStatus.SCORED,
            0,
            0,
        )

    async def test_exact_profile_claim_returns_internal_purpose_caps_and_90m_lease(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        _install_transport(app, session_maker)

        response = await _claim(client)

        assert response.status_code == 200, response.text
        body = response.json()
        profile = verification_profile()
        assert body["purpose"] == "v9_confirmation_bundle"
        assert body["bundle_id"] == str(seeded.bundle_id)
        assert body["agent_id"] == str(seeded.agent_id)
        assert body["bench_version"] == 9
        assert body["slot_id"] == "longmem-0"
        assert body["per_bundle_request_cap"] == 100
        assert body["per_bundle_token_cap"] == 10_000
        assert body["mode"] == "shadow"
        execution = body["execution_profile"]
        assert execution["revision"] == profile.revision
        assert execution["checksum"] == profile.checksum()
        assert execution["longmem_profile_revision"] == (
            profile.longmem_profile_revision
        )
        assert execution["longmem_profile_checksum"] == profile.longmem_checksum()
        assert execution["longmem_selector_revision"] == (
            profile.longmem_selector_revision
        )
        assert execution["longmem_selection_seed"] == profile.longmem_selection_seed
        assert execution["longmem_cases_per_capability"] == (
            profile.longmem_cases_per_capability
        )
        assert execution["longmem_seed_batch_pairs"] == (
            profile.longmem_seed_batch_pairs
        )
        assert execution["longmem_projection_key_sha256"] == (
            profile.longmem_projection_key_sha256
        )
        assert execution["ablation_profile_revision"] == (
            profile.ablation_profile_revision
        )
        assert execution["ablation_profile_checksum"] == profile.ablation_checksum()
        assert execution["ablation_dataset_sha256"] == (profile.ablation_dataset_sha256)
        assert execution["ablation_threshold_manifest_sha256"] == (
            profile.ablation_threshold_manifest_sha256
        )
        assert execution["ablation_selection_key_sha256"] == (
            profile.ablation_selection_key_sha256
        )
        assert execution["ablation_projection_key_sha256"] == (
            profile.ablation_projection_key_sha256
        )
        assert execution["ablation_coordinator_policy"] == (
            profile.ablation_coordinator_policy.payload()
        )
        assert set(execution["inference_ablation"]) == {
            "intervention",
            "contract_version",
            "threshold_micros",
            "budget",
        }
        assert set(execution["embedding_ablation"]) == {
            "intervention",
            "contract_version",
            "threshold_micros",
            "budget",
        }
        lanes = execution["provider_lanes"]
        assert [lane["lane"] for lane in lanes] == [
            "judge",
            "reader",
        ]
        bundle, ticket, reservation, budget = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )
        assert ticket.ticket_id == UUID(body["ticket_id"])
        assert reservation.reservation_id == UUID(body["reservation_id"])
        assert ticket.deadline - ticket.issued_at == timedelta(minutes=90)
        assert bundle.state == "leased"
        # Reader, judge, and embedding caps are all reserved before execution.
        assert reservation.reserved_microusd == 300_000
        assert reservation.state == "reserved"
        assert budget.issued_attempts == 1
        assert budget.outstanding_reserved_microusd == 300_000
        assert await _canonical_counts(session_maker, agent_id=seeded.agent_id) == (
            AgentStatus.SCORED,
            0,
            0,
        )

    async def test_paused_validator_receives_no_new_confirmation_bundle(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        await _pause_validator_issuance(session_maker)
        _install_transport(app, session_maker)
        app.state.session_maker = session_maker
        app.state.validator_slot_settings.invalidate()

        response = await _claim(client)

        assert response.status_code == 204, response.text
        async with session_maker() as session:
            bundle = await session.get(ConfirmationBundle, seeded.bundle_id)
            assert bundle is not None
            assert bundle.state == "pending"
            assert (
                await session.scalar(
                    select(func.count()).select_from(ConfirmationBundleTicket)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(ConfirmationBudgetReservation)
                )
                == 0
            )

    async def test_paused_validator_can_resume_live_confirmation_bundle(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        _install_transport(app, session_maker)
        app.state.session_maker = session_maker
        first = await _claim(client)
        assert first.status_code == 200, first.text
        await _pause_validator_issuance(session_maker)
        app.state.validator_slot_settings.invalidate()

        resumed = await _claim(client)

        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["bundle_id"] == str(seeded.bundle_id)
        assert resumed.json()["ticket_id"] == first.json()["ticket_id"]

    async def test_claim_nonce_is_one_shot_even_when_first_claim_returns_204(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_bundle(session_maker)
        _install_transport(app, session_maker, register_profile=False)
        payload = _claim_payload()

        first = await _claim(client, payload=payload)
        app.state.confirmation_verification_profiles = {
            (
                verification_profile().revision,
                verification_profile().checksum(),
            ): verification_profile()
        }
        replay = await _claim(client, payload=payload)

        assert first.status_code == 204
        assert replay.status_code == 409
        assert "nonce has already been used" in replay.text

    async def test_unregistered_exactly_signed_profile_returns_204(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        _install_transport(app, session_maker)
        payload = _claim_payload(profile_checksum="9" * 64)

        response = await _claim(client, payload=payload)

        assert response.status_code == 204, response.text
        async with session_maker() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(ConfirmationBundleTicket)
                )
                == 0
            )
            bundle = await session.get(ConfirmationBundle, seeded.bundle_id)
            assert bundle is not None and bundle.state == "pending"

    async def test_claim_header_must_equal_the_signed_hotkey(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_bundle(session_maker)
        _install_transport(app, session_maker)

        response = await _claim(
            client,
            payload=_claim_payload(),
            header_hotkey=_OTHER_KEYPAIR.ss58_address,
        )

        assert response.status_code == 401
        assert response.json()["error_code"] == 4000

    async def test_claim_signature_cannot_be_relabelled_to_another_message(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_bundle(session_maker)
        _install_transport(app, session_maker)
        payload = _claim_payload()
        payload["signature"] = VALIDATOR_KEYPAIR.sign(b"different-domain").hex()

        response = await _claim(client, payload=payload)

        assert response.status_code == 401
        assert response.json()["error_code"] == 4000


class TestV9ConfirmationPrepareAdmission:
    async def test_prepare_signature_domain_binds_every_authoritative_field(
        self,
    ) -> None:
        bundle_id = UUID("11111111-1111-1111-1111-111111111111")
        ticket_id = UUID("22222222-2222-2222-2222-222222222222")
        nonce = UUID("33333333-3333-3333-3333-333333333333")
        requested_at = datetime(2026, 8, 8, 12, 34, 56, 789, tzinfo=UTC)
        wire_sha256 = "4" * 64

        message = v9_confirmation_prepare_signing_message(
            validator_hotkey=VALIDATOR_KEYPAIR.ss58_address,
            bundle_id=bundle_id,
            ticket_id=ticket_id,
            wire_sha256=wire_sha256,
            nonce=nonce,
            requested_at=requested_at,
        )

        assert (
            message
            == (
                "validator-v9-confirmation-prepare:v1:"
                f"{VALIDATOR_KEYPAIR.ss58_address}:{bundle_id}:{ticket_id}:"
                f"{wire_sha256}:{nonce}:2026-08-08T12:34:56.000789Z"
            ).encode()
        )

    @pytest.mark.parametrize(
        "forgery",
        ["bundle", "ticket", "wire", "nonce", "requested_at", "hotkey"],
    )
    async def test_prepare_signature_cannot_be_replayed_under_changed_fields(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        forgery: str,
    ) -> None:
        seeded, _, ticket = await _seed_claimed_go_case(app, client, session_maker)
        payload = _prepare_payload(
            bundle_id=seeded.bundle_id,
            ticket_id=ticket.ticket_id,
        )
        path_bundle_id = seeded.bundle_id
        header_hotkey = VALIDATOR_KEYPAIR.ss58_address
        if forgery == "bundle":
            path_bundle_id = uuid4()
        elif forgery == "ticket":
            payload["ticket_id"] = str(uuid4())
        elif forgery == "wire":
            payload["wire_sha256"] = "9" * 64
        elif forgery == "nonce":
            payload["nonce"] = str(uuid4())
        elif forgery == "requested_at":
            payload["requested_at"] = (
                datetime.now(UTC) - timedelta(seconds=1)
            ).isoformat()
        else:
            payload["validator_hotkey"] = _OTHER_KEYPAIR.ss58_address
            header_hotkey = _OTHER_KEYPAIR.ss58_address

        response = await _prepare(
            client,
            bundle_id=path_bundle_id,
            payload=payload,
            header_hotkey=header_hotkey,
        )

        assert response.status_code == 401, response.text
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_native_wire_tamper_is_rejected_before_normalization(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, _, ticket = await _seed_claimed_go_case(app, client, session_maker)
        payload = _prepare_payload(
            bundle_id=seeded.bundle_id,
            ticket_id=ticket.ticket_id,
        )
        longmem = payload["longmemeval"]
        assert isinstance(longmem, dict)
        longmem["latency_ms"] = 1

        response = await _prepare(client, bundle_id=seeded.bundle_id, payload=payload)

        assert response.status_code == 401
        assert response.json()["error_code"] == 4000
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_prepare_nonce_is_one_shot(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, _, ticket = await _seed_claimed_go_case(app, client, session_maker)
        payload = _prepare_payload(
            bundle_id=seeded.bundle_id,
            ticket_id=ticket.ticket_id,
        )

        accepted = await _prepare(client, bundle_id=seeded.bundle_id, payload=payload)
        replay = await _prepare(client, bundle_id=seeded.bundle_id, payload=payload)

        assert accepted.status_code == 200, accepted.text
        assert replay.status_code == 409
        assert "nonce has already been used" in replay.text
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_stale_prepare_is_rejected_without_consuming_work(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, _, ticket = await _seed_claimed_go_case(app, client, session_maker)
        payload = _prepare_payload(
            bundle_id=seeded.bundle_id,
            ticket_id=ticket.ticket_id,
            requested_at=datetime.now(UTC) - timedelta(minutes=6),
        )

        response = await _prepare(client, bundle_id=seeded.bundle_id, payload=payload)

        assert response.status_code == 409
        assert "prepare is stale" in response.text
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_prepare_header_must_equal_signed_hotkey(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, _, ticket = await _seed_claimed_go_case(app, client, session_maker)
        payload = _prepare_payload(
            bundle_id=seeded.bundle_id,
            ticket_id=ticket.ticket_id,
        )

        response = await _prepare(
            client,
            bundle_id=seeded.bundle_id,
            payload=payload,
            header_hotkey=_OTHER_KEYPAIR.ss58_address,
        )

        assert response.status_code == 401
        assert response.json()["error_code"] == 4000
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_correctly_signed_wrong_ticket_cannot_prepare_bundle(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, _, _ = await _seed_claimed_go_case(app, client, session_maker)
        payload = _prepare_payload(
            bundle_id=seeded.bundle_id,
            ticket_id=uuid4(),
        )

        response = await _prepare(client, bundle_id=seeded.bundle_id, payload=payload)

        assert response.status_code == 409
        assert "does not match a live internal ticket" in response.text
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_expired_ticket_cannot_prepare_bundle(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, _, ticket = await _seed_claimed_go_case(app, client, session_maker)
        async with session_maker() as session, session.begin():
            stored = await session.get(ConfirmationBundleTicket, ticket.ticket_id)
            assert stored is not None
            stored.issued_at = datetime.now(UTC) - timedelta(hours=2)
            stored.deadline = datetime.now(UTC) - timedelta(hours=1)
        payload = _prepare_payload(
            bundle_id=seeded.bundle_id,
            ticket_id=ticket.ticket_id,
        )

        response = await _prepare(client, bundle_id=seeded.bundle_id, payload=payload)

        assert response.status_code == 409
        assert "does not match a live internal ticket" in response.text
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_unregistered_bundle_profile_cannot_prepare(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, _, ticket = await _seed_claimed_go_case(app, client, session_maker)
        app.state.confirmation_verification_profiles = {}
        payload = _prepare_payload(
            bundle_id=seeded.bundle_id,
            ticket_id=ticket.ticket_id,
        )

        response = await _prepare(client, bundle_id=seeded.bundle_id, payload=payload)

        assert response.status_code == 409
        assert "profile is not registered" in response.text
        await _assert_unsettled(session_maker, seeded=seeded)

    @pytest.mark.parametrize(
        "drift", ["extra_wrapper", "missing_binding", "legacy_schema", "micros"]
    )
    async def test_strict_native_go_wrapper_drift_is_rejected_after_authentication(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        drift: str,
    ) -> None:
        seeded, _, ticket = await _seed_claimed_go_case(app, client, session_maker)
        fixture = _go_fixture()
        longmem = fixture["longmemeval"]
        inference = fixture["inference_ablation"]
        assert isinstance(longmem, dict)
        assert isinstance(inference, dict)
        longmem_evidence = longmem["evidence"]
        inference_evidence = inference["evidence"]
        assert isinstance(longmem_evidence, dict)
        assert isinstance(inference_evidence, dict)
        if drift == "extra_wrapper":
            longmem["producer_version"] = "unregistered"
        elif drift == "missing_binding":
            del inference_evidence["selected_cases_sha256"]
        elif drift == "legacy_schema":
            longmem_evidence["schema_version"] = 1
        else:
            score = longmem_evidence["score"]
            assert isinstance(score, dict)
            del score["longmem_mean"]
            score["longmem_mean_micros"] = 500_000
        payload = _prepare_payload(
            bundle_id=seeded.bundle_id,
            ticket_id=ticket.ticket_id,
            fixture=fixture,
        )

        response = await _prepare(client, bundle_id=seeded.bundle_id, payload=payload)

        assert response.status_code == (422 if drift == "extra_wrapper" else 409), (
            response.text
        )
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_prepare_returns_canonical_typed_root_without_settlement(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, bundle, ticket = await _seed_claimed_go_case(app, client, session_maker)
        fixture = _go_fixture()
        payload = _prepare_payload(
            bundle_id=seeded.bundle_id,
            ticket_id=ticket.ticket_id,
            fixture=fixture,
        )

        response = await _prepare(client, bundle_id=seeded.bundle_id, payload=payload)

        assert response.status_code == 200, response.text
        body = response.json()
        longmemeval = fixture["longmemeval"]
        inference_ablation = fixture["inference_ablation"]
        embedding_ablation = fixture["embedding_ablation"]
        assert isinstance(longmemeval, dict)
        assert isinstance(inference_ablation, dict)
        assert isinstance(embedding_ablation, dict)
        normalized = completion_report_from_go_dimensions(
            ablation_coordinator_latency_ms=_ABLATION_COORDINATOR_LATENCY_MS,
            longmemeval=longmemeval,
            inference_ablation=inference_ablation,
            embedding_ablation=embedding_ablation,
        )
        verified = rebuild_confirmation_evidence(
            normalized,
            artifact_sha256=bundle.artifact_sha256,
            profile_revision=bundle.profile_revision,
            profile_checksum=bundle.profile_checksum,
            settings_revision=bundle.settings_revision,
            settings_checksum=bundle.settings_checksum,
            retest_generation=bundle.retest_generation,
            mode=ConfirmationBundleMode.SHADOW,
            profile=go_verification_profile(),
        )
        assert body == {
            "bundle_id": str(bundle.bundle_id),
            "ticket_id": str(ticket.ticket_id),
            "ablation_coordinator_latency_ms": _ABLATION_COORDINATOR_LATENCY_MS,
            "longmemeval": normalized.longmemeval.model_dump(mode="json"),
            "inference_ablation": normalized.inference_ablation.model_dump(mode="json"),
            "embedding_ablation": normalized.embedding_ablation.model_dump(mode="json"),
            "evidence_sha256": verified.evidence_sha256,
        }
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_exact_prepared_content_can_be_signed_and_submitted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, bundle, ticket = await _seed_claimed_go_case(app, client, session_maker)
        prepared_response = await _prepare(
            client,
            bundle_id=seeded.bundle_id,
            payload=_prepare_payload(
                bundle_id=seeded.bundle_id,
                ticket_id=ticket.ticket_id,
            ),
        )
        assert prepared_response.status_code == 200, prepared_response.text
        report = _signed_prepared_report(
            prepared=prepared_response.json(), bundle=bundle, ticket=ticket
        )

        submitted = await client.post(
            _REPORT_URL.format(bundle_id=seeded.bundle_id),
            json={
                "validator_hotkey": VALIDATOR_KEYPAIR.ss58_address,
                "ticket_id": str(ticket.ticket_id),
                "report": report.model_dump(mode="json"),
            },
            headers={"X-Validator-Hotkey": VALIDATOR_KEYPAIR.ss58_address},
        )

        assert submitted.status_code == 200, submitted.text
        assert (
            submitted.json()["evidence_sha256"]
            == prepared_response.json()["evidence_sha256"]
        )
        assert submitted.json()["accepted"] is True

    async def test_final_submit_revalidates_exact_prepared_content(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, bundle, ticket = await _seed_claimed_go_case(app, client, session_maker)
        prepared_response = await _prepare(
            client,
            bundle_id=seeded.bundle_id,
            payload=_prepare_payload(
                bundle_id=seeded.bundle_id,
                ticket_id=ticket.ticket_id,
            ),
        )
        assert prepared_response.status_code == 200, prepared_response.text
        prepared = prepared_response.json()
        report = _signed_prepared_report(
            prepared=prepared, bundle=bundle, ticket=ticket
        ).model_dump(mode="json")
        report["longmemeval"]["latency_ms"] += 1

        submitted = await client.post(
            _REPORT_URL.format(bundle_id=seeded.bundle_id),
            json={
                "validator_hotkey": VALIDATOR_KEYPAIR.ss58_address,
                "ticket_id": str(ticket.ticket_id),
                "report": report,
            },
            headers={"X-Validator-Hotkey": VALIDATOR_KEYPAIR.ss58_address},
        )

        assert submitted.status_code == 409
        await _assert_unsettled(session_maker, seeded=seeded)


class TestV9ConfirmationReportAdmission:
    async def test_off_revision_does_not_cancel_an_already_issued_report(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        _install_transport(app, session_maker)
        assert (await _claim(client)).status_code == 200
        bundle, ticket, _, _ = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )
        async with session_maker() as session, session.begin():
            await lock_confirmation_policy(session)
            await _append_off_revision(session, parent=seeded)

        response = await client.post(
            _REPORT_URL.format(bundle_id=seeded.bundle_id),
            json=_report_payload(bundle=bundle, ticket=ticket),
            headers={"X-Validator-Hotkey": VALIDATOR_KEYPAIR.ss58_address},
        )

        assert response.status_code == 200, response.text
        assert response.json()["accepted"] is True
        stored_bundle, stored_ticket, reservation, budget = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )
        assert stored_bundle.state == "completed"
        assert stored_ticket.status == "scored"
        assert reservation.state == "settled"
        assert budget.outstanding_reserved_microusd == 0

    async def test_wrong_ticket_cannot_settle_the_real_reservation(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        _install_transport(app, session_maker)
        assert (await _claim(client)).status_code == 200
        bundle, ticket, _, _ = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )
        payload = _report_payload(bundle=bundle, ticket=ticket)
        payload["ticket_id"] = str(uuid4())

        response = await client.post(
            _REPORT_URL.format(bundle_id=seeded.bundle_id),
            json=payload,
            headers={"X-Validator-Hotkey": VALIDATOR_KEYPAIR.ss58_address},
        )

        assert response.status_code == 409
        assert "does not match its internal ticket" in response.text
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_report_requires_the_bundle_profile_to_remain_registered(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        _install_transport(app, session_maker)
        assert (await _claim(client)).status_code == 200
        bundle, ticket, _, _ = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )
        app.state.confirmation_verification_profiles = {}

        response = await client.post(
            _REPORT_URL.format(bundle_id=seeded.bundle_id),
            json=_report_payload(bundle=bundle, ticket=ticket),
            headers={"X-Validator-Hotkey": VALIDATOR_KEYPAIR.ss58_address},
        )

        assert response.status_code == 409
        assert "profile is not registered" in response.text
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_report_header_must_equal_ticket_reporter(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        _install_transport(app, session_maker)
        assert (await _claim(client)).status_code == 200
        bundle, ticket, _, _ = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )

        response = await client.post(
            _REPORT_URL.format(bundle_id=seeded.bundle_id),
            json=_report_payload(bundle=bundle, ticket=ticket),
            headers={"X-Validator-Hotkey": _OTHER_KEYPAIR.ss58_address},
        )

        assert response.status_code == 401
        assert response.json()["error_code"] == 4000
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_evidence_tampering_rolls_back_settlement(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        _install_transport(app, session_maker)
        assert (await _claim(client)).status_code == 200
        bundle, ticket, _, _ = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )
        payload = _report_payload(bundle=bundle, ticket=ticket)
        payload["report"]["longmemeval"]["evidence"]["score"][
            "longmem_mean_micros"
        ] += 1

        response = await client.post(
            _REPORT_URL.format(bundle_id=seeded.bundle_id),
            json=payload,
            headers={"X-Validator-Hotkey": VALIDATOR_KEYPAIR.ss58_address},
        )

        assert response.status_code == 409
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_wrong_bundle_signature_rolls_back_settlement(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        _install_transport(app, session_maker)
        assert (await _claim(client)).status_code == 200
        bundle, ticket, _, _ = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )
        payload = _report_payload(bundle=bundle, ticket=ticket)
        payload["report"]["bundle_signature"] = "00"

        response = await client.post(
            _REPORT_URL.format(bundle_id=seeded.bundle_id),
            json=payload,
            headers={"X-Validator-Hotkey": VALIDATOR_KEYPAIR.ss58_address},
        )

        assert response.status_code == 409
        assert "signature did not verify" in response.text
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_bundle_budget_cap_failure_is_atomic(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        settings = active_settings(mode=ConfirmationBundleMode.SHADOW).model_copy(
            update={"per_bundle_request_cap": 2}
        )
        seeded = await _seed_bundle(session_maker, settings=settings)
        _install_transport(app, session_maker)
        claim = await _claim(client)
        assert claim.status_code == 200, claim.text
        assert claim.json()["per_bundle_request_cap"] == 2
        bundle, ticket, _, _ = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )

        response = await client.post(
            _REPORT_URL.format(bundle_id=seeded.bundle_id),
            json=_report_payload(bundle=bundle, ticket=ticket),
            headers={"X-Validator-Hotkey": VALIDATOR_KEYPAIR.ss58_address},
        )

        assert response.status_code == 409
        assert "bundle request cap exceeded" in response.text
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_success_settles_and_completes_atomically_then_replays_idempotently(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        _install_transport(app, session_maker)
        claim = await _claim(client)
        assert claim.status_code == 200, claim.text
        bundle, ticket, reservation, budget = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )
        assert reservation.state == "reserved"
        assert budget.revision == 1
        payload = _report_payload(bundle=bundle, ticket=ticket)
        url = _REPORT_URL.format(bundle_id=seeded.bundle_id)
        headers = {"X-Validator-Hotkey": VALIDATOR_KEYPAIR.ss58_address}

        accepted = await client.post(url, json=payload, headers=headers)
        replay = await client.post(url, json=payload, headers=headers)

        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["accepted"] is True
        assert accepted.json()["state"] == "completed"
        assert accepted.json()["qualification_status"] == "qualified"
        assert accepted.json()["replayed"] is False
        assert replay.status_code == 200, replay.text
        assert replay.json() == {**accepted.json(), "replayed": True}

        (
            stored_bundle,
            stored_ticket,
            stored_reservation,
            stored_budget,
        ) = await _claimed_rows(session_maker, bundle_id=seeded.bundle_id)
        assert stored_bundle.state == "completed"
        assert stored_bundle.completion_ticket_id == stored_ticket.ticket_id
        assert stored_bundle.evidence_sha256 == accepted.json()["evidence_sha256"]
        assert stored_bundle.reporter_hotkey == VALIDATOR_KEYPAIR.ss58_address
        assert stored_ticket.status == "scored"
        assert stored_reservation.state == "settled"
        assert stored_reservation.actual_microusd == 15_000
        assert stored_reservation.failed_attempt is False
        assert stored_budget.revision == 2
        assert stored_budget.issued_attempts == 1
        assert stored_budget.outstanding_reserved_microusd == 0
        assert stored_budget.settled_microusd == 15_000
        async with session_maker() as session:
            subject = await session.get(ConfirmationBundleSubject, (seeded.agent_id, 9))
            assert subject is not None
            # Completion in shadow mode persists the full projection for audit,
            # but it cannot confer reward authority until an enforce bundle does.
            assert subject.result_status == "provisional"
            assert subject.full_quality_micros is not None
            assert subject.full_effective_micros == subject.full_quality_micros
            assert subject.applied_factor_bps == 10_000
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ConfirmationDimensionEvidence)
                    .where(ConfirmationDimensionEvidence.bundle_id == seeded.bundle_id)
                )
                == 3
            )
        assert await _canonical_counts(session_maker, agent_id=seeded.agent_id) == (
            AgentStatus.SCORED,
            0,
            0,
        )


class TestOrdinarySlotsAreDisjointFromConfirmation:
    async def test_live_ordinary_slot_does_not_block_dedicated_longmem_slot(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_bundle(session_maker)
        _install_transport(app, session_maker)
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=seeded.agent_id,
                    validator_hotkey=VALIDATOR_KEYPAIR.ss58_address,
                    slot_id="slot-0",
                    status=TicketStatus.ISSUED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    issued_at=now,
                    deadline=now + timedelta(minutes=30),
                    bench_version=8,
                    attempt_count=1,
                )
            )

        response = await _claim(client)

        assert response.status_code == 200, response.text
        async with session_maker() as session:
            bundle = await session.get(ConfirmationBundle, seeded.bundle_id)
            assert bundle is not None and bundle.state == "leased"
            assert (
                await session.scalar(
                    select(func.count()).select_from(ConfirmationBundleTicket)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(ConfirmationBudgetReservation)
                )
                == 1
            )
        assert await _canonical_counts(session_maker, agent_id=seeded.agent_id) == (
            AgentStatus.SCORED,
            0,
            0,
        )


async def _seed_claimed_failure_case(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> tuple[
    SeededBundle,
    ConfirmationBundle,
    ConfirmationBundleTicket,
    ConfirmationBudgetReservation,
    ConfirmationBudgetDay,
]:
    seeded = await _seed_bundle(maker)
    _install_transport(app, maker)
    claim = await _claim(client)
    assert claim.status_code == 200, claim.text
    bundle, ticket, reservation, budget = await _claimed_rows(
        maker, bundle_id=seeded.bundle_id
    )
    return seeded, bundle, ticket, reservation, budget


class TestV9ConfirmationFailureRecovery:
    async def test_failure_signature_domain_binds_every_authoritative_field(
        self,
    ) -> None:
        bundle_id = UUID("11111111-1111-1111-1111-111111111111")
        ticket_id = UUID("22222222-2222-2222-2222-222222222222")
        nonce = UUID("33333333-3333-3333-3333-333333333333")
        requested_at = datetime(2026, 8, 8, 12, 34, 56, 789, tzinfo=UTC)

        message = v9_confirmation_fail_signing_message(
            validator_hotkey=VALIDATOR_KEYPAIR.ss58_address,
            bundle_id=bundle_id,
            ticket_id=ticket_id,
            reason="infrastructure",
            nonce=nonce,
            requested_at=requested_at,
        )

        assert (
            message
            == (
                "validator-v9-confirmation-fail:v1:"
                f"{VALIDATOR_KEYPAIR.ss58_address}:{bundle_id}:{ticket_id}:"
                f"infrastructure:{nonce}:2026-08-08T12:34:56.000789Z"
            ).encode()
        )

    async def test_same_nonce_is_rejected_but_new_nonce_replays_settlement(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, _, ticket, reservation, _ = await _seed_claimed_failure_case(
            app, client, session_maker
        )
        payload = _fail_payload(
            bundle_id=seeded.bundle_id,
            ticket_id=ticket.ticket_id,
            reason="execution_failed",
        )

        accepted = await _fail(client, bundle_id=seeded.bundle_id, payload=payload)
        nonce_replay = await _fail(client, bundle_id=seeded.bundle_id, payload=payload)
        settlement_replay = await _fail(
            client,
            bundle_id=seeded.bundle_id,
            payload=_fail_payload(
                bundle_id=seeded.bundle_id,
                ticket_id=ticket.ticket_id,
                reason="execution_failed",
            ),
        )

        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["replayed"] is False
        assert accepted.json()["settled_microusd"] == reservation.reserved_microusd
        assert nonce_replay.status_code == 409
        assert "nonce has already been used" in nonce_replay.text
        assert settlement_replay.status_code == 200, settlement_replay.text
        assert settlement_replay.json()["replayed"] is True
        _, stored_ticket, stored_reservation, budget = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )
        assert stored_ticket.status == "expired"
        assert stored_reservation.state == "settled"
        assert budget.revision == 2
        assert budget.settled_microusd == reservation.reserved_microusd

    @pytest.mark.parametrize(
        "forgery",
        ["ticket", "hotkey", "signature", "reason", "unknown_reason"],
    )
    async def test_forged_failure_cannot_close_or_charge_work(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        forgery: str,
    ) -> None:
        seeded, _, ticket, _, _ = await _seed_claimed_failure_case(
            app, client, session_maker
        )
        header_hotkey = VALIDATOR_KEYPAIR.ss58_address
        if forgery == "ticket":
            payload = _fail_payload(
                bundle_id=seeded.bundle_id,
                ticket_id=uuid4(),
            )
            expected = 409
        elif forgery == "hotkey":
            payload = _fail_payload(
                bundle_id=seeded.bundle_id,
                ticket_id=ticket.ticket_id,
                keypair=_OTHER_KEYPAIR,
            )
            header_hotkey = _OTHER_KEYPAIR.ss58_address
            expected = 409
        else:
            payload = _fail_payload(
                bundle_id=seeded.bundle_id,
                ticket_id=ticket.ticket_id,
            )
            if forgery == "signature":
                payload["signature"] = "00"
                expected = 401
            elif forgery == "reason":
                payload["reason"] = "cancelled"
                expected = 401
            else:
                payload["reason"] = "miner_fault"
                expected = 422

        response = await _fail(
            client,
            bundle_id=seeded.bundle_id,
            payload=payload,
            header_hotkey=header_hotkey,
        )

        assert response.status_code == expected, response.text
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_failure_header_cannot_name_a_different_validator(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, _, ticket, _, _ = await _seed_claimed_failure_case(
            app, client, session_maker
        )
        response = await _fail(
            client,
            bundle_id=seeded.bundle_id,
            payload=_fail_payload(
                bundle_id=seeded.bundle_id,
                ticket_id=ticket.ticket_id,
            ),
            header_hotkey=_OTHER_KEYPAIR.ss58_address,
        )

        assert response.status_code == 401
        assert response.json()["error_code"] == 4000
        await _assert_unsettled(session_maker, seeded=seeded)

    async def test_unknown_partial_cost_is_charged_at_reservation_ceiling(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        (
            seeded,
            _,
            ticket,
            reservation,
            initial_budget,
        ) = await _seed_claimed_failure_case(app, client, session_maker)

        response = await _fail(
            client,
            bundle_id=seeded.bundle_id,
            payload=_fail_payload(
                bundle_id=seeded.bundle_id,
                ticket_id=ticket.ticket_id,
                reason="infrastructure",
            ),
        )

        assert response.status_code == 200, response.text
        assert response.json()["settled_microusd"] == reservation.reserved_microusd
        bundle, stored_ticket, stored_reservation, budget = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )
        assert bundle.state == "failed"
        assert stored_ticket.status == "expired"
        assert stored_ticket.failure_reason == "confirmation_infrastructure"
        assert stored_reservation.state == "settled"
        assert stored_reservation.actual_microusd == reservation.reserved_microusd
        assert stored_reservation.failed_attempt is True
        assert budget.revision == initial_budget.revision + 1
        assert budget.issued_attempts == 1
        assert budget.outstanding_reserved_microusd == 0
        assert budget.settled_microusd == reservation.reserved_microusd
        assert await _canonical_counts(session_maker, agent_id=seeded.agent_id) == (
            AgentStatus.SCORED,
            0,
            0,
        )

    async def test_failed_attempt_releases_slot_and_budget_for_a_bounded_retry(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        (
            seeded,
            _,
            first_ticket,
            first_reservation,
            _,
        ) = await _seed_claimed_failure_case(app, client, session_maker)
        failed = await _fail(
            client,
            bundle_id=seeded.bundle_id,
            payload=_fail_payload(
                bundle_id=seeded.bundle_id,
                ticket_id=first_ticket.ticket_id,
            ),
        )
        assert failed.status_code == 200, failed.text

        reclaimed = await _claim(client, payload=_claim_payload(slot_id="longmem-0"))

        assert reclaimed.status_code == 200, reclaimed.text
        assert reclaimed.json()["ticket_id"] != str(first_ticket.ticket_id)
        assert reclaimed.json()["slot_id"] == "longmem-0"
        async with session_maker() as session:
            tickets = list(
                await session.scalars(
                    select(ConfirmationBundleTicket)
                    .where(ConfirmationBundleTicket.bundle_id == seeded.bundle_id)
                    .order_by(ConfirmationBundleTicket.attempt)
                )
            )
            reservations = list(
                await session.scalars(
                    select(ConfirmationBudgetReservation)
                    .where(ConfirmationBudgetReservation.bundle_id == seeded.bundle_id)
                    .order_by(ConfirmationBudgetReservation.attempt)
                )
            )
            budget = await session.get(ConfirmationBudgetDay, reservations[0].utc_day)
        assert [(row.attempt, row.status) for row in tickets] == [
            (1, "expired"),
            (2, "issued"),
        ]
        assert [row.state for row in reservations] == ["settled", "reserved"]
        assert budget is not None
        assert budget.revision == 3
        assert budget.issued_attempts == 2
        assert budget.settled_microusd == first_reservation.reserved_microusd
        assert budget.outstanding_reserved_microusd == reservations[1].reserved_microusd

    async def test_crash_expiry_is_pessimistic_and_idempotent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, _, ticket, reservation, _ = await _seed_claimed_failure_case(
            app, client, session_maker
        )
        now = ticket.deadline + timedelta(seconds=1)

        async with session_maker() as session, session.begin():
            first = await expire_overdue_confirmation_bundle_tickets(session, now=now)
        async with session_maker() as session, session.begin():
            replay = await expire_overdue_confirmation_bundle_tickets(
                session, now=now + timedelta(seconds=1)
            )

        assert first == 1
        assert replay == 0
        bundle, stored_ticket, stored_reservation, budget = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )
        assert bundle.state == "failed"
        assert stored_ticket.status == "expired"
        assert stored_ticket.failure_reason == "confirmation_failed"
        assert stored_reservation.state == "settled"
        assert stored_reservation.actual_microusd == reservation.reserved_microusd
        assert stored_reservation.failed_attempt is True
        assert budget.revision == 2
        assert budget.outstanding_reserved_microusd == 0
        assert budget.settled_microusd == reservation.reserved_microusd

    async def test_concurrent_failure_and_expiry_settle_exactly_once(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, _, ticket, reservation, _ = await _seed_claimed_failure_case(
            app, client, session_maker
        )
        now = ticket.deadline + timedelta(seconds=1)
        # Force the exact historical deadlock interleaving: recovery owns the
        # ticket while /fail starts. With the old bundle-first fail order, fail
        # then owned the bundle and each transaction waited on the other. The
        # shared ticket->reservation->budget->bundle order leaves the bundle
        # free for recovery, after which the fail request replays exactly once.
        async with session_maker() as recovery_session, recovery_session.begin():
            locked = await recovery_session.scalar(
                select(ConfirmationBundleTicket)
                .where(ConfirmationBundleTicket.ticket_id == ticket.ticket_id)
                .with_for_update()
            )
            assert locked is not None
            fail_task = asyncio.create_task(
                _fail(
                    client,
                    bundle_id=seeded.bundle_id,
                    payload=_fail_payload(
                        bundle_id=seeded.bundle_id,
                        ticket_id=ticket.ticket_id,
                        reason="infrastructure",
                    ),
                )
            )
            await asyncio.sleep(0.05)
            assert not fail_task.done()
            swept = await expire_overdue_confirmation_bundle_tickets(
                recovery_session, now=now
            )
        failure = await fail_task

        assert failure.status_code == 200, failure.text
        assert swept == 1
        assert failure.json()["replayed"] is True
        bundle, stored_ticket, stored_reservation, budget = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )
        assert bundle.state == "failed"
        assert stored_ticket.status == "expired"
        assert stored_reservation.state == "settled"
        assert stored_reservation.actual_microusd == reservation.reserved_microusd
        assert budget.revision == 2
        assert budget.issued_attempts == 1
        assert budget.outstanding_reserved_microusd == 0
        assert budget.settled_microusd == reservation.reserved_microusd

    @pytest.mark.parametrize("operation", ("prepare", "submit", "recovery"))
    async def test_claim_waits_at_budget_before_attempt_bundle_lock(
        self,
        operation: str,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Force the former claim/attempt deadlock and prove exact settlement.

        The attempt transaction owns ticket -> reservation -> budget but has
        not reached its bundle. A claim for another slot must wait on that
        budget before reconciliation can lock the leased bundle. If claim ever
        regresses to bundle -> budget, the final bundle lock below closes the
        historical cycle and PostgreSQL aborts one side as a deadlock.
        """
        seeded, bundle, ticket = await _seed_claimed_go_case(app, client, session_maker)
        profile = go_verification_profile()
        pending = await _seed_pending_bundle_on_revision(
            session_maker,
            parent=seeded,
            artifact_sha256="d" * 64,
            profile=profile,
        )
        prepared = await _prepare(
            client,
            bundle_id=seeded.bundle_id,
            payload=_prepare_payload(
                bundle_id=seeded.bundle_id,
                ticket_id=ticket.ticket_id,
            ),
        )
        assert prepared.status_code == 200, prepared.text
        report = _signed_prepared_report(
            prepared=prepared.json(), bundle=bundle, ticket=ticket
        )
        claim_payload = _claim_payload(
            slot_id="longmem-1",
            profile_revision=profile.revision,
            profile_checksum=profile.checksum(),
        )

        async with session_maker() as attempt_session, attempt_session.begin():
            owner_pid = int(
                await attempt_session.scalar(text("SELECT pg_backend_pid()")) or 0
            )
            locked_ticket = await attempt_session.scalar(
                select(ConfirmationBundleTicket)
                .where(ConfirmationBundleTicket.ticket_id == ticket.ticket_id)
                .with_for_update()
            )
            assert locked_ticket is not None
            locked_reservation = await attempt_session.scalar(
                select(ConfirmationBudgetReservation)
                .where(
                    ConfirmationBudgetReservation.bundle_id == seeded.bundle_id,
                    ConfirmationBudgetReservation.attempt == ticket.attempt,
                )
                .with_for_update()
            )
            assert locked_reservation is not None
            locked_budget = await attempt_session.get(
                ConfirmationBudgetDay,
                locked_reservation.utc_day,
                with_for_update=True,
                populate_existing=True,
            )
            assert locked_budget is not None

            claim_task = asyncio.create_task(_claim(client, payload=claim_payload))
            await _wait_for_budget_lock_waiter(session_maker, owner_pid=owner_pid)
            assert not claim_task.done()

            locked_bundle = await attempt_session.get(
                ConfirmationBundle,
                seeded.bundle_id,
                with_for_update=True,
                populate_existing=True,
            )
            assert locked_bundle is not None
            attempt = await lock_confirmation_attempt(
                attempt_session,
                bundle_id=seeded.bundle_id,
                ticket_id=ticket.ticket_id,
            )
            assert attempt is not None

            if operation == "recovery":
                assert (
                    await expire_overdue_confirmation_bundle_tickets(
                        attempt_session,
                        now=ticket.deadline + timedelta(seconds=1),
                    )
                    == 1
                )
            elif operation == "submit":
                verified = rebuild_confirmation_evidence(
                    report,
                    artifact_sha256=attempt.bundle.artifact_sha256,
                    profile_revision=attempt.bundle.profile_revision,
                    profile_checksum=attempt.bundle.profile_checksum,
                    settings_revision=attempt.bundle.settings_revision,
                    settings_checksum=attempt.bundle.settings_checksum,
                    retest_generation=attempt.bundle.retest_generation,
                    mode=seeded.settings.mode,
                    profile=profile,
                )
                await settle_confirmation_bundle_budget(
                    attempt_session,
                    reservation_id=attempt.reservation.reservation_id,
                    expected_revision=attempt.budget.revision,
                    actual_microusd=verified.root.totals.provider_cost_microusd,
                    failed_attempt=False,
                    settled_at=datetime.now(UTC),
                )
                await complete_confirmation_bundle(
                    attempt_session,
                    bundle_id=seeded.bundle_id,
                    ticket_id=ticket.ticket_id,
                    report=report,
                    verification_profile=profile,
                    now=datetime.now(UTC),
                )
            else:
                # Prepare is read-only after taking the same lifecycle locks.
                rebuild_confirmation_evidence(
                    report,
                    artifact_sha256=attempt.bundle.artifact_sha256,
                    profile_revision=attempt.bundle.profile_revision,
                    profile_checksum=attempt.bundle.profile_checksum,
                    settings_revision=attempt.bundle.settings_revision,
                    settings_checksum=attempt.bundle.settings_checksum,
                    retest_generation=attempt.bundle.retest_generation,
                    mode=seeded.settings.mode,
                    profile=profile,
                )

        claim = await asyncio.wait_for(claim_task, timeout=5)
        assert claim.status_code == 200, claim.text
        expected_claimed = (
            seeded.bundle_id if operation == "recovery" else pending.bundle_id
        )
        assert claim.json()["bundle_id"] == str(expected_claimed)

        async with session_maker() as session:
            reservations = list(
                await session.scalars(
                    select(ConfirmationBudgetReservation).order_by(
                        ConfirmationBudgetReservation.created_at,
                        ConfirmationBudgetReservation.reservation_id,
                    )
                )
            )
            budget = await session.get(ConfirmationBudgetDay, reservations[0].utc_day)
            stored_source = await session.get(ConfirmationBundle, seeded.bundle_id)
        assert budget is not None
        assert stored_source is not None
        assert len(reservations) == 2
        settled = [row for row in reservations if row.state == "settled"]
        assert len(settled) == (0 if operation == "prepare" else 1)
        assert budget.issued_attempts == 2
        assert budget.revision == (2 if operation == "prepare" else 3)
        assert budget.outstanding_reserved_microusd == sum(
            row.reserved_microusd for row in reservations if row.state == "reserved"
        )
        if operation == "prepare":
            assert stored_source.state == "leased"
            assert budget.settled_microusd == 0
        elif operation == "recovery":
            assert stored_source.state == "leased"
            assert settled[0].failed_attempt is True
            assert budget.settled_microusd == settled[0].reserved_microusd
        else:
            assert stored_source.state == "completed"
            assert settled[0].failed_attempt is False
            assert budget.settled_microusd == settled[0].actual_microusd

    @pytest.mark.parametrize("operation", ("prepare", "submit"))
    async def test_prepare_and_submit_serialize_after_overdue_recovery(
        self,
        operation: str,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, bundle, ticket = await _seed_claimed_go_case(app, client, session_maker)
        report = None
        if operation == "submit":
            prepared = await _prepare(
                client,
                bundle_id=seeded.bundle_id,
                payload=_prepare_payload(
                    bundle_id=seeded.bundle_id,
                    ticket_id=ticket.ticket_id,
                ),
            )
            assert prepared.status_code == 200, prepared.text
            report = _signed_prepared_report(
                prepared=prepared.json(), bundle=bundle, ticket=ticket
            )

        async def request_attempt() -> httpx.Response:
            if operation == "prepare":
                return await _prepare(
                    client,
                    bundle_id=seeded.bundle_id,
                    payload=_prepare_payload(
                        bundle_id=seeded.bundle_id,
                        ticket_id=ticket.ticket_id,
                    ),
                )
            assert report is not None
            return await client.post(
                _REPORT_URL.format(bundle_id=seeded.bundle_id),
                json={
                    "validator_hotkey": VALIDATOR_KEYPAIR.ss58_address,
                    "ticket_id": str(ticket.ticket_id),
                    "report": report.model_dump(mode="json"),
                },
                headers={"X-Validator-Hotkey": VALIDATOR_KEYPAIR.ss58_address},
            )

        recovery_now = ticket.deadline + timedelta(seconds=1)
        async with session_maker() as recovery_session, recovery_session.begin():
            locked = await recovery_session.scalar(
                select(ConfirmationBundleTicket)
                .where(ConfirmationBundleTicket.ticket_id == ticket.ticket_id)
                .with_for_update()
            )
            assert locked is not None
            attempt_task = asyncio.create_task(request_attempt())
            await asyncio.sleep(0.05)
            assert not attempt_task.done()
            swept = await expire_overdue_confirmation_bundle_tickets(
                recovery_session, now=recovery_now
            )
        attempted = await attempt_task

        assert swept == 1
        assert attempted.status_code == 409, attempted.text
        stored_bundle, stored_ticket, reservation, budget = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )
        assert stored_bundle.state == "failed"
        assert stored_ticket.status == "expired"
        assert reservation.state == "settled"
        assert reservation.failed_attempt is True
        assert budget.revision == 2
        assert budget.outstanding_reserved_microusd == 0
        assert budget.settled_microusd == reservation.reserved_microusd

    @pytest.mark.parametrize("operation", ("prepare", "submit"))
    async def test_prepare_and_submit_queue_behind_cooperative_fail(
        self,
        operation: str,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded, bundle, ticket = await _seed_claimed_go_case(app, client, session_maker)
        report = None
        if operation == "submit":
            prepared = await _prepare(
                client,
                bundle_id=seeded.bundle_id,
                payload=_prepare_payload(
                    bundle_id=seeded.bundle_id,
                    ticket_id=ticket.ticket_id,
                ),
            )
            assert prepared.status_code == 200, prepared.text
            report = _signed_prepared_report(
                prepared=prepared.json(), bundle=bundle, ticket=ticket
            )

        async def request_attempt() -> httpx.Response:
            if operation == "prepare":
                return await _prepare(
                    client,
                    bundle_id=seeded.bundle_id,
                    payload=_prepare_payload(
                        bundle_id=seeded.bundle_id,
                        ticket_id=ticket.ticket_id,
                    ),
                )
            assert report is not None
            return await client.post(
                _REPORT_URL.format(bundle_id=seeded.bundle_id),
                json={
                    "validator_hotkey": VALIDATOR_KEYPAIR.ss58_address,
                    "ticket_id": str(ticket.ticket_id),
                    "report": report.model_dump(mode="json"),
                },
                headers={"X-Validator-Hotkey": VALIDATOR_KEYPAIR.ss58_address},
            )

        async with session_maker() as blocker, blocker.begin():
            owner_pid = int(await blocker.scalar(text("SELECT pg_backend_pid()")) or 0)
            locked = await blocker.scalar(
                select(ConfirmationBundleTicket)
                .where(ConfirmationBundleTicket.ticket_id == ticket.ticket_id)
                .with_for_update()
            )
            assert locked is not None
            fail_task = asyncio.create_task(
                _fail(
                    client,
                    bundle_id=seeded.bundle_id,
                    payload=_fail_payload(
                        bundle_id=seeded.bundle_id,
                        ticket_id=ticket.ticket_id,
                        reason="infrastructure",
                    ),
                )
            )
            await _wait_for_table_lock_waiters(
                session_maker,
                owner_pid=owner_pid,
                table_name="confirmation_bundle_tickets",
            )
            assert not fail_task.done()
            attempt_task = asyncio.create_task(request_attempt())
            await _wait_for_table_lock_waiters(
                session_maker,
                owner_pid=owner_pid,
                table_name="confirmation_bundle_tickets",
                minimum=2,
            )
            assert not attempt_task.done()

        failure, attempted = await asyncio.gather(fail_task, attempt_task)

        assert failure.status_code == 200, failure.text
        assert failure.json()["replayed"] is False
        assert attempted.status_code == 409, attempted.text
        stored_bundle, stored_ticket, reservation, budget = await _claimed_rows(
            session_maker, bundle_id=seeded.bundle_id
        )
        assert stored_bundle.state == "failed"
        assert stored_ticket.status == "expired"
        assert reservation.state == "settled"
        assert reservation.failed_attempt is True
        assert budget.revision == 2
        assert budget.outstanding_reserved_microusd == 0
        assert budget.settled_microusd == reservation.reserved_microusd
