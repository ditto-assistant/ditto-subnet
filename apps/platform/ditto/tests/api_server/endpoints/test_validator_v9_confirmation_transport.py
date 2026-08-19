"""Hostile transport tests for the private Bench v9 confirmation lane.

These tests deliberately use the real PostgreSQL harness.  The endpoint owns
three pieces of durable state -- its lease, its spend reservation, and its
typed completion evidence -- so a mock session would miss the cross-table and
rollback properties this protocol exists to provide.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
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
    reconcile_confirmation_candidates,
)
from ditto.api_server.confirmation_evidence import (
    ConfirmationVerificationProfile,
    confirmation_signing_message,
    rebuild_confirmation_evidence,
)
from ditto.api_server.confirmation_profile_installation import (
    installed_confirmation_verification_profiles,
)
from ditto.api_server.confirmation_wire import completion_report_from_go_dimensions
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints import inference as inference_mod
from ditto.api_server.endpoints import validator_confirmation as confirmation_mod
from ditto.api_server.endpoints.inference import _proxy_message
from ditto.api_server.endpoints.validator_confirmation import (
    v9_confirmation_claim_signing_message,
    v9_confirmation_fail_signing_message,
    v9_confirmation_prepare_signing_message,
    v9_confirmation_prepare_wire_sha256,
)
from ditto.chain.models import NeuronInfo
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
    ConfirmationInferenceGrant,
    ConfirmationInferenceRequest,
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

# The epoch these transport fixtures run on. Bundles are no longer pinned to a
# single benchmark, so the fixture states its epoch and activates it.
_BUNDLE_BENCH_VERSION = 9
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
        # A claim only leases work for the LIVE benchmark, so the epoch this
        # bundle belongs to has to be the activated one. Without this the
        # fixture builds a bundle for a superseded epoch and the claim
        # correctly returns 204.
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=_BUNDLE_BENCH_VERSION - 1,
                desired_version=_BUNDLE_BENCH_VERSION,
                status="activated",
                cohort_size=5,
                activated_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
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
            bench_version=_BUNDLE_BENCH_VERSION,
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
        # Claims only lease work for the live benchmark (see _seed_bundle).
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=_BUNDLE_BENCH_VERSION - 1,
                desired_version=_BUNDLE_BENCH_VERSION,
                status="activated",
                cohort_size=5,
                activated_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
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
        await reconcile_confirmation_candidates(
            session,
            bench_version=_BUNDLE_BENCH_VERSION,
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
    broker_public_key: str = "A" * 43,
) -> dict[str, Any]:
    profile = verification_profile()
    revision = profile_revision or profile.revision
    checksum = profile_checksum or profile.checksum()
    claim_nonce = nonce or uuid4()
    claimed_at = requested_at or datetime.now(UTC)
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


def _installed_profile_settings() -> tuple[
    ConfirmationBundleSettings, ConfirmationVerificationProfile
]:
    registry = installed_confirmation_verification_profiles()
    assert len(registry) == 1
    profile = next(iter(registry.values()))
    settings = active_settings(mode=ConfirmationBundleMode.SHADOW).model_copy(
        update={
            "daily_dollar_cap_microusd": 10_000_000,
            "per_bundle_request_cap": 10_000,
            "per_bundle_token_cap": 10_000_000,
            "profile_revision": profile.revision,
            "profile_checksum": profile.checksum(),
        }
    )
    return settings, profile


async def _claim_installed_profile_offers(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> tuple[list[dict[str, Any]], Ed25519PrivateKey]:
    settings, profile = _installed_profile_settings()
    await _seed_bundle(
        maker,
        settings=settings,
        verification_profile_override=profile,
    )
    signer = Ed25519PrivateKey.generate()
    public_key = signer.public_key().public_bytes_raw()
    broker_public_key = base64.urlsafe_b64encode(public_key).decode().rstrip("=")
    _install_transport(app, maker, profile=profile)
    response = await _claim(
        client,
        payload=_claim_payload(
            profile_revision=profile.revision,
            profile_checksum=profile.checksum(),
            broker_public_key=broker_public_key,
        ),
    )
    assert response.status_code == 200, response.text
    offers = response.json()["inference_grants"]
    return offers, signer


async def _claim_installed_profile(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> tuple[dict[str, Any], Ed25519PrivateKey]:
    offers, signer = await _claim_installed_profile_offers(app, client, maker)
    embedding = next(offer for offer in offers if offer["lane"] == "embedding")
    assert embedding["provider"] == "perplexity"
    return embedding, signer


def _confirmation_embedding_request(
    offer: dict[str, Any], signer: Ed25519PrivateKey
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "model": offer["model"],
            "input": ["private memory"],
            "dimensions": 768,
            "encoding_format": "float",
        },
        separators=(",", ":"),
    ).encode()
    grant_id = UUID(offer["grant_id"])
    generation = int(offer["generation"])
    nonce = uuid4()
    requested_at = datetime.now(UTC)
    proof = signer.sign(
        _proxy_message(
            grant_id=grant_id,
            generation=generation,
            nonce=nonce,
            requested_at=requested_at,
            body=body,
        )
    )
    return body, {
        "Authorization": f"Bearer {offer['bearer']}",
        "X-Ditto-Grant": str(grant_id),
        "X-Ditto-Generation": str(generation),
        "X-Ditto-Nonce": str(nonce),
        "X-Ditto-Requested-At": requested_at.isoformat(),
        "X-Ditto-Proof": base64.urlsafe_b64encode(proof).decode().rstrip("="),
    }


def _confirmation_chat_request(
    offer: dict[str, Any],
    signer: Ed25519PrivateKey,
    *,
    provider: dict[str, Any] | None = None,
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "model": offer["model"],
            "messages": [{"role": "user", "content": "Answer yes or no."}],
            "max_tokens": 10,
            "n": 1,
            "stream": False,
            "provider": provider
            or {
                "only": [offer["route_provider"]],
                "order": [offer["route_provider"]],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
            },
        },
        separators=(",", ":"),
    ).encode()
    grant_id = UUID(offer["grant_id"])
    generation = int(offer["generation"])
    nonce = uuid4()
    requested_at = datetime.now(UTC)
    proof = signer.sign(
        _proxy_message(
            grant_id=grant_id,
            generation=generation,
            nonce=nonce,
            requested_at=requested_at,
            body=body,
        )
    )
    return body, {
        "Authorization": f"Bearer {offer['bearer']}",
        "X-Ditto-Grant": str(grant_id),
        "X-Ditto-Generation": str(generation),
        "X-Ditto-Nonce": str(nonce),
        "X-Ditto-Requested-At": requested_at.isoformat(),
        "X-Ditto-Proof": base64.urlsafe_b64encode(proof).decode().rstrip("="),
    }


def _enable_confirmation_proxy(app: FastAPI) -> None:
    inference = replace(
        app.state.config.inference_proxy,
        enabled=True,
        openrouter_api_key="openrouter-test-key",
        perplexity_api_key=None,
    )
    app.state.config = replace(app.state.config, inference_proxy=inference)


class TestV9ConfirmationEmbeddingProxy:
    async def test_installed_profile_provider_slug_reaches_embedding_upstream(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        offer, signer = await _claim_installed_profile(app, client, session_maker)
        app.state.session_maker = session_maker
        _enable_confirmation_proxy(app)
        body, headers = _confirmation_embedding_request(offer, signer)
        upstream_calls = 0

        async def provider(request: httpx.Request) -> httpx.Response:
            nonlocal upstream_calls
            upstream_calls += 1
            return httpx.Response(
                200,
                request=request,
                json={
                    "object": "list",
                    "model": offer["model"],
                    "data": [
                        {
                            "object": "embedding",
                            "index": 0,
                            "embedding": [0.0] * 768,
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        ) as provider_client:
            app.state.inference_client = provider_client
            response = await client.post(
                "/api/v1/inference/confirmation/embeddings",
                content=body,
                headers=headers,
            )

        assert response.status_code == 200, response.text
        assert upstream_calls == 1
        assert response.json()["model"] == offer["model"]

    async def test_different_embedding_provider_is_rejected_before_upstream(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        offer, signer = await _claim_installed_profile(app, client, session_maker)
        app.state.session_maker = session_maker
        _enable_confirmation_proxy(app)
        async with session_maker() as session, session.begin():
            grant = await session.get(
                ConfirmationInferenceGrant, UUID(offer["grant_id"])
            )
            assert grant is not None
            grant.provider = "different-provider"
        body, headers = _confirmation_embedding_request(offer, signer)
        app.state.inference_client = MagicMock(
            side_effect=AssertionError("mismatched provider reached upstream")
        )

        response = await client.post(
            "/api/v1/inference/confirmation/embeddings",
            content=body,
            headers=headers,
        )

        assert response.status_code == 401, response.text
        assert "invalid confirmation proof" in response.text


class TestV9ConfirmationChatProxy:
    async def test_reader_retries_backpressure_without_widening_frozen_route(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        offers, signer = await _claim_installed_profile_offers(
            app, client, session_maker
        )
        offer = next(item for item in offers if item["lane"] == "reader")
        app.state.session_maker = session_maker
        _enable_confirmation_proxy(app)
        body, headers = _confirmation_chat_request(offer, signer)
        upstream_calls = 0
        upstream_bodies: list[bytes] = []
        sleeps: list[float] = []
        retry_backpressure_values: list[bool] = []
        original_post = inference_mod._post_provider_with_retry

        async def no_delay_post(
            provider_client: httpx.AsyncClient,
            url: str,
            *,
            payload: dict[str, Any],
            headers: dict[str, str],
            retry_backpressure: bool = True,
            retry_pre_provider_not_found_model: str | None = None,
            backpressure_max_attempts: int = inference_mod._PROVIDER_MAX_ATTEMPTS,
            require_receipt_free_backpressure: bool = False,
            receipt_free_expected_model: str = "",
            receipt_free_expected_provider: str = "",
            max_elapsed_seconds: float | None = None,
        ) -> Any:
            retry_backpressure_values.append(retry_backpressure)

            async def record_sleep(delay: float) -> None:
                sleeps.append(delay)

            return await original_post(
                provider_client,
                url,
                payload=payload,
                headers=headers,
                retry_backpressure=retry_backpressure,
                retry_pre_provider_not_found_model=(retry_pre_provider_not_found_model),
                backpressure_max_attempts=backpressure_max_attempts,
                require_receipt_free_backpressure=require_receipt_free_backpressure,
                receipt_free_expected_model=receipt_free_expected_model,
                receipt_free_expected_provider=receipt_free_expected_provider,
                max_elapsed_seconds=max_elapsed_seconds,
                sleep=record_sleep,
            )

        monkeypatch.setattr(inference_mod, "_post_provider_with_retry", no_delay_post)

        async def provider(request: httpx.Request) -> httpx.Response:
            nonlocal upstream_calls
            upstream_calls += 1
            upstream_bodies.append(request.content)
            payload = json.loads(request.content)
            assert payload["provider"] == {
                "only": ["deepinfra"],
                "order": ["deepinfra"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "zdr": True,
            }
            if upstream_calls < 7:
                retry_after = (
                    "120"
                    if upstream_calls == 1
                    else "invalid"
                    if upstream_calls == 2
                    else "1"
                )
                return httpx.Response(
                    429,
                    request=request,
                    headers={"Retry-After": retry_after},
                    json={"error": {"code": 429, "message": "rate limited"}},
                )
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "generation-reader-retried",
                    "object": "chat.completion",
                    "created": 1,
                    "model": offer["model"],
                    "provider": offer["receipt_provider"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "memory"},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 1,
                        "total_tokens": 6,
                        "cost": 0.00001,
                    },
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        ) as provider_client:
            app.state.inference_client = provider_client
            response = await client.post(
                "/api/v1/inference/confirmation/chat/completions",
                content=body,
                headers=headers,
            )

        assert response.status_code == 200, response.text
        assert upstream_calls == 7
        assert all(item == upstream_bodies[0] for item in upstream_bodies)
        assert sleeps == [60.0, 10.0, 1.0, 1.0, 1.0, 1.0]
        assert retry_backpressure_values == [True]
        async with session_maker() as session:
            request_rows = list(
                await session.scalars(
                    select(ConfirmationInferenceRequest).where(
                        ConfirmationInferenceRequest.grant_id == UUID(offer["grant_id"])
                    )
                )
            )
            grant = await session.get(
                ConfirmationInferenceGrant, UUID(offer["grant_id"])
            )
            assert len(request_rows) == 1
            request_row = request_rows[0]
            assert request_row.status == "completed"
            assert request_row.prompt_tokens == 5
            assert request_row.completion_tokens == 1
            assert request_row.cost_microusd == 10
            assert grant is not None
            assert grant.request_count == 1
            assert grant.active_requests == 0
            assert grant.prompt_tokens == 5
            assert grant.completion_tokens == 1
            assert grant.cost_microusd == 10

    async def test_reader_backpressure_exhaustion_settles_one_failed_request(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        offers, signer = await _claim_installed_profile_offers(
            app, client, session_maker
        )
        offer = next(item for item in offers if item["lane"] == "reader")
        app.state.session_maker = session_maker
        _enable_confirmation_proxy(app)
        body, headers = _confirmation_chat_request(offer, signer)
        upstream_calls = 0
        original_post = inference_mod._post_provider_with_retry

        async def no_delay_post(
            provider_client: httpx.AsyncClient,
            url: str,
            *,
            payload: dict[str, Any],
            headers: dict[str, str],
            retry_backpressure: bool = True,
            retry_pre_provider_not_found_model: str | None = None,
            backpressure_max_attempts: int = inference_mod._PROVIDER_MAX_ATTEMPTS,
            require_receipt_free_backpressure: bool = False,
            receipt_free_expected_model: str = "",
            receipt_free_expected_provider: str = "",
            max_elapsed_seconds: float | None = None,
        ) -> Any:
            async def no_sleep(_: float) -> None:
                return None

            return await original_post(
                provider_client,
                url,
                payload=payload,
                headers=headers,
                retry_backpressure=retry_backpressure,
                retry_pre_provider_not_found_model=retry_pre_provider_not_found_model,
                backpressure_max_attempts=backpressure_max_attempts,
                require_receipt_free_backpressure=require_receipt_free_backpressure,
                receipt_free_expected_model=receipt_free_expected_model,
                receipt_free_expected_provider=receipt_free_expected_provider,
                max_elapsed_seconds=max_elapsed_seconds,
                sleep=no_sleep,
            )

        monkeypatch.setattr(inference_mod, "_post_provider_with_retry", no_delay_post)

        async def provider(request: httpx.Request) -> httpx.Response:
            nonlocal upstream_calls
            upstream_calls += 1
            return httpx.Response(
                429,
                request=request,
                headers={"Retry-After": "1"},
                json={"error": {"code": 429, "message": "rate limited"}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        ) as provider_client:
            app.state.inference_client = provider_client
            response = await client.post(
                "/api/v1/inference/confirmation/chat/completions",
                content=body,
                headers=headers,
            )

        assert response.status_code == 502, response.text
        assert upstream_calls == 7
        async with session_maker() as session:
            request_rows = list(
                await session.scalars(
                    select(ConfirmationInferenceRequest).where(
                        ConfirmationInferenceRequest.grant_id == UUID(offer["grant_id"])
                    )
                )
            )
            grant = await session.get(
                ConfirmationInferenceGrant, UUID(offer["grant_id"])
            )
            assert len(request_rows) == 1
            request_row = request_rows[0]
            assert request_row.status == "failed"
            assert request_row.prompt_tokens == request_row.reserved_tokens
            assert request_row.completion_tokens == 0
            assert request_row.cost_microusd == 0
            assert request_row.upstream_provider is None
            assert grant is not None
            assert grant.request_count == 1
            assert grant.active_requests == 0
            assert grant.prompt_tokens == request_row.reserved_tokens
            assert grant.completion_tokens == 0
            assert grant.cost_microusd == 0

    async def test_reader_does_not_retry_ambiguous_backpressure_shapes(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        offers, signer = await _claim_installed_profile_offers(
            app, client, session_maker
        )
        offer = next(item for item in offers if item["lane"] == "reader")
        app.state.session_maker = session_maker
        _enable_confirmation_proxy(app)
        responses = [
            {
                "error": {"code": 429, "message": "rate limited"},
                "billing": {"amount_microusd": 0},
            },
            {
                "error": {"code": 429, "message": "rate limited"},
                "openrouter_metadata": {
                    "requested": offer["model"],
                    "strategy": "direct",
                    "attempt": 0,
                    "endpoints": {
                        "total": 1,
                        "available": [
                            {
                                "provider": "Azure",
                                "model": offer["model"],
                                "selected": False,
                            }
                        ],
                    },
                },
            },
        ]
        upstream_calls = 0

        async def provider(request: httpx.Request) -> httpx.Response:
            nonlocal upstream_calls
            response_payload = responses[upstream_calls]
            upstream_calls += 1
            return httpx.Response(429, request=request, json=response_payload)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        ) as provider_client:
            app.state.inference_client = provider_client
            for expected_calls in range(1, len(responses) + 1):
                body, headers = _confirmation_chat_request(offer, signer)
                response = await client.post(
                    "/api/v1/inference/confirmation/chat/completions",
                    content=body,
                    headers=headers,
                )
                assert response.status_code == 502, response.text
                assert upstream_calls == expected_calls

        async with session_maker() as session:
            request_rows = list(
                await session.scalars(
                    select(ConfirmationInferenceRequest).where(
                        ConfirmationInferenceRequest.grant_id == UUID(offer["grant_id"])
                    )
                )
            )
            grant = await session.get(
                ConfirmationInferenceGrant, UUID(offer["grant_id"])
            )
            assert len(request_rows) == 2
            assert all(row.status == "failed" for row in request_rows)
            assert grant is not None
            assert grant.request_count == 2
            assert grant.active_requests == 0
            assert grant.completion_tokens == 0
            assert grant.cost_microusd == 0

    async def test_reader_retries_only_documented_pre_provider_route_miss(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        offers, signer = await _claim_installed_profile_offers(
            app, client, session_maker
        )
        offer = next(item for item in offers if item["lane"] == "reader")
        app.state.session_maker = session_maker
        _enable_confirmation_proxy(app)
        body, headers = _confirmation_chat_request(offer, signer)
        attempts: list[bytes] = []
        sleeps: list[float] = []
        original_post = inference_mod._post_provider_with_retry

        async def no_delay_post(
            provider_client: httpx.AsyncClient,
            url: str,
            *,
            payload: dict[str, Any],
            headers: dict[str, str],
            retry_backpressure: bool = True,
            retry_pre_provider_not_found_model: str | None = None,
            backpressure_max_attempts: int = inference_mod._PROVIDER_MAX_ATTEMPTS,
            require_receipt_free_backpressure: bool = False,
            receipt_free_expected_model: str = "",
            receipt_free_expected_provider: str = "",
            max_elapsed_seconds: float | None = None,
        ) -> Any:
            async def record_sleep(delay: float) -> None:
                sleeps.append(delay)

            return await original_post(
                provider_client,
                url,
                payload=payload,
                headers=headers,
                retry_backpressure=retry_backpressure,
                retry_pre_provider_not_found_model=(retry_pre_provider_not_found_model),
                backpressure_max_attempts=backpressure_max_attempts,
                require_receipt_free_backpressure=require_receipt_free_backpressure,
                receipt_free_expected_model=receipt_free_expected_model,
                receipt_free_expected_provider=receipt_free_expected_provider,
                max_elapsed_seconds=max_elapsed_seconds,
                sleep=record_sleep,
            )

        monkeypatch.setattr(inference_mod, "_post_provider_with_retry", no_delay_post)

        async def provider(request: httpx.Request) -> httpx.Response:
            attempts.append(request.content)
            payload = json.loads(request.content)
            assert payload["model"] == offer["model"]
            assert payload["provider"] == {
                "only": ["deepinfra"],
                "order": ["deepinfra"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "zdr": True,
            }
            if len(attempts) == 1:
                return httpx.Response(
                    404,
                    request=request,
                    headers={"Retry-After": "120"},
                    json={
                        "error": {
                            "code": 404,
                            "message": (
                                "No allowed providers are available for the "
                                "selected model"
                            ),
                        },
                        "openrouter_metadata": {
                            "requested": offer["model"],
                            "strategy": "direct",
                            "attempt": 0,
                            "endpoints": {
                                "total": 1,
                                "available": [
                                    {
                                        "provider": "DeepInfra",
                                        "model": offer["model"],
                                        "selected": False,
                                    }
                                ],
                            },
                        },
                    },
                )
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "generation-reader-route-recovered",
                    "object": "chat.completion",
                    "created": 1,
                    "model": offer["model"],
                    "provider": offer["receipt_provider"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "memory"},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 1,
                        "total_tokens": 6,
                        "cost": 0.00001,
                    },
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        ) as provider_client:
            app.state.inference_client = provider_client
            response = await client.post(
                "/api/v1/inference/confirmation/chat/completions",
                content=body,
                headers=headers,
            )

        assert response.status_code == 200, response.text
        assert len(attempts) == 2
        assert attempts[0] == attempts[1]
        assert sleeps == [0.25]
        async with session_maker() as session:
            request_rows = list(
                await session.scalars(
                    select(ConfirmationInferenceRequest).where(
                        ConfirmationInferenceRequest.grant_id == UUID(offer["grant_id"])
                    )
                )
            )
            grant = await session.get(
                ConfirmationInferenceGrant, UUID(offer["grant_id"])
            )
            assert len(request_rows) == 1
            request_row = request_rows[0]
            assert request_row.status == "completed"
            assert request_row.prompt_tokens == 5
            assert request_row.completion_tokens == 1
            assert request_row.cost_microusd == 10
            assert grant is not None
            assert grant.request_count == 1
            assert grant.active_requests == 0
            assert grant.prompt_tokens == 5
            assert grant.completion_tokens == 1
            assert grant.cost_microusd == 10

    async def test_pre_provider_route_miss_classifier_fails_closed(self) -> None:
        model = "openai/gpt-oss-20b"
        valid: dict[str, Any] = {
            "error": {"code": 404, "message": "No allowed providers"},
            "openrouter_metadata": {
                "requested": model,
                "strategy": "direct",
                "attempt": 0,
                "endpoints": {
                    "total": 1,
                    "available": [
                        {
                            "provider": "DeepInfra",
                            "model": model,
                            "selected": False,
                        }
                    ],
                },
            },
        }

        def response(payload: dict[str, Any]) -> httpx.Response:
            return httpx.Response(404, json=payload)

        assert inference_mod._is_retryable_pre_provider_not_found(
            response(valid), expected_model=model
        )
        invalid = [
            {"error": {"code": 404}},
            {**valid, "error": {"code": 404.0}},
            {**valid, "usage": {"cost": 0}},
            {**valid, "id": "gen-billed"},
            {**valid, "generation": "gen-billed"},
            {**valid, "generation_id": "gen-billed"},
            {**valid, "model": model},
            {**valid, "provider": "DeepInfra"},
            {**valid, "choices": []},
            {**valid, "cost": 0},
            {
                **valid,
                "openrouter_metadata": {
                    **valid["openrouter_metadata"],
                    "attempt": 1,
                },
            },
            {
                **valid,
                "openrouter_metadata": {
                    **valid["openrouter_metadata"],
                    "attempt": 0.0,
                },
            },
            {
                **valid,
                "openrouter_metadata": {
                    **valid["openrouter_metadata"],
                    "attempts": [],
                },
            },
            {
                **valid,
                "openrouter_metadata": {
                    **valid["openrouter_metadata"],
                    "requested": "other/model",
                },
            },
            {
                **valid,
                "openrouter_metadata": {
                    **valid["openrouter_metadata"],
                    "endpoints": {
                        "total": 1,
                        "available": [
                            {
                                "provider": "DeepInfra",
                                "model": model,
                                "selected": True,
                            }
                        ],
                    },
                },
            },
        ]
        assert all(
            not inference_mod._is_retryable_pre_provider_not_found(
                response(payload), expected_model=model
            )
            for payload in invalid
        )
        duplicate_bodies = [
            b'{"error":{"code":500,"code":404},"openrouter_metadata":{"requested":"openai/gpt-oss-20b","attempt":0,"endpoints":{"available":[{"selected":false}]}}}',
            b'{"error":{"code":404},"openrouter_metadata":{"requested":"other/model","requested":"openai/gpt-oss-20b","attempt":0,"endpoints":{"available":[{"selected":false}]}}}',
            b'{"error":{"code":404},"openrouter_metadata":{"requested":"openai/gpt-oss-20b","attempt":1,"attempt":0,"endpoints":{"available":[{"selected":false}]}}}',
            b'{"error":{"code":404},"openrouter_metadata":{"requested":"openai/gpt-oss-20b","attempt":0,"endpoints":{"available":[{"selected":true,"selected":false}]}}}',
        ]
        assert all(
            not inference_mod._is_retryable_pre_provider_not_found(
                httpx.Response(404, content=body), expected_model=model
            )
            for body in duplicate_bodies
        )

    async def test_confirmation_backpressure_receipt_proof_fails_closed(
        self,
    ) -> None:
        canonical_metadata = {
            "requested": "fixed/model",
            "strategy": "direct",
            "attempt": 0,
            "endpoints": {
                "total": 1,
                "available": [
                    {
                        "provider": "DeepInfra",
                        "model": "fixed/model",
                        "selected": False,
                    }
                ],
            },
        }
        valid = [
            httpx.Response(
                429, content=b'{"error":{"code":429,"message":"rate limited"}}'
            ),
            httpx.Response(
                429,
                json={
                    "error": {"code": 429, "message": "rate limited"},
                    "openrouter_metadata": canonical_metadata,
                },
            ),
        ]
        invalid = [
            b"{}",
            b'{"error":',
            b'{"error":"rate limited"}',
            b'{"error":{"code":429}}',
            b'{"error":{"code":429,"message":""}}',
            b'{"error":{"code":429,"message":"rate limited",'
            b'"provider_name":"DeepInfra"}}',
            b'{"error":{"code":429,"message":"rate limited"},'
            b'"billing":{"amount_microusd":0}}',
            b'{"error":{"code":429,"message":"rate limited"},"response_id":"r"}',
            b'{"error":{"code":429,"message":"rate limited"},"token_usage":{}}',
            b'{"error":{"code":503,"message":"rate limited"}}',
            b'{"error":"first","error":"second"}',
            b'{"error":{"code":429,"message":"rate limited"},"usage":{}}',
            b'{"error":{"code":429,"message":"rate limited","metadata":{"usage":{}}}}',
            b'{"error":{"code":429,"message":"rate limited"},"cost":0}',
            b'{"error":{"code":429,"message":"rate limited"},"generation":"gen-1"}',
            b'{"error":{"code":429,"message":"rate limited"},"generation_id":"gen-1"}',
            b'{"error":{"code":429,"message":"rate limited"},"provider":"DeepInfra"}',
            b'{"error":{"code":429,"message":"rate limited"},"choices":[]}',
            b'{"error":{"code":429,"message":"rate limited"},"receipt":{"id":"r"}}',
            b'{"error":{"code":429,"message":"rate limited"},"receipt_id":"r"}',
            b'{"error":{"code":429,"message":"rate limited"},"billed":false}',
            b'{"error":{"code":429,"message":"rate limited",'
            b'"metadata":{"charged":false}}}',
            b'{"error":{"code":429,"message":"rate limited",'
            b'"metadata":{"provider":"DeepInfra"}}}',
            b'{"error":{"code":429,"message":"rate limited","metadata":[{"cost":0}]}}',
            b'{"error":{"code":429,"message":"rate limited",'
            b'"metadata":{"receipt":"r"}}}',
            b'{"error":{"code":429,"message":"rate limited",'
            b'"metadata":[{"choices":[]}]}}',
            b'{"error":{"code":429,"message":"rate limited"},"value":NaN}',
            b'{"error":{"code":429,"message":"rate limited"},"value":Infinity}',
            b'{"error":{"code":429,"message":"rate limited"},"value":-Infinity}',
            b'{"error":{"code":429,"message":"rate limited"},'
            b'"openrouter_metadata":{"requested":"fixed/model","strategy":"direct",'
            b'"attempt":-0,"endpoints":{"total":1,"available":[{"provider":'
            b'"DeepInfra","model":"fixed/model","selected":false}]}}}',
        ]
        invalid_metadata = [
            {**canonical_metadata, "attempt": 1},
            {**canonical_metadata, "requested": "other/model"},
            {**canonical_metadata, "attempts": []},
            {
                **canonical_metadata,
                "endpoints": {
                    "total": 1,
                    "available": [
                        {
                            "provider": "DeepInfra",
                            "model": "fixed/model",
                            "selected": True,
                        }
                    ],
                },
            },
            None,
        ]
        invalid_metadata.extend(
            [
                {
                    **canonical_metadata,
                    "endpoints": {
                        "total": 1,
                        "available": [
                            {
                                "provider": "Azure",
                                "model": "fixed/model",
                                "selected": False,
                            }
                        ],
                    },
                },
                {
                    **canonical_metadata,
                    "endpoints": {
                        "total": 1,
                        "available": [
                            {
                                "provider": "DeepInfra",
                                "model": "other/model",
                                "selected": False,
                            }
                        ],
                    },
                },
            ]
        )
        assert all(
            inference_mod._provider_backpressure_is_receipt_free(
                response,
                expected_model="fixed/model",
                expected_provider="deepinfra",
            )
            for response in valid
        )
        assert all(
            not inference_mod._provider_backpressure_is_receipt_free(
                httpx.Response(429, content=body),
                expected_model="fixed/model",
                expected_provider="deepinfra",
            )
            for body in invalid
        )
        assert all(
            not inference_mod._provider_backpressure_is_receipt_free(
                httpx.Response(
                    429,
                    json={
                        "error": {"code": 429, "message": "rate limited"},
                        "openrouter_metadata": metadata,
                    },
                ),
                expected_model="fixed/model",
                expected_provider="deepinfra",
            )
            for metadata in invalid_metadata
        )

    async def test_confirmation_reader_retry_after_contract(self) -> None:
        cases = {
            None: 10.0,
            "invalid": 10.0,
            "+1": 10.0,
            "6_0": 10.0,
            "６０": 10.0,
            "0": 10.0,
            "-3": 10.0,
            "1": 1.0,
            "60": 60.0,
            "120": 60.0,
            "9223372036854775808": 60.0,
        }
        for retry_after, expected in cases.items():
            headers: httpx.Headers | dict[str, str]
            if retry_after is None:
                headers = {}
            elif retry_after.isascii():
                headers = {"Retry-After": retry_after}
            else:
                headers = httpx.Headers(
                    [(b"Retry-After", retry_after.encode())], encoding="utf-8"
                )
            response = httpx.Response(429, headers=headers)
            assert (
                inference_mod._confirmation_reader_backpressure_delay(response)
                == expected
            )
        duplicate = httpx.Response(
            429,
            headers=[("Retry-After", "1"), ("Retry-After", "60")],
        )
        assert inference_mod._confirmation_reader_backpressure_delay(duplicate) == 10.0

    async def test_judge_and_503_keep_ordinary_retry_cap(self) -> None:
        async def no_sleep(_: float) -> None:
            return None

        cases = (
            (429, 3, False, {"error": "legacy judge rate limit"}),
            (503, 7, True, {"error": {"code": 503, "message": "capacity"}}),
        )
        for status, extended_attempts, require_receipt_free, error_body in cases:
            calls = 0

            def provider_for(
                provider_status: int, provider_error: dict[str, Any]
            ) -> Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]:
                async def provider(request: httpx.Request) -> httpx.Response:
                    nonlocal calls
                    calls += 1
                    return httpx.Response(
                        provider_status,
                        request=request,
                        json=provider_error,
                    )

                return provider

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(provider_for(status, error_body))
            ) as provider_client:
                result = await inference_mod._post_provider_with_retry(
                    provider_client,
                    "https://provider.invalid/chat",
                    payload={"model": "fixed/model"},
                    headers={"Authorization": "redacted"},
                    backpressure_max_attempts=extended_attempts,
                    require_receipt_free_backpressure=require_receipt_free,
                    sleep=no_sleep,
                )
            assert result.response.status_code == status
            assert result.attempts == 3
            assert calls == 3

    async def test_receipt_bearing_429_is_never_retried(self) -> None:
        calls = 0

        async def provider(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                429,
                request=request,
                json={
                    "error": {
                        "code": 429,
                        "message": "capacity",
                        "metadata": {"usage": {"prompt_tokens": 1}},
                    }
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        ) as provider_client:
            result = await inference_mod._post_provider_with_retry(
                provider_client,
                "https://provider.invalid/chat",
                payload={"model": "fixed/model"},
                headers={"Authorization": "redacted"},
                backpressure_max_attempts=7,
                require_receipt_free_backpressure=True,
            )
        assert result.response.status_code == 429
        assert result.attempts == 1
        assert calls == 1

    async def test_reader_backpressure_respects_hard_elapsed_deadline(self) -> None:
        calls = 0
        sleeps: list[float] = []

        async def provider(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                429,
                request=request,
                json={"error": {"code": 429, "message": "capacity"}},
            )

        async def record_sleep(delay: float) -> None:
            sleeps.append(delay)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        ) as provider_client:
            with pytest.raises(inference_mod._ProviderCallError) as exc_info:
                await inference_mod._post_provider_with_retry(
                    provider_client,
                    "https://provider.invalid/chat",
                    payload={"model": "fixed/model"},
                    headers={"Authorization": "redacted"},
                    backpressure_max_attempts=7,
                    require_receipt_free_backpressure=True,
                    max_elapsed_seconds=1.0,
                    sleep=record_sleep,
                )
        assert exc_info.value.timed_out is True
        assert calls == 1
        assert sleeps == []

    async def test_reader_backpressure_propagates_parent_cancellation(self) -> None:
        calls = 0
        sleeping = asyncio.Event()

        async def provider(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                429,
                request=request,
                json={"error": {"code": 429, "message": "capacity"}},
            )

        async def blocked_sleep(_: float) -> None:
            sleeping.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        ) as provider_client:
            task = asyncio.create_task(
                inference_mod._post_provider_with_retry(
                    provider_client,
                    "https://provider.invalid/chat",
                    payload={"model": "fixed/model"},
                    headers={"Authorization": "redacted"},
                    backpressure_max_attempts=7,
                    require_receipt_free_backpressure=True,
                    max_elapsed_seconds=80.0,
                    sleep=blocked_sleep,
                )
            )
            await sleeping.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert calls == 1

    async def test_installed_judge_profile_reaches_zdr_azure_route(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        offers, signer = await _claim_installed_profile_offers(
            app, client, session_maker
        )
        offer = next(item for item in offers if item["lane"] == "judge")
        app.state.session_maker = session_maker
        _enable_confirmation_proxy(app)
        body, headers = _confirmation_chat_request(offer, signer)
        upstream_calls = 0

        async def provider(request: httpx.Request) -> httpx.Response:
            nonlocal upstream_calls
            upstream_calls += 1
            payload = json.loads(request.content)
            assert payload["provider"] == {
                "only": ["azure"],
                "order": ["azure"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "zdr": True,
            }
            assert payload["max_completion_tokens"] == 10
            assert "max_tokens" not in payload
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "generation-judge-1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": offer["model"],
                    "provider": offer["receipt_provider"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "yes"},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 1,
                        "total_tokens": 6,
                        "cost": 0.00001,
                    },
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        ) as provider_client:
            app.state.inference_client = provider_client
            response = await client.post(
                "/api/v1/inference/confirmation/chat/completions",
                content=body,
                headers=headers,
            )

        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"] == "yes"
        assert upstream_calls == 1
        async with session_maker() as session:
            request_row = await session.scalar(
                select(ConfirmationInferenceRequest).where(
                    ConfirmationInferenceRequest.grant_id == UUID(offer["grant_id"])
                )
            )
            assert request_row is not None
            assert request_row.status == "completed"
            assert request_row.upstream_provider == "Azure"

    async def test_installed_reader_profile_reaches_zdr_deepinfra_route(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        offers, signer = await _claim_installed_profile_offers(
            app, client, session_maker
        )
        offer = next(item for item in offers if item["lane"] == "reader")
        app.state.session_maker = session_maker
        _enable_confirmation_proxy(app)
        body, headers = _confirmation_chat_request(offer, signer)

        async def provider(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["provider"] == {
                "only": ["deepinfra"],
                "order": ["deepinfra"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "zdr": True,
            }
            assert payload["max_tokens"] == 10
            assert "max_completion_tokens" not in payload
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "generation-reader-1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": offer["model"],
                    "provider": offer["receipt_provider"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "memory"},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 1,
                        "total_tokens": 6,
                        "cost": 0.00001,
                    },
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        ) as provider_client:
            app.state.inference_client = provider_client
            response = await client.post(
                "/api/v1/inference/confirmation/chat/completions",
                content=body,
                headers=headers,
            )

        assert response.status_code == 200, response.text

    async def test_caller_cannot_override_platform_zdr_policy(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        offers, signer = await _claim_installed_profile_offers(
            app, client, session_maker
        )
        offer = next(item for item in offers if item["lane"] == "judge")
        app.state.session_maker = session_maker
        _enable_confirmation_proxy(app)
        body, headers = _confirmation_chat_request(
            offer,
            signer,
            provider={
                "only": ["azure"],
                "order": ["azure"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "zdr": True,
            },
        )
        app.state.inference_client = MagicMock(
            side_effect=AssertionError("unfrozen provider policy reached upstream")
        )

        response = await client.post(
            "/api/v1/inference/confirmation/chat/completions",
            content=body,
            headers=headers,
        )

        assert response.status_code == 403, response.text
        assert "confirmation route is not permitted" in response.text


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
    failure_class: str | None = None,
    failure_stage: str | None = None,
    sign_class: str | None = None,
    sign_stage: str | None = None,
    nonce: UUID | None = None,
    requested_at: datetime | None = None,
    keypair: bittensor.Keypair = VALIDATOR_KEYPAIR,
) -> dict[str, Any]:
    """Build a hand-back, optionally signing diagnostics other than it sends.

    ``sign_class``/``sign_stage`` default to the sent values; passing them
    explicitly produces a payload whose diagnostics were never signed, which is
    what a tampered or fabricated diagnostic looks like on the wire.
    """
    failure_nonce = nonce or uuid4()
    failed_at = requested_at or datetime.now(UTC)
    signature = keypair.sign(
        v9_confirmation_fail_signing_message(
            validator_hotkey=keypair.ss58_address,
            bundle_id=bundle_id,
            ticket_id=ticket_id,
            reason=reason,
            failure_class=sign_class if sign_class is not None else failure_class,
            failure_stage=sign_stage if sign_stage is not None else failure_stage,
            nonce=failure_nonce,
            requested_at=failed_at,
        )
    ).hex()
    payload: dict[str, Any] = {
        "validator_hotkey": keypair.ss58_address,
        "ticket_id": str(ticket_id),
        "reason": reason,
        "nonce": str(failure_nonce),
        "requested_at": failed_at.isoformat(),
        "signature": signature,
    }
    if failure_class is not None:
        payload["failure_class"] = failure_class
        payload["failure_stage"] = failure_stage
    return payload


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
            confirmation_mod, "reconcile_confirmation_candidates", reconcile
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
            await reconcile_confirmation_candidates(
                session,
                bench_version=_BUNDLE_BENCH_VERSION,
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
        assert body["per_bundle_request_cap"] == 1_100
        assert body["per_bundle_token_cap"] == 1_010_000
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
    async def test_native_go_semantic_drift_is_rejected_but_additive_wrapper_is_ignored(
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

        assert response.status_code == (200 if drift == "extra_wrapper" else 409), (
            response.text
        )
        if drift == "extra_wrapper":
            assert "producer_version" not in response.json()["longmemeval"]
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

    async def test_underfunded_bundle_caps_block_issuance_atomically(
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
        assert claim.status_code == 204, claim.text
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

    async def test_diagnostic_message_binds_class_and_stage_without_breaking_v1(
        self,
    ) -> None:
        """v1 stays byte-identical; diagnostics get their own bound message."""
        bundle_id = UUID("11111111-1111-1111-1111-111111111111")
        ticket_id = UUID("22222222-2222-2222-2222-222222222222")
        nonce = UUID("33333333-3333-3333-3333-333333333333")
        requested_at = datetime(2026, 8, 8, 12, 34, 56, 789, tzinfo=UTC)

        def message(**diagnostics: str) -> bytes:
            return v9_confirmation_fail_signing_message(
                validator_hotkey=VALIDATOR_KEYPAIR.ss58_address,
                bundle_id=bundle_id,
                ticket_id=ticket_id,
                reason="execution_failed",
                nonce=nonce,
                requested_at=requested_at,
                **diagnostics,
            )

        assert message().startswith(b"validator-v9-confirmation-fail:v1:")
        assert (
            message(failure_class="dittobench", failure_stage="running_confirmation")
            == (
                "validator-v9-confirmation-fail:v2:"
                f"{VALIDATOR_KEYPAIR.ss58_address}:{bundle_id}:{ticket_id}:"
                f"execution_failed:dittobench:running_confirmation:{nonce}:"
                "2026-08-08T12:34:56.000789Z"
            ).encode()
        )

        with pytest.raises(ValueError, match="travel together"):
            message(failure_class="dittobench")

    async def test_signed_failure_diagnostics_are_persisted_for_operators(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The whole point of the change: the cause survives the hand-back.

        Without this, every attempt reads as ``confirmation_execution_failed``
        and a repeatable lane break is indistinguishable from a transient one.
        """
        seeded, _, ticket, _, _ = await _seed_claimed_failure_case(
            app, client, session_maker
        )

        response = await _fail(
            client,
            bundle_id=seeded.bundle_id,
            payload=_fail_payload(
                bundle_id=seeded.bundle_id,
                ticket_id=ticket.ticket_id,
                failure_class="sandbox_oom",
                failure_stage="running_confirmation",
            ),
        )

        assert response.status_code == 200, response.text
        async with session_maker() as session:
            stored = await session.get(ConfirmationBundleTicket, ticket.ticket_id)
            assert stored is not None
            assert stored.failure_reason == "confirmation_execution_failed"
            assert stored.failure_class == "sandbox_oom"
            assert stored.failure_stage == "running_confirmation"

    async def test_unsigned_failure_diagnostics_are_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A diagnostic an operator acts on must be one the reporter signed."""
        seeded, _, ticket, _, _ = await _seed_claimed_failure_case(
            app, client, session_maker
        )

        response = await _fail(
            client,
            bundle_id=seeded.bundle_id,
            payload=_fail_payload(
                bundle_id=seeded.bundle_id,
                ticket_id=ticket.ticket_id,
                failure_class="sandbox_oom",
                failure_stage="running_confirmation",
                sign_class="dittobench",
                sign_stage="finalizing",
            ),
        )

        assert response.status_code == 401, response.text
        async with session_maker() as session:
            stored = await session.get(ConfirmationBundleTicket, ticket.ticket_id)
            assert stored is not None
            assert stored.failure_class is None

    async def test_reporter_without_diagnostics_still_hands_back(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Old producer against new consumer is the safe skew direction."""
        seeded, _, ticket, _, _ = await _seed_claimed_failure_case(
            app, client, session_maker
        )

        response = await _fail(
            client,
            bundle_id=seeded.bundle_id,
            payload=_fail_payload(
                bundle_id=seeded.bundle_id, ticket_id=ticket.ticket_id
            ),
        )

        assert response.status_code == 200, response.text
        async with session_maker() as session:
            stored = await session.get(ConfirmationBundleTicket, ticket.ticket_id)
            assert stored is not None
            assert stored.failure_reason == "confirmation_execution_failed"
            assert stored.failure_class is None
            assert stored.failure_stage is None

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

        cooling_down = await _claim(client, payload=_claim_payload(slot_id="longmem-0"))
        assert cooling_down.status_code == 204, cooling_down.text

        with patch.object(
            confirmation_mod,
            "CONFIRMATION_FAILED_REISSUE_COOLDOWN",
            timedelta(0),
        ):
            reclaimed = await _claim(
                client, payload=_claim_payload(slot_id="longmem-0")
            )

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
