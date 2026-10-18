"""Runtime upgrades admitted by consumed interface, not by WASM artifact."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ditto.chain import audited_runtime_interfaces, source_emission_verifier
from ditto.chain.errors import ChainConnectionError
from ditto.chain.runtime_interface import (
    require_audited_interface,
    runtime_interface,
)
from ditto.chain.source_emission_verifier import (
    AUDITED_RUNTIME_CODE_HASHES,
    admit_runtime,
)
from ditto.tests.chain.runtime_metadata import (
    EVENTS,
    TREE,
    U16,
    body,
    clone,
    entry,
    metadata,
    type_def,
)

UPGRADE = "0x" + "ab" * 32
# Captured before the autouse fixture swaps in the synthetic pin.
PINNED = audited_runtime_interfaces.AUDITED_RUNTIME_INTERFACES


@pytest.fixture(autouse=True)
def audited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the synthetic interface as the only audited one, for every test."""
    interface = runtime_interface(metadata())
    monkeypatch.setattr(
        audited_runtime_interfaces,
        "AUDITED_RUNTIME_INTERFACES",
        {interface.digest: ("vTest", interface)},
    )
    monkeypatch.setattr(source_emission_verifier, "_INTERFACE_ADMISSIONS", {})


def test_cosmetic_and_unrelated_changes_keep_the_fingerprint() -> None:
    base = runtime_interface(metadata())
    changed = clone(metadata())
    for row in body(changed)["types"]["types"]:
        row["type"]["docs"] = ["reworded"]
    entry(changed, "SubtensorModule", "Keys")["documentation"] = ["reworded"]
    type_def(changed, EVENTS)["def"]["variant"]["variants"].append(
        {"name": "BrandNewEvent", "index": 99, "fields": [], "docs": []}
    )
    body(changed)["pallets"].append({"name": "NewPallet", "storage": None})
    assert runtime_interface(changed).digest == base.digest


@pytest.mark.parametrize(
    ("mutate", "moved"),
    [
        (
            lambda m: type_def(m, EVENTS)["def"]["variant"]["variants"][0][
                "fields"
            ].append({"name": "extra", "type": U16}),
            "event:SubtensorModule.IncentiveAlphaEmittedToMiners",
        ),
        (
            lambda m: entry(m, "SubtensorModule", "Weights").update(default="\x01"),
            "storage:SubtensorModule.Weights",
        ),
        (
            lambda m: entry(m, "SubtensorModule", "Keys")["type"]["Map"].update(
                hashers=["Blake2_128Concat"]
            ),
            "storage:SubtensorModule.Keys",
        ),
    ],
    ids=["event-field", "storage-default", "storage-hasher"],
)
def test_consumed_change_fails_closed_and_names_the_entry(mutate, moved: str) -> None:
    changed = clone(metadata())
    mutate(changed)
    interface = runtime_interface(changed)
    with pytest.raises(ValueError, match=f"changed since vTest: {moved}$"):
        require_audited_interface(interface)


def test_missing_consumed_entry_is_unreadable_not_degenerate() -> None:
    changed = clone(metadata())
    pallet = next(p for p in body(changed)["pallets"] if p["name"] == "SubtensorModule")
    pallet["storage"]["entries"] = [
        e for e in pallet["storage"]["entries"] if e["name"] != "StakeWeight"
    ]
    with pytest.raises(ValueError, match="lacks consumed interface"):
        runtime_interface(changed)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: entry(m, "SubtensorModule", "Keys")["type"]["Map"].pop("key"),
        lambda m: entry(m, "SubtensorModule", "Keys")["type"].update(Map=7),
        lambda m: type_def(m, EVENTS)["def"]["variant"]["variants"][0]["fields"][0].pop(
            "type"
        ),
        lambda m: type_def(m, TREE)["def"].update(array={"len": 2}),
    ],
    ids=["map-without-key", "map-not-a-dict", "field-without-type", "array-no-type"],
)
def test_malformed_metadata_fails_closed_as_value_error(mutate) -> None:
    changed = clone(metadata())
    type_def(changed, TREE)["def"] = {"array": {"len": 2, "type": U16}}
    entry(changed, "SubtensorModule", "Weights")["type"]["Map"]["value"] = TREE
    mutate(changed)
    with pytest.raises(ValueError):
        runtime_interface(changed)


def test_unboundedly_deep_types_fail_closed_as_value_error() -> None:
    """An acyclic chain deeper than the interpreter stack is unreadable, not a crash."""
    changed = clone(metadata())
    rows = body(changed)["types"]["types"]
    depth, start = 5000, 1000
    rows += [
        {
            "id": start + i,
            "type": {
                "path": [],
                "params": [],
                "def": (
                    {"sequence": {"type": start + i + 1}}
                    if i < depth
                    else {"primitive": "u8"}
                ),
                "docs": [],
            },
        }
        for i in range(depth + 1)
    ]
    entry(changed, "SubtensorModule", "Weights")["type"]["Map"]["value"] = start
    with pytest.raises(ValueError, match="RecursionError"):
        runtime_interface(changed)


