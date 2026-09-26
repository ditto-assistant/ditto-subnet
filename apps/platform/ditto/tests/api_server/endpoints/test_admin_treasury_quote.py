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


async def test_gm_alpha_path_impact_compounds_both_hops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DITTO->TAO loses 100 bps and TAO->SN28 loses another 100 bps. The GM route
    # pays both, so it reports the compounded 199 bps, not the last hop's 100.
    import bittensor as bt

    class _Pool:
        def __init__(self, received: int, lost: int) -> None:
            self._swap = (bt.Balance.from_rao(received), bt.Balance.from_rao(lost))

        def alpha_to_tao_with_slippage(self, _amount: object) -> tuple:
            return self._swap

        def tao_to_alpha_with_slippage(self, _tao: object) -> tuple:
            return self._swap

    class _Substrate:
        async def get_chain_finalised_head(self) -> str:
            return "0x" + "b" * 64

        async def get_block_number(self, _block_hash: str) -> int:
            return 7

    pools = {118: _Pool(9_900, 100), 28: _Pool(19_800, 200)}

    class _Chain:
        substrate = _Substrate()

        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_Chain":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def subnet(self, netuid: int, *, block_hash: str) -> _Pool:
            # Both pools must be read at the same finalized block.
            assert block_hash == "0x" + "b" * 64
            return pools[netuid]

    monkeypatch.setattr(admin_treasury_quote.bt, "AsyncSubtensor", _Chain)

    quote = await admin_treasury_quote._read_quote(10_000)

    assert quote["tao_path"]["price_impact_bps"] == 100
    assert quote["gm_alpha_path"]["price_impact_bps"] == 199
    assert quote["gm_alpha_path"]["amount_rao"] == 19_800
