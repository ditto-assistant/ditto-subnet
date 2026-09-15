"""Public /chain snapshot endpoint tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from ditto.api_server.subnet_market import SubnetMarketConfig, TaostatsSubnetMarket
from ditto.chain.models import NeuronInfo, RegistrationEconomics
from ditto.tests.api_server.conftest import make_api_server_config


@pytest.mark.asyncio
async def test_public_chain_returns_metagraph_and_economics(
    app: FastAPI, client: AsyncClient
) -> None:
    neuron = NeuronInfo(
        hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        coldkey="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
        uid=7,
        stake=12.5,
        axon_info={"ip": "1.2.3.4", "port": 8091, "version": 1},
        is_active=True,
        validator_permit=True,
        incentive=0.1,
        dividends=0.2,
        trust=0.3,
        consensus=0.4,
        emission=0.5,
        last_update=100,
        validator_trust=0.6,
    )
    chain = AsyncMock()
    chain.get_recent_neurons = AsyncMock(return_value=[neuron])
    chain.get_registration_economics = AsyncMock(
        return_value=RegistrationEconomics(
            netuid=118,
            block=9000000,
            block_hash="0x" + "ab" * 32,
            recycle_rao=1_500_000_000,
            recycle_tao=1.5,
            immunity_period=5000,
            alpha_tao=0.0123,
            tao_in=1000.0,
            alpha_out=50000.0,
            market_cap_tao=615.0,
        )
    )
    app.state.chain = chain
    app.state.config = make_api_server_config()
    app.state.price_oracle = SimpleNamespace(get_tao_usd=AsyncMock(return_value=250.0))
    app.state.subnet_market = TaostatsSubnetMarket(SubnetMarketConfig())

    response = await client.get("/api/v1/public/chain")

    assert response.status_code == 200
    body = response.json()
    assert body["netuid"] == 118
    assert body["tao_usd"] == 250.0
    assert body["registration"]["recycle_tao"] == 1.5
    assert body["market"]["status"] == "fresh"
    assert body["market"]["source"] == "chain"
    assert body["market"]["alpha_tao"] == 0.0123
    assert body["market"]["alpha_usd"] == pytest.approx(0.0123 * 250.0)
    assert body["totals"]["neuron_count"] == 1
    assert body["totals"]["validator_count"] == 1
    assert body["metagraph"][0]["uid"] == 7
    assert body["metagraph"][0]["stake"] == 12.5
    assert body["metagraph"][0]["incentive"] == 0.1
    assert body["metagraph"][0]["axon"]["ip"] == "1.2.3.4"