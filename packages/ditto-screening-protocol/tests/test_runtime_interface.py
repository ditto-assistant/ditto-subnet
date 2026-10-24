"""Signer-surface fields fingerprint what custody matches by number or encodes."""

import copy

import pytest

from ditto_screening_protocol.runtime_interface import (
    RuntimeSurface,
    runtime_interface,
)

U64, ENUM_CALLS, ENUM_ERRORS, UNIT = range(4)


def metadata():
    def variants(*names):
        return {
            "variant": {
                "variants": [
                    {"name": name, "index": index, "fields": []}
                    for index, name in enumerate(names)
                ]
            }
        }

    types = [
        {"id": U64, "type": {"path": [], "params": [], "def": {"primitive": "u64"}}},
        {"id": ENUM_CALLS, "type": {"path": [], "def": variants("transfer_stake")}},
        {"id": ENUM_ERRORS, "type": {"path": [], "def": variants("X", "AmountTooLow")}},
        {"id": UNIT, "type": {"path": [], "def": {"tuple": []}}},
    ]
    return {
        "V14": {
            "types": {"types": types},
            "pallets": [
                {
                    "name": "SubtensorModule",
                    "index": 7,
                    "storage": {"entries": []},
                    "calls": {"ty": ENUM_CALLS},
                    "error": {"ty": ENUM_ERRORS},
                    "constants": [
                        {"name": "InitialMinTransfer", "type": U64, "value": "0x01"}
                    ],
                }
            ],
            "extrinsic": {
                "version": 4,
                "signed_extensions": [
                    {"identifier": "CheckNonce", "ty": U64, "additional_signed": UNIT}
                ],
            },
        }
    }


SURFACE = RuntimeSurface(
    "signer",
    storage=(),
    events=(),
    calls=(("SubtensorModule", "transfer_stake"),),
    errors=(("SubtensorModule", "AmountTooLow"),),
    constants=(("SubtensorModule", "InitialMinTransfer"),),
    pallet_indices=("SubtensorModule",),
    signed_extensions=True,
)


def test_signer_fields_are_entries_and_constant_values_are_not():
    base = runtime_interface(metadata(), SURFACE)
    assert set(base.entries) == {
        "call:SubtensorModule.transfer_stake",
        "error:SubtensorModule.AmountTooLow",
        "constant:SubtensorModule.InitialMinTransfer",
        "pallet:SubtensorModule",
        "extrinsic:signed_extensions",
    }
    changed = metadata()
    changed["V14"]["pallets"][0]["constants"][0]["value"] = "0x02"
    assert runtime_interface(changed, SURFACE).digest == base.digest


@pytest.mark.parametrize(
    "mutate,entry",
    [
        (lambda b: b["pallets"][0].update(index=8), "pallet:SubtensorModule"),
        (
            # The retry proof matches error 0x52 by number, not by name.
            lambda b: b["types"]["types"][ENUM_ERRORS]["type"]["def"]["variant"][
                "variants"
            ][1].update(index=82),
            "error:SubtensorModule.AmountTooLow",
        ),
        (
            lambda b: b["extrinsic"]["signed_extensions"].append(
                {"identifier": "New", "ty": UNIT, "additional_signed": UNIT}
            ),
            "extrinsic:signed_extensions",
        ),
        (
            lambda b: b["extrinsic"].update(version=5),
            "extrinsic:signed_extensions",
        ),
    ],
)
def test_signer_dependencies_move_the_digest(mutate, entry):
    base = runtime_interface(metadata(), SURFACE)
    changed = copy.deepcopy(metadata())
    mutate(changed["V14"])
    moved = runtime_interface(changed, SURFACE)
    assert moved.digest != base.digest
    assert moved.changed_entries(base) == [entry]


def test_removed_signer_dependency_fails_closed():
    changed = metadata()
    changed["V14"]["pallets"][0]["constants"] = []
    with pytest.raises(ValueError, match="InitialMinTransfer"):
        runtime_interface(changed, SURFACE)


def test_plain_surfaces_keep_their_entries():
    plain = RuntimeSurface("reader", storage=(), events=(), calls=SURFACE.calls)
    assert set(runtime_interface(metadata(), plain).entries) == {
        "call:SubtensorModule.transfer_stake"
    }


@pytest.mark.parametrize("version", [None, "4", True])
def test_malformed_extrinsic_version_fails_closed(version):
    changed = metadata()
    if version is None:
        del changed["V14"]["extrinsic"]["version"]
    else:
        changed["V14"]["extrinsic"]["version"] = version
    with pytest.raises(ValueError, match="extrinsic version"):
        runtime_interface(changed, SURFACE)
