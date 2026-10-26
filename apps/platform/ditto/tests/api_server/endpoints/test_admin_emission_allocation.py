"""Real-Postgres contract for the typed-weights emission allocation (SN-350)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.emission_allocation import EmissionAllocation
from ditto.api_models.emission_allocation_math import (
    AllocationEntry,
    AllocationError,
    canonical_entries,
    confirmation_phrase,
    derive_served,
    effective_split,
    entries_from_split,
)
from ditto.api_server.burn_settings import settings_from_row as burn_from_row
from ditto.api_server.dependencies import get_session
from ditto.api_server.emission_allocation import (
    ServedAllocation,
    overlay_served,
    served_from_row,
)
from ditto.api_server.treasury_runtime import canonical_settings
from ditto.db.models import (
    BurnSettingsRevision,
    EmissionAllocationRevision,
    LedgerEpochSnapshot,
    TrackShareSettingsRevision,
    TreasurySettingsRevision,
)

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
_URL = "/api/v1/admin/emission-allocation"
_CURVE = [0.5, 0.2, 0.13, 0.1, 0.07]
_LIVE = [
    {"kind": "burn", "bps": 5000},
    {"kind": "track", "id": "memory", "bps": 3500},
    {"kind": "track", "id": "generator", "bps": 1500},
]
_LIVE_PHRASE = "APPLY EMISSION ALLOCATION burn=5000 memory=3500 generator=1500"
E = AllocationEntry


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    for name in ("emission_allocation", "burn_settings", "track_share_settings"):
        getattr(app.state, name).invalidate()


def _payload(
    entries: list[dict[str, Any]],
    *,
    expected_revision: int = 0,
    confirmation: str | None = None,
) -> dict[str, Any]:
    if confirmation is None:
        allocation = EmissionAllocation.model_validate({"entries": entries})
        confirmation = confirmation_phrase(allocation.math_entries())
    return {
        "expected_revision": expected_revision,
        "settings": {"entries": entries},
        "reason": "operator-approved emission split",
        "actor": "operator@example.com",
        "confirmation": confirmation,
    }


def _checksum(raw: dict) -> str:
    return canonical_settings(raw)


async def _seed_live(maker: async_sessionmaker[AsyncSession]) -> None:
    """Production today: burn rev with 0.5 + curve, track rev 7000/3000."""
    burn = {"burn_share": 0.5, "rank_shares": _CURVE}
    track = {"track_shares_bps": {"memory": 7000, "generator": 3000}}
    async with maker() as session, session.begin():
        session.add(
            BurnSettingsRevision(
                parent_revision=0,
                scope="*",
                settings=burn,
                checksum=_checksum(burn),
                reason="production burn 50%",
                actor="operator@example.com",
            )
        )
        session.add(
            TrackShareSettingsRevision(
                parent_revision=0,
                scope="*",
                settings=track,
                checksum=_checksum(track),
                reason="production generator 30%",
                actor="operator@example.com",
            )
        )


async def _count(maker: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with maker() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# ─── Pure derivation ────────────────────────────────────────────────────────


def test_live_split_derives_exactly_todays_served_fields() -> None:
    derived = derive_served(
        [
            E("burn", None, 5000),
            E("track", "memory", 3500),
            E("track", "generator", 1500),
        ]
    )
    assert derived.burn_share == 0.5
    assert derived.track_shares_bps == {"memory": 7000, "generator": 3000}
    assert derived.gamma_bps == {}
    assert effective_split(
        burn_share=0.5,
        track_shares_bps={"memory": 7000, "generator": 3000},
        gamma_bps={},
    ) == {"memory": 3500.0, "generator": 1500.0, "burn": 5000.0}


def test_confirmation_spells_every_nonzero_entry_in_canonical_order() -> None:
    entries = canonical_entries(
        [
            E("gamma", "gm", 500),
            E("track", "generator", 1500),
            E("track", "router", 0),
            E("track", "memory", 3500),
            E("burn", None, 4500),
        ]
    )
    assert [e.label for e in entries] == [
        "burn",
        "memory",
        "router",
        "generator",
        "gamma:gm",
    ]
    assert confirmation_phrase(entries) == (
        "APPLY EMISSION ALLOCATION burn=4500 memory=3500 generator=1500 gamma:gm=500"
    )


def test_gamma_is_carved_before_the_burn_and_round_trips() -> None:
    entries = [
        E("burn", None, 4500),
        E("track", "memory", 3500),
        E("track", "generator", 1500),
        E("gamma", "gm", 500),
    ]
    derived = derive_served(entries)
    assert derived.burn_fraction.numerator == 9
    assert derived.burn_fraction.denominator == 19
    assert derived.track_shares_bps == {"memory": 7000, "generator": 3000}
    folded = effective_split(
        burn_share=derived.burn_share,
        track_shares_bps=derived.track_shares_bps,
        gamma_bps=derived.gamma_bps,
    )
    assert entries_from_split(folded) == canonical_entries(entries)


def test_full_burn_keeps_the_current_track_map() -> None:
    derived = derive_served(
        [E("burn", None, 10000), E("track", "memory", 0), E("track", "generator", 0)]
    )
    assert derived.burn_share == 1.0
    assert derived.track_shares_bps is None


def test_a_single_paid_track_and_burn_half_are_always_representable() -> None:
    for memory in range(0, 5001, 7):
        derived = derive_served(
            [
                E("burn", None, 5000),
                E("track", "memory", memory),
                E("track", "generator", 5000 - memory),
            ]
        )
        assert derived.track_shares_bps is not None
        assert derived.track_shares_bps["memory"] == 2 * memory
    only = derive_served(
        [E("burn", None, 3333), E("track", "memory", 6667), E("track", "generator", 0)]
    )
    assert only.track_shares_bps == {"memory": 10000}


@pytest.mark.parametrize(
    "entries, message",
    [
        (
            [
                E("burn", None, 4000),
                E("track", "memory", 4000),
                E("track", "generator", 2000),
            ],
            "not exactly representable",
        ),
        (
            [
                E("burn", None, 5000),
                E("track", "memory", 3500),
                E("track", "generator", 1400),
            ],
            "sum to 9900",
        ),
        (
            [
                E("burn", None, 5000),
                E("track", "memory", 3500),
                E("track", "generator", 1600),
            ],
            "sum to 10100",
        ),
        (
            [
                E("burn", None, 5000),
                E("track", "memory", 5000),
                E("track", "memory", 0),
            ],
            "duplicate",
        ),
        (
            [
                E("burn", None, 4000),
                E("track", "memory", 5000),
                E("track", "generator", 0),
                E("track", "coding", 1000),
            ],
            "no shipped validator pays track coding",
        ),
        (
            [
                E("burn", None, 4000),
                E("track", "memory", 5000),
                E("track", "generator", 0),
                E("track", "router", 1000),
            ],
            "no shipped validator pays track router",
        ),
        (
            [E("burn", None, 5000), E("track", "memory", 5000), E("track", "bogus", 0)],
            "unknown track",
        ),
        ([E("track", "memory", 5000), E("track", "generator", 5000)], "burn entry"),
        ([E("burn", None, 5000), E("track", "memory", 5000)], "missing ['generator']"),
        (
            [
                E("burn", None, 0),
                E("track", "memory", 0),
                E("track", "generator", 0),
                E("gamma", "gm", 10000),
            ],
            "whole emission",
        ),
    ],
)
def test_incoherent_allocations_are_refused(
    entries: list[AllocationEntry], message: str
) -> None:
    with pytest.raises(AllocationError, match=message.replace("[", r"\[")):
        derive_served(entries)


def test_gamma_buckets_must_match_the_treasury_policy() -> None:
    base = [
        E("burn", None, 5000),
        E("track", "memory", 5000),
        E("track", "generator", 0),
    ]
    with pytest.raises(AllocationError, match="missing \\['gm'\\]"):
        canonical_entries(base, gamma_buckets=["gm"])
    with pytest.raises(AllocationError, match="unknown Gamma bucket"):
        canonical_entries([*base, E("gamma", "other", 0)], gamma_buckets=["gm"])
    assert canonical_entries([*base, E("gamma", "gm", 0)], gamma_buckets=["gm"])


@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "reward", "bps": 0},
        {"kind": "burn", "id": "memory", "bps": 0},
        {"kind": "track", "bps": 0},
        {"kind": "gamma", "id": "gm", "bps": 0},
        {"kind": "track", "id": "memory", "bps": 1.5},
        {"kind": "track", "id": "memory", "bps": -1},
        {"kind": "track", "id": "memory", "bps": True},
    ],
)
def test_wire_entries_are_typed(entry: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EmissionAllocation.model_validate({"entries": [*_LIVE, entry]})


def test_overlay_is_a_passthrough_without_an_allocation() -> None:
    from ditto.api_models.burn_settings import BurnSettings
    from ditto.api_models.track_share_settings import TrackShareSettings

    burn = BurnSettings(burn_share=0.5, rank_shares=tuple(_CURVE))
    track = TrackShareSettings(track_shares_bps={"memory": 7000, "generator": 3000})
    assert overlay_served(None, burn=burn, track_shares=track) == (burn, track)
    served = ServedAllocation(
        revision=3,
        burn_share=0.6,
        track_shares_bps={"memory": 7500, "generator": 2500},
        gamma_bps={},
    )
    new_burn, new_track = overlay_served(served, burn=burn, track_shares=track)
    assert new_burn.burn_share == 0.6
    assert new_burn.rank_shares == tuple(_CURVE)
    assert new_track.track_shares_bps == {"memory": 7500, "generator": 2500}


def test_an_unreadable_stored_allocation_falls_back_to_legacy_settings() -> None:
    row = SimpleNamespace(revision=4, served={"burn_share": 2.0})
    assert served_from_row(row) is None  # type: ignore[arg-type]
    assert served_from_row(None) is None


# ─── Endpoint: migration, atomic derivation, coexistence ────────────────────


async def test_live_split_migrates_without_changing_a_served_byte(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from ditto.api_server.endpoints.scoring import _resolve_ledger_policy

    _install(app, session_maker)
    await _seed_live(session_maker)
    before = await _resolve_ledger_policy(app.state)
    assert before.allocation_revision == 0

    control = await client.get(_URL, headers=_HEADERS)
    assert control.status_code == 200, control.text
    body = control.json()
    assert body["source"] == "legacy_settings"
    assert body["revision"] == 0
    assert body["in_sync"] is None
    assert body["rank_shares"] == _CURVE
    shares = {
        (s["kind"], s.get("id")): s["percent"] for s in body["configured"]["split"]
    }
    assert shares == {
        ("burn", None): 50.0,
        ("track", "memory"): 35.0,
        ("track", "generator"): 15.0,
    }
    legacy = body["legacy_equivalent"]
    assert legacy["confirmation"] == _LIVE_PHRASE
    assert legacy["byte_identical"] is True
    assert legacy["allocation"]["entries"] == _LIVE

    applied = await client.post(_URL, headers=_HEADERS, json=_payload(_LIVE))
    assert applied.status_code == 200, applied.text
    revision = applied.json()
    assert revision["revision"] == 1
    assert revision["served"] == {
        "burn_share": 0.5,
        "track_shares_bps": {"memory": 7000, "generator": 3000},
        "gamma_bps": {},
    }
    # Nothing served changed, so no derived revision was written.
    assert revision["burn_settings_revision"] == 1
    assert revision["track_share_settings_revision"] == 1
    assert await _count(session_maker, BurnSettingsRevision) == 1
    assert await _count(session_maker, TrackShareSettingsRevision) == 1

    after = await _resolve_ledger_policy(app.state)
    assert after.allocation_revision == 1
    assert after.burn == before.burn
    assert after.track_shares == before.track_shares

    synced = (await client.get(_URL, headers=_HEADERS)).json()
    assert synced["source"] == "allocation"
    assert synced["in_sync"] is True
    assert synced["confirmation_hint"] == _LIVE_PHRASE


async def test_a_new_split_derives_every_field_in_one_revision(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    await _seed_live(session_maker)
    new = [
        {"kind": "burn", "bps": 6000},
        {"kind": "track", "id": "memory", "bps": 3000},
        {"kind": "track", "id": "generator", "bps": 1000},
        {"kind": "track", "id": "coding", "bps": 0},
    ]
    wrong = await client.post(
        _URL, headers=_HEADERS, json=_payload(new, confirmation=_LIVE_PHRASE)
    )
    assert wrong.status_code == 409
    assert "burn=6000 memory=3000 generator=1000" in wrong.json()["message"]
    stale = await client.post(
        _URL, headers=_HEADERS, json=_payload(new, expected_revision=3)
    )
    assert stale.status_code == 409
    unrepresentable = await client.post(
        _URL,
        headers=_HEADERS,
        json=_payload(
            [
                {"kind": "burn", "bps": 4000},
                {"kind": "track", "id": "memory", "bps": 4000},
                {"kind": "track", "id": "generator", "bps": 2000},
            ]
        ),
    )
    assert unrepresentable.status_code == 422
    assert "not exactly representable" in unrepresentable.json()["message"]
    assert await _count(session_maker, EmissionAllocationRevision) == 0

    applied = await client.post(_URL, headers=_HEADERS, json=_payload(new))
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["burn_settings_revision"] == 2
    assert body["track_share_settings_revision"] == 2
    async with session_maker() as session:
        burn = await session.get(BurnSettingsRevision, 2)
        track = await session.get(TrackShareSettingsRevision, 2)
    assert burn is not None and track is not None
    # rank_shares stays its own setting and is carried untouched.
    assert burn.settings == {"burn_share": 0.6, "rank_shares": _CURVE}
    assert burn_from_row(burn).burn_share == 0.6
    assert track.settings == {"track_shares_bps": {"memory": 7500, "generator": 2500}}
    assert burn.reason == track.reason
    assert burn.reason.startswith(f"emission allocation revision {body['revision']}:")
    assert burn.actor == "operator@example.com"

    history = (await client.get(_URL, headers=_HEADERS)).json()
    assert history["in_sync"] is True
    assert [row["revision"] for row in history["history"]] == [body["revision"]]
    split = {(s["kind"], s.get("id")): s["bps"] for s in history["configured"]["split"]}
    assert split[("burn", None)] == pytest.approx(6000)
    assert split[("track", "memory")] == pytest.approx(3000)
    assert split[("track", "generator")] == pytest.approx(1000)


async def test_a_failed_derived_write_rolls_the_whole_allocation_back(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ditto.api_server.emission_allocation as service

    _install(app, session_maker)
    await _seed_live(session_maker)

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise IntegrityError("INSERT", {}, Exception("concurrent track write"))

    monkeypatch.setattr(service, "insert_track_share_settings_revision", _boom)
    new = [
        {"kind": "burn", "bps": 6000},
        {"kind": "track", "id": "memory", "bps": 3000},
        {"kind": "track", "id": "generator", "bps": 1000},
    ]
    response = await client.post(_URL, headers=_HEADERS, json=_payload(new))
    assert response.status_code == 409
    assert await _count(session_maker, EmissionAllocationRevision) == 0
    # The burn revision inserted before the failure rolled back with it.
    assert await _count(session_maker, BurnSettingsRevision) == 1


async def test_legacy_writers_defer_to_the_allocation(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    await _seed_live(session_maker)
    # Before any allocation the legacy writers behave exactly as before.
    curve_only = {
        "expected_revision": 1,
        "scope": "*",
        "settings": {"burn_share": 0.5, "rank_shares": [0.6, 0.15, 0.1, 0.1, 0.05]},
        "reason": "flatten the payout curve",
        "actor": "operator@example.com",
        "confirmation": "APPLY BURN SETTINGS",
    }
    pre = await client.post(
        "/api/v1/admin/burn-settings", headers=_HEADERS, json=curve_only
    )
    assert pre.status_code == 200, pre.text

    assert (
        await client.post(_URL, headers=_HEADERS, json=_payload(_LIVE))
    ).status_code == 200

    track = await client.post(
        "/api/v1/admin/track-share-settings",
        headers=_HEADERS,
        json={
            "expected_revision": 1,
            "scope": "*",
            "settings": {"track_shares_bps": {"memory": 9000, "generator": 1000}},
            "reason": "move the generator share",
            "actor": "operator@example.com",
            "confirmation": "APPLY TRACK SHARES memory=9000 generator=1000",
        },
    )
    assert track.status_code == 409
    assert "set_emission_allocation" in track.json()["message"]

    burn = await client.post(
        "/api/v1/admin/burn-settings",
        headers=_HEADERS,
        json={**curve_only, "expected_revision": 2, "settings": {"burn_share": 0.4}},
    )
    assert burn.status_code == 409
    assert "owns burn_share" in burn.json()["message"]

    curve = await client.post(
        "/api/v1/admin/burn-settings",
        headers=_HEADERS,
        json={
            **curve_only,
            "expected_revision": 2,
            "settings": {"burn_share": 0.5, "rank_shares": _CURVE},
        },
    )
    assert curve.status_code == 200, curve.text
    assert await _count(session_maker, TrackShareSettingsRevision) == 1


async def test_write_requires_admin(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get(_URL)).status_code == 401
    assert (await client.post(_URL, json=_payload(_LIVE))).status_code == 401


# ─── Gamma: validated against the coldkey-signed policy ─────────────────────


def _gamma_entries(gamma: int, burn: int) -> list[dict[str, Any]]:
    return [
        {"kind": "burn", "bps": burn},
        {"kind": "track", "id": "memory", "bps": 3500},
        {"kind": "track", "id": "generator", "bps": 1500},
        {"kind": "gamma", "bucket": "gamma", "bps": gamma},
    ]


async def _pin_epoch(maker: async_sessionmaker[AsyncSession], epoch: int) -> None:
    async with maker() as session, session.begin():
        session.add(
            LedgerEpochSnapshot(
                snapshot_id=uuid4(),
                netuid=118,
                epoch_index=epoch,
                last_epoch_block=9_000_000,
                pinned_block=9_000_002,
                pinned_block_hash="0x" + "ab" * 32,
                pinned_at=datetime.now(UTC),
                bench_version=13,
                entries=[],
                context={"served": {"burn_share": 0.5}},
                ledger_digest="cd" * 32,
            )
        )


async def test_gamma_moves_only_inside_the_signed_range_while_enforcing(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ditto.tests.api_server.test_treasury_gamma_range import (
        enforce_runtime,
        ranged_pin,
        request,
        settings,
    )

    _install(app, session_maker)
    monkeypatch.setattr(
        "ditto.api_server.treasury_runtime.verify_public_signature", lambda *_: True
    )
    app.state.config = replace(
        app.state.config, chain=replace(app.state.config.chain, netuid=118)
    )
    p = ranged_pin()
    await enforce_runtime(session_maker, p)  # settings rev 1 at the signed 1000

    unnamed = await client.post(_URL, headers=_HEADERS, json=_payload(_LIVE))
    assert unnamed.status_code == 422
    assert "missing ['gamma']" in unnamed.json()["message"]

    # Enforcing, but no pin has reached the activation epoch yet.
    early = await client.post(
        _URL, headers=_HEADERS, json=_payload(_gamma_entries(500, 4500))
    )
    assert early.status_code == 409
    assert "activates at epoch" in early.json()["message"]

    await _pin_epoch(session_maker, p.epoch_index)
    outside = await client.post(
        _URL,
        headers=_HEADERS,
        json=_payload(
            [
                {"kind": "burn", "bps": 0},
                {"kind": "track", "id": "memory", "bps": 1680},
                {"kind": "track", "id": "generator", "bps": 720},
                {"kind": "gamma", "bucket": "gamma", "bps": 7600},
            ]
        ),
    )
    assert outside.status_code == 409
    assert "outside the coldkey-signed policy range" in outside.json()["message"]

    applied = await client.post(
        _URL, headers=_HEADERS, json=_payload(_gamma_entries(500, 4500))
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["served"]["gamma_bps"] == {"gamma": 500}
    assert body["served"]["burn_share"] == 4500 / 9500
    assert body["treasury_settings_revision"] == 2
    async with session_maker() as session:
        treasury = await session.get(TreasurySettingsRevision, 2)
    assert treasury is not None
    assert treasury.settings["service_buckets"][0]["allocation_bps"] == 500
    assert treasury.checksum == canonical_settings(treasury.settings)

    control = (await client.get(_URL, headers=_HEADERS)).json()
    assert control["in_sync"] is True
    assert control["gamma_mode"] == "enforce"
    assert control["gamma_ranges"] == {"gamma": [0, 7500]}
    split = {
        (s["kind"], s.get("id") or s.get("bucket")): s["bps"]
        for s in control["configured"]["split"]
    }
    assert split[("gamma", "gamma")] == 500
    assert split[("burn", None)] == pytest.approx(4500)
    assert split[("track", "memory")] == pytest.approx(3500)

    # The treasury writer can no longer move the enforcing Gamma share.
    moved = await client.post(
        "/api/v1/admin/treasury-settings",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        json=request(settings(p, 700), 2),
    )
    assert moved.status_code == 409
    assert "set_emission_allocation" in moved.json()["message"]


async def test_nonzero_gamma_is_refused_while_gamma_does_not_enforce(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from ditto.tests.api_server.test_treasury_gamma_range import (
        ranged_pin,
        seed_settings,
        settings,
    )

    _install(app, session_maker)
    async with session_maker() as session:
        await seed_settings(session, settings(ranged_pin(), 300))
        await session.commit()
    refused = await client.post(
        _URL, headers=_HEADERS, json=_payload(_gamma_entries(500, 4500))
    )
    assert refused.status_code == 409
    assert "Gamma is not enforcing" in refused.json()["message"]
    # Gamma 0 is accepted; the shadow bucket value folds nothing and is untouched.
    accepted = await client.post(
        _URL, headers=_HEADERS, json=_payload(_gamma_entries(0, 5000))
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["treasury_settings_revision"] == 1
    assert await _count(session_maker, TreasurySettingsRevision) == 1


# ─── Epoch pin pairing ──────────────────────────────────────────────────────


@pytest.mark.parametrize("context_revision,stored", [(1, True), (0, False)])
async def test_epoch_pin_refuses_an_allocation_that_moved_mid_materialization(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    context_revision: int,
    stored: bool,
) -> None:
    from unittest.mock import AsyncMock

    from ditto.api_server.endpoints.scoring import _LedgerSnapshot
    from ditto.api_server.ledger_pin import LedgerPinMaterializer
    from ditto.tests.api_server.test_ledger_pin import _schedule

    _install(app, session_maker)
    await _seed_live(session_maker)
    assert (
        await client.post(_URL, headers=_HEADERS, json=_payload(_LIVE))
    ).status_code == 200
    monkeypatch.setattr(
        "ditto.api_server.endpoints.scoring.resolve_ledger_context",
        AsyncMock(
            return_value=SimpleNamespace(
                policy=SimpleNamespace(allocation_revision=context_revision)
            )
        ),
    )
    monkeypatch.setattr(
        "ditto.api_server.endpoints.scoring.materialize_ledger_snapshot",
        AsyncMock(
            return_value=_LedgerSnapshot(
                entries=[], generated_at=datetime.now(UTC), active_bench_version=13
            )
        ),
    )
    monkeypatch.setattr(
        "ditto.api_server.ledger_pin.observe_shadow_treasury",
        AsyncMock(return_value=None),
    )
    result = await LedgerPinMaterializer().ensure(
        app.state, session_maker, now=datetime.now(UTC), schedule=_schedule()
    )
    assert (result is not None) is stored
    assert await _count(session_maker, LedgerEpochSnapshot) == int(stored)
