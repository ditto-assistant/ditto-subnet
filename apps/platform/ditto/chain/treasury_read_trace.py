"""Fixed labels on the same read-only calls, without deadlines or evidence changes."""

from typing import Any

from ditto.chain.errors import TreasuryReadStep


class TreasuryReadTrace:
    def __init__(self, *, setters: bool = False):
        self.step: TreasuryReadStep = "connection"
        self.client: Any = None
        self.setters = setters

    async def get_chain_finalised_head(self):
        self.step = "finalized_head"
        return await self.client.get_chain_finalised_head()

    async def get_block_number(self, block_hash):
        self.step = "finalized_height"
        return await self.client.get_block_number(block_hash)

    async def get_block_hash(self, block):
        self.step = "genesis_hash" if block == 0 else "canonical_hash"
        return await self.client.get_block_hash(block)

    async def query(self, **kwargs: Any):
        # Concurrent members of one batch share a checkpoint; a label does
        # not claim that just one underlying request was pending. The first
        # storage checkpoint also includes the SDK's metadata initialization.
        storage = kwargs.get("storage_function")
        if self.setters:
            self.step = (
                "permit_vector" if storage == "ValidatorPermit" else "setter_binding"
            )
        elif storage == "SubnetEpochIndex":
            self.step = "epoch_storage"
        elif storage == "Keys":
            self.step = "uid_binding"
        else:
            self.step = "collector_storage"
        return await self.client.query(**kwargs)
