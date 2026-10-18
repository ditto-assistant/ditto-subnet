"""Treasury receipts stay readable across upgrades that leave their surface alone."""

import pytest

from ditto.chain import audited_runtime_interfaces, treasury_receipts
from ditto.chain.errors import ChainConnectionError
from ditto.chain.runtime_interface import TREASURY_RECEIPT_SURFACE, runtime_interface
from ditto.chain.treasury_receipts import finalized_block
from ditto.tests.chain.runtime_metadata import (
    RUNTIME_CALL,
    body,
    clone,
    entry,
    metadata,
    type_def,
)
from ditto_screening_protocol.collector_receipts import (
    AUDITED_COLLECTOR_CODE_HASH,
    FINNEY_GENESIS,
)

UPGRADE = "0x" + "ab" * 32


def h(n: int) -> str:
    return "0x" + f"{n:064x}"


def treasury_metadata(**kwargs):
    return metadata(surface=TREASURY_RECEIPT_SURFACE, **kwargs)


@pytest.fixture(autouse=True)
def pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the synthetic treasury interface (with and without epoch consensus)."""
    pins = {}
    for epoch in (False, True):
        interface = runtime_interface(
            treasury_metadata(epoch_consensus=epoch), TREASURY_RECEIPT_SURFACE
        )
        pins[interface.digest] = ("vTest", interface)
    monkeypatch.setattr(audited_runtime_interfaces, "TREASURY_RECEIPT_INTERFACES", pins)
    monkeypatch.setattr(treasury_receipts, "_INTERFACE_RECEIPT_RUNTIMES", {})


class Chain:
    """Block 119 runs ``parent``; block 120 runs ``post``."""

    def __init__(self, parent: str, post: str, specs=(478, 479)) -> None:
        self.parent, self.post, self.specs = parent, post, specs
        self.consensus = "Yuma"
        self.meta = treasury_metadata()
        self.runtime_reads = 0

    async def get_chain_finalised_head(self):
        return h(200)

    async def get_block_number(self, _):
        return 200

    async def get_block_hash(self, n):
        return FINNEY_GENESIS if n == 0 else h(n)

    async def rpc_request(self, method, params):
        parent = params[-1] == h(119)
        if method == "state_getStorageHash":
            return {"result": self.parent if parent else self.post}
        assert method == "state_getRuntimeVersion"
        return {"result": {"specVersion": self.specs[0 if parent else 1]}}

    async def get_block_metadata(self, **_kwargs):
        self.runtime_reads += 1
        return self.meta

    async def query(self, **_kwargs):
        return self.consensus


async def test_forward_upgrade_to_unchanged_interface_reads_under_parent() -> None:
    chain = Chain(AUDITED_COLLECTOR_CODE_HASH, UPGRADE)
    at, parent, code = await finalized_block(chain, 120, FINNEY_GENESIS)
    assert (at, parent, code) == (h(120), h(119), AUDITED_COLLECTOR_CODE_HASH)


async def test_receipt_inside_admitted_runtime_is_read_once_per_code() -> None:
    chain = Chain(UPGRADE, UPGRADE)
    assert (await finalized_block(chain, 120, FINNEY_GENESIS))[2] == UPGRADE
    await finalized_block(chain, 120, FINNEY_GENESIS)
    assert chain.runtime_reads == 1


@pytest.mark.parametrize(
    ("parent", "post", "specs"),
    [
        (UPGRADE, AUDITED_COLLECTOR_CODE_HASH, (479, 478)),
        (AUDITED_COLLECTOR_CODE_HASH, UPGRADE, (479, 479)),
    ],
    ids=["reverse", "same-spec"],
)
async def test_unordered_transition_refuses(parent, post, specs) -> None:
    with pytest.raises(ValueError, match="unaudited runtime transition"):
        await finalized_block(Chain(parent, post, specs), 120, FINNEY_GENESIS)


async def test_epoch_consensus_must_be_yuma() -> None:
    chain = Chain(UPGRADE, UPGRADE)
    chain.consensus = "Null"
    with pytest.raises(ValueError, match="Yuma"):
        await finalized_block(chain, 120, FINNEY_GENESIS)


async def test_changed_interface_refuses_naming_the_entry() -> None:
    chain = Chain(AUDITED_COLLECTOR_CODE_HASH, UPGRADE)
    chain.meta = clone(treasury_metadata())
    entry(chain.meta, "SubtensorModule", "AutoStakeDestination").update(default="\x01")
    with pytest.raises(
        ValueError,
        match=f"not audited: {UPGRADE} .*storage:SubtensorModule.AutoStakeDestination",
    ):
        await finalized_block(chain, 120, FINNEY_GENESIS)
    assert treasury_receipts._INTERFACE_RECEIPT_RUNTIMES == {}


async def test_unreadable_metadata_is_a_provider_fault_not_a_refusal() -> None:
    chain = Chain(AUDITED_COLLECTOR_CODE_HASH, UPGRADE)

    async def unavailable(**_kwargs):
        raise OSError("archive dropped")

    setattr(chain, "get_block_metadata", unavailable)  # noqa: B010
    with pytest.raises(ChainConnectionError):
        await finalized_block(chain, 120, FINNEY_GENESIS)


def test_outer_call_enum_is_opaque_but_proxy_arguments_are_not() -> None:
    base = runtime_interface(treasury_metadata(), TREASURY_RECEIPT_SURFACE)
    grown = clone(treasury_metadata())
    # A new pallet anywhere grows RuntimeCall; the proxy call's shape is unchanged.
    type_def(grown, RUNTIME_CALL)["def"]["variant"]["variants"].append(
        {"name": "NewPallet", "index": 99, "fields": [], "docs": []}
    )
    assert runtime_interface(grown, TREASURY_RECEIPT_SURFACE).digest == base.digest
    proxy = next(p for p in body(grown)["pallets"] if p["name"] == "Proxy")
    type_def(grown, proxy["calls"]["ty"])["def"]["variant"]["variants"][0][
        "fields"
    ].append({"name": "force_proxy_type", "type": RUNTIME_CALL})
    changed = runtime_interface(grown, TREASURY_RECEIPT_SURFACE)
    assert base.changed_entries(changed) == ["call:Proxy.proxy"]
