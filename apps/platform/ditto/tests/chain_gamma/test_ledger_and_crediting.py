"""Crediting exactly once, and the conditional charge (requirements 2 and 4.2)."""

from __future__ import annotations

import asyncio

import pytest

from ditto.chain_gamma.amounts import UNITS_PER_USD
from ditto.chain_gamma.crediting import credit_all, credit_hotkey
from ditto.chain_gamma.ledger import (
    DuplicateCharge,
    InMemoryChainGammaLedger,
    TotalKey,
    TotalObservation,
)
from ditto.tests.chain_gamma.helpers import ALICE_SR25519, BOB, CHARLIE, FakeChain

NETUID = 118
USD = UNITS_PER_USD


def _obs(total: int, *, counter: int = 0, block: int = 1) -> TotalObservation:
    return TotalObservation(
        key=TotalKey(NETUID, counter, BOB, ALICE_SR25519),
        total_units=total,
        block_number=block,
        block_hash=f"0x{block:064x}",
    )


async def test_only_growth_is_credited() -> None:
    ledger = InMemoryChainGammaLedger()
    assert await ledger.observe_total(_obs(5 * USD)) == 5 * USD
    assert await ledger.observe_total(_obs(5 * USD, block=2)) == 0
    assert await ledger.observe_total(_obs(7 * USD, block=3)) == 2 * USD
    # An older, smaller read credits nothing and does not rewind the total.
    assert await ledger.observe_total(_obs(5 * USD, block=1)) == 0
    assert await ledger.observe_total(_obs(7 * USD, block=4)) == 0
    assert (await ledger.balance(ALICE_SR25519)).credited_units == 7 * USD


async def test_concurrent_reads_of_one_total_credit_once() -> None:
    ledger = InMemoryChainGammaLedger()
    credited = await asyncio.gather(
        *(ledger.observe_total(_obs(3 * USD)) for _ in range(20))
    )
    assert sum(credited) == 3 * USD


async def test_a_new_counter_starts_its_totals_from_zero() -> None:
    ledger = InMemoryChainGammaLedger()
    await ledger.observe_total(_obs(9 * USD, counter=0))
    # Re-registered netuid: totals restart, and the smaller total still credits.
    assert await ledger.observe_total(_obs(1 * USD, counter=1)) == 1 * USD
    assert (await ledger.balance(ALICE_SR25519)).credited_units == 10 * USD


async def test_charge_is_conditional_and_settles_down() -> None:
    ledger = InMemoryChainGammaLedger()
    await ledger.observe_total(_obs(1 * USD))
    assert await ledger.try_charge("a:1", ALICE_SR25519, USD // 2)
    assert not await ledger.try_charge("a:2", ALICE_SR25519, USD)
    assert await ledger.settle("a:1", USD // 4) == USD // 4
    assert await ledger.settle("a:1", USD // 4) == 0  # idempotent retry
    with pytest.raises(ValueError):
        await ledger.settle("a:1", USD // 8)
    view = await ledger.balance(ALICE_SR25519)
    assert (view.credited_units, view.used_units) == (USD, USD // 4)


async def test_charge_ids_are_single_use_and_prices_positive() -> None:
    ledger = InMemoryChainGammaLedger()
    await ledger.observe_total(_obs(1 * USD))
    assert await ledger.try_charge("a:1", ALICE_SR25519, 1)
    with pytest.raises(DuplicateCharge):
        await ledger.try_charge("a:1", ALICE_SR25519, 1)
    with pytest.raises(ValueError):
        await ledger.try_charge("a:3", ALICE_SR25519, 0)
    with pytest.raises(ValueError):
        await ledger.settle("a:1", 2)


async def test_concurrent_charges_never_overdraw() -> None:
    ledger = InMemoryChainGammaLedger()
    await ledger.observe_total(_obs(10))
    results = await asyncio.gather(
        *(ledger.try_charge(f"a:{i}", ALICE_SR25519, 3) for i in range(10))
    )
    assert results.count(True) == 3
    assert (await ledger.balance(ALICE_SR25519)).remaining_units == 1


async def test_unknown_hotkey_reads_zero() -> None:
    view = await InMemoryChainGammaLedger().balance(CHARLIE)
    assert view.to_response() == {
        "hotkey": CHARLIE,
        "credited_usd": "0.00",
        "used_usd": "0.00",
        "remaining_usd": "0.00",
    }


async def test_scan_credits_every_spend_once_across_publishers() -> None:
    chain, ledger = FakeChain(), InMemoryChainGammaLedger()
    old_service = CHARLIE  # a hotkey published under before a swap
    chain.spend(NETUID, BOB, ALICE_SR25519, 2 * USD)
    chain.spend(NETUID, old_service, ALICE_SR25519, 1 * USD)
    chain.spend(NETUID + 1, BOB, ALICE_SR25519, 50 * USD)  # another subnet's gamma
    first = await credit_all(
        chain, ledger, netuid=NETUID, publisher_hotkeys={BOB, old_service}
    )
    assert (first.totals_read, first.units_credited) == (2, 3 * USD)
    again = await credit_all(
        chain, ledger, netuid=NETUID, publisher_hotkeys={BOB, old_service}
    )
    assert again.units_credited == 0
    chain.spend(NETUID, BOB, ALICE_SR25519, USD // 10)
    grown = await credit_all(chain, ledger, netuid=NETUID, publisher_hotkeys={BOB})
    assert grown.units_credited == USD // 10
    assert (await ledger.balance(ALICE_SR25519)).credited_units == 3 * USD + USD // 10


async def test_on_demand_credit_and_scan_do_not_double_count() -> None:
    chain, ledger = FakeChain(), InMemoryChainGammaLedger()
    chain.spend(NETUID, BOB, ALICE_SR25519, 4 * USD)
    assert (
        await credit_hotkey(
            chain, ledger, netuid=NETUID, publisher_hotkeys={BOB}, hotkey=ALICE_SR25519
        )
        == 4 * USD
    )
    report = await credit_all(chain, ledger, netuid=NETUID, publisher_hotkeys={BOB})
    assert report.units_credited == 0


async def test_re_registration_credits_new_spends_from_zero() -> None:
    chain, ledger = FakeChain(), InMemoryChainGammaLedger()
    chain.spend(NETUID, BOB, ALICE_SR25519, 5 * USD)
    await credit_all(chain, ledger, netuid=NETUID, publisher_hotkeys={BOB})
    chain.dissolve_and_reregister()
    chain.spend(NETUID, BOB, ALICE_SR25519, 1 * USD)
    report = await credit_all(chain, ledger, netuid=NETUID, publisher_hotkeys={BOB})
    assert report.units_credited == 1 * USD
