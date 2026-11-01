"""Every outage-voided continual-retest sample is re-run exactly once.

Fixture: the 2026-11-01 Wonder v6 shape. The agent sits outside the capped
retest cohort, so the cohort lane never leases it again, and the post-outage
sweep has voided one of its samples. The void is a re-run debt: one replacement
lease for that seed, issued regardless of cohort, and nothing more.

Cadence is pinned closed (no scheduled round, no idle retests) so the cohort
lane itself has nothing to issue and every lease here is the debt's.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.endpoints import validator as validator_endpoint
from ditto.db.models import (
    ConfirmationScore,
    ConfirmationScoreVoid,
    ProviderOutageCircuit,
    ValidatorTicket,
)
from ditto.db.queries.confirmation_score_voids import outstanding_void_rerun_debts
from ditto.tests.api_server.endpoints.test_validator import (
    _AUTH_HEADER,
    _BENCH_VERSION,
    _KEYPAIRS,
    _VALIDATOR_HOTKEY,
    _auto_top5_job_payload,
    _install_chain_with_block,
    _install_dataset_generator,
    _install_db,
    _install_ticket_inference,
    _seed_top5_emission_set,
    _set_retest_cohort_size,
    _top5_auth_header,
    _top5_score_payload,
)

pytestmark = pytest.mark.asyncio

_VOIDED_SEED = 2_639_827_674_349_253_378
_JOB = "/api/v1/validator/top5-confirmation-job"


async def _seed_outsider(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    *,
    voided: bool = True,
    voided_at: datetime | None = None,
) -> UUID:
    """Six scored agents, a cohort of five, and the sixth's sample voided."""
    pool = await _seed_top5_emission_set(
        maker, composites=[0.90, 0.88, 0.86, 0.84, 0.82, 0.48]
    )
    outsider = pool[-1]
    now = datetime.now(UTC)
    voided_at = voided_at or now - timedelta(minutes=5)
    async with maker() as session, session.begin():
        session.add(
            ConfirmationScore(
                agent_id=outsider,
                validator_hotkey=_VALIDATOR_HOTKEY,
                bench_version=_BENCH_VERSION,
                seed=_VOIDED_SEED,
                composite=0.31,
                run_id="outage-run",
                signature=None,
                created_at=voided_at - timedelta(hours=1),
            )
        )
        await session.flush()
        if voided:
            session.add(
                ConfirmationScoreVoid(
                    void_id=uuid4(),
                    agent_id=outsider,
                    bench_version=_BENCH_VERSION,
                    validator_hotkey=_VALIDATOR_HOTKEY,
                    seed=_VOIDED_SEED,
                    void_generation=0,
                    sample_kind="continual_retest",
                    run_id="outage-run",
                    composite=0.31,
                    reason="openrouter credit refusal during the run",
                    evidence={"outage_failed_requests": 2127},
                    evidence_digest="a" * 64,
                    actor="outage_sweep",
                    source="outage_sweep",
                    created_at=voided_at,
                )
            )
    await _set_retest_cohort_size(maker, 5, idle_retests_enabled=False)
    _install_db(app, maker)
    app.state.session_maker = maker
    app.state.continual_retest_settings.invalidate()
    _install_chain_with_block(app, block_number=1)
    app.state.config = replace(app.state.config, top5_backoff_base=2)
    return outsider


async def _poll(
    client: httpx.AsyncClient, *, keypair_index: int = 0, slot_id: str = "slot-0"
) -> httpx.Response:
    keypair = _KEYPAIRS[keypair_index]
    return await client.post(
        _JOB,
        headers=_top5_auth_header(keypair),
        json=_auto_top5_job_payload(slot_id, keypair=keypair),
    )


async def _retest_leases(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID
) -> list[ValidatorTicket]:
    async with maker() as session:
        return list(
            await session.scalars(
                select(ValidatorTicket).where(
                    ValidatorTicket.agent_id == agent_id,
                    ValidatorTicket.purpose == TicketPurpose.CONTINUAL_RETEST,
                    ValidatorTicket.status == TicketStatus.ISSUED,
                )
            )
        )


