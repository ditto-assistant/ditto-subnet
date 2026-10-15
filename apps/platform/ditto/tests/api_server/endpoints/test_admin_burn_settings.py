"""Contract tests for the hot-swappable emission-burn control."""

from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.burn_settings import BurnSettingsResolver, settings_from_row
from ditto.api_server.dependencies import get_session
from ditto.db.models import BurnSettingsRevision as RevisionRow

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/burn-settings"


@pytest.fixture
def settings_maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Local alias for the root Postgres ``session_maker``."""
    return session_maker


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.state.burn_settings.invalidate()


def _payload(
    *, expected_revision: int = 0, burn_share: float = 0.25
) -> dict[str, object]:
    return {
        "expected_revision": expected_revision,
        "scope": "*",
        "settings": {"burn_share": burn_share},
        "reason": "owner-approved emission burn change",
        "actor": "operator@example.com",
        "confirmation": "APPLY BURN SETTINGS",
    }


async def test_default_is_no_burn_and_a_revision_is_audited(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, settings_maker)

    initial = await client.get(_URL, headers=_HEADERS)
    assert initial.status_code == 200, initial.text
    effective = initial.json()["effective"]
    # An unconfigured platform must serve exactly what the validator's frozen
    # MINER_EMISSION_SHARE already does, or deploying this surface would move
    # emissions by itself.
    assert effective["settings"] == {"burn_share": 0.0}
    assert effective["miner_emission_share"] == 1.0
    assert effective["source"] == "default"
    assert effective["revision"] == 0
    # Bounds come from the platform so the console cannot offer a refused share.
    assert effective["min_burn_share"] == 0.0
    assert effective["max_burn_share"] == 1.0
    assert effective["live_validator_count"] == 0

    applied = await client.post(_URL, headers=_HEADERS, json=_payload(burn_share=0.4))
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["revision"] == 1
    assert body["parent_revision"] == 0
    assert body["settings"]["burn_share"] == 0.4
    assert body["actor"] == "operator@example.com"
    assert body["reason"] == "owner-approved emission burn change"

    refreshed = await client.get(_URL, headers=_HEADERS)
    assert refreshed.status_code == 200
    refreshed_effective = refreshed.json()["effective"]
    assert refreshed_effective["revision"] == 1
    assert refreshed_effective["source"] == "revision"
    assert refreshed_effective["settings"]["burn_share"] == 0.4
    # Derived, never stored: the two can then never disagree.
    assert refreshed_effective["miner_emission_share"] == pytest.approx(0.6)
    assert [r["settings"]["burn_share"] for r in refreshed.json()["history"]] == [0.4]


async def test_rejects_stale_revision_and_wrong_confirmation(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, settings_maker)
    first = await client.post(_URL, headers=_HEADERS, json=_payload())
    assert first.status_code == 200, first.text

    stale = await client.post(_URL, headers=_HEADERS, json=_payload())
    assert stale.status_code == 409
    assert "refresh before applying" in stale.text

    wrong = _payload(expected_revision=1)
    wrong["confirmation"] = "BURN IT"
    confirmation = await client.post(_URL, headers=_HEADERS, json=wrong)
    assert confirmation.status_code == 409

    # Neither refusal may leave a revision behind.
    current = await client.get(_URL, headers=_HEADERS)
    assert current.json()["effective"]["revision"] == 1


@pytest.mark.parametrize("burn_share", [-0.01, 1.01, 2])
async def test_rejects_shares_outside_the_unit_interval(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
    burn_share: float,
) -> None:
    _install(app, settings_maker)
    response = await client.post(
        _URL, headers=_HEADERS, json=_payload(burn_share=burn_share)
    )
    assert response.status_code == 422, response.text


async def test_full_burn_is_a_permitted_setting(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    """1.0 is the same all-to-burn vector an empty ledger already produces."""
    _install(app, settings_maker)
    response = await client.post(_URL, headers=_HEADERS, json=_payload(burn_share=1.0))
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["burn_share"] == 1.0

    effective = (await client.get(_URL, headers=_HEADERS)).json()["effective"]
    assert effective["miner_emission_share"] == 0.0


async def test_write_requires_admin(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, settings_maker)
    assert (await client.get(_URL)).status_code in {401, 403}
    assert (await client.post(_URL, json=_payload())).status_code in {401, 403}


async def test_a_malformed_revision_degrades_to_no_burn() -> None:
    """The ledger path reads this before every weight submission.

    Failing that read to protect a corrupt row would stop weight submission
    subnet-wide, and the default errs toward paying miners rather than
    withholding from them.
    """

    corrupt = RevisionRow(
        revision=7,
        parent_revision=6,
        scope="*",
        settings={"burn_share": "most of it"},
        checksum="ab" * 32,
        reason="a revision whose payload no longer parses",
        actor="operator@example.com",
    )
    assert settings_from_row(corrupt).burn_share == 0.0
    assert settings_from_row(None).burn_share == 0.0


async def test_resolver_without_a_session_maker_returns_the_default() -> None:
    assert (await BurnSettingsResolver().resolve(None)).burn_share == 0.0


async def test_rank_shares_must_be_a_five_slot_curve_summing_to_one() -> None:
    from pydantic import ValidationError

    from ditto.api_models.burn_settings import BurnSettings

    assert BurnSettings().rank_shares is None
    curve = BurnSettings.model_validate(
        {"burn_share": 0.0, "rank_shares": [0.5, 0.2, 0.13, 0.1, 0.07]}
    )
    assert curve.rank_shares == (0.5, 0.2, 0.13, 0.1, 0.07)
    for bad in (
        [0.5, 0.2, 0.3],
        [0.5, 0.2, 0.13, 0.1, 0.0],
        [0.5, 0.2, 0.13, 0.1, 0.08],
        [0.6, 0.2, 0.13, 0.1, -0.03],
        # Numeric strings are refused, never coerced.
        [0.5, "0.2", 0.13, 0.1, 0.07],
    ):
        with pytest.raises(ValidationError):
            BurnSettings.model_validate({"burn_share": 0.0, "rank_shares": bad})


_CURVE = [0.5, 0.2, 0.13, 0.1, 0.07]


async def test_a_burn_only_write_keeps_the_payout_curve(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The console dial posts burn_share alone; that must not reset the curve."""
    _install(app, settings_maker)
    curved = _payload(burn_share=0.0)
    curved["settings"] = {"burn_share": 0.0, "rank_shares": _CURVE}
    first = await client.post(_URL, headers=_HEADERS, json=curved)
    assert first.status_code == 200, first.text

    burn_only = await client.post(
        _URL, headers=_HEADERS, json=_payload(expected_revision=1, burn_share=0.1)
    )
    assert burn_only.status_code == 200, burn_only.text
    assert burn_only.json()["settings"] == {"burn_share": 0.1, "rank_shares": _CURVE}

    effective = (await client.get(_URL, headers=_HEADERS)).json()["effective"]
    assert effective["settings"]["rank_shares"] == _CURVE
    # No live v31 weight setter: the curve is configured but not yet served.
    assert effective["rank_shares_served"] is False

    cleared = _payload(expected_revision=2, burn_share=0.1)
    cleared["settings"] = {"burn_share": 0.1, "rank_shares": None}
    reset = await client.post(_URL, headers=_HEADERS, json=cleared)
    assert reset.status_code == 200, reset.text
    assert reset.json()["settings"] == {"burn_share": 0.1}


