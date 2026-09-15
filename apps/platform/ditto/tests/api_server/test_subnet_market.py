"""Tests for the optional Taostats subnet market quote parser."""

from __future__ import annotations

from ditto.api_server.subnet_market import (
    SubnetMarketConfig,
    TaostatsSubnetMarket,
    parse_taostats_market_quote,
)


def test_parse_taostats_market_quote_reads_allowlisted_fields() -> None:
    quote = parse_taostats_market_quote(
        {
            "data": [
                {
                    "price": 0.0123,
                    "price_in_usd": 4.5,
                    "market_cap": 1000.0,
                    "market_cap_usd": 350000.0,
                    "ignored": "x",
                }
            ]
        }
    )
    assert quote == {
        "alpha_tao": 0.0123,
        "alpha_usd": 4.5,
        "market_cap_tao": 1000.0,
        "market_cap_usd": 350000.0,
    }


def test_disabled_market_snapshot() -> None:
    market = TaostatsSubnetMarket(SubnetMarketConfig())
    snap = market.snapshot()
    assert snap.status == "disabled"
    assert snap.alpha_tao is None
