"""Services document, balance amounts and the default-off switch."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ditto.api_models.chain_gamma import (
    ChainGammaBalanceResponse,
    ChainGammaInsufficientBalance,
    ChainGammaServicesDocument,
    check_service_url,
)
from ditto.chain_gamma.amounts import BalanceView, units_to_usd, usd_to_units
from ditto.chain_gamma.config import (
    ChainGammaConfig,
    ChainGammaConfigError,
    parse_chain_gamma_from_env,
)
from ditto.tests.chain_gamma.helpers import ALICE_SR25519, BOB, CHARLIE


def _document(**overrides) -> dict:
    doc = {
        "version": 1,
        "name": "Example",
        "description": "Pay-per-call API, paid in SN118 gamma.",
        "publishers": [{"netuid": 118, "hotkey": BOB}],
        "services": [{"type": "http", "endpoint": "https://api.example.test"}],
        "payments": [
            {
                "type": "gamma",
                "signature": "btauth/1",
                "balance": "https://api.example.test/chain-gamma/balance",
            }
        ],
        "pricing": "https://example.test/pricing",
    }
    doc.update(overrides)
    return doc


def test_services_document_round_trips_the_required_shape() -> None:
    document = ChainGammaServicesDocument.model_validate(_document())
    assert document.model_dump(mode="json") == _document()
    assert document.publisher_hotkeys(118) == {BOB}
    assert document.publisher_hotkeys(1) == frozenset()


def test_old_publishers_stay_listed() -> None:
    document = ChainGammaServicesDocument.model_validate(
        _document(
            publishers=[
                {"netuid": 118, "hotkey": BOB},
                {"netuid": 118, "hotkey": CHARLIE},
            ]
        )
    )
    assert document.publisher_hotkeys(118) == {BOB, CHARLIE}


@pytest.mark.parametrize(
    "overrides",
    [
        {"publishers": []},
        {"publishers": [{"netuid": 118, "hotkey": "not-ss58"}]},
        {
            "publishers": [
                {"netuid": 118, "hotkey": BOB},
                {"netuid": 118, "hotkey": BOB},
            ]
        },
        {"services": [{"type": "http", "endpoint": "http://api.example.test"}]},
        {
            "payments": [
                {"type": "gamma", "signature": "none", "balance": "https://x.test"}
            ]
        },
        {"payments": [{"type": "gamma", "balance": "https://user:pw@x.test/b"}]},
        {"unexpected": True},
    ],
)
def test_services_document_refuses_bad_shapes(overrides) -> None:
    with pytest.raises(ValidationError):
        ChainGammaServicesDocument.model_validate(_document(**overrides))


def test_set_service_url_limit() -> None:
    assert check_service_url("https://a.test/services.json")
    with pytest.raises(ValueError):
        check_service_url("https://a.test/" + "x" * 250)
    with pytest.raises(ValueError):
        check_service_url("http://a.test/services.json")


@pytest.mark.parametrize(
    ("units", "usd"),
    [
        (0, "0.00"),
        (5_000_000_000, "5.00"),
        (1_250_000_000, "1.25"),
        (1_234_567_891, "1.234567891"),
        (100_000_000, "0.10"),
        (1, "0.000000001"),
    ],
)
def test_units_and_usd_strings_are_exact(units: int, usd: str) -> None:
    assert units_to_usd(units) == usd
    assert usd_to_units(usd) == units


@pytest.mark.parametrize("bad", ["-1", "0.0000000001", "NaN", "Infinity", "abc"])
def test_usd_parsing_refuses_inexact_values(bad: str) -> None:
    with pytest.raises(ValueError):
        usd_to_units(bad)


def test_balance_response_matches_the_standard() -> None:
    view = BalanceView(
        ALICE_SR25519, credited_units=5_000_000_000, used_units=1_250_000_000
    )
    body = view.to_response()
    assert body == {
        "hotkey": ALICE_SR25519,
        "credited_usd": "5.00",
        "used_usd": "1.25",
        "remaining_usd": "3.75",
    }
    assert ChainGammaBalanceResponse.model_validate(body).remaining_usd == "3.75"
    assert ChainGammaInsufficientBalance().model_dump() == {
        "error": "insufficient_balance"
    }
    with pytest.raises(ValueError):
        BalanceView(ALICE_SR25519, credited_units=1, used_units=2)


def test_switch_is_off_by_default() -> None:
    assert parse_chain_gamma_from_env({}) == ChainGammaConfig()
    assert not parse_chain_gamma_from_env(
        {"DITTO_CHAIN_GAMMA_ENABLED": "false"}
    ).enabled


def test_switch_on_needs_publisher_hotkeys() -> None:
    with pytest.raises(ChainGammaConfigError):
        parse_chain_gamma_from_env({"DITTO_CHAIN_GAMMA_ENABLED": "true"})
    with pytest.raises(ChainGammaConfigError):
        parse_chain_gamma_from_env(
            {
                "DITTO_CHAIN_GAMMA_ENABLED": "true",
                "DITTO_CHAIN_GAMMA_PUBLISHER_HOTKEYS": "nope",
            }
        )
    config = parse_chain_gamma_from_env(
        {
            "DITTO_CHAIN_GAMMA_ENABLED": "on",
            "DITTO_CHAIN_GAMMA_PUBLISHER_HOTKEYS": f"{BOB}, {CHARLIE}",
        }
    )
    assert config == ChainGammaConfig(
        enabled=True, netuid=118, publisher_hotkeys=frozenset({BOB, CHARLIE})
    )


@pytest.mark.parametrize(
    "env",
    [
        {"DITTO_CHAIN_GAMMA_ENABLED": "maybe"},
        {
            "DITTO_CHAIN_GAMMA_ENABLED": "1",
            "DITTO_CHAIN_GAMMA_NETUID": "70000",
            "DITTO_CHAIN_GAMMA_PUBLISHER_HOTKEYS": BOB,
        },
    ],
)
def test_switch_refuses_malformed_values(env) -> None:
    with pytest.raises(ChainGammaConfigError):
        parse_chain_gamma_from_env(env)


def test_nothing_in_the_server_imports_chain_gamma_yet() -> None:
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    offenders = [
        path
        for folder in ("api_server", "db", "chain")
        for path in (root / folder).rglob("*.py")
        if "chain_gamma" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
