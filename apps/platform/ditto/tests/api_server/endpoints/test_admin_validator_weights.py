from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from ditto.api_server.endpoints import admin_validator_weights as endpoint
from ditto.chain.errors import ChainConnectionError
from ditto.chain.models import ChainWeight, ChainWeightsSnapshot, ChainWeightVector
from ditto.chain.weight_diagnostics import PendingWeightCommit, WeightDiagnostics

URL = "/api/v1/admin/validator-weight-diagnostics"
TOKEN = "test-admin-token-at-least-32-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
HOTKEY = "5" + "A" * 47


@pytest.fixture(autouse=True)
def installed_chain(app):
    # ASGI unit clients do not run the application's lifespan.
    app.state.chain = object()


def fixture():
    trust = [0] * 140
    trust[139] = 60947
    updates = [0] * 140
    updates[139] = 9_029_448
    return WeightDiagnostics(
        ChainWeightsSnapshot(
            118,
            9_029_509,
            "0x" + "ab" * 32,
            None,
            (ChainWeightVector(139, HOTKEY, (ChainWeight(43, HOTKEY, 65535),)),),
        ),
        tuple(trust),
        tuple(updates),
        (65535,),
        9_029_509,
        0,
        25017,
        (PendingWeightCommit(HOTKEY, 25016, 9_029_448, 32049696),),
    )


async def test_admin_read_binds_trust_and_pending_to_one_block(
    app, client, monkeypatch
):
    app.state.config = replace(app.state.config, admin_api_token=TOKEN)
    reader = AsyncMock(return_value=fixture())
    monkeypatch.setattr(endpoint, "read_weight_diagnostics", reader)
    response = await client.get(URL + "?validator_uid=139", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["block"] == 9_029_509
    assert body["validators"][0]["validator_trust"] == pytest.approx(60947 / 65535)
    assert body["pending_commits"][0]["reveal_round"] == 32049696
    assert body["weights_submitted"] is body["historical_clipping_verified"] is False
    assert "ciphertext" not in response.text


async def test_auth_and_bad_uid_do_not_read_chain(app, client, monkeypatch):
    app.state.config = replace(app.state.config, admin_api_token=TOKEN)
    reader = AsyncMock(return_value=fixture())
    monkeypatch.setattr(endpoint, "read_weight_diagnostics", reader)
    assert (await client.get(URL)).status_code == 401
    assert (
        await client.get(URL + "?validator_uid=-1", headers=HEADERS)
    ).status_code == 422
    reader.assert_not_awaited()


async def test_failed_or_missing_evidence_never_looks_healthy(app, client, monkeypatch):
    app.state.config = replace(app.state.config, admin_api_token=TOKEN)
    reader = AsyncMock(side_effect=ChainConnectionError("down"))
    monkeypatch.setattr(endpoint, "read_weight_diagnostics", reader)
    assert (await client.get(URL, headers=HEADERS)).status_code == 503
    reader.side_effect = None
    reader.return_value = fixture()
    assert (
        await client.get(URL + "?validator_uid=138", headers=HEADERS)
    ).status_code == 404
    reader.return_value = replace(fixture(), validator_trust=())
    assert (await client.get(URL, headers=HEADERS)).status_code == 503