def test_epoch_consensus_is_optional_and_reported() -> None:
    assert runtime_interface(metadata()).has_epoch_consensus is True
    legacy = runtime_interface(metadata(epoch_consensus=False))
    assert legacy.has_epoch_consensus is False
    assert legacy.digest != runtime_interface(metadata()).digest


def test_recursive_types_terminate() -> None:
    changed = clone(metadata())
    type_def(changed, TREE)["def"]["composite"]["fields"][0]["type"] = TREE
    entry(changed, "SubtensorModule", "Weights")["type"]["Map"]["value"] = TREE
    assert runtime_interface(changed).digest


@pytest.mark.parametrize(
    "value", [None, {}, ("0x6d657461", {"V13": {}}), ("0x6d657461", {"V15": []})]
)
def test_unknown_metadata_envelope_fails_closed(value) -> None:
    with pytest.raises(ValueError):
        runtime_interface(value)


@pytest.mark.parametrize(
    "pinned",
    [PINNED, audited_runtime_interfaces.TREASURY_RECEIPT_INTERFACES],
    ids=["source_emission", "treasury_receipt"],
)
def test_pinned_audited_interfaces_are_self_consistent(pinned) -> None:
    assert {release for release, _ in pinned.values()} == {"v473", "v478"}
    for digest, (_, interface) in pinned.items():
        assert interface.digest == digest
        assert not interface.changed_entries(interface)
    # v478 differs from v473 only by the epoch-consensus map added in v475.
    v473, v478 = (i for _, i in sorted(pinned.values(), key=lambda item: item[0]))
    assert v473.changed_entries(v478) == [
        "storage:SubtensorModule.SubnetEpochConsensus"
    ]
    assert (v473.has_epoch_consensus, v478.has_epoch_consensus) == (False, True)


def test_treasury_call_baseline_names_the_signed_runtime() -> None:
    release, calls = audited_runtime_interfaces.TREASURY_CALL_BASELINE
    assert release == "v478"
    assert {"Proxy.proxy", "SubtensorModule.transfer_stake"} <= set(calls)
    assert all(len(digest) == 16 for digest in calls.values())


async def test_upgrade_with_audited_interface_is_admitted_once() -> None:
    substrate = AsyncMock()
    substrate.get_block_metadata.return_value = metadata()
    first = await admit_runtime(substrate, code_hash=UPGRADE, at="h1")
    second = await admit_runtime(substrate, code_hash=UPGRADE, at="h2")
    assert first == second
    assert first.epoch_consensus_gated is True
    assert first.interface_digest == runtime_interface(metadata()).digest
    substrate.get_block_metadata.assert_awaited_once()
    assert substrate.get_block_metadata.await_args.kwargs["block_hash"] == "h1"
    substrate.init_runtime.assert_not_awaited()


async def test_upgrade_block_is_fingerprinted_on_its_own_runtime() -> None:
    """At an upgrade block the library decodes with the parent's runtime.

    Fingerprinting that would admit the new code on the old, audited
    interface; admission must read the exact state's metadata instead.
    """
    changed = clone(metadata())
    entry(changed, "SubtensorModule", "Weights").update(default="\x01")
    substrate = AsyncMock()
    substrate.init_runtime.return_value = SimpleNamespace(metadata=metadata())
    substrate.get_block_metadata.return_value = changed
    with pytest.raises(ValueError, match="storage:SubtensorModule.Weights"):
        await admit_runtime(substrate, code_hash=UPGRADE, at="upgrade-block")
    substrate.init_runtime.assert_not_awaited()


async def test_audited_artifact_needs_no_metadata() -> None:
    substrate = AsyncMock()
    code = sorted(AUDITED_RUNTIME_CODE_HASHES)[0]
    admission = await admit_runtime(substrate, code_hash=code, at="h1")
    assert admission.interface_digest is None
    substrate.get_block_metadata.assert_not_awaited()


async def test_changed_interface_is_refused_and_not_cached() -> None:
    changed = clone(metadata())
    entry(changed, "SubtensorModule", "Active").update(modifier="Optional")
    substrate = AsyncMock()
    substrate.get_block_metadata.return_value = changed
    for _ in range(2):
        with pytest.raises(
            ValueError,
            match=f"not audited: {UPGRADE} .*storage:SubtensorModule.Active",
        ):
            await admit_runtime(substrate, code_hash=UPGRADE, at="h1")
    assert substrate.get_block_metadata.await_count == 2


async def test_unavailable_metadata_retries_as_a_connection_fault() -> None:
    substrate = AsyncMock()
    substrate.get_block_metadata.side_effect = OSError("archive dropped")
    with pytest.raises(ChainConnectionError):
        await admit_runtime(substrate, code_hash=UPGRADE, at="h1")
    assert source_emission_verifier._INTERFACE_ADMISSIONS == {}


@pytest.mark.parametrize("code_hash", [None, "", "abc", 7])
async def test_malformed_fingerprint_is_refused(code_hash) -> None:
    with pytest.raises(ValueError, match="not audited"):
        await admit_runtime(AsyncMock(), code_hash=code_hash, at="h1")
