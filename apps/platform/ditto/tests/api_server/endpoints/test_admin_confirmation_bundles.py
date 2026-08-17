"""Authenticated control-plane tests for v9 confirmation bundles."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.confirmation_bundles import (
    ConfirmationBundleMode,
)
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints import admin_confirmation_bundles
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    ConfirmationBudgetReservation,
    ConfirmationBundle,
    ConfirmationBundleSettingsRevision,
    ConfirmationRetestAuthorization,
    InferenceGrant,
    ValidatorTicket,
)
from ditto.db.queries.confirmation_bundles import (
    complete_confirmation_bundle,
    get_or_create_confirmation_bundle,
    issue_confirmation_bundle_ticket,
    reserve_confirmation_bundle_budget,
    settle_confirmation_bundle_budget,
)
from ditto.tests.confirmation_evidence_fixtures import (
    BASE_EVIDENCE_SHA256,
    VALIDATOR_KEYPAIR,
    active_settings,
    base_proof_kwargs,
    signed_report,
    verification_profile,
)

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_SETTINGS_URL = "/api/v1/admin/confirmation-bundle-settings"
_BUNDLES_URL = "/api/v1/admin/confirmation-bundles"
_NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)
_PROFILE = "a" * 64


@pytest.fixture
def settings_maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    return session_maker


def install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.session_maker = maker

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = session_override


def settings_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "mode": "shadow",
        "top_n": 5,
        "daily_bundle_cap": 10,
        "daily_dollar_cap_microusd": 1_000_000,
        "per_bundle_request_cap": 20,
        "per_bundle_token_cap": 2_000,
        "profile_revision": "profile-1",
        "profile_checksum": _PROFILE,
        "challenger_z": 1.64,
        "eligibility_mode": "rank",
        "min_base_score_micros": 950_000,
    }
    payload.update(overrides)
    return payload


def request_payload(
    *, expected_revision: int = 0, mode: str = "shadow", **overrides: object
) -> dict[str, object]:
    settings = settings_payload(mode=mode)
    settings.update(cast(Any, overrides.pop("settings_overrides", {})))
    payload: dict[str, object] = {
        "scope": "*",
        "expected_revision": expected_revision,
        "settings": settings,
        "reason": "operator approved bounded confirmation policy",
        "actor": "operator@example.com",
        "confirmation": f"APPLY V9 CONFIRMATION MODE {mode.upper()}",
    }
    payload.update(overrides)
    return payload


def canonical_checksum(settings: dict[str, object]) -> str:
    encoded = json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def seed_completed_bundle(
    maker: async_sessionmaker[AsyncSession],
    *,
    artifact_sha256: str = "b" * 64,
) -> tuple[UUID, UUID]:
    agent_id = uuid4()
    settings = active_settings(mode=ConfirmationBundleMode.ENFORCE)
    async with maker() as session, session.begin():
        revision = ConfirmationBundleSettingsRevision(
            parent_revision=0,
            scope="*",
            settings=settings.model_dump(mode="json"),
            checksum=canonical_checksum(settings.model_dump(mode="json")),
            reason="operator approved bounded confirmation policy",
            actor="operator@example.com",
        )
        session.add(revision)
        await session.flush()
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5Miner",
                name="candidate",
                sha256=artifact_sha256,
                status=AgentStatus.SCORED,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=_NOW,
            )
        )
        resolution = await get_or_create_confirmation_bundle(
            session,
            agent_id=agent_id,
            bench_version=9,
            **base_proof_kwargs(quality_micros=750_000, stderr_micros=20_000),
            settings_revision=revision.revision,
            settings=settings,
            verification_profile=verification_profile(),
        )
        assert resolution.bundle is not None
        bundle = resolution.bundle
        decision = await reserve_confirmation_bundle_budget(
            session,
            bundle_id=bundle.bundle_id,
            reservation_id=uuid4(),
            now=_NOW,
            expected_revision=0,
            settings_revision=revision.revision,
            settings=settings,
            reserve_microusd=50_000,
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
        await settle_confirmation_bundle_budget(
            session,
            reservation_id=decision.reservation.reservation_id,
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
    return agent_id, bundle.bundle_id


async def activate_bench_version(
    maker: async_sessionmaker[AsyncSession], version: int
) -> None:
    """Make ``version`` the live benchmark for this app.

    Calibration is scoped to the benchmark actually being confirmed, so a test
    that seeds base runs at one epoch has to say that epoch is live.
    """
    async with maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=version - 1,
                desired_version=version,
                status="activated",
                cohort_size=5,
                activated_at=_NOW,
            )
        )


async def seed_settled_base_run(
    maker: async_sessionmaker[AsyncSession], *, agent_id: UUID
) -> None:
    deadline = _NOW + timedelta(minutes=30)
    validator_hotkey = "5BaseValidator"
    async with maker() as session, session.begin():
        session.add(
            ValidatorTicket(
                agent_id=agent_id,
                validator_hotkey=validator_hotkey,
                slot_id="slot-1",
                bench_version=9,
                status=TicketStatus.SCORED,
                purpose=TicketPurpose.CANONICAL_QUORUM,
                issued_at=_NOW,
                deadline=deadline,
            )
        )
        await session.flush()
        session.add(
            InferenceGrant(
                grant_id=uuid4(),
                agent_id=agent_id,
                bench_version=9,
                validator_hotkey=validator_hotkey,
                slot_id="slot-1",
                ticket_deadline=deadline,
                expires_at=deadline,
                status="exhausted",
                generation=1,
                allowed_models=["openai/gpt-oss-20b"],
                request_budget=100,
                request_count=4,
                token_budget=10_000,
                prompt_tokens=1_000,
                completion_tokens=200,
                cost_microusd=120_000,
                embedding_model="perplexity/pplx-embed-v1-0.6b",
                embedding_profile="dittobench-v9-pplx-embed-v1-0.6b-768-v1",
                embedding_provider="Perplexity",
                embedding_dimensions=768,
                embedding_request_budget=10,
                embedding_request_count=1,
                embedding_token_budget=1_000,
                embedding_tokens=100,
                embedding_cost_microusd=10_000,
                usage_accounting_version=2,
                created_at=_NOW,
                updated_at=deadline,
            )
        )


class TestSettingsPermissionsAndDefaults:
    async def test_missing_token_is_rejected(
        self, app: FastAPI, client: httpx.AsyncClient, settings_maker
    ) -> None:
        install(app, settings_maker)
        response = await client.get(_SETTINGS_URL)
        assert response.status_code == 401

    async def test_bad_token_is_rejected(
        self, app: FastAPI, client: httpx.AsyncClient, settings_maker
    ) -> None:
        install(app, settings_maker)
        response = await client.get(
            _SETTINGS_URL, headers={"Authorization": "Bearer wrong"}
        )
        assert response.status_code == 401

    async def test_unconfigured_admin_api_fails_closed(
        self, app: FastAPI, client: httpx.AsyncClient, settings_maker
    ) -> None:
        install(app, settings_maker)
        app.state.config = replace(app.state.config, admin_api_token=None)
        response = await client.get(_SETTINGS_URL, headers=_HEADERS)
        assert response.status_code == 503

    async def test_empty_ledger_returns_exact_off_default(
        self, app: FastAPI, client: httpx.AsyncClient, settings_maker
    ) -> None:
        install(app, settings_maker)
        response = await client.get(_SETTINGS_URL, headers=_HEADERS)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["current"] == []
        assert body["history"] == []
        assert body["default"] == {
            "mode": "off",
            "eligibility_mode": "rank",
            "min_base_score_micros": 950_000,
            "top_n": 5,
            "daily_bundle_cap": 0,
            "daily_dollar_cap_microusd": 0,
            "per_bundle_request_cap": 0,
            "per_bundle_token_cap": 0,
            "profile_revision": None,
            "profile_checksum": None,
            "challenger_z": 1.64,
        }
        effective = body["effective"]
        assert effective["revision"] == 0
        assert effective["source"] == "default"
        assert effective["configured"] is False
        assert effective["issuance_active"] is False
        assert effective["checksum"] is None
        assert effective["max_top_n"] == 10

    async def test_bundle_list_is_also_admin_only(
        self, app: FastAPI, client: httpx.AsyncClient, settings_maker
    ) -> None:
        install(app, settings_maker)
        response = await client.get(_BUNDLES_URL)
        assert response.status_code == 401


class TestSettingsWrites:
    async def test_settings_append_reconciles_before_commit_with_empty_registry(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        install(app, settings_maker)
        app.state.confirmation_verification_profiles = {}
        reconcile = AsyncMock()
        monkeypatch.setattr(
            admin_confirmation_bundles,
            "reconcile_confirmation_candidates",
            reconcile,
        )

        response = await client.post(
            _SETTINGS_URL, headers=_HEADERS, json=request_payload()
        )

        assert response.status_code == 200, response.text
        reconcile.assert_awaited_once()
        awaited = reconcile.await_args
        assert awaited is not None
        assert awaited.kwargs["verification_profiles"] == {}

    async def test_audited_shadow_revision_round_trips(
        self, app: FastAPI, client: httpx.AsyncClient, settings_maker
    ) -> None:
        install(app, settings_maker)
        payload = request_payload()
        response = await client.post(_SETTINGS_URL, headers=_HEADERS, json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["revision"] == 1
        assert body["parent_revision"] == 0
        assert body["scope"] == "*"
        assert body["actor"] == "operator@example.com"
        assert body["reason"] == "operator approved bounded confirmation policy"
        expected_settings = {
            "eligibility_mode": "rank",
            "min_base_score_micros": 950_000,
            **cast(dict[str, object], payload["settings"]),
        }
        assert body["settings"] == expected_settings
        assert body["checksum"] == canonical_checksum(expected_settings)

        read = await client.get(_SETTINGS_URL, headers=_HEADERS)
        assert read.status_code == 200
        effective = read.json()["effective"]
        assert effective["revision"] == 1
        assert effective["configured"] is True
        assert effective["issuance_active"] is True
        assert effective["settings"]["mode"] == "shadow"

    @pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
    async def test_confirmation_phrase_names_resulting_mode(
        self, app: FastAPI, client: httpx.AsyncClient, settings_maker, mode: str
    ) -> None:
        install(app, settings_maker)
        payload = request_payload(mode=mode)
        if mode == "off":
            payload["settings"] = {
                **cast(dict[str, object], payload["settings"]),
                "daily_bundle_cap": 0,
                "daily_dollar_cap_microusd": 0,
                "per_bundle_request_cap": 0,
                "per_bundle_token_cap": 0,
                "profile_revision": None,
                "profile_checksum": None,
            }
        response = await client.post(_SETTINGS_URL, headers=_HEADERS, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["settings"]["mode"] == mode

    async def test_wrong_confirmation_is_rejected_without_write(
        self, app: FastAPI, client: httpx.AsyncClient, settings_maker
    ) -> None:
        install(app, settings_maker)
        payload = request_payload(confirmation="APPLY SETTINGS")
        response = await client.post(_SETTINGS_URL, headers=_HEADERS, json=payload)
        assert response.status_code == 409
        assert "APPLY V9 CONFIRMATION MODE SHADOW" in response.text
        assert (await client.get(_SETTINGS_URL, headers=_HEADERS)).json()[
            "current"
        ] == []

    async def test_stale_revision_is_rejected(
        self, app, client, settings_maker
    ) -> None:
        install(app, settings_maker)
        first = await client.post(
            _SETTINGS_URL, headers=_HEADERS, json=request_payload()
        )
        assert first.status_code == 200
        stale = await client.post(
            _SETTINGS_URL,
            headers=_HEADERS,
            json=request_payload(mode="enforce"),
        )
        assert stale.status_code == 409
        assert "current 1" in stale.text

    async def test_history_preserves_every_complete_policy(
        self, app, client, settings_maker
    ) -> None:
        install(app, settings_maker)
        first = await client.post(
            _SETTINGS_URL, headers=_HEADERS, json=request_payload()
        )
        assert first.status_code == 200
        second = await client.post(
            _SETTINGS_URL,
            headers=_HEADERS,
            json=request_payload(expected_revision=1, mode="enforce"),
        )
        assert second.status_code == 200
        body = (await client.get(_SETTINGS_URL, headers=_HEADERS)).json()
        assert body["current"][0]["revision"] == 2
        assert [row["revision"] for row in body["history"]] == [2, 1]
        assert body["history"][1]["settings"]["mode"] == "shadow"

    async def test_non_global_scope_is_rejected(
        self, app, client, settings_maker
    ) -> None:
        install(app, settings_maker)
        response = await client.post(
            _SETTINGS_URL,
            headers=_HEADERS,
            json=request_payload(scope="tenant"),
        )
        assert response.status_code == 422
        assert response.json()["message"] == "scope must be '*'"

    @pytest.mark.parametrize(
        "settings_overrides",
        [
            {"top_n": 11},
            {"daily_bundle_cap": 0},
            {"daily_dollar_cap_microusd": 0},
            {"per_bundle_request_cap": 0},
            {"per_bundle_token_cap": 0},
            {"profile_revision": None, "profile_checksum": None},
            {"profile_checksum": "short"},
        ],
    )
    async def test_invalid_or_incomplete_active_policy_is_422(
        self,
        app,
        client,
        settings_maker,
        settings_overrides: dict[str, object],
    ) -> None:
        install(app, settings_maker)
        response = await client.post(
            _SETTINGS_URL,
            headers=_HEADERS,
            json=request_payload(settings_overrides=settings_overrides),
        )
        assert response.status_code == 422, response.text

    async def test_concurrent_same_parent_writers_admit_exactly_one(
        self, app, client, settings_maker
    ) -> None:
        install(app, settings_maker)
        shadow = request_payload()
        enforce = request_payload(mode="enforce")
        first, second = await asyncio.gather(
            client.post(_SETTINGS_URL, headers=_HEADERS, json=shadow),
            client.post(_SETTINGS_URL, headers=_HEADERS, json=enforce),
        )
        assert sorted([first.status_code, second.status_code]) == [200, 409]
        body = (await client.get(_SETTINGS_URL, headers=_HEADERS)).json()
        assert len(body["history"]) == 1
        assert body["effective"]["revision"] == 1


class TestBundleReadAndRetest:
    async def test_empty_list_reports_zero_utc_budget(
        self, app, client, settings_maker
    ) -> None:
        install(app, settings_maker)
        response = await client.get(_BUNDLES_URL, headers=_HEADERS)
        assert response.status_code == 200
        body = response.json()
        assert body["items"] == []
        assert body["count"] == 0
        assert body["budget"]["revision"] == 0
        assert body["budget"]["issued_attempts"] == 0
        assert body["budget"]["outstanding_reserved_microusd"] == 0
        assert body["budget"]["settled_microusd"] == 0
        assert body["shadow_calibration"] == {
            "observed_from_utc_day": None,
            "observed_through_utc_day": None,
            "observation_days": 0,
            "confirmation_profile_revision": None,
            "confirmation_profile_checksum": None,
            "base_run_count": 0,
            "measured_base_cost_microusd": None,
            "confirmation_bundle_count": 0,
            "measured_bundle_cost_microusd": None,
            "bench_version": 7,
            "completed_bundle_count": 0,
            "superseded_bundle_count": 0,
            "failed_bundle_count": 0,
            "qualified_bundle_count": 0,
            "promotion_rate_bps": None,
            "projected_daily_spend_microusd": None,
            "epoch_duration_seconds": None,
            "projected_epoch_spend_microusd": None,
            "epoch_projection_unavailable_reason": (
                "Bench v7 has no configured epoch duration; no projection was guessed."
            ),
        }

    async def test_shadow_calibration_uses_settled_rows_and_marks_epoch_unavailable(
        self, app, client, settings_maker
    ) -> None:
        install(app, settings_maker)
        agent_id, bundle_id = await seed_completed_bundle(settings_maker)
        await activate_bench_version(settings_maker, 9)
        await seed_settled_base_run(settings_maker, agent_id=agent_id)
        async with settings_maker() as session, session.begin():
            session.add(
                ConfirmationBudgetReservation(
                    reservation_id=uuid4(),
                    bundle_id=bundle_id,
                    attempt=2,
                    utc_day=_NOW.date(),
                    settings_revision=1,
                    reserved_microusd=10_000,
                    state="settled",
                    actual_microusd=5_000,
                    failed_attempt=True,
                    created_at=_NOW + timedelta(minutes=6),
                    settled_at=_NOW + timedelta(minutes=7),
                )
            )

        response = await client.get(_BUNDLES_URL, headers=_HEADERS)

        assert response.status_code == 200, response.text
        calibration = response.json()["shadow_calibration"]
        assert calibration == {
            "observed_from_utc_day": "2026-08-08",
            "observed_through_utc_day": "2026-08-08",
            "observation_days": 1,
            "confirmation_profile_revision": "confirmation-v9-test-1",
            "confirmation_profile_checksum": verification_profile().checksum(),
            "base_run_count": 1,
            "measured_base_cost_microusd": 130_000,
            "confirmation_bundle_count": 1,
            "measured_bundle_cost_microusd": 20_000,
            "bench_version": 9,
            "completed_bundle_count": 1,
            "superseded_bundle_count": 0,
            "failed_bundle_count": 0,
            "qualified_bundle_count": 1,
            "promotion_rate_bps": 10_000,
            "projected_daily_spend_microusd": 150_000,
            "epoch_duration_seconds": None,
            "projected_epoch_spend_microusd": None,
            "epoch_projection_unavailable_reason": (
                "Bench v9 has no configured epoch duration; no projection was guessed."
            ),
        }

    async def test_list_and_detail_distinguish_base_and_full_contract(
        self, app, client, settings_maker
    ) -> None:
        install(app, settings_maker)
        agent_id, bundle_id = await seed_completed_bundle(settings_maker)
        listed = await client.get(_BUNDLES_URL, headers=_HEADERS)
        assert listed.status_code == 200, listed.text
        item = listed.json()["items"][0]
        assert item["bundle_id"] == str(bundle_id)
        assert item["state"] == "completed"
        assert item["evidence_sha256"]
        assert item["evidence_root"]["schema_version"] == 1
        assert len(item["dimensions"]) == 3
        subject = item["subjects"][0]
        assert subject["agent_id"] == str(agent_id)
        assert subject["bench_version"] == 9
        assert subject["artifact_sha256"] == "b" * 64
        assert subject["result_status"] == "full_confirmed"
        assert subject["base_evidence_sha256"] == BASE_EVIDENCE_SHA256
        assert subject["base_quality_micros"] == 750_000
        assert subject["base_stderr_micros"] == 20_000
        assert subject["full_quality_micros"] == 650_000
        assert subject["full_effective_micros"] == 650_000
        assert subject["bundle_id"] == str(bundle_id)
        detail = await client.get(f"{_BUNDLES_URL}/{bundle_id}", headers=_HEADERS)
        assert detail.status_code == 200
        assert detail.json()["bundle_id"] == str(bundle_id)

    async def test_missing_bundle_is_404(self, app, client, settings_maker) -> None:
        install(app, settings_maker)
        response = await client.get(f"{_BUNDLES_URL}/{uuid4()}", headers=_HEADERS)
        assert response.status_code == 404

    async def test_state_filter_is_typed(self, app, client, settings_maker) -> None:
        install(app, settings_maker)
        await seed_completed_bundle(settings_maker)
        completed = await client.get(
            _BUNDLES_URL, headers=_HEADERS, params={"state": "completed"}
        )
        pending = await client.get(
            _BUNDLES_URL, headers=_HEADERS, params={"state": "pending"}
        )
        invalid = await client.get(
            _BUNDLES_URL, headers=_HEADERS, params={"state": "done"}
        )
        assert completed.json()["count"] == 1
        assert pending.json()["count"] == 0
        assert invalid.status_code == 422

    async def test_offset_pages_rows_without_changing_total_count(
        self, app, client, settings_maker
    ) -> None:
        install(app, settings_maker)
        await seed_completed_bundle(settings_maker)
        response = await client.get(
            _BUNDLES_URL,
            headers=_HEADERS,
            params={"limit": 1, "offset": 1},
        )
        assert response.status_code == 200
        assert response.json()["items"] == []
        assert response.json()["count"] == 1

    async def test_negative_offset_is_rejected(
        self, app, client, settings_maker
    ) -> None:
        install(app, settings_maker)
        response = await client.get(
            _BUNDLES_URL, headers=_HEADERS, params={"offset": -1}
        )
        assert response.status_code == 422

    async def test_retest_requires_exact_confirmation(
        self, app, client, settings_maker
    ) -> None:
        install(app, settings_maker)
        _, bundle_id = await seed_completed_bundle(settings_maker)
        response = await client.post(
            f"{_BUNDLES_URL}/{bundle_id}/authorize-retest",
            headers=_HEADERS,
            json={
                "request_id": str(uuid4()),
                "expected_generation": 0,
                "reason": "operator approved fresh confirmation evidence",
                "actor": "operator@example.com",
                "confirmation": "RETEST",
            },
        )
        assert response.status_code == 409

    async def test_retest_is_audited_idempotent_and_generation_bounded(
        self, app, client, settings_maker
    ) -> None:
        install(app, settings_maker)
        agent_id, bundle_id = await seed_completed_bundle(settings_maker)
        request_id = uuid4()
        payload = {
            "request_id": str(request_id),
            "expected_generation": 0,
            "reason": "operator approved fresh confirmation evidence",
            "actor": "operator@example.com",
            "confirmation": "AUTHORIZE CONFIRMATION BUNDLE RETEST",
        }
        first = await client.post(
            f"{_BUNDLES_URL}/{bundle_id}/authorize-retest",
            headers=_HEADERS,
            json=payload,
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["authorization_id"] == str(request_id)
        assert body["superseded_bundle_id"] == str(bundle_id)
        assert body["bundle"]["retest_generation"] == 1
        assert body["bundle"]["state"] == "pending"
        assert body["bundle"]["subjects"][0]["agent_id"] == str(agent_id)
        assert body["bundle"]["subjects"][0]["result_status"] == "provisional"
        assert body["replayed"] is False

        replay = await client.post(
            f"{_BUNDLES_URL}/{bundle_id}/authorize-retest",
            headers=_HEADERS,
            json=payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["bundle"]["bundle_id"] == body["bundle"]["bundle_id"]
        assert replay.json()["replayed"] is True

        async with settings_maker() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(ConfirmationRetestAuthorization)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(ConfirmationBundle)
                )
                == 2
            )

    async def test_retest_replay_with_changed_actor_is_rejected(
        self, app, client, settings_maker
    ) -> None:
        install(app, settings_maker)
        _, bundle_id = await seed_completed_bundle(settings_maker)
        request_id = uuid4()
        payload = {
            "request_id": str(request_id),
            "expected_generation": 0,
            "reason": "operator approved fresh confirmation evidence",
            "actor": "operator@example.com",
            "confirmation": "AUTHORIZE CONFIRMATION BUNDLE RETEST",
        }
        first = await client.post(
            f"{_BUNDLES_URL}/{bundle_id}/authorize-retest",
            headers=_HEADERS,
            json=payload,
        )
        assert first.status_code == 200
        changed = await client.post(
            f"{_BUNDLES_URL}/{bundle_id}/authorize-retest",
            headers=_HEADERS,
            json={**payload, "actor": "different@example.com"},
        )
        assert changed.status_code == 409
        assert "different input" in changed.text
