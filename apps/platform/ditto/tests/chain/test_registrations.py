"""Registration answers when Pylon's metagraph snapshot trails the chain.

SN-463: a miner re-registered into uid 237, but Pylon kept serving a
recent-neurons snapshot that still mapped the uid to its previous occupant.
Pylon ages that cache by when it wrote it, not by the block it read, so the
upload pre-check answered 1101 and the public activity map showed the old
hotkey long after the chain had moved on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ditto.chain.client import SNAPSHOT_LAG_RECONCILE_BLOCKS, ChainClient
from ditto.chain.errors import ChainConnectionError, ChainTimeoutError
from ditto.chain.models import BlockInfo, ChainRegistration, ChainRegistrations
from ditto.tests.chain.test_client import AsyncRows, make_chain_config

NETUID = 118
UID = 237
OLD_HOTKEY = "5OldOccupantHotkey"
NEW_HOTKEY = "5NewOccupantHotkey"
NEW_COLDKEY = "5NewCk"
VALIDATOR_HOTKEY = "5ValidatorHotkey"
VALIDATOR_COLDKEY = "5ValCk"
SNAPSHOT_BLOCK = 9_458_900
REGISTERED_AT = 9_458_935
NOW = 1_800_000_000.0


def pylon_neuron(uid: int, coldkey: str, **overrides: Any) -> MagicMock:
    fields: dict[str, Any] = {
        "coldkey": coldkey,
        "uid": uid,
        "stake": 2.5,
        "axon_info": {},
        "active": True,
        "validator_permit": False,
    }
    fields.update(overrides)
    return MagicMock(**fields)


def pylon_snapshot(block: int, neurons: dict[str, MagicMock]) -> MagicMock:
    """Mirror ``GetNeuronsResponse``: the snapshot's block and its neurons."""
    return MagicMock(
        block=MagicMock(number=block, hash="0x" + "11" * 32), neurons=neurons
    )


def stale_snapshot() -> MagicMock:
    """Uid 237 still held by its previous occupant, as Pylon served it."""
    return pylon_snapshot(
        SNAPSHOT_BLOCK,
        {
            VALIDATOR_HOTKEY: pylon_neuron(
                0, VALIDATOR_COLDKEY, stake=900.0, validator_permit=True
            ),
            OLD_HOTKEY: pylon_neuron(UID, "5OldCk"),
        },
    )


def registrations(block: int, *entries: tuple[int, str, str]) -> ChainRegistrations:
    return ChainRegistrations(
        netuid=NETUID,
        block=block,
        block_timestamp=int(NOW),
        provider="archive.example",
        observed_at=datetime.now(UTC),
        registrations=tuple(
            ChainRegistration(uid=uid, hotkey=hotkey, coldkey=coldkey)
            for uid, hotkey, coldkey in entries
        ),
    )


def remapped(block: int = REGISTERED_AT + 300) -> ChainRegistrations:
    """Chain storage after the re-registration: uid 237 is the new hotkey."""
    return registrations(
        block,
        (0, VALIDATOR_HOTKEY, VALIDATOR_COLDKEY),
        (UID, NEW_HOTKEY, NEW_COLDKEY),
    )


@pytest.fixture
async def client(install_pylon_module: AsyncMock) -> AsyncIterator[ChainClient]:
    install_pylon_module.v1.open_access.get_recent_neurons.return_value = (
        stale_snapshot()
    )
    async with ChainClient(make_chain_config()) as chain:
        yield chain


def install_chain_reads(chain: ChainClient, *reads: Any) -> AsyncMock:
    """Answer chain-storage registration reads in order, without a network."""
    reader = AsyncMock(side_effect=list(reads))
    chain.read_chain_registrations = reader  # type: ignore[method-assign]
    return reader


