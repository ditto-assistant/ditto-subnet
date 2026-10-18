"""The upgrade report names what moved without changing any admission."""

from types import SimpleNamespace

import pytest

from ditto.chain import audited_runtime_interfaces
from ditto.chain.runtime_interface import (
    SOURCE_EMISSION_SURFACE,
    TREASURY_RECEIPT_SURFACE,
    RuntimeSurface,
    runtime_call_digests,
    runtime_interface,
)
from ditto.chain.runtime_interface_report import (
    BlockNotFinalized,
    read_runtime_interface_report,
)
from ditto.tests.chain.runtime_metadata import CALLS, clone, entry, metadata, type_def
from ditto_screening_protocol.collector_receipts import AUDITED_COLLECTOR_CODE_HASH

UPGRADE = "0x" + "ab" * 32


def combined(epoch: bool = True):
    """One runtime that carries both surfaces, as Finney does."""

    def union(*parts: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted({item for part in parts for item in part}))

    both = RuntimeSurface(
        "both",
        union(SOURCE_EMISSION_SURFACE.storage, TREASURY_RECEIPT_SURFACE.storage),
        union(SOURCE_EMISSION_SURFACE.events, TREASURY_RECEIPT_SURFACE.events),
        union(SOURCE_EMISSION_SURFACE.calls, TREASURY_RECEIPT_SURFACE.calls),
    )
    return metadata(epoch_consensus=epoch, surface=both)


class Chain:
    def __init__(self, code: str, meta) -> None:
        self.code, self.meta = code, meta

    async def get_chain_finalised_head(self):
        return "head"

    async def get_block_header(self, *, block_hash):
        assert block_hash == "head"
        return {"header": {"number": 500}}

    async def get_block_hash(self, number):
        return f"h{number}"

    async def rpc_request(self, method, _params):
        if method == "state_getStorageHash":
            return {"result": self.code}
        return {"result": {"specVersion": 479}}

    async def init_runtime(self, **_kwargs):
        return SimpleNamespace(metadata=self.meta)


@pytest.fixture
def pins(monkeypatch: pytest.MonkeyPatch):
    meta = combined()
    source = runtime_interface(meta, SOURCE_EMISSION_SURFACE)
    treasury = runtime_interface(meta, TREASURY_RECEIPT_SURFACE)
    monkeypatch.setattr(
        audited_runtime_interfaces,
        "AUDITED_RUNTIME_INTERFACES",
        {source.digest: ("vTest", source)},
    )
    monkeypatch.setattr(
        audited_runtime_interfaces,
        "TREASURY_RECEIPT_INTERFACES",
        {treasury.digest: ("vTest", treasury)},
    )
    monkeypatch.setattr(
        audited_runtime_interfaces,
        "TREASURY_CALL_BASELINE",
        ("vTest", runtime_call_digests(meta)),
    )
    return meta


async def test_unchanged_upgrade_is_admitted_on_both_surfaces(pins) -> None:
    report = await read_runtime_interface_report(Chain(UPGRADE, pins))
    assert (report.block, report.block_hash, report.spec_version) == (500, "head", 479)
    assert [(v.surface, v.admitted_by, v.changed_entries) for v in report.surfaces] == [
        ("source_emission", "interface", ()),
        ("treasury_receipt", "interface", ()),
    ]
    assert report.treasury_signer.signs_on_this_runtime is False
    assert report.treasury_signer.changed_calls == ()


async def test_moved_entries_and_new_calls_are_named(pins) -> None:
    changed = clone(pins)
    entry(changed, "SubtensorModule", "Uids").update(default="\x01")
    type_def(changed, CALLS)["def"]["variant"]["variants"].append(
        {"name": "set_new_knob", "index": 77, "fields": [], "docs": []}
    )
    report = await read_runtime_interface_report(Chain(UPGRADE, changed), block=120)
    assert report.block_hash == "h120"
    source, treasury = report.surfaces
    assert source.admitted_by == "interface"
    assert treasury.admitted_by is None
    assert treasury.changed_entries == ("storage:SubtensorModule.Uids",)
    assert report.treasury_signer.changed_calls == ("SubtensorModule.set_new_knob",)


async def test_signed_artifact_is_reported_as_signable(pins) -> None:
    report = await read_runtime_interface_report(
        Chain(AUDITED_COLLECTOR_CODE_HASH, pins)
    )
    assert report.treasury_signer.signs_on_this_runtime is True
    assert report.surfaces[1].admitted_by == "audited_artifact"


async def test_unreadable_surface_reports_the_error_not_a_verdict(pins) -> None:
    broken = clone(pins)
    pallet = next(p for p in broken[1]["V15"]["pallets"] if p["name"] == "Proxy")
    pallet["calls"] = None
    report = await read_runtime_interface_report(Chain(UPGRADE, broken))
    treasury = report.surfaces[1]
    assert treasury.admitted_by is None
    assert "call:Proxy.proxy" in (treasury.error or "")


async def test_unreadable_calls_still_return_each_surface_verdict(pins) -> None:
    broken = clone(pins)
    proxy = next(p for p in broken[1]["V15"]["pallets"] if p["name"] == "Proxy")
    proxy["calls"] = {"ty": 0}  # a primitive, not a call enum
    report = await read_runtime_interface_report(Chain(UPGRADE, broken))
    source, treasury = report.surfaces
    assert source.admitted_by == "interface"
    assert treasury.admitted_by is None and treasury.error
    signer = report.treasury_signer
    assert signer.calls_error and signer.changed_calls == ()


@pytest.mark.parametrize("block", [0, 501])
async def test_unfinalized_or_invalid_block_refuses(pins, block) -> None:
    with pytest.raises(BlockNotFinalized, match="not finalized"):
        await read_runtime_interface_report(Chain(UPGRADE, pins), block=block)
