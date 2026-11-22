"""Test fixtures for chain gamma: public test vectors only.

``//Alice`` is the well-known Substrate development key that the signed-requests
guide's own vectors use. It holds nothing on Finney and is not a wallet; it only
produces sr25519 signatures inside tests. ``BOB`` is a public address used as
the receiving service hotkey and is never signed for.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import bittensor

from ditto.chain_gamma.btauth import (
    HEADER_CRYPTO,
    HEADER_HOTKEY,
    HEADER_NONCE,
    HEADER_RECEIVER,
    HEADER_SIGNATURE,
    HEADER_VERSION,
    build_payload,
)
from ditto.chain_gamma.crediting import FinalizedBlock

ALICE_DEV = bittensor.Keypair.create_from_uri("//Alice")
ALICE_SR25519 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
ALICE_ED25519 = "5FA9nQDVg267DEd8m1ZypXLBnvN7SFxYwV7ndqSYGiN9TTpu"
BOB = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
CHARLIE = "5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y"

VECTOR_NONCE = 1_752_076_800_000_000_000
VECTOR_BODY = b'{"prompt": "hello"}'
VECTOR_TARGET = "/generate?stream=false"
VECTOR_ED25519_SIGNATURE = (
    "0x1a6b631988bf1f7ea093ab9d5acf61f05c316d0b0e428b24b487b75a5f6ee0e1"
    "a56a26918fdf871b6cf09437e6588023a4ce423cf622621b2a7feee17612f704"
)


def sr25519_headers(
    *,
    method: str = "POST",
    target: str = VECTOR_TARGET,
    body: bytes = VECTOR_BODY,
    nonce_ns: int = VECTOR_NONCE,
    receiver: str = BOB,
    signer: bittensor.Keypair = ALICE_DEV,
) -> dict[str, str]:
    payload = build_payload(
        scheme="sr25519",
        method=method,
        target=target,
        body=body,
        nonce_ns=nonce_ns,
        sender=signer.ss58_address,
        receiver=receiver,
    )
    return {
        HEADER_VERSION: "1",
        HEADER_HOTKEY: signer.ss58_address,
        HEADER_RECEIVER: receiver,
        HEADER_NONCE: str(nonce_ns),
        HEADER_SIGNATURE: "0x" + signer.sign(payload).hex(),
    }


def ed25519_vector_headers() -> dict[str, str]:
    return {
        HEADER_VERSION: "1",
        HEADER_HOTKEY: ALICE_ED25519,
        HEADER_RECEIVER: BOB,
        HEADER_NONCE: str(VECTOR_NONCE),
        HEADER_CRYPTO: "ed25519",
        HEADER_SIGNATURE: VECTOR_ED25519_SIGNATURE,
    }


async def never_protected(_hotkey: str) -> bool:
    return False


async def always_protected(_hotkey: str) -> bool:
    return True


@dataclass
class FakeChain:
    """An in-memory ``ChainGammaReader``: finalized state you can edit."""

    number: int = 100
    counter: int = 0
    spent: dict[tuple[int, str, str], int] = field(default_factory=dict)
    reads: int = 0

    def spend(self, netuid: int, service: str, hotkey: str, units: int) -> None:
        key = (netuid, service, hotkey)
        self.spent[key] = self.spent.get(key, 0) + units
        self.number += 1

    def dissolve_and_reregister(self) -> None:
        self.spent.clear()
        self.counter += 1
        self.number += 1

    async def finalized_block(self) -> FinalizedBlock:
        return FinalizedBlock(number=self.number, block_hash=f"0x{self.number:064x}")

    async def registered_subnet_counter(self, netuid: int, block_hash: str) -> int:
        del netuid, block_hash
        return self.counter

    async def gamma_spent_at_service(
        self, netuid: int, service: str, block_hash: str
    ) -> Mapping[str, int]:
        del block_hash
        self.reads += 1
        return {
            user: total
            for (net, svc, user), total in self.spent.items()
            if net == netuid and svc == service
        }

    async def gamma_spent(
        self, netuid: int, service: str, hotkey: str, block_hash: str
    ) -> int:
        del block_hash
        self.reads += 1
        return self.spent.get((netuid, service, hotkey), 0)
