"""Treasury quote is authenticated, bounded and read-only."""

from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI

from ditto.api_server.endpoints import admin_treasury_quote

pytestmark = pytest.mark.asyncio
_TOKEN = "test-admin-token-at-least-32-characters"
_URL = "/api/v1/admin/treasury-quote"


async def test_quote_surface(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)
    calls: list[int] = []

    async def fake_quote(amount: int) -> dict:
        calls.append(amount)
        return {
            "block": 123,
            "block_hash": "0x" + "a" * 64,
            "source_alpha_rao": amount,
            "tao_path": {
                "deposit_asset": "TAO",
                "amount_rao": 50,
                "price_impact_bps": 1,
            },
            "gm_alpha_path": {
                "deposit_asset": "SN28_ALPHA",
                "amount_rao": 70,
                "price_impact_bps": 2,
            },
            "gm_credit_usd": None,
            "execution_enabled": False,
            "settlement": "confirmed deposit rate",
        }

    monkeypatch.setattr(admin_treasury_quote, "_read_quote", fake_quote)
    assert (await client.get(_URL + "?source_alpha_rao=1")).status_code in {401, 403}
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    too_large = await client.get(
        _URL + "?source_alpha_rao=10000000001", headers=headers
    )
    assert too_large.status_code == 422
    assert calls == []
    response = await client.get(_URL + "?source_alpha_rao=1000000000", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["execution_enabled"] is False
    assert calls == [1_000_000_000]
