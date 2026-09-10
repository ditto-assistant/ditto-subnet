"""The router-ledger compute-destination seam (RouterLedgerSource).

The validator only reads + folds a published router ledger, so it is never
locked to one place the (heavy) router eval runs. v1 defaults to the empty
shadow source; a promotion swaps in the platform reader, which is fail-closed.
"""

from __future__ import annotations

from ditto.api_models.router_ledger import RouterLedgerResponse
from ditto.validator.worker import (
    EmptyRouterLedgerSource,
    PlatformRouterLedgerSource,
    RouterLedgerSource,
)


async def test_empty_source_folds_an_empty_ledger() -> None:
    source = EmptyRouterLedgerSource()
    ledger = await source.fetch()
    assert isinstance(ledger, RouterLedgerResponse)
    assert list(ledger.entries) == []
    # Structural typing: the default source satisfies the seam protocol.
    assert isinstance(source, RouterLedgerSource)


async def test_platform_source_reads_the_published_ledger() -> None:
    published = RouterLedgerResponse(count=0)

    async def read() -> RouterLedgerResponse:
        return published

    source = PlatformRouterLedgerSource(read)
    assert await source.fetch() is published
    assert isinstance(source, RouterLedgerSource)


async def test_platform_source_is_fail_closed_on_error() -> None:
    # A scorer outage or malformed publish must degrade to an empty ledger, never
    # crash or distort the consensus put_weights fold.
    async def boom() -> RouterLedgerResponse:
        raise RuntimeError("scorer unreachable")

    source = PlatformRouterLedgerSource(boom)
    ledger = await source.fetch()
    assert isinstance(ledger, RouterLedgerResponse)
    assert list(ledger.entries) == []
