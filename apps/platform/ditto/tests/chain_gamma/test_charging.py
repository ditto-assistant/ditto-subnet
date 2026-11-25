"""The standard's acceptance steps 3-5, run end to end against fakes."""

from __future__ import annotations

import pytest

from ditto.chain_gamma.amounts import UNITS_PER_USD
from ditto.chain_gamma.btauth import HEADER_SIGNATURE, InMemoryNonceStore
from ditto.chain_gamma.charging import (
    INSUFFICIENT_BALANCE,
    SERVICE_PAUSED,
    authorize_and_charge,
)
from ditto.chain_gamma.crediting import credit_all, credit_hotkey
from ditto.chain_gamma.ledger import InMemoryChainGammaLedger
from ditto.tests.chain_gamma.helpers import (
    ALICE_SR25519,
    BOB,
    VECTOR_BODY,
    VECTOR_NONCE,
    VECTOR_TARGET,
    FakeChain,
    never_protected,
    sr25519_headers,
)

NETUID = 118
USD = UNITS_PER_USD
PRICE = USD // 4  # $0.25 per request


class Service:
    """One chain gamma service wired from the library's parts."""

    def __init__(self, *, accepting: bool = True) -> None:
        self.chain = FakeChain()
        self.ledger = InMemoryChainGammaLedger()
        self.nonces = InMemoryNonceStore()
        self.accepting = accepting
        self.nonce = VECTOR_NONCE

    async def request(self, headers: dict[str, str] | None = None):
        self.nonce += 1
        headers = headers or sr25519_headers(nonce_ns=self.nonce)
        return await authorize_and_charge(
            headers=headers,
            body=VECTOR_BODY,
            method="POST",
            target=VECTOR_TARGET,
            price_units=PRICE,
            receivers=frozenset({BOB}),
            nonce_store=self.nonces,
            is_protected_sender=never_protected,
            ledger=self.ledger,
            credit_hotkey=self._credit_hotkey,
            accepting=self.accepting,
            now_ns=self.nonce,
        )

    async def _credit_hotkey(self, hotkey: str) -> int:
        return await credit_hotkey(
            self.chain,
            self.ledger,
            netuid=NETUID,
            publisher_hotkeys={BOB},
            hotkey=hotkey,
        )


async def test_spend_then_use_until_402() -> None:
    service = Service()
    service.chain.spend(NETUID, BOB, ALICE_SR25519, 1 * USD)  # step 2: ~$1
    await credit_all(
        service.chain, service.ledger, netuid=NETUID, publisher_hotkeys={BOB}
    )
    assert (await service.ledger.balance(ALICE_SR25519)).to_response()[
        "remaining_usd"
    ] == "1.00"  # step 3
    for expected_remaining in (3 * PRICE, 2 * PRICE, PRICE, 0):  # step 4
        outcome = await service.request()
        assert outcome.status == 200 and outcome.may_proceed
        balance = await service.ledger.balance(ALICE_SR25519)
        assert balance.remaining_units == expected_remaining
    refused = await service.request()  # step 5
    assert refused.status == 402
    assert refused.body == INSUFFICIENT_BALANCE == {"error": "insufficient_balance"}
    assert not refused.may_proceed


async def test_402_path_credits_a_spend_the_scan_has_not_seen() -> None:
    service = Service()
    service.chain.spend(NETUID, BOB, ALICE_SR25519, USD // 10)
    outcome = await service.request()  # no scan ran; on-demand credit, then charge
    assert outcome.status == 402  # $0.10 cannot cover $0.25
    service.chain.spend(NETUID, BOB, ALICE_SR25519, USD // 2)
    outcome = await service.request()
    assert outcome.status == 200
    view = await service.ledger.balance(ALICE_SR25519)
    assert (view.credited_units, view.used_units) == (USD // 10 + USD // 2, PRICE)


async def test_bad_auth_is_401_and_charges_nothing() -> None:
    service = Service()
    service.chain.spend(NETUID, BOB, ALICE_SR25519, 1 * USD)
    forged = sr25519_headers(nonce_ns=service.nonce + 1) | {
        HEADER_SIGNATURE: "0x" + "22" * 64
    }
    outcome = await service.request(forged)
    assert outcome.status == 401
    assert outcome.body is not None and outcome.body["error"] == "bad_signature"
    assert (await service.ledger.balance(ALICE_SR25519)).used_units == 0


async def test_paused_service_refuses_before_any_work() -> None:
    service = Service(accepting=False)
    outcome = await service.request()
    assert (outcome.status, outcome.body) == (503, SERVICE_PAUSED)
    assert service.chain.reads == 0


async def test_a_paid_endpoint_needs_a_price() -> None:
    with pytest.raises(ValueError):
        await authorize_and_charge(
            headers={},
            body=b"",
            method="GET",
            target="/",
            price_units=0,
            receivers=frozenset({BOB}),
            nonce_store=InMemoryNonceStore(),
            is_protected_sender=never_protected,
            ledger=InMemoryChainGammaLedger(),
            credit_hotkey=_unused_credit,
        )


async def _unused_credit(_hotkey: str) -> int:
    raise AssertionError("not reached")
