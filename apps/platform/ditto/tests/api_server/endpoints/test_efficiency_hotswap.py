"""End-to-end tests for the HOT-SWAPPABLE efficiency bonus.

Proves the operational contract of the fast-follow to #403: an operator flips
the bonus / fold / knobs via the admin settings endpoint (the platform-native
successor to the boot env), and the change lands on the NEXT compute /
leaderboard / ledger read of the SAME running app — no restart — while every
already-frozen epoch snapshot keeps its own knobs, so a published bonus never
moves. Default-off (no revision) stays byte-identical to pre-change, and
bench_version < 7 boards are untouched.

Reuses the board-seeding helpers from ``test_public_efficiency`` and wires the
resolver with ``ttl_seconds=0`` so each read re-reads the latest revision (the
production default TTL is small but nonzero; ``invalidate()`` on write also
makes a change land immediately).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_server import EfficiencyBonusConfig, create_api_server
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.efficiency_settings import EfficiencyBonusSettingsResolver
from ditto.chain.models import NeuronInfo
from ditto.db.models import EfficiencyBonus, EfficiencyCohortSnapshot
from ditto.tests.api_server.conftest import make_api_server_config
from ditto.tests.api_server.endpoints.test_public_efficiency import (
    _KEYPAIR,
    _MINERS,
    _VALIDATOR_HOTKEY,
    _entry,
    _seed_finalized,
    _seed_v7_board,
)
from ditto.tests.legacy_era import retired_era_writes_allowed

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_SETTINGS_URL = "/api/v1/admin/efficiency-bonus-settings"


def _make_hotswap_app(
    maker: async_sessionmaker[AsyncSession],
    *,
    seed: EfficiencyBonusConfig | None = None,
) -> FastAPI:
    """An app whose compute path resolves the hot-swappable policy from the DB
    (ttl=0) with the request session maker wired, so a revision written through
    the admin endpoint is visible to the next leaderboard / ledger read."""
    import os

    os.environ["PUBLIC_CACHE_DISABLED"] = "1"
    seed = seed if seed is not None else EfficiencyBonusConfig()
    app = create_api_server(
        make_api_server_config(efficiency_bonus=seed, admin_api_token=_ADMIN_TOKEN)
    )
    app.state.commit_hash = "test-commit"
    app.state.session_maker = maker
    app.state.efficiency_settings = EfficiencyBonusSettingsResolver(seed, ttl_seconds=0)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    return app


def _install_chain(app: FastAPI) -> None:
    async def _chain() -> MagicMock:
        c = MagicMock()
        c.get_recent_neurons = AsyncMock(
            return_value=[
                NeuronInfo(
                    hotkey=_VALIDATOR_HOTKEY,
                    coldkey="5GReceiverColdkeyPlaceholderXXXXXXXXXXXXXXXXXXX",
                    uid=1,
                    stake=1000.0,
                    validator_permit=True,
                )
            ]
        )
        return c

    app.dependency_overrides[get_chain_client] = _chain


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


def _settings_body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "enabled": True,
        "fold_enabled": False,
        "cap": 0.05,
        "deep_cap": 0.10,
        "deep_frontier_ratio": 0.5,
        "factor_alpha": 0.25,
        "minimum_factor": 0.85,
        "maximum_factor": 1.10,
        "cohort_size": 25,
        "min_cohort": 3,  # the seeded board has 3 agents
        "epoch_hours": 24,
        "quality_floor": 0.0,
        "memory_floor": 0.0,
    }
    base.update(overrides)
    return base


async def _apply(
    client: httpx.AsyncClient, *, expected: int = 0, **overrides: Any
) -> dict:
    body = _settings_body(**overrides)
    payload = {
        "scope": "*",
        "expected_revision": expected,
        "settings": body,
        "reason": "hot-swap the efficiency bonus for a canary",
        "actor": "backroom:test",
        "confirmation": (
            f"APPLY EFFICIENCY BONUS {'ENABLED' if body['enabled'] else 'DISABLED'}"
        ),
    }
    resp = await client.post(_SETTINGS_URL, headers=_ADMIN_HEADERS, json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ledger_headers() -> dict[str, str]:
    nonce = uuid4()
    requested_at = datetime.now(UTC)
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    signed = f"validator-ledger:v1:{_VALIDATOR_HOTKEY}:{nonce}:{requested}".encode()
    return {
        "X-Validator-Hotkey": _VALIDATOR_HOTKEY,
        "X-Validator-Ledger-Nonce": str(nonce),
        "X-Validator-Ledger-Requested-At": requested_at.isoformat(),
        "X-Validator-Ledger-Signature": _KEYPAIR.sign(signed).hex(),
    }


class TestRuntimeFlip:
    async def test_enable_flip_materializes_on_next_read_no_restart(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        agents = await _seed_v7_board(session_maker)
        app = _make_hotswap_app(session_maker)
        async with _client(app) as client:
            # No revision yet: default-off. The board previews rather than
            # hiding the block, but nothing is applied and nothing is written.
            before = (await client.get("/api/v1/public/leaderboard")).json()
            assert before["efficiency"]["preview"] is True
            assert before["efficiency"]["active"] is False
            assert _entry(before, agents["lean"])["efficiency_bonus"] is None
            assert _entry(before, agents["lean"])["effective_composite"] is None
            async with session_maker() as s:
                assert (await s.scalars(select(EfficiencyCohortSnapshot))).all() == []
                assert (await s.scalars(select(EfficiencyBonus))).all() == []

            # Operator flips the bonus ON from backroom — no redeploy.
            await _apply(client, expected=0, enabled=True)

            # The very next leaderboard read of the SAME app materializes.
            after = (await client.get("/api/v1/public/leaderboard")).json()
            assert after["efficiency"] is not None
            lean = _entry(after, agents["lean"])
            assert lean["efficiency_bonus"] == 0.05
            assert lean["effective_composite"] == pytest.approx(0.80 * 1.05)
            async with session_maker() as s:
                assert (
                    len((await s.scalars(select(EfficiencyCohortSnapshot))).all()) == 1
                )

    async def test_disable_flip_stops_new_materialization(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_v7_board(session_maker)
        app = _make_hotswap_app(session_maker)
        async with _client(app) as client:
            await _apply(client, expected=0, enabled=True)
            (await client.get("/api/v1/public/leaderboard")).json()
            # Roll back: disable. A new epoch would not snapshot, and the board
            # drops back to an unapplied preview -- visible, but not awarded.
            await _apply(client, expected=1, enabled=False)
            rolled = (await client.get("/api/v1/public/leaderboard")).json()
            assert rolled["efficiency"]["preview"] is True
            assert rolled["efficiency"]["active"] is False
            for entry in rolled["entries"]:
                assert entry["efficiency_bonus"] is None
                assert entry["effective_composite"] is None
            # Exactly the one snapshot the enabled read froze; the disabled read
            # added nothing.
            async with session_maker() as s:
                assert (
                    len((await s.scalars(select(EfficiencyCohortSnapshot))).all()) == 1
                )


class TestReproducibility:
    async def test_mid_epoch_knob_change_does_not_mutate_frozen_snapshot(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        agents = await _seed_v7_board(session_maker)
        app = _make_hotswap_app(session_maker)
        async with _client(app) as client:
            # Freeze this epoch's snapshot under a NON-default cap=0.08, proving
            # the revision knob (not the seed default 0.05) drives compute-time
            # materialization.
            await _apply(client, expected=0, enabled=True, cap=0.08)
            first = (await client.get("/api/v1/public/leaderboard")).json()
            assert _entry(first, agents["lean"])["efficiency_bonus"] == 0.08

            async with session_maker() as s:
                snap = (await s.scalars(select(EfficiencyCohortSnapshot))).one()
                assert snap.bonus_cap == 0.08  # effective knob frozen INTO snapshot
                frozen_id = snap.snapshot_id

            # Mid-epoch retune to cap=0.05. Same wall-clock epoch → the frozen
            # snapshot and its insert-once bonuses must not move.
            await _apply(client, expected=1, enabled=True, cap=0.05)
            second = (await client.get("/api/v1/public/leaderboard")).json()
            assert _entry(second, agents["lean"])["efficiency_bonus"] == 0.08

            async with session_maker() as s:
                snaps = (await s.scalars(select(EfficiencyCohortSnapshot))).all()
                assert len(snaps) == 1
                assert snaps[0].snapshot_id == frozen_id
                assert snaps[0].bonus_cap == 0.08  # still the frozen policy


class TestBenchVersionGate:
    async def test_pre_v7_board_untouched_even_with_revision_enabled(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A finalized v6 board takes no bonus, whatever the operator sets.

        The board this describes is history now -- v6 is retired, the active
        version only moves forward, and neither the activated v2 -> v6 rollout
        row nor a v6 score can be written any more. The guard is not history,
        though: the rows it protects are still in the ledger, so it is seeded
        the way production came by its own, with the floor lifted for the write
        and restored (NOT VALID) before the read under test.
        """
        from ditto.tests.api_server.endpoints.test_public_efficiency import (
            _activate_bench_version,
        )

        async with (
            session_maker() as floor_session,
            retired_era_writes_allowed(floor_session),
        ):
            await _activate_bench_version(session_maker, 6)
            agent = await _seed_finalized(
                session_maker,
                miner=_MINERS[0],
                composite=0.80,
                total_tokens=100_000,
                bench_version=6,
            )
        app = _make_hotswap_app(session_maker)
        async with _client(app) as client:
            await _apply(client, expected=0, enabled=True)
            board = (await client.get("/api/v1/public/leaderboard")).json()
        assert board["efficiency"] is None
        assert _entry(board, agent)["efficiency_bonus"] is None
        async with session_maker() as s:
            assert (await s.scalars(select(EfficiencyCohortSnapshot))).all() == []


class TestFoldGating:
    async def test_fold_gated_on_enabled_and_flips_at_read_time(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        agents = await _seed_v7_board(session_maker)
        app = _make_hotswap_app(session_maker)
        _install_chain(app)
        async with _client(app) as client:
            # Enable the bonus with the fold OFF, then materialize.
            await _apply(client, expected=0, enabled=True, fold_enabled=False)
            (await client.get("/api/v1/public/leaderboard")).json()

            off = (
                await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
            ).json()
            assert off["count"] == 3
            for entry in off["entries"]:
                assert entry["efficiency_bonus"] is None
                assert entry["effective_composite"] is None

            # Flip the fold ON — the next ledger read of the same app folds the
            # already-frozen bonuses, no restart.
            await _apply(client, expected=1, enabled=True, fold_enabled=True)
            on = (
                await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
            ).json()
            by_agent = {entry["agent_id"]: entry for entry in on["entries"]}
            lean = by_agent[str(agents["lean"])]
            assert lean["efficiency_bonus"] == 0.05
            assert lean["effective_composite"] == pytest.approx(0.80 * 1.05)