class TestHotkeyRegistration:
    async def test_new_occupant_of_a_remapped_uid_is_registered(
        self, client: ChainClient
    ) -> None:
        reader = install_chain_reads(client, remapped())

        registration = await client.get_hotkey_registration(NEW_HOTKEY, NETUID)

        assert registration.registered
        assert (registration.uid, registration.coldkey) == (UID, NEW_COLDKEY)
        assert registration.snapshot_block == SNAPSHOT_BLOCK
        assert registration.chain_block == REGISTERED_AT + 300
        reader.assert_awaited_once_with(NETUID)

    async def test_stale_previous_occupant_is_gone_from_every_consumer(
        self, client: ChainClient
    ) -> None:
        install_chain_reads(client, remapped())
        await client.get_hotkey_registration(NEW_HOTKEY, NETUID)

        # Public activity, the emission ledger and validator auth all read
        # get_recent_neurons: they now see the chain's uid 237.
        neurons = {n.uid: n for n in await client.get_recent_neurons(NETUID)}
        assert neurons[UID].hotkey == NEW_HOTKEY
        assert neurons[UID].coldkey == NEW_COLDKEY
        assert neurons[UID].validator_permit is False
        assert OLD_HOTKEY not in {n.hotkey for n in neurons.values()}
        # An unchanged uid keeps Pylon's other fields.
        assert neurons[0].stake == 900.0
        assert neurons[0].validator_permit is True

        previous = await client.get_hotkey_registration(OLD_HOTKEY, NETUID)
        assert not previous.registered
        assert previous.chain_block == REGISTERED_AT + 300

    async def test_unregistered_hotkey_is_confirmed_absent_on_chain(
        self, client: ChainClient
    ) -> None:
        reader = install_chain_reads(client, remapped())

        registration = await client.get_hotkey_registration("5NeverRegistered", NETUID)

        assert not registration.registered
        assert registration.coldkey is None and registration.uid is None
        assert registration.chain_block == REGISTERED_AT + 300
        reader.assert_awaited_once()

    async def test_snapshot_hit_reads_no_chain(self, client: ChainClient) -> None:
        reader = install_chain_reads(client)

        registration = await client.get_hotkey_registration(VALIDATOR_HOTKEY, NETUID)

        assert registration.registered
        assert registration.coldkey == VALIDATOR_COLDKEY
        assert registration.chain_block is None
        reader.assert_not_awaited()

    async def test_unconfirmed_absence_reads_no_chain(
        self, client: ChainClient
    ) -> None:
        reader = install_chain_reads(client)

        registration = await client.get_hotkey_registration(
            NEW_HOTKEY, NETUID, confirm_absence=False
        )

        assert not registration.registered
        assert registration.chain_block is None
        reader.assert_not_awaited()

    async def test_unreadable_chain_is_an_outage_not_a_miss(
        self, client: ChainClient
    ) -> None:
        install_chain_reads(client, ChainConnectionError("every provider down"))

        with pytest.raises(ChainConnectionError):
            await client.get_hotkey_registration(NEW_HOTKEY, NETUID)

    async def test_one_chain_read_serves_a_block_of_misses(
        self, client: ChainClient
    ) -> None:
        now = [1_000.0]
        client._clock = lambda: now[0]
        gate = asyncio.Event()

        async def _slow_read(_netuid: int) -> ChainRegistrations:
            await gate.wait()
            return remapped()

        reader = install_chain_reads(client)
        reader.side_effect = _slow_read
        waiters = [
            asyncio.create_task(client.get_hotkey_registration(hotkey, NETUID))
            for hotkey in (NEW_HOTKEY, "5NeverRegistered", NEW_HOTKEY)
        ]
        await asyncio.sleep(0)
        gate.set()
        answers = await asyncio.gather(*waiters)

        assert [a.registered for a in answers] == [True, False, True]
        assert reader.await_count == 1

        now[0] += 13.0  # past one block
        await client.get_hotkey_registration("5NeverRegistered", NETUID)
        assert reader.await_count == 2

    async def test_read_older_than_the_snapshot_cannot_resurrect_a_hotkey(
        self, client: ChainClient
    ) -> None:
        # The snapshot already shows uid 237 re-registered; a provider still
        # behind it must not bring the previous occupant back.
        install_chain_reads(
            client,
            registrations(
                SNAPSHOT_BLOCK - 10,
                (0, VALIDATOR_HOTKEY, VALIDATOR_COLDKEY),
                (UID, "5EvenEarlierOccupant", "5EvenEarlierColdkey"),
            ),
        )

        registration = await client.get_hotkey_registration(
            "5EvenEarlierOccupant", NETUID
        )

        assert not registration.registered
        neurons = {n.uid: n.hotkey for n in await client.get_recent_neurons(NETUID)}
        assert neurons[UID] == OLD_HOTKEY

    async def test_pylon_catching_up_retires_the_correction(
        self, client: ChainClient, install_pylon_module: AsyncMock
    ) -> None:
        now = [1_000.0]
        client._clock = lambda: now[0]
        install_chain_reads(client, remapped(REGISTERED_AT + 300))
        await client.get_hotkey_registration(NEW_HOTKEY, NETUID)

        install_pylon_module.v1.open_access.get_recent_neurons.return_value = (
            pylon_snapshot(
                REGISTERED_AT + 400,
                {
                    VALIDATOR_HOTKEY: pylon_neuron(0, VALIDATOR_COLDKEY),
                    "5LaterOccupant": pylon_neuron(UID, "5LaterColdkey"),
                },
            )
        )
        now[0] += 13.0

        snapshot = await client.get_neuron_snapshot(NETUID)

        assert snapshot.registrations_block == snapshot.block == REGISTERED_AT + 400
        assert snapshot.neuron("5LaterOccupant") is not None
        assert snapshot.neuron(NEW_HOTKEY) is None

    async def test_wrappers_share_the_confirmed_answer(
        self, client: ChainClient
    ) -> None:
        install_chain_reads(client, remapped())

        assert await client.is_registered(NEW_HOTKEY, NETUID) is True
        assert await client.get_registered_coldkey(NEW_HOTKEY, NETUID) == NEW_COLDKEY
        assert await client.is_registered("5NeverRegistered", NETUID) is False


