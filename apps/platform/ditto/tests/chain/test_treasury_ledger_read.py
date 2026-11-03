"""The Gamma ledger reader decodes public chain state at one block hash."""

import pytest

from ditto.chain.treasury_ledger_read import read_treasury_ledger

HOTKEY = "5" + "h" * 47
COLDKEY = "5" + "c" * 47
HOLDING = "5" + "g" * 47
OWNER = "5" + "o" * 47
HEAD = "0x" + "AB" * 32


class Substrate:
    def __init__(self, *, locked=0, decaying_lock=None, pairs=False):
        self.locked = locked
        self.decaying_lock = decaying_lock
        self.pairs = pairs
        self.hashes: set[str] = set()

    async def get_chain_finalised_head(self):
        return HEAD

    async def get_block_number(self, _block_hash):
        return 9_000

    async def query(self, module, storage, _params, block_hash=None):
        self.hashes.add(block_hash)
        values = {
            ("SubtensorModule", "SubnetEpochIndex"): 25_710,
            ("Timestamp", "Now"): 1_791_951_348_000,
            ("SubtensorModule", "Uids"): 2,
            ("SubtensorModule", "Incentive"): [16_383, 0, 49_149],
            ("SubtensorModule", "Emission"): [5, 0, 110_701_901_792],
            ("SubtensorModule", "DecayingLock"): self.decaying_lock,
            ("SubtensorModule", "SubnetOwnerHotkey"): OWNER,
            ("System", "Account"): {"data": {"free": 309_114_431}},
        }
        return values[(module, storage)]

    async def runtime_call(self, api, method, _params, block_hash=None):
        assert api == "StakeInfoRuntimeApi"
        self.hashes.add(block_hash)
        if method == "get_stake_availability_for_coldkeys":
            rows = {
                COLDKEY: {
                    118: {
                        "total": 8_549,
                        "locked": self.locked,
                        "available": 8_549 - self.locked,
                    }
                },
                HOLDING: {118: {"total": 200, "locked": 0, "available": 200}},
            }
            if self.pairs:
                return [(key, list(value.items())) for key, value in rows.items()]
            return rows
        if method == "get_hotkey_conviction":
            return {"bits": 102_039 << 64}
        if method == "get_most_convicted_hotkey_on_subnet":
            return OWNER
        raise AssertionError(method)


async def read(substrate):
    return await read_treasury_ledger(
        substrate,
        netuid=118,
        collector_hotkey=HOTKEY,
        collector_coldkey=COLDKEY,
        holdings=[("gm", HOLDING)],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pairs", [False, True])
async def test_reads_one_block_and_decodes_every_field(pairs):
    substrate = Substrate(pairs=pairs)
    reading = await read(substrate)
    assert substrate.hashes == {HEAD}
    assert reading.block_hash == HEAD.lower()
    assert reading.epoch_index == 25_710
    assert reading.collector_uid == 2
    assert reading.collector_incentive == 49_149 / 65_535
    assert reading.collector_emission_rao == 110_701_901_792
    assert (reading.collector_total_rao, reading.collector_locked_rao) == (8_549, 0)
    assert reading.collector_lock_perpetual is None
    assert reading.owner_conviction_rao == 102_039
    assert reading.most_convicted_hotkey == OWNER
    assert reading.holdings[0].alpha_total_rao == 200
    assert reading.holdings[0].tao_free_rao == 309_114_431


@pytest.mark.asyncio
@pytest.mark.parametrize(("decaying", "perpetual"), [(False, True), (None, False)])
async def test_lock_mode_follows_decaying_lock_storage(decaying, perpetual):
    reading = await read(Substrate(locked=8_000, decaying_lock=decaying))
    assert reading.collector_locked_rao == 8_000
    assert reading.collector_available_rao == 549
    assert reading.collector_lock_perpetual is perpetual


@pytest.mark.asyncio
async def test_sampled_epoch_costs_one_query():
    substrate = Substrate()
    seen = []

    async def should_read(epoch):
        seen.append(epoch)
        return False

    assert (
        await read_treasury_ledger(
            substrate,
            netuid=118,
            collector_hotkey=HOTKEY,
            collector_coldkey=COLDKEY,
            should_read=should_read,
        )
        is None
    )
    assert seen == [25_710]
