"""The scoring lease resolver fails open onto the pre-#1156 180 minutes."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

from ditto.api_server.scoring_lease_settings import (
    DEFAULT_SETTINGS,
    ScoringLeaseSettingsResolver,
    resolve_scoring_ticket_ttl,
    settings_from_row,
)


async def test_missing_resolver_serves_the_default() -> None:
    assert await resolve_scoring_ticket_ttl(SimpleNamespace()) == timedelta(minutes=180)


async def test_no_session_maker_serves_the_default() -> None:
    resolver = ScoringLeaseSettingsResolver()
    assert await resolver.resolve(None) is DEFAULT_SETTINGS


def test_corrupt_revision_serves_the_default() -> None:
    row = SimpleNamespace(revision=4, settings={"scoring_ticket_ttl_minutes": 5})
    assert settings_from_row(cast(Any, row)) is DEFAULT_SETTINGS


async def test_database_error_serves_the_default_without_caching_it() -> None:
    calls = 0

    def broken_maker() -> Any:
        nonlocal calls
        calls += 1
        raise RuntimeError("database unavailable")

    resolver = ScoringLeaseSettingsResolver(ttl_seconds=3600)
    assert await resolver.resolve(cast(Any, broken_maker)) is DEFAULT_SETTINGS
    assert await resolver.resolve(cast(Any, broken_maker)) is DEFAULT_SETTINGS
    assert calls == 2
