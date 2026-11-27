"""The clean-board partial burn (#2911): rule, attestation, ledger, pin, board.

One rule (:func:`ditto.api_server.clean_board.unpaid_fraction`) drives the
validator ledger, the epoch pin and the public board, so each surface is
asserted against the same facts here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models import LedgerResponse
from ditto.api_models.clean_board import CleanBoardSettings
from ditto.api_server.clean_board import unpaid_fraction, unpaid_fractions
from ditto.api_server.endpoints.admin_clean_board_settings import checksum
from ditto.api_server.endpoints.scoring import _require_statistical_cap_requester
from ditto.api_server.ledger_pin import (
    LedgerPin,
    build_pin_draft,
    clean_board_shadow_fractions,
    pin_expected_burn,
    pin_expected_shares,
    response_from_pin,
    served_unpaid_fractions,
)
from ditto.db.models import (
    AthReview,
    CleanBoardSettingsRevision,
    Score,
    ValidatorHeartbeat,
)
from ditto.db.queries.clean_board import (
    CleanBoardFacts,
    list_board_attested_agent_ids,
    load_clean_board_facts,
)
from ditto.tests.api_server.endpoints.test_emission_eligibility_ledger import (
    _LEADER_HOTKEY,
    _NOW,
    _RUNNER_UP_HOTKEY,
    _VALIDATOR,
    _fleet,
    _seed,
    _snapshot,
    _two_miners,
)
from ditto.tests.api_server.endpoints.test_emission_eligibility_ledger import (
    _install as _install_eligibility,
)
from ditto.tests.api_server.test_ledger_pin import _entry, _schedule
from ditto.tests.api_server.test_ledger_pin import _snapshot as _pin_snapshot

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/clean-board-settings"
_GRACE = timedelta(seconds=86_400)


def _facts(*, attested: bool = False, finalized: datetime | None = _NOW - 2 * _GRACE):
    return CleanBoardFacts(attested=attested, finalized_at=finalized)


class TestRule:
    def test_only_an_unattested_agent_past_grace_is_partly_unpaid(self) -> None:
        settings = CleanBoardSettings(enabled=True)
        assert unpaid_fraction(_facts(), settings, now=_NOW) == 0.5
        assert unpaid_fraction(_facts(attested=True), settings, now=_NOW) is None
        assert unpaid_fraction(_facts(finalized=None), settings, now=_NOW) is None
        assert unpaid_fraction(None, settings, now=_NOW) is None
        inside = _facts(finalized=_NOW - _GRACE + timedelta(seconds=1))
        assert unpaid_fraction(inside, settings, now=_NOW) is None
        boundary = _facts(finalized=_NOW - _GRACE)
        assert unpaid_fraction(boundary, settings, now=_NOW) == 0.5

    def test_shadow_computes_the_same_numbers_and_zero_burns_nothing(self) -> None:
        assert unpaid_fraction(_facts(), CleanBoardSettings(), now=_NOW) == 0.5
        free = CleanBoardSettings(enabled=True, unattested_slot_burn_share=0.0)
        assert unpaid_fraction(_facts(), free, now=_NOW) is None
        whole = CleanBoardSettings(enabled=True, unattested_slot_burn_share=1.0)
        assert unpaid_fraction(_facts(), whole, now=_NOW) == 1.0

    def test_fractions_name_only_unpaid_agents(self) -> None:
        paid, unpaid = uuid4(), uuid4()
        facts = {paid: _facts(attested=True), unpaid: _facts()}
        assert unpaid_fractions(
            [paid, unpaid, uuid4()], facts, CleanBoardSettings(), now=_NOW
        ) == {unpaid: 0.5}

    def test_bounds_are_enforced(self) -> None:
        for bad in (
            {"unattested_slot_burn_share": 1.5},
            {"unattested_slot_burn_share": -0.1},
            {"attestation_grace_seconds": -1},
            {"attestation_grace_seconds": 604_801},
        ):
            with pytest.raises(ValueError):
                CleanBoardSettings.model_validate(bad)


async def _review(
    session: AsyncSession,
    agent_id: UUID,
    *,
    status: str = "resolved",
    resolution: str | None = "clear",
    resolved_by: str | None = "operator@example.com",
) -> None:
    resolved = status == "resolved"
    async with session.begin():
        session.add(
            AthReview(
                review_id=uuid4(),
                agent_id=agent_id,
                status=status,
                opened_at=_NOW - timedelta(days=1),
                resolved_at=_NOW - timedelta(hours=1) if resolved else None,
                resolved_by=resolved_by if resolved else None,
                resolution=resolution if resolved else None,
                resolution_reason="reviewed and kept" if resolved else None,
                original_reason="board review",
                original_policy_version=1,
                original_evidence={},
                algorithm_provenance={},
            )
        )


async def _finalized_at(session: AsyncSession, agent_id: UUID, at: datetime) -> None:
    """Scores insert at the database's own clock; move the quorum back."""
    async with session.begin():
        await session.execute(
            update(Score).where(Score.agent_id == agent_id).values(created_at=at)
        )