class TestVoidRerunDebt:
    @pytest.fixture(autouse=True)
    async def _closed_cadence(
        self,
        app: FastAPI,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_dataset_generator(app)
        await _install_ticket_inference(app, session_maker)
        monkeypatch.setattr(
            validator_endpoint, "top5_round_is_due", lambda *_a, **_k: False
        )

    async def test_voided_outsider_gets_exactly_one_lease_for_that_seed(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        outsider = await _seed_outsider(app, session_maker)

        issued = await _poll(client)
        assert issued.status_code == 200, issued.text
        body = issued.json()
        assert body["agent_id"] == str(outsider)
        assert [pin["seed"] for pin in body["confirmation_datasets"]] == [_VOIDED_SEED]
        [lease] = await _retest_leases(session_maker, outsider)
        assert lease.validator_hotkey == _VALIDATOR_HOTKEY
        assert lease.seed == _VOIDED_SEED
        assert lease.purpose == TicketPurpose.CONTINUAL_RETEST
        assert lease.purpose_revision >= 1
        assert (
            lease.dataset_sha256 == body["confirmation_datasets"][0]["dataset_sha256"]
        )

        # Another validator, and another slot on the same one, get nothing:
        # one live lease per voided seed.
        other = await _poll(client, keypair_index=1)
        second_slot = await _poll(client, slot_id="slot-1")
        assert other.status_code == 204, other.text
        assert second_slot.status_code == 204, second_slot.text

        # The same slot re-polling resumes its debt lease instead of expiring
        # it as "left the cohort".
        resumed = await _poll(client)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["agent_id"] == str(outsider)
        [still] = await _retest_leases(session_maker, outsider)
        assert still.deadline == lease.deadline

    async def test_no_further_lease_once_the_rerun_lands(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        outsider = await _seed_outsider(app, session_maker)
        issued = await _poll(client)
        assert issued.status_code == 200, issued.text
        [lease] = await _retest_leases(session_maker, outsider)

        submitted = await client.post(
            f"/api/v1/validator/agent/{outsider}/top5-confirmation-score",
            headers=_AUTH_HEADER,
            json=_top5_score_payload(
                outsider,
                deadline=lease.deadline,
                seeds=[_VOIDED_SEED],
                composites=[0.47],
            ),
        )
        assert submitted.status_code == 200, submitted.text
        async with session_maker() as session:
            rerun = await session.get(
                ConfirmationScore,
                (outsider, _BENCH_VERSION, _VALIDATOR_HOTKEY, _VOIDED_SEED, 1),
            )
            debts = await outstanding_void_rerun_debts(
                session, bench_version=_BENCH_VERSION
            )
        assert rerun is not None
        assert rerun.composite == pytest.approx(0.47)
        assert debts == []

        for keypair_index in range(len(_KEYPAIRS)):
            again = await _poll(client, keypair_index=keypair_index)
            assert again.status_code == 204, again.text
        assert await _retest_leases(session_maker, outsider) == []

    async def test_no_lease_while_the_provider_circuit_is_open(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        outsider = await _seed_outsider(app, session_maker)
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                ProviderOutageCircuit(
                    provider="openrouter",
                    state="open",
                    epoch=uuid4(),
                    opened_at=now,
                    retry_at=now + timedelta(minutes=2),
                    last_failure_at=now,
                    failure_count=3,
                    last_status=402,
                    last_error_code="upstream_http_402",
                    updated_at=now,
                )
            )

        blocked = await _poll(client)
        assert blocked.status_code == 204, blocked.text
        assert await _retest_leases(session_maker, outsider) == []

        # Closed and past the recovery quiet window: the debt is still owed.
        async with session_maker() as session, session.begin():
            circuit = await session.get(ProviderOutageCircuit, "openrouter")
            assert circuit is not None
            circuit.state = "closed"
            circuit.closed_at = now - timedelta(hours=1)
            circuit.last_failure_at = now - timedelta(hours=1)
        recovered = await _poll(client)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["agent_id"] == str(outsider)

    async def test_unvoided_outsider_still_gets_nothing(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        outsider = await _seed_outsider(app, session_maker, voided=False)

        for keypair_index in range(len(_KEYPAIRS)):
            response = await _poll(client, keypair_index=keypair_index)
            assert response.status_code == 204, response.text
        assert await _retest_leases(session_maker, outsider) == []

    async def test_another_validator_takes_the_seed_only_after_the_grace(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The voiding validator is preferred; a stale debt opens to the fleet.

        Its own re-run would append at generation 1. Another validator has no
        row on the seed, so its fresh result appends at generation 0 under its
        own key -- and that one replacement settles the debt.
        """
        outsider = await _seed_outsider(app, session_maker)
        held = await _poll(client, keypair_index=1)
        assert held.status_code == 204, held.text

        async with session_maker() as session, session.begin():
            void = await session.scalar(select(ConfirmationScoreVoid))
            assert void is not None
            void.created_at = datetime.now(UTC) - timedelta(hours=2)
        taken = await _poll(client, keypair_index=1)
        assert taken.status_code == 200, taken.text
        assert taken.json()["agent_id"] == str(outsider)
        [lease] = await _retest_leases(session_maker, outsider)
        assert lease.validator_hotkey == _KEYPAIRS[1].ss58_address
        assert lease.seed == _VOIDED_SEED


def _circuit(*, retry_at: datetime, now: datetime) -> ProviderOutageCircuit:
    return ProviderOutageCircuit(
        provider="openrouter",
        state="open",
        epoch=uuid4(),
        opened_at=now - timedelta(minutes=5),
        retry_at=retry_at,
        last_failure_at=now,
        failure_count=3,
        last_status=429,
        last_error_code="upstream_http_429",
        updated_at=now,
    )


class TestContinualRetestProviderGate:
    """The whole lane honours the provider-outage gate, not only the debt.

    Cadence is left real here: the cohort lane has ordinary work to issue, so
    every 204 below is the gate's.
    """

    @pytest.fixture(autouse=True)
    async def _lane(
        self, app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        _install_dataset_generator(app)
        await _install_ticket_inference(app, session_maker)

    async def _board(
        self, app: FastAPI, maker: async_sessionmaker[AsyncSession]
    ) -> list[UUID]:
        members = await _seed_top5_emission_set(maker)
        _install_db(app, maker)
        _install_chain_with_block(app, block_number=1)
        return members

    async def test_open_circuit_issues_no_cohort_retest_until_recovery(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        members = await self._board(app, session_maker)
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(_circuit(retry_at=now + timedelta(minutes=2), now=now))

        for keypair_index in range(len(_KEYPAIRS)):
            response = await _poll(client, keypair_index=keypair_index)
            assert response.status_code == 204, response.text
        async with session_maker() as session:
            issued = (
                await session.scalars(
                    select(ValidatorTicket).where(
                        ValidatorTicket.purpose == TicketPurpose.CONTINUAL_RETEST
                    )
                )
            ).all()
        assert issued == []

        # Closed, and past the recovery quiet window: issuance resumes.
        async with session_maker() as session, session.begin():
            circuit = await session.get(ProviderOutageCircuit, "openrouter")
            assert circuit is not None
            circuit.state = "closed"
            circuit.closed_at = now - timedelta(hours=1)
            circuit.last_failure_at = now - timedelta(hours=1)
        resumed = await _poll(client)
        assert resumed.status_code == 200, resumed.text
        assert UUID(resumed.json()["agent_id"]) in set(members)

    async def test_open_circuit_parks_live_retests_and_admits_one_probe(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._board(app, session_maker)
        first = await _poll(client)
        assert first.status_code == 200, first.text
        parked_agent = UUID(first.json()["agent_id"])

        # Cooled down with no probe yet: the next poll parks the live lease
        # and becomes the single half-open probe.
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(_circuit(retry_at=now - timedelta(seconds=1), now=now))
        probe = await _poll(client, keypair_index=1)
        assert probe.status_code == 200, probe.text

        async with session_maker() as session:
            parked = await session.get(
                ValidatorTicket, (parked_agent, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            circuit = await session.get(ProviderOutageCircuit, "openrouter")
        assert parked is not None
        assert parked.status == TicketStatus.EXPIRED
        assert parked.failure_detail == "provider_outage_parked"
        assert circuit is not None
        assert circuit.probe_kind == "scoring"
        assert circuit.probe_key == f"{_KEYPAIRS[1].ss58_address}:slot-0"

        # Every other slot waits on the probe, including the parked one.
        sibling = await _poll(client)
        assert sibling.status_code == 204, sibling.text
        # The probe's own slot keeps resuming its live lease.
        again = await _poll(client, keypair_index=1)
        assert again.status_code == 200, again.text
        assert again.json()["agent_id"] == probe.json()["agent_id"]
