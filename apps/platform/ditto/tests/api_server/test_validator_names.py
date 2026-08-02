"""Tests for optional, fail-open Taostats validator-name decoration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ditto.api_server.validator_names import (
    TaostatsValidatorNames,
    ValidatorNamesConfig,
    parse_taostats_names,
    parse_taostats_validator_metadata,
)

_ALICE = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_BOB = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
_UNKNOWN = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_URL = "https://api.taostats.io/api/dtao/validator/available/v1?netuid=118"
_NOW = datetime(2026, 7, 14, 20, 0, tzinfo=UTC)


def _config() -> ValidatorNamesConfig:
    return ValidatorNamesConfig(
        url=_URL,
        api_key="test-free-api-key",
        timeout_seconds=0.1,
        refresh_seconds=60,
        retry_seconds=10,
        max_stale_seconds=300,
    )


def _client(handler: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, timeout=0.1)


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"data": {}}, {"results": []}, "not-json"],
)
def test_parser_rejects_malformed_top_level_response(payload: object) -> None:
    with pytest.raises(ValueError, match="data list"):
        parse_taostats_names(payload)


def test_parser_allowlists_fields_and_neutralizes_injection_text() -> None:
    names = parse_taostats_names(
        {
            "data": [
                {
                    "address": {"ss58": _ALICE, "private": "ignore"},
                    "name": "<img src=x onerror=alert(1)>\u202e",
                    "stake": 999,
                    "internal_ip": "ignore",
                },
                {"address": {"ss58": "invalid"}, "name": "drop"},
                {"address": {"ss58": _BOB}, "name": "\x00\u200f"},
            ],
            "private": "ignore",
        }
    )

    assert names == {_ALICE: "<img src=x onerror=alert(1)>"}


def test_parser_allowlists_finite_non_negative_stake_weights() -> None:
    names, stake_weights = parse_taostats_validator_metadata(
        {
            "data": [
                {
                    "address": {"ss58": _ALICE},
                    "name": "Alice",
                    "hotkey_alpha": "12.5",
                },
                {"address": {"ss58": _BOB}, "hotkey_alpha": 0},
                {
                    "address": {"ss58": _UNKNOWN},
                    "name": "Bad",
                    "hotkey_alpha": "nan",
                    "stake": 999,
                },
            ]
        }
    )

    assert names == {_ALICE: "Alice", _UNKNOWN: "Bad"}
    assert stake_weights == {_ALICE: 12.5, _BOB: 0.0}


async def test_success_unknown_hotkey_and_duplicate_names() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "test-free-api-key"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "address": {"ss58": _ALICE},
                        "name": "Rizzo",
                        "hotkey_alpha": "30",
                    },
                    {
                        "address": {"ss58": _BOB},
                        "name": "Rizzo",
                        "hotkey_alpha": "20",
                    },
                    {"address": {"ss58": _UNKNOWN}, "name": "Unknown node"},
                ]
            },
        )

    async with _client(httpx.MockTransport(handler)) as client:
        cache = TaostatsValidatorNames(_config(), client)
        assert await cache.refresh(now=_NOW) is True
        snapshot = cache.snapshot([_ALICE, _BOB], now=_NOW)

    assert snapshot.status == "fresh"
    assert snapshot.names == {_ALICE: "Rizzo", _BOB: "Rizzo"}
    assert snapshot.stake_weights == {_ALICE: 30.0, _BOB: 20.0}
    assert _UNKNOWN not in snapshot.names


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("timeout"),
        httpx.ConnectError("unavailable"),
    ],
)
async def test_timeout_and_unavailable_service_fail_open(failure: Exception) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise failure

    async with _client(httpx.MockTransport(handler)) as client:
        cache = TaostatsValidatorNames(_config(), client)
        assert await cache.refresh(now=_NOW) is False
        snapshot = cache.snapshot([_ALICE], now=_NOW)

    assert snapshot.status == "unavailable"
    assert snapshot.names == {}
    assert snapshot.stake_weights == {}


async def test_rate_limit_honors_bounded_retry_window() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with _client(httpx.MockTransport(handler)) as client:
        cache = TaostatsValidatorNames(_config(), client)
        assert await cache.refresh(now=_NOW) is False
        assert await cache.refresh(now=_NOW + timedelta(seconds=59)) is False

    assert calls == 1


async def test_malformed_json_keeps_service_unavailable() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    async with _client(httpx.MockTransport(handler)) as client:
        cache = TaostatsValidatorNames(_config(), client)
        assert await cache.refresh(now=_NOW) is False
        assert cache.snapshot([_ALICE], now=_NOW).status == "unavailable"


async def test_stale_while_revalidate_and_expiry() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={"data": [{"address": {"ss58": _ALICE}, "name": "Rizzo"}]},
            )
        return httpx.Response(503)

    async with _client(httpx.MockTransport(handler)) as client:
        cache = TaostatsValidatorNames(_config(), client)
        assert await cache.refresh(now=_NOW) is True
        assert await cache.refresh(now=_NOW + timedelta(seconds=61)) is False
        stale = cache.snapshot([_ALICE], now=_NOW + timedelta(seconds=61))
        expired = cache.snapshot([_ALICE], now=_NOW + timedelta(seconds=301))

    assert stale.status == "stale"
    assert stale.names == {_ALICE: "Rizzo"}
    assert stale.stake_weights == {}
    assert expired.status == "unavailable"
    assert expired.names == {}
    assert expired.stake_weights == {}


def test_disabled_source_never_constructs_or_requires_network_client() -> None:
    cache = TaostatsValidatorNames(ValidatorNamesConfig())
    snapshot = cache.snapshot([_ALICE], now=_NOW)

    assert snapshot.status == "disabled"
    assert snapshot.names == {}
    assert snapshot.stake_weights == {}