_SHA = iter(f"{index:02x}" * 32 for index in range(10, 99))


async def _agent(session: AsyncSession, hotkey: str, composite: float) -> UUID:
    return await _seed(
        session,
        hotkey=hotkey,
        name=f"agent-{uuid4().hex[:6]}",
        composite=composite,
        sha256=next(_SHA),
    )


class TestAttestation:
    async def test_only_an_operator_clear_attests(self, session: AsyncSession) -> None:
        operator = await _agent(session, _LEADER_HOTKEY, 0.9)
        owner_link = await _agent(session, _LEADER_HOTKEY, 0.8)
        platform = await _agent(session, _LEADER_HOTKEY, 0.7)
        pending = await _agent(session, _RUNNER_UP_HOTKEY, 0.6)
        rejected = await _agent(session, _RUNNER_UP_HOTKEY, 0.5)
        unreviewed = await _agent(session, _RUNNER_UP_HOTKEY, 0.4)
        await _review(session, operator)
        await _review(session, owner_link, resolved_by=f"owner-link:{uuid4()}")
        await _review(session, platform, resolved_by="platform:deferred-source-review")
        await _review(session, pending, status="pending")
        await _review(session, rejected, resolution="reject")

        ids = [operator, owner_link, platform, pending, rejected, unreviewed]
        assert await list_board_attested_agent_ids(session, ids) == {operator}
        facts = await load_clean_board_facts(session, ids, quorum=3)
        assert set(facts) == set(ids)
        assert facts[operator].attested and not facts[unreviewed].attested
        assert all(fact.finalized_at is not None for fact in facts.values())
        assert await list_board_attested_agent_ids(session, []) == set()


async def _policy(session: AsyncSession, **fields: Any) -> None:
    settings = CleanBoardSettings(**fields)
    async with session.begin():
        session.add(
            CleanBoardSettingsRevision(
                parent_revision=0,
                scope="*",
                settings=settings.model_dump(mode="json"),
                checksum=checksum(settings),
                reason="test clean-board policy",
                actor="operator@example.com",
            )
        )


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    _install_eligibility(app, maker)
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.clean_board_settings.invalidate()


async def _board(session: AsyncSession) -> tuple[UUID, UUID]:
    """An attested leader and an unattested runner-up, both well past grace."""
    leader, runner_up = await _two_miners(session)
    for agent_id in (leader, runner_up):
        await _finalized_at(session, agent_id, _NOW - 3 * _GRACE)
    await _review(session, leader)
    return leader, runner_up


