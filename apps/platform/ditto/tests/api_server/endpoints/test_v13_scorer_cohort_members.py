"""A V13 scorer cohort may hold three to sixteen members; quorum stays three.

Production 2026-10-13..15: a three-member pin meant one degraded member parked
every agent it failed at 2/3, while a fourth healthy validator running the same
signed packet could never be issued V13 work. The pin now names up to sixteen
members, and issuance follows the *authority packet*: the release signed by a
strict majority of the live members, which is itself at least three members.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_models.validator_slot_settings import ValidatorSlotSettings
from ditto.api_server.endpoints import admin_v13_scorer_cohort
from ditto.api_server.endpoints.admin_v13_scorer_cohort import (
    ActivateV13ScorerCohortRequest,
    RotateV13ScorerCohortRequest,
    activate_pin,
    get_preflight,
    get_report_only_current_packet,
    rotate_pin,
)
from ditto.api_server.endpoints.admin_validator_slot_settings import _checksum
from ditto.api_server.scored_runtime_evidence import scored_runtime_evidence_for_lease
from ditto.api_server.v13_scorer_cohort import (
    CohortPacket,
    V13ScorerPacket,
    authority_from_packets,
    pinned_validator_allowed,
    pinned_validator_may_submit,
)
from ditto.db.models import (
    V13ScorerCohortPin,
    V13ScorerCohortRotation,
    ValidatorHeartbeat,
    ValidatorSlotSettingsRevision,
    ValidatorTicket,
)
from ditto.db.queries.tickets import issue_confirmation_ticket
from ditto.tests.api_server.endpoints.test_screener import _seed_agent
from ditto.tests.db.queries.test_benchmark_rollout import _heartbeat

_NOW = datetime.now(UTC).replace(microsecond=0)
_KEYS = ["DITTOBENCH_DB"]


@pytest.fixture(autouse=True)
def _fresh_clock() -> None:
    """Heartbeats go stale after five minutes, and the admin writes read the
    wall clock, so stamp each test's fleet at that test's start rather than at
    collection."""
    global _NOW
    _NOW = datetime.now(UTC).replace(microsecond=0)


# SS58-shaped and sorted. The fifth stands in for a qualifying validator the
# operator adds later (5Cg3DiRf in production).
_A, _B, _C, _D, _E = sorted(
    (
        "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
        "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
        "5CqJAjSjv8fjF9uAQpDLyfN1hZEvBjwpFgcGeLbYpcbSaD1C",
        "5Cg3DiRfvXyfz4ZrNvkMJE2KfaZQMNcfQbSdtiDmLD1BNqKd",
    )
)
_FOUR = (_A, _B, _C, _D)


def _packet(source: str) -> dict[str, Any]:
    return {
        "source_revision": source,
        "release_descriptor_digest": "sha256:" + source[0] * 64,
        "scorer_image_digest": "sha256:" + source[1] * 64,
        "scorer_env_sha256": hashlib.sha256(
            ("scored-runtime-env-v1\n13\n" + source + "\n" + "\n".join(_KEYS)).encode()
        ).hexdigest(),
        "injected_keys": _KEYS,
    }


_OLD = _packet("ab" * 20)
_NEW = _packet("cd" * 20)


def _managed(hotkey: str, packet: dict[str, Any] = _NEW) -> ValidatorHeartbeat:
    """A fresh, accepting, managed V13 heartbeat signing ``packet``."""
    row = _heartbeat(hotkey, _NOW, versions=[7, 13], protocol_version=18)
    row.benchmark_capacity = {
        "configured_slots": 1,
        "healthy_slots": ["slot-0"],
        "admission": "accepting",
        "active": [],
    }
    assert row.stack is not None and row.capabilities is not None
    row.stack["mode"] = "managed"
    row.stack["release_descriptor_digest"] = packet["release_descriptor_digest"]
    for component in row.stack["components"].values():
        component["provenance"] = "signed_descriptor"
        component["image_digest"] = packet["scorer_image_digest"]
        component["source_revision"] = packet["source_revision"]
    row.capabilities["scorer_benchmarks"]["source_revision"] = packet["source_revision"]
    row.capabilities["scorer_benchmarks"]["scored_runtime_env"] = {
        "bench_version": 13,
        "scope": "scorer-injected-env-only",
        "source_revision": packet["source_revision"],
        "injected_keys": _KEYS,
        "sha256": packet["scorer_env_sha256"],
    }
    return row


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    *,
    heartbeats: list[ValidatorHeartbeat],
    pin: tuple[str, ...] | None,
    paused: tuple[str, ...] = (),
) -> ValidatorSlotSettings:
    settings = ValidatorSlotSettings(paused_validator_hotkeys=list(paused))
    async with maker() as session, session.begin():
        session.add_all(heartbeats)
        session.add(
            ValidatorSlotSettingsRevision(
                parent_revision=0,
                scope="*",
                settings=settings.model_dump(mode="json"),
                checksum=_checksum(settings),
                reason="test slot settings",
                actor="test",
            )
        )
        if pin is not None:
            session.add(
                V13ScorerCohortPin(
                    bench_version=13,
                    hotkeys=list(pin),
                    packet=_NEW,
                    slot_settings_revision=1,
                    slot_settings_checksum=_checksum(settings),
                    reason="test exact signed scorer cohort",
                    actor="test",
                )
            )
    return settings


async def _allowed(
    maker: async_sessionmaker[AsyncSession], *hotkeys: str
) -> dict[str, bool]:
    async with maker() as session:
        return {
            hotkey: await pinned_validator_allowed(session, hotkey=hotkey, now=_NOW)
            for hotkey in hotkeys
        }


async def _evidence(maker: async_sessionmaker[AsyncSession]):
    async with maker() as session:
        return await scored_runtime_evidence_for_lease(
            session,
            attempt_id=uuid4(),
            artifact_sha256="f" * 64,
            policy_version=13,
            bench_version=13,
            now=_NOW,
        )


class TestAuthorityPacket:
    """The pure rule, independent of storage."""

    old = V13ScorerPacket.model_validate(_OLD)
    new = V13ScorerPacket.model_validate(_NEW)

    def test_three_identical_is_authority(self) -> None:
        authority = authority_from_packets({_A: self.new, _B: self.new, _C: self.new})
        assert authority is not None
        assert authority.packet == self.new
        assert authority.supporters == (_A, _B, _C)

    @pytest.mark.parametrize(
        "packets",
        [
            pytest.param({_A: new, _B: new, _C: old}, id="three-one-differs"),
            pytest.param({_A: new, _B: new, _C: None}, id="three-one-offline"),
            pytest.param({_A: new, _B: new, _C: old, _D: old}, id="four-split-2-2"),
            pytest.param({_A: new, _B: new, _C: None, _D: None}, id="four-two-offline"),
            pytest.param({_A: None, _B: None, _C: None}, id="nobody-live"),
        ],
    )
    def test_no_majority_of_three_is_no_authority(
        self, packets: dict[str, V13ScorerPacket | None]
    ) -> None:
        assert authority_from_packets(packets) is None

    def test_majority_needs_more_than_half_of_live_members(self) -> None:
        """Three of six live is a tie with the other three, not a majority."""
        others = [_packet(f"{n}" * 40) for n in range(3)]
        packets: dict[str, V13ScorerPacket | None] = {
            _A: self.new,
            _B: self.new,
            _C: self.new,
            **{
                f"x{n}": V13ScorerPacket.model_validate(packet)
                for n, packet in enumerate(others)
            },
        }
        assert authority_from_packets(packets) is None
        packets["x2"] = self.new
        authority = authority_from_packets(packets)
        assert authority is not None and authority.packet == self.new


class TestIssuance:
    async def test_three_members_identical_all_allowed(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed(
            session_maker,
            heartbeats=[_managed(h) for h in (_A, _B, _C)],
            pin=(_A, _B, _C),
        )
        assert await _allowed(session_maker, _A, _B, _C) == dict.fromkeys(
            (_A, _B, _C), True
        )

    async def test_three_members_one_differing_none_allowed(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The original unanimity rule, unchanged for a three-member pin."""
        await _seed(
            session_maker,
            heartbeats=[_managed(_A), _managed(_B), _managed(_C, _OLD)],
            pin=(_A, _B, _C),
        )
        assert await _allowed(session_maker, _A, _B, _C) == dict.fromkeys(
            (_A, _B, _C), False
        )
        assert await _evidence(session_maker) is None

    async def test_four_members_one_stale_packet_others_allowed(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed(
            session_maker,
            heartbeats=[_managed(_A), _managed(_B), _managed(_C), _managed(_D, _OLD)],
            pin=_FOUR,
        )
        assert await _allowed(session_maker, *_FOUR) == {
            _A: True,
            _B: True,
            _C: True,
            _D: False,
        }
        # The stale member may still finish a lease issued before it diverged.
        async with session_maker() as session:
            assert await pinned_validator_may_submit(session, hotkey=_D, now=_NOW)
        evidence = await _evidence(session_maker)
        assert evidence is not None
        assert evidence.scorer_source_revision == _NEW["source_revision"]
        assert evidence.validator_count == 3

    async def test_four_members_one_offline_others_allowed(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        offline = _managed(_D)
        offline.seen_at = _NOW - timedelta(days=1)
        offline.reported_at = offline.seen_at
        await _seed(
            session_maker,
            heartbeats=[_managed(_A), _managed(_B), _managed(_C), offline],
            pin=_FOUR,
        )
        assert await _allowed(session_maker, *_FOUR) == {
            _A: True,
            _B: True,
            _C: True,
            _D: False,
        }

    async def test_four_members_split_two_two_none_allowed(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed(
            session_maker,
            heartbeats=[
                _managed(_A),
                _managed(_B),
                _managed(_C, _OLD),
                _managed(_D, _OLD),
            ],
            pin=_FOUR,
        )
        assert await _allowed(session_maker, *_FOUR) == dict.fromkeys(_FOUR, False)
        assert await _evidence(session_maker) is None

    async def test_nonmember_on_the_authority_packet_is_declined(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed(
            session_maker,
            heartbeats=[_managed(h) for h in (*_FOUR, _E)],
            pin=_FOUR,
            paused=(_E,),
        )
        allowed = await _allowed(session_maker, *_FOUR, _E)
        assert allowed == {**dict.fromkeys(_FOUR, True), _E: False}
        async with session_maker() as session:
            assert not await pinned_validator_may_submit(session, hotkey=_E, now=_NOW)
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.SCORED, sha256="1" * 64
        )
        async with session_maker() as session:
            assert (
                await issue_confirmation_ticket(
                    session,
                    agent_id=agent_id,
                    validator_hotkey=_E,
                    now=_NOW,
                    ttl=timedelta(minutes=30),
                    bench_version=13,
                )
                is None
            )
            assert (
                await issue_confirmation_ticket(
                    session,
                    agent_id=agent_id,
                    validator_hotkey=_D,
                    now=_NOW,
                    ttl=timedelta(minutes=30),
                    bench_version=13,
                )
                is not None
            )
            await session.rollback()
        evidence = await _evidence(session_maker)
        assert evidence is not None and evidence.validator_count == 4

    async def test_a_paused_member_votes_but_is_not_routable(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Pause keeps the member out of screening evidence and new leases.

        It still counts toward the live majority, so pausing one member of a
        three-member cohort leaves the other two issuable, as before.
        """
        await _seed(
            session_maker,
            heartbeats=[_managed(h) for h in (_A, _B, _C)],
            pin=(_A, _B, _C),
            paused=(_C,),
        )
        assert await _allowed(session_maker, _A, _B) == {_A: True, _B: True}
        assert await _evidence(session_maker) is None

    async def test_preflight_and_report_show_the_authority(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed(
            session_maker,
            heartbeats=[_managed(_A), _managed(_B), _managed(_C), _managed(_D, _OLD)],
            pin=_FOUR,
        )
        async with session_maker() as session:
            preflight = await get_preflight(None, session)
            report = await get_report_only_current_packet(None, session)
        assert preflight.pinned_hotkeys == list(_FOUR)
        assert preflight.authority_packet is not None
        assert preflight.authority_packet.model_dump(mode="json") == _NEW
        assert preflight.authority_hotkeys == [_A, _B, _C]
        assert {item.hotkey for item in preflight.validators if item.member} == set(
            _FOUR
        )
        assert report is not None
        assert report.hotkeys == list(_FOUR)
        assert report.routable_hotkeys == [_A, _B, _C]
        assert report.matches_effective_pin


def _activate_body(hotkeys: list[str], **overrides: Any) -> dict[str, Any]:
    return {
        "hotkeys": hotkeys,
        "packet": _NEW,
        "expected_slot_settings_revision": 1,
        "expected_slot_settings_checksum": "a" * 64,
        "reason": "test exact signed scorer cohort",
        "actor": "test",
        "confirmation": "PIN V13 SCORER COHORT",
        **overrides,
    }


def _ss58(index: int) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return "5" + alphabet[index // len(alphabet)] * 45 + alphabet[index % 58] + "Q"


class TestRequestCardinality:
    @pytest.mark.parametrize("size", [3, 4, 16])
    def test_accepts_three_to_sixteen(self, size: int) -> None:
        hotkeys = sorted(_ss58(n) for n in range(size))
        request = ActivateV13ScorerCohortRequest.model_validate(_activate_body(hotkeys))
        assert len(request.hotkeys) == size
        rotate = RotateV13ScorerCohortRequest.model_validate(
            {
                **_activate_body(hotkeys),
                "expected_current_packet": _OLD,
                "expected_current_hotkeys": hotkeys,
            }
        )
        assert rotate.expected_current_hotkeys == tuple(hotkeys)

    @pytest.mark.parametrize(
        "hotkeys",
        [
            pytest.param(sorted(_ss58(n) for n in range(2)), id="two"),
            pytest.param(sorted(_ss58(n) for n in range(17)), id="seventeen"),
            pytest.param([_A, _A, _B, _C], id="duplicate"),
            pytest.param([_B, _A, _C], id="unsorted"),
            pytest.param([_A, _B, "not-a-hotkey"], id="not-ss58"),
        ],
    )
    def test_rejects_out_of_range_duplicate_or_unsorted(
        self, hotkeys: list[str]
    ) -> None:
        with pytest.raises(ValidationError):
            ActivateV13ScorerCohortRequest.model_validate(_activate_body(hotkeys))
        with pytest.raises(ValidationError):
            RotateV13ScorerCohortRequest.model_validate(
                {
                    **_activate_body([_A, _B, _C]),
                    "expected_current_packet": _OLD,
                    "expected_current_hotkeys": hotkeys,
                }
            )


class TestActivateAndRotateMembership:
    async def test_activate_four_members(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        settings = await _seed(
            session_maker,
            heartbeats=[_managed(h) for h in (*_FOUR, _E)],
            pin=None,
            paused=(_E,),
        )
        async with session_maker() as session:
            row = await activate_pin(
                ActivateV13ScorerCohortRequest.model_validate(
                    _activate_body(
                        list(_FOUR),
                        expected_slot_settings_checksum=_checksum(settings),
                    )
                ),
                None,
                session,
            )
        assert row.hotkeys == list(_FOUR)
        assert await _allowed(session_maker, *_FOUR, _E) == {
            **dict.fromkeys(_FOUR, True),
            _E: False,
        }

    async def _seed_three_plus_candidate(
        self, maker: async_sessionmaker[AsyncSession]
    ) -> ValidatorSlotSettings:
        """The production shape: a three-member pin and a fourth on the packet."""
        return await _seed(
            maker,
            heartbeats=[_managed(h) for h in _FOUR],
            pin=(_A, _B, _C),
        )

    def _add_member(
        self, settings: ValidatorSlotSettings, **overrides: Any
    ) -> RotateV13ScorerCohortRequest:
        return RotateV13ScorerCohortRequest.model_validate(
            {
                "hotkeys": list(_FOUR),
                "packet": _NEW,
                "expected_current_packet": _NEW,
                "expected_current_rotation_id": None,
                "expected_current_hotkeys": [_A, _B, _C],
                "expected_slot_settings_revision": 1,
                "expected_slot_settings_checksum": _checksum(settings),
                "reason": "add a fourth qualifying validator to the cohort",
                "actor": "operator@example.com",
                "confirmation": "ROTATE V13 SCORER PACKET",
                **overrides,
            }
        )

    async def test_add_a_member_keeps_retained_members_live_leases(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        settings = await self._seed_three_plus_candidate(session_maker)
        assert await _allowed(session_maker, _D) == {_D: False}
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, sha256="e" * 64
        )
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    bench_version=13,
                    validator_hotkey=_A,
                    slot_id="slot-0",
                    status=TicketStatus.ISSUED,
                    issued_at=_NOW,
                    deadline=_NOW + timedelta(minutes=30),
                )
            )
        async with session_maker() as session:
            rotated = await rotate_pin(self._add_member(settings), None, session)
        assert rotated.hotkeys == list(_FOUR)
        assert rotated.packet == _NEW and rotated.previous_packet == _NEW
        assert await _allowed(session_maker, *_FOUR) == dict.fromkeys(_FOUR, True)

    async def test_membership_must_be_bound_to_the_current_pin(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        settings = await self._seed_three_plus_candidate(session_maker)
        for request in (
            # Omitting the current membership means "same members".
            self._add_member(settings, expected_current_hotkeys=None),
            self._add_member(settings, expected_current_hotkeys=[_A, _B, _D]),
        ):
            async with session_maker() as session:
                with pytest.raises(HTTPException, match="current V13 cohort changed"):
                    await rotate_pin(request, None, session)
        no_change = self._add_member(
            settings, hotkeys=[_A, _B, _C], expected_current_hotkeys=None
        )
        async with session_maker() as session:
            with pytest.raises(HTTPException, match="must change the scorer packet"):
                await rotate_pin(no_change, None, session)

    async def test_a_new_member_must_sign_the_target_and_accept(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        settings = await self._seed_three_plus_candidate(session_maker)
        async with session_maker() as session, session.begin():
            heartbeat = await session.get(ValidatorHeartbeat, _D)
            assert heartbeat is not None and heartbeat.benchmark_capacity is not None
            heartbeat.benchmark_capacity = {
                **heartbeat.benchmark_capacity,
                "admission": "draining",
                "healthy_slots": [],
            }
        async with session_maker() as session:
            with pytest.raises(HTTPException, match="not accepting"):
                await rotate_pin(self._add_member(settings), None, session)
        async with session_maker() as session, session.begin():
            heartbeat = await session.get(ValidatorHeartbeat, _D)
            assert heartbeat is not None
            replacement = _managed(_D, _OLD)
            heartbeat.stack = replacement.stack
            heartbeat.capabilities = replacement.capabilities
            heartbeat.benchmark_capacity = replacement.benchmark_capacity
        async with session_maker() as session:
            with pytest.raises(HTTPException, match="member signed packet"):
                await rotate_pin(self._add_member(settings), None, session)

    async def test_a_new_member_must_not_be_paused(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        settings = await _seed(
            session_maker,
            heartbeats=[_managed(h) for h in _FOUR],
            pin=(_A, _B, _C),
            paused=(_D,),
        )
        async with session_maker() as session:
            with pytest.raises(HTTPException, match="pause state"):
                await rotate_pin(self._add_member(settings), None, session)

    async def test_a_degraded_retained_member_does_not_block_adding_one(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The incident: the degraded member stays pinned but is not required."""
        settings = await self._seed_three_plus_candidate(session_maker)
        async with session_maker() as session, session.begin():
            heartbeat = await session.get(ValidatorHeartbeat, _C)
            assert heartbeat is not None and heartbeat.benchmark_capacity is not None
            heartbeat.benchmark_capacity = {
                **heartbeat.benchmark_capacity,
                "admission": "draining",
                "healthy_slots": [],
            }
        async with session_maker() as session:
            rotated = await rotate_pin(self._add_member(settings), None, session)
        assert rotated.hotkeys == list(_FOUR)
        evidence = await _evidence(session_maker)
        assert evidence is not None and evidence.validator_count == 3

    async def test_remove_a_member_drains_only_its_live_tickets(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        settings = await _seed(
            session_maker,
            heartbeats=[_managed(h) for h in _FOUR],
            pin=_FOUR,
        )
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, sha256="e" * 64
        )
        async with session_maker() as session, session.begin():
            for hotkey in (_A, _D):
                session.add(
                    ValidatorTicket(
                        agent_id=agent_id,
                        bench_version=13,
                        validator_hotkey=hotkey,
                        slot_id="slot-0",
                        status=TicketStatus.ISSUED,
                        issued_at=_NOW,
                        deadline=_NOW + timedelta(minutes=30),
                    )
                )
        remove = self._add_member(
            settings,
            hotkeys=[_A, _B, _C],
            expected_current_hotkeys=list(_FOUR),
            reason="remove a degraded validator from the cohort",
        )
        # The removed member must be paused: every capable nonmember is.
        async with session_maker() as session:
            with pytest.raises(HTTPException, match="nonmember V13 validator"):
                await rotate_pin(remove, None, session)
        paused = ValidatorSlotSettings(paused_validator_hotkeys=[_D])
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorSlotSettingsRevision(
                    parent_revision=1,
                    scope="*",
                    settings=paused.model_dump(mode="json"),
                    checksum=_checksum(paused),
                    reason="pause the member being removed",
                    actor="test",
                )
            )
        remove = remove.model_copy(
            update={
                "expected_slot_settings_revision": 2,
                "expected_slot_settings_checksum": _checksum(paused),
            }
        )
        # Its live lease must drain; the retained member's lease may stay.
        async with session_maker() as session:
            with pytest.raises(HTTPException, match="removed members' live V13"):
                await rotate_pin(remove, None, session)
        async with session_maker() as session, session.begin():
            ticket = await session.get(ValidatorTicket, (agent_id, 13, _D))
            assert ticket is not None
            ticket.status = TicketStatus.EXPIRED
        async with session_maker() as session:
            rotated = await rotate_pin(remove, None, session)
        assert rotated.hotkeys == [_A, _B, _C]
        assert await _allowed(session_maker, *_FOUR) == {
            _A: True,
            _B: True,
            _C: True,
            _D: False,
        }


class TestPostWriteRoutingCheck:
    """The post-write check names which of its two failures happened.

    No routable packet is an admission problem (pause, capacity, probe); a
    routable but different packet is a stale target. Both refuse the write.
    """

    @staticmethod
    def _route(monkeypatch: pytest.MonkeyPatch, routed: CohortPacket | None) -> None:
        async def fake(*_args: object, **_kwargs: object) -> CohortPacket | None:
            return routed

        monkeypatch.setattr(admin_v13_scorer_cohort, "pinned_cohort_packet", fake)

    _OTHER = CohortPacket(
        packet=V13ScorerPacket.model_validate(_OLD),
        oldest_scorer_observed_at=0,
        routable=(_A, _B, _C),
    )

    @pytest.mark.parametrize(
        ("routed", "message"),
        [
            pytest.param(None, "fewer than three proposed members", id="none"),
            pytest.param(
                _OTHER,
                f"routes signed packet source_revision {_OLD['source_revision']}, "
                f"not the target {_NEW['source_revision']}",
                id="different",
            ),
        ],
    )
    async def test_activate(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        routed: CohortPacket | None,
        message: str,
    ) -> None:
        settings = await _seed(
            session_maker, heartbeats=[_managed(h) for h in _FOUR], pin=None
        )
        self._route(monkeypatch, routed)
        async with session_maker() as session:
            with pytest.raises(HTTPException, match=message):
                await activate_pin(
                    ActivateV13ScorerCohortRequest.model_validate(
                        _activate_body(
                            list(_FOUR),
                            expected_slot_settings_checksum=_checksum(settings),
                        )
                    ),
                    None,
                    session,
                )
        async with session_maker() as session:
            assert await session.get(V13ScorerCohortPin, 13) is None

    @pytest.mark.parametrize(
        ("routed", "message"),
        [
            pytest.param(None, "fewer than three proposed members", id="none"),
            pytest.param(
                _OTHER,
                f"routes signed packet source_revision {_OLD['source_revision']}, "
                f"not the target {_NEW['source_revision']}",
                id="different",
            ),
        ],
    )
    async def test_rotate(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        routed: CohortPacket | None,
        message: str,
    ) -> None:
        settings = await _seed(
            session_maker, heartbeats=[_managed(h) for h in _FOUR], pin=(_A, _B, _C)
        )
        self._route(monkeypatch, routed)
        request = TestActivateAndRotateMembership()._add_member(settings)
        async with session_maker() as session:
            with pytest.raises(HTTPException, match=message):
                await rotate_pin(request, None, session)
        async with session_maker() as session:
            assert await session.scalar(select(V13ScorerCohortRotation)) is None