class FakeStorageKey:
    def __init__(self, hotkey: str) -> None:
        self.hotkey = hotkey

    def to_hex(self) -> str:
        return "0x" + self.hotkey.encode().hex()


class FakeRegistrationSubstrate:
    """One provider's finalized head, ``Keys`` map and ``Owner`` entries."""

    def __init__(
        self,
        *,
        block: int,
        age_seconds: float = 12.0,
        keys: list[tuple[int, str]] | None = None,
        owners: dict[str, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.block = block
        self.timestamp_ms = int((NOW - age_seconds) * 1000)
        self.keys = (
            keys if keys is not None else [(0, VALIDATOR_HOTKEY), (UID, NEW_HOTKEY)]
        )
        self.owners = (
            owners
            if owners is not None
            else {VALIDATOR_HOTKEY: VALIDATOR_COLDKEY, NEW_HOTKEY: NEW_COLDKEY}
        )
        self.error = error
        self.reads: list[str] = []

    async def __aenter__(self) -> FakeRegistrationSubstrate:
        if self.error is not None:
            raise self.error
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def get_chain_finalised_head(self) -> str:
        return f"0x{self.block:064x}"

    async def get_block_header(self, block_hash: str) -> dict[str, Any]:
        assert block_hash == f"0x{self.block:064x}"
        return {"header": {"number": self.block}}

    async def query(
        self, module: str, storage_function: str, block_hash: str, **_: Any
    ) -> int:
        assert (module, storage_function) == ("Timestamp", "Now")
        assert block_hash == f"0x{self.block:064x}"
        return self.timestamp_ms

    async def query_map(
        self,
        module: str,
        storage_function: str,
        params: list[Any],
        block_hash: str,
        fully_exhaust: bool,
    ) -> AsyncRows:
        assert (module, storage_function, params) == (
            "SubtensorModule",
            "Keys",
            [NETUID],
        )
        assert block_hash == f"0x{self.block:064x}" and fully_exhaust
        self.reads.append("Keys")
        # Storage iteration order is by key hash, not by uid.
        return AsyncRows(list(reversed(self.keys)))

    async def create_storage_key(
        self, pallet: str, storage_function: str, params: list[str], block_hash: str
    ) -> FakeStorageKey:
        assert (pallet, storage_function) == ("SubtensorModule", "Owner")
        assert block_hash == f"0x{self.block:064x}"
        return FakeStorageKey(params[0])

    async def query_multi(
        self, storage_keys: list[FakeStorageKey], block_hash: str
    ) -> list[tuple[FakeStorageKey, str | None]]:
        assert block_hash == f"0x{self.block:064x}"
        self.reads.append("Owner")
        # Results come back keyed by storage key, in no promised order.
        return [(key, self.owners.get(key.hotkey)) for key in reversed(storage_keys)]


PAID_URL = "wss://paid.example/ws"
PAID_KEY = "paid-provider-credential"
ARCHIVE_URL = "wss://archive.example:443"
ENTRYPOINT_URL = "wss://entrypoint-finney.opentensor.ai:443"


@pytest.fixture
async def provider_client(
    install_pylon_module: AsyncMock,
) -> AsyncIterator[ChainClient]:
    install_pylon_module.v1.open_access.get_recent_neurons.return_value = (
        stale_snapshot()
    )
    config = make_chain_config(
        archive_rpc_url=PAID_URL,
        archive_rpc_api_key=PAID_KEY,
        public_archive_rpc_urls=(ARCHIVE_URL,),
    )
    async with ChainClient(config) as chain:
        chain._wall_clock = lambda: NOW
        yield chain


def install_providers(
    chain: ChainClient, **by_host: FakeRegistrationSubstrate
) -> list[str]:
    """Route each provider URL to a fake; returns the hosts in call order."""
    hosts = {
        f"{PAID_URL}?authorization={PAID_KEY}": "paid",
        ARCHIVE_URL: "archive",
        ENTRYPOINT_URL: "entrypoint",
    }
    called: list[str] = []

    def _substrate(*, archive: bool = False, url: str | None = None) -> Any:
        assert url is not None and not archive
        called.append(hosts[url])
        return by_host[hosts[url]]

    chain._new_treasury_substrate = _substrate  # type: ignore[method-assign]
    return called


class TestReadChainRegistrations:
    async def test_reads_keys_and_owners_at_one_finalized_block(
        self, provider_client: ChainClient
    ) -> None:
        paid = FakeRegistrationSubstrate(block=REGISTERED_AT + 5)
        called = install_providers(provider_client, paid=paid)

        chain = await provider_client.read_chain_registrations(NETUID)

        assert called == ["paid"]
        assert paid.reads == ["Keys", "Owner"]
        assert chain.block == REGISTERED_AT + 5
        assert chain.provider == "paid.example"
        assert chain.registrations == (
            ChainRegistration(
                uid=0, hotkey=VALIDATOR_HOTKEY, coldkey=VALIDATOR_COLDKEY
            ),
            ChainRegistration(uid=UID, hotkey=NEW_HOTKEY, coldkey=NEW_COLDKEY),
        )

    async def test_provider_behind_the_chain_cannot_prove_absence(
        self, provider_client: ChainClient
    ) -> None:
        # A node stuck before the re-registration still maps uid 237 to the
        # previous occupant; a current provider must answer instead.
        stuck = FakeRegistrationSubstrate(
            block=SNAPSHOT_BLOCK,
            age_seconds=3_600,
            keys=[(0, VALIDATOR_HOTKEY), (UID, OLD_HOTKEY)],
            owners={VALIDATOR_HOTKEY: VALIDATOR_COLDKEY, OLD_HOTKEY: "5Old"},
        )
        current = FakeRegistrationSubstrate(block=REGISTERED_AT + 300)
        called = install_providers(provider_client, paid=stuck, archive=current)

        chain = await provider_client.read_chain_registrations(NETUID)

        assert called == ["paid", "archive"]
        assert chain.provider == "archive.example"
        assert chain.lookup(NEW_HOTKEY) is not None

    async def test_newest_answer_when_every_provider_is_behind(
        self, provider_client: ChainClient
    ) -> None:
        called = install_providers(
            provider_client,
            paid=FakeRegistrationSubstrate(block=REGISTERED_AT + 10, age_seconds=900),
            archive=FakeRegistrationSubstrate(
                block=REGISTERED_AT + 50, age_seconds=600
            ),
            entrypoint=FakeRegistrationSubstrate(
                block=REGISTERED_AT + 20, age_seconds=800
            ),
        )

        chain = await provider_client.read_chain_registrations(NETUID)

        assert called == ["paid", "archive", "entrypoint"]
        assert chain.block == REGISTERED_AT + 50

    async def test_empty_map_is_a_failed_read_not_an_empty_subnet(
        self, provider_client: ChainClient
    ) -> None:
        called = install_providers(
            provider_client,
            paid=FakeRegistrationSubstrate(block=REGISTERED_AT, keys=[]),
            archive=FakeRegistrationSubstrate(block=REGISTERED_AT + 1),
        )

        chain = await provider_client.read_chain_registrations(NETUID)

        assert called == ["paid", "archive"]
        assert chain.lookup(NEW_HOTKEY) is not None

    async def test_every_provider_failing_is_a_connection_error_without_secrets(
        self, provider_client: ChainClient
    ) -> None:
        install_providers(
            provider_client,
            paid=FakeRegistrationSubstrate(
                block=1,
                error=OSError(
                    f"handshake refused for {PAID_URL}?authorization={PAID_KEY}"
                ),
            ),
            archive=FakeRegistrationSubstrate(block=1, keys=[]),
            entrypoint=FakeRegistrationSubstrate(block=1, error=OSError("429")),
        )

        with pytest.raises(ChainConnectionError) as raised:
            await provider_client.read_chain_registrations(NETUID)

        assert PAID_KEY not in str(raised.value)
        assert "<redacted>" in str(raised.value)

    async def test_every_provider_timing_out_is_a_timeout(
        self, provider_client: ChainClient
    ) -> None:
        install_providers(
            provider_client,
            **{
                host: FakeRegistrationSubstrate(block=1, error=TimeoutError())
                for host in ("paid", "archive", "entrypoint")
            },
        )

        with pytest.raises(ChainTimeoutError):
            await provider_client.read_chain_registrations(NETUID)


@pytest.fixture
async def watched_client(
    install_pylon_module: AsyncMock,
) -> AsyncIterator[ChainClient]:
    install_pylon_module.v1.open_access.get_recent_neurons.return_value = (
        stale_snapshot()
    )
    async with ChainClient(
        make_chain_config(metagraph_lag_check_seconds=300.0)
    ) as chain:
        yield chain


def install_head(chain: ChainClient, block: int | Exception) -> AsyncMock:
    head = AsyncMock(
        side_effect=block
        if isinstance(block, Exception)
        else lambda: BlockInfo(number=block, hash=f"0x{block:064x}")
    )
    chain.get_finalized_block = head  # type: ignore[method-assign]
    return head


async def finish_lag_check(chain: ChainClient) -> None:
    await asyncio.gather(*tuple(chain._metagraph_lag_checks.values()))


class TestMetagraphLagCheck:
    async def test_lagging_snapshot_is_corrected_from_chain_storage(
        self, watched_client: ChainClient
    ) -> None:
        head = SNAPSHOT_BLOCK + SNAPSHOT_LAG_RECONCILE_BLOCKS + 1
        install_head(watched_client, head)
        reader = install_chain_reads(watched_client, remapped(head))

        before = {
            n.uid: n.hotkey for n in await watched_client.get_recent_neurons(NETUID)
        }
        await finish_lag_check(watched_client)
        after = {
            n.uid: n.hotkey for n in await watched_client.get_recent_neurons(NETUID)
        }

        assert before[UID] == OLD_HOTKEY  # the read that noticed is never delayed
        assert after[UID] == NEW_HOTKEY
        reader.assert_awaited_once_with(NETUID)

    async def test_snapshot_within_its_refresh_cycle_reads_nothing(
        self, watched_client: ChainClient
    ) -> None:
        install_head(watched_client, SNAPSHOT_BLOCK + SNAPSHOT_LAG_RECONCILE_BLOCKS)
        reader = install_chain_reads(watched_client)

        await watched_client.get_recent_neurons(NETUID)
        await finish_lag_check(watched_client)

        reader.assert_not_awaited()

    async def test_checks_at_most_once_per_interval(
        self, watched_client: ChainClient
    ) -> None:
        now = [1_000.0]
        watched_client._clock = lambda: now[0]
        head = install_head(watched_client, SNAPSHOT_BLOCK)

        for _ in range(3):
            await watched_client.get_recent_neurons(NETUID)
            await finish_lag_check(watched_client)
        assert head.await_count == 1

        now[0] += 301.0
        await watched_client.get_recent_neurons(NETUID)
        await finish_lag_check(watched_client)
        assert head.await_count == 2

    async def test_a_failed_check_never_fails_a_read(
        self, watched_client: ChainClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        install_head(watched_client, ChainConnectionError("headers unavailable"))

        with caplog.at_level(logging.WARNING, logger="ditto.chain.client"):
            neurons = await watched_client.get_recent_neurons(NETUID)
            await finish_lag_check(watched_client)

        assert {n.hotkey for n in neurons} == {VALIDATOR_HOTKEY, OLD_HOTKEY}
        assert "metagraph lag check for netuid=118 failed" in caplog.text

    async def test_disabled_without_an_interval(self, client: ChainClient) -> None:
        head = install_head(client, SNAPSHOT_BLOCK + 10_000)

        await client.get_recent_neurons(NETUID)
        await finish_lag_check(client)

        head.assert_not_awaited()


class TestRegistrationFreshness:
    async def test_reports_lag_and_the_uids_it_corrected(
        self, client: ChainClient
    ) -> None:
        install_chain_reads(client, remapped(REGISTERED_AT + 300))
        await client.get_hotkey_registration(NEW_HOTKEY, NETUID)
        install_head(client, REGISTERED_AT + 302)

        freshness = await client.get_registration_freshness(NETUID)

        assert freshness.snapshot_block == SNAPSHOT_BLOCK
        assert freshness.finalized_block == REGISTERED_AT + 302
        assert freshness.serving_registrations_block == REGISTERED_AT + 300
        assert freshness.chain_registrations is not None
        assert [(c.uid, c.pylon_hotkey, c.chain_hotkey) for c in freshness.changes] == [
            (UID, OLD_HOTKEY, NEW_HOTKEY)
        ]

    async def test_a_failed_head_read_still_reports_the_snapshot(
        self, client: ChainClient
    ) -> None:
        install_head(client, ChainConnectionError("headers unavailable"))

        freshness = await client.get_registration_freshness(NETUID)

        assert freshness.snapshot_block == SNAPSHOT_BLOCK
        assert freshness.finalized_block is None
        assert freshness.finalized_block_error == "headers unavailable"
        assert freshness.changes == ()
