import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ditto_screening_protocol.treasury import TreasuryEmissionPolicy
from ditto_screening_protocol.treasury_identity import (
    read_finalized_collector_pin,
    read_treasury_dispatch_observation,
)

FIXTURE = Path(__file__).parent / "fixtures/treasury_ledger_pin_v1.json"


class Reader:
    def __init__(self):
        self.raw = json.loads(FIXTURE.read_text())
        self.number = 101
        self.block_hash = self.raw["identity"]["finalized_block_hash"]
        self.hashes = {0: self.raw["policy"]["genesis_hash"], 101: self.block_hash}
        self.storage = {
            "Owner": self.raw["identity"]["owner_coldkey"],
            "SubnetOwner": self.raw["identity"]["subnet_owner_coldkey"],
            "Uids": self.raw["identity"]["uid"],
            "Keys": self.raw["identity"]["uid_hotkey"],
        }
        self.reads = []

    async def get_chain_finalised_head(self):
        return self.block_hash

    async def get_block_number(self, _hash):
        return self.number

    async def get_block_hash(self, number):
        return self.hashes[number]

    async def query(self, **kwargs):
        self.reads.append(kwargs)
        return SimpleNamespace(value=self.storage[kwargs["storage_function"]])

    def pin(self, **kwargs):
        return asyncio.run(
            read_finalized_collector_pin(
                self,
                TreasuryEmissionPolicy.model_validate(self.raw["policy"]),
                first_block=100,
                pinned_block=kwargs.get("pinned_block", 101),
            )
        )


def test_all_identity_reads_use_one_finalized_hash_and_reciprocal_uid():
    reader = Reader()
    pin = reader.pin()
    assert pin.model_dump(mode="json") == reader.raw
    assert len(reader.reads) == 4
    assert {read["block_hash"] for read in reader.reads} == {reader.block_hash}
    assert reader.reads[-1]["params"] == [118, 42]
    assert pin.mode == "shadow"


@pytest.mark.parametrize(
    "change",
    [
        "wrong_owner",
        "owner_associated",
        "reused_uid",
        "unregistered",
        "boolean_uid",
        "future_hash",
        "wrong_chain",
        "stale",
        "no_finality",
    ],
)
def test_missing_or_wrong_finalized_evidence_refuses_pin(change):
    reader = Reader()
    if change == "wrong_owner":
        reader.storage["Owner"] = "5" + "E" * 47
    elif change == "owner_associated":
        reader.storage["SubnetOwner"] = reader.storage["Owner"]
    elif change == "reused_uid":
        reader.storage["Keys"] = "5" + "E" * 47
    elif change == "unregistered":
        reader.storage["Uids"] = None
    elif change == "boolean_uid":
        reader.storage["Uids"] = False
    elif change == "future_hash":
        reader.hashes[101] = "0x" + "22" * 32
    elif change == "wrong_chain":
        reader.hashes[0] = "0x" + "22" * 32
    elif change == "stale":
        reader.number = 99
    else:
        reader.number = None
    with pytest.raises(ValueError):
        reader.pin()


def test_future_finalized_head_reads_only_pinned_block():
    reader = Reader()
    reader.number = 102
    reader.block_hash = "0x" + "22" * 32
    pin = reader.pin()
    assert pin.identity.finalized_block == 101
    assert {read["block_hash"] for read in reader.reads} == {reader.hashes[101]}


def test_uid_zero_is_valid_only_with_reciprocal_registration_evidence():
    reader = Reader()
    reader.storage["Uids"] = 0
    pin = reader.pin()
    assert pin.identity.uid == 0
    assert reader.reads[-1]["params"] == [118, 0]


def test_dispatch_independent_reads_overlap_after_runtime_warmup():
    class ConcurrentReader(Reader):
        active = 0
        peak = 0

        async def query(self, **kwargs):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0)
                return await super().query(**kwargs)
            finally:
                self.active -= 1

    reader = ConcurrentReader()
    reader.storage.update(SubnetEpochIndex=7, LastEpochBlock=100)
    result = asyncio.run(
        read_treasury_dispatch_observation(
            reader, TreasuryEmissionPolicy.model_validate(reader.raw["policy"])
        )
    )
    assert result.identity.model_dump(mode="json") == reader.raw["identity"]
    assert result.epoch_index == 7 and result.first_block == 100
    assert reader.peak == 4 and reader.active == 0
    assert reader.reads[0]["storage_function"] == "SubnetEpochIndex"
    assert reader.reads[-1]["storage_function"] == "Keys"
    assert {read["block_hash"] for read in reader.reads} == {reader.block_hash}