async def test_an_increasing_curve_is_refused_on_write_but_still_reads(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Ordering guards writes; a stored legacy curve must not reset the policy."""
    from ditto.api_models.burn_settings import BurnSettings
    from ditto.api_server.burn_settings import settings_from_row
    from ditto.db.models import BurnSettingsRevision as Row

    _install(app, settings_maker)
    for increasing in ([0.07, 0.1, 0.13, 0.2, 0.5], [0.5, 0.13, 0.2, 0.1, 0.07]):
        refused = _payload(burn_share=0.25)
        refused["settings"] = {"burn_share": 0.25, "rank_shares": increasing}
        response = await client.post(_URL, headers=_HEADERS, json=refused)
        assert response.status_code == 422, response.text

    legacy = Row(
        revision=1,
        parent_revision=0,
        scope="*",
        settings={"burn_share": 0.25, "rank_shares": [0.07, 0.1, 0.13, 0.2, 0.5]},
        checksum="ab" * 32,
        reason="a revision written before the ordering rule",
        actor="operator@example.com",
    )
    assert settings_from_row(legacy) == BurnSettings(
        burn_share=0.25, rank_shares=(0.07, 0.1, 0.13, 0.2, 0.5)
    )

    # A burn-only write must not republish that legacy curve either.
    async with settings_maker() as session, session.begin():
        session.add(legacy)
    burn_only = await client.post(
        _URL, headers=_HEADERS, json=_payload(expected_revision=1, burn_share=0.1)
    )
    assert burn_only.status_code == 409, burn_only.text
    assert "increasing" in burn_only.text


async def test_an_unreadable_latest_revision_is_never_carried_as_a_clear(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    from ditto.db.models import BurnSettingsRevision as Row

    _install(app, settings_maker)
    async with settings_maker() as session, session.begin():
        session.add(
            Row(
                parent_revision=0,
                scope="*",
                settings={"burn_share": 7.0, "rank_shares": _CURVE},
                checksum="ab" * 32,
                reason="a malformed stored revision",
                actor="operator@example.com",
            )
        )
    burn_only = await client.post(
        _URL, headers=_HEADERS, json=_payload(expected_revision=1, burn_share=0.1)
    )
    assert burn_only.status_code == 409, burn_only.text
    assert "rank_shares explicitly" in burn_only.text
    explicit = _payload(expected_revision=1, burn_share=0.1)
    explicit["settings"] = {"burn_share": 0.1, "rank_shares": _CURVE}
    assert (await client.post(_URL, headers=_HEADERS, json=explicit)).status_code == 200