class TestValidatorLedger:
    async def test_enforced_policy_serves_the_runner_ups_fraction(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        leader, runner_up = await _board(session)
        await _policy(session, enabled=True)
        await _fleet(session, protocol=36)

        snapshot = await _snapshot(app, session)

        assert [entry.agent_id for entry in snapshot.entries] == [leader, runner_up]
        assert snapshot.unpaid_fractions == {runner_up: 0.5}
        assert snapshot.clean_board_fractions == {runner_up: 0.5}
        assert snapshot.fleet_readiness["clean_board"] is True

    async def test_disabled_policy_is_shadow_only(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        _, runner_up = await _board(session)
        await _fleet(session, protocol=36)

        snapshot = await _snapshot(app, session)

        assert snapshot.unpaid_fractions is None
        assert snapshot.clean_board_fractions == {runner_up: 0.5}

    async def test_a_fleet_below_protocol_36_is_not_served(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        _, runner_up = await _board(session)
        await _policy(session, enabled=True)
        await _fleet(session, protocol=35)

        snapshot = await _snapshot(app, session)

        assert snapshot.unpaid_fractions is None
        assert snapshot.clean_board_fractions == {runner_up: 0.5}
        assert snapshot.fleet_readiness["clean_board"] is False

    async def test_inside_grace_everyone_is_paid(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        _, runner_up = await _two_miners(session)
        await _finalized_at(session, runner_up, _NOW - timedelta(hours=2))
        await _policy(session, enabled=True)
        await _fleet(session, protocol=36)

        snapshot = await _snapshot(app, session)

        assert runner_up not in (snapshot.clean_board_fractions or {})
        assert runner_up not in (snapshot.unpaid_fractions or {})


async def _heartbeat(session: AsyncSession, protocol: int) -> None:
    async with session.begin():
        await session.merge(
            ValidatorHeartbeat(
                validator_hotkey=_VALIDATOR,
                software_version="0.600.0",
                protocol_version=protocol,
                code_digest="ab" * 32,
                state="idle",
                reported_at=datetime.now(UTC),
                seen_at=datetime.now(UTC),
                signature="cd" * 64,
                capabilities={},
            )
        )


class TestRequesterGate:
    async def test_a_sub_36_rejoiner_is_refused_a_served_burn(
        self, session: AsyncSession
    ) -> None:
        entry = _entry(_LEADER_HOTKEY, 0.9, first_seen=_NOW)
        ledger = LedgerResponse(
            entries=[entry], count=1, unpaid_fractions={entry.agent_id: 0.5}
        )
        await _heartbeat(session, 35)
        with pytest.raises(HTTPException) as refused:
            await _require_statistical_cap_requester(
                session, _VALIDATOR, ledger, now=datetime.now(UTC)
            )
        assert refused.value.status_code == 428
        assert "protocol 36" in str(refused.value.detail)
        await session.rollback()  # the gate's read autobegan a transaction

        await _heartbeat(session, 36)
        served = await _require_statistical_cap_requester(
            session, _VALIDATOR, ledger, now=datetime.now(UTC)
        )
        assert served.unpaid_fractions == {entry.agent_id: 0.5}

    async def test_an_empty_map_needs_no_protocol(self, session: AsyncSession) -> None:
        entry = _entry(_LEADER_HOTKEY, 0.9, first_seen=_NOW)
        ledger = LedgerResponse(entries=[entry], count=1)
        assert "unpaid_fractions" not in ledger.model_dump(mode="json")
        assert (
            await _require_statistical_cap_requester(
                session, _VALIDATOR, ledger, now=datetime.now(UTC)
            )
            is ledger
        )


def _pin(draft: Any) -> LedgerPin:
    return LedgerPin(
        netuid=draft.netuid,
        epoch_index=draft.epoch_index,
        last_epoch_block=draft.last_epoch_block,
        pinned_block=draft.pinned_block,
        pinned_block_hash=draft.pinned_block_hash,
        pinned_at=draft.pinned_at,
        bench_version=draft.bench_version,
        entries=tuple(
            _entry(item["miner_hotkey"], item["composite"], first_seen=_NOW).model_copy(
                update={"agent_id": UUID(item["agent_id"])}
            )
            for item in draft.entries
        ),
        context=draft.context,
        ledger_digest=draft.ledger_digest,
        champion_agent_id=draft.champion_agent_id,
        incumbent_agent_id=draft.incumbent_agent_id,
    )


class TestPin:
    def _entries(self) -> list:
        return [
            _entry(_LEADER_HOTKEY, 0.90, first_seen=_NOW - timedelta(days=3)),
            _entry(_RUNNER_UP_HOTKEY, 0.80, first_seen=_NOW - timedelta(days=2)),
        ]

    def _draft(self, entries: list, **overrides: Any) -> Any:
        return build_pin_draft(
            _schedule(),
            snapshot=_pin_snapshot(entries, **overrides),
            previous_pin=None,
            previous_champion_owner_root=None,
            now=_NOW,
        )

    def test_a_served_map_is_frozen_and_replayed(self) -> None:
        entries = self._entries()
        tail = entries[1].agent_id
        legacy = self._draft(entries)
        draft = self._draft(
            entries,
            unpaid_fractions={tail: 0.5},
            clean_board_fractions={tail: 0.5},
        )

        assert "unpaid_fractions" not in legacy.context["served"]
        assert "clean_board_fractions" not in legacy.context
        assert draft.context["served"]["unpaid_fractions"] == {str(tail): 0.5}
        assert draft.ledger_digest != legacy.ledger_digest
        replayed = response_from_pin(_pin(draft), stale=False, now=_NOW)
        assert replayed.unpaid_fractions == {tail: 0.5}
        assert clean_board_shadow_fractions(draft.context) == {tail: 0.5}

    def test_a_shadow_rehearsal_never_moves_the_digest(self) -> None:
        entries = self._entries()
        legacy = self._draft(entries)
        shadow = self._draft(entries, clean_board_fractions={entries[1].agent_id: 0.5})
        assert "unpaid_fractions" not in shadow.context["served"]
        assert shadow.ledger_digest == legacy.ledger_digest
        assert (
            response_from_pin(_pin(shadow), stale=False, now=_NOW).unpaid_fractions
            == {}
        )

    def test_expected_shares_pay_one_minus_f_and_burn_the_rest(self) -> None:
        entries = self._entries()
        tail = entries[1]
        full = pin_expected_shares(_pin(self._draft(entries)))
        partial = pin_expected_shares(
            _pin(self._draft(entries, unpaid_fractions={tail.agent_id: 0.5}))
        )
        assert full is not None and partial is not None
        assert sum(full.values()) == pytest.approx(1.0)
        # Crown and rank are untouched: only the tail's paid share shrinks.
        assert partial[_LEADER_HOTKEY] == pytest.approx(full[_LEADER_HOTKEY])
        assert partial[_RUNNER_UP_HOTKEY] == pytest.approx(
            full[_RUNNER_UP_HOTKEY] * 0.5
        )
        unpaid = full[_RUNNER_UP_HOTKEY] * 0.5
        assert pin_expected_burn(0.1, partial) == pytest.approx(
            1.0 - 0.9 * (1.0 - unpaid)
        )

    def test_a_malformed_served_map_reads_as_absent_whole(self) -> None:
        good = str(uuid4())
        assert served_unpaid_fractions({"unpaid_fractions": {good: 0.25}}) == {
            UUID(good): 0.25
        }
        for bad in (
            {good: 0.0},
            {good: 1.5},
            {good: True},
            {good: float("nan")},
            {"not-a-uuid": 0.5},
            [good],
        ):
            assert served_unpaid_fractions({"unpaid_fractions": bad}) == {}


class TestAdminPolicy:
    async def test_default_is_disabled_and_a_revision_is_audited(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        initial = await client.get(_URL, headers=_HEADERS)
        assert initial.status_code == 200, initial.text
        body = initial.json()
        assert body["effective"]["source"] == "default"
        assert body["effective"]["settings"] == {
            "enabled": False,
            "unattested_slot_burn_share": 0.5,
            "attestation_grace_seconds": 86_400,
        }
        assert body["effective"]["served"] is False
        assert body["effective"]["required_protocol"] == 36

        payload = {
            "expected_revision": 0,
            "settings": {"enabled": True, "unattested_slot_burn_share": 0.25},
            "reason": "owner-approved clean board rollout",
            "actor": "operator@example.com",
            "confirmation": "APPLY CLEAN BOARD POLICY",
        }
        created = await client.post(_URL, json=payload, headers=_HEADERS)
        assert created.status_code == 200, created.text
        revision = created.json()
        assert revision["revision"] >= 1
        assert revision["settings"]["unattested_slot_burn_share"] == 0.25
        assert revision["actor"] == "operator@example.com"

        after = (await client.get(_URL, headers=_HEADERS)).json()
        assert after["effective"]["settings"]["enabled"] is True
        assert len(after["history"]) == 1
        # Enabled, but no weight setter reports v36: not served yet.
        assert after["effective"]["served"] is False

    async def test_writes_are_confirmed_and_optimistic(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        payload: dict[str, Any] = {
            "expected_revision": 0,
            "settings": {"enabled": True},
            "reason": "owner-approved clean board rollout",
            "confirmation": "apply",
        }
        wrong = await client.post(_URL, json=payload, headers=_HEADERS)
        assert wrong.status_code == 409
        payload["confirmation"] = "APPLY CLEAN BOARD POLICY"
        payload["expected_revision"] = 7
        stale = await client.post(_URL, json=payload, headers=_HEADERS)
        assert stale.status_code == 409
        payload["settings"] = {"unattested_slot_burn_share": 2.0}
        payload["expected_revision"] = 0
        bounded = await client.post(_URL, json=payload, headers=_HEADERS)
        assert bounded.status_code == 422
        unauthorized = await client.get(_URL)
        assert unauthorized.status_code in {401, 403}


class TestPublicBoard:
    async def _recipients(self, client: httpx.AsyncClient) -> tuple[dict, dict]:
        response = await client.get("/api/v1/public/leaderboard")
        assert response.status_code == 200, response.text
        emissions = response.json()["emissions"]
        return emissions, {item["agent_id"]: item for item in emissions["recipients"]}

    async def test_shadow_publishes_the_fraction_without_applying_it(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        leader, runner_up = await _board(session)

        emissions, recipients = await self._recipients(client)

        assert emissions["clean_board_mode"] == "shadow"
        assert emissions["clean_board_required_protocol"] == 36
        king, tail = recipients[str(leader)], recipients[str(runner_up)]
        assert king["attested"] is True and king["unpaid_fraction"] == 0.0
        assert tail["attested"] is False and tail["unpaid_fraction"] == 0.5
        # Shadow: nobody is actually paid less.
        assert king["paid_fraction"] == 1.0 and tail["paid_fraction"] == 1.0
        assert emissions["unattested_burn_share"] == pytest.approx(
            tail["share_of_miner_pool"] * 0.5
        )

    async def test_enforced_policy_projects_the_fold(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        leader, runner_up = await _board(session)
        await _policy(session, enabled=True)
        await _fleet(session, protocol=36, seen_at=datetime.now(UTC))

        emissions, recipients = await self._recipients(client)

        assert emissions["clean_board_mode"] == "enforce"
        king, tail = recipients[str(leader)], recipients[str(runner_up)]
        # Ranks and shares are untouched; only the paid fraction moves.
        assert emissions["champion_agent_id"] == str(leader)
        assert king["paid_fraction"] == 1.0
        assert tail["paid_fraction"] == pytest.approx(0.5)
        burned = tail["share_of_miner_pool"] * 0.5
        assert emissions["unattested_burn_share"] == pytest.approx(burned)
        # Parity with the pin projection of the same pool and fractions: the
        # tail's paid share of the miner pool is the board's share * (1 - f).
        entries = [
            _entry(_LEADER_HOTKEY, 0.95, first_seen=_NOW - timedelta(days=3)),
            _entry(_RUNNER_UP_HOTKEY, 0.80, first_seen=_NOW - timedelta(days=3)),
        ]
        expected = pin_expected_shares(
            _pin(
                build_pin_draft(
                    _schedule(),
                    snapshot=_pin_snapshot(
                        entries,
                        dethrone_band_mode=None,
                        unpaid_fractions={entries[1].agent_id: 0.5},
                    ),
                    previous_pin=None,
                    previous_champion_owner_root=None,
                    now=_NOW,
                )
            )
        )
        assert expected is not None
        assert expected[_RUNNER_UP_HOTKEY] == pytest.approx(
            tail["share_of_miner_pool"] * tail["paid_fraction"]
        )
        assert expected[_LEADER_HOTKEY] == pytest.approx(king["share_of_miner_pool"])
