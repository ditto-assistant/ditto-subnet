"""Synthetic V15 runtime metadata carrying every entry of a consumed surface."""

from __future__ import annotations

import copy
from typing import Any

from ditto.chain.runtime_interface import SOURCE_EMISSION_SURFACE, RuntimeSurface

U16, BOOL, VECTOR, PAIR, EVENTS, CALLS, SYSTEM_EVENTS, TREE, RUNTIME_CALL = range(9)
# Further pallets' event and call enums are allocated from here.
_DYNAMIC = 9


def _variants(names: list[str], field_type: int = U16) -> dict[str, Any]:
    return {
        "variant": {
            "variants": [
                {
                    "name": name,
                    "index": index,
                    "fields": [
                        {
                            "name": "netuid",
                            "type": field_type,
                            "typeName": "NetUid",
                            "docs": [],
                        }
                    ],
                    "docs": [f"{name} docs"],
                }
                for index, name in enumerate(names)
            ]
        }
    }


def metadata(
    *,
    epoch_consensus: bool = True,
    surface: RuntimeSurface = SOURCE_EMISSION_SURFACE,
) -> tuple[str, dict[str, Any]]:
    """Return a decoded ``MetadataVersioned`` value as the library exposes it."""

    def names(entries: tuple[tuple[str, str], ...], pallet: str) -> list[str]:
        return [n for p, n in entries if p == pallet]

    pallet_names = sorted(
        {p for p, _ in (*surface.storage, *surface.events, *surface.calls)}
        | {"SubtensorModule", "System"}
    )
    types: list[tuple[int, dict[str, Any], list[str]]] = [
        (U16, {"primitive": "u16"}, []),
        (BOOL, {"primitive": "bool"}, []),
        (VECTOR, {"sequence": {"type": PAIR}}, []),
        (PAIR, {"tuple": [U16, U16]}, []),
        (
            EVENTS,
            _variants([*names(surface.events, "SubtensorModule"), "Unrelated"]),
            [],
        ),
        (CALLS, _variants(names(surface.calls, "SubtensorModule")), []),
        (SYSTEM_EVENTS, _variants(names(surface.events, "System")), []),
        # Recursive: a tree whose children are trees.
        (TREE, {"composite": {"fields": [{"name": "kids", "type": VECTOR}]}}, []),
        # The outer call enum; any pallet's change moves it.
        (
            RUNTIME_CALL,
            _variants(pallet_names),
            ["node_subtensor_runtime", "RuntimeCall"],
        ),
    ]
    holders: dict[str, dict[str, Any]] = {
        "SubtensorModule": {"event": {"ty": EVENTS}, "calls": {"ty": CALLS}},
        "System": {"event": {"ty": SYSTEM_EVENTS}, "calls": None},
    }
    next_id = _DYNAMIC
    for pallet in pallet_names:
        holder = holders.setdefault(pallet, {"event": None, "calls": None})
        for field, entries in (("event", surface.events), ("calls", surface.calls)):
            if holder.get(field) is not None or not names(entries, pallet):
                continue
            # A proxy-style call carries the opaque outer enum as an argument.
            types.append((next_id, _variants(names(entries, pallet), RUNTIME_CALL), []))
            holder[field] = {"ty": next_id}
            next_id += 1
    registry = [
        {
            "id": type_id,
            "type": {
                "path": path or [f"t{type_id}"],
                "params": [],
                "def": definition,
                "docs": [],
            },
        }
        for type_id, definition, path in types
    ]
    storage: dict[str, list[dict[str, Any]]] = {}
    for pallet, name in surface.storage:
        if name == "SubnetEpochConsensus" and not epoch_consensus:
            continue
        storage.setdefault(pallet, []).append(
            {
                "name": name,
                "modifier": "Default",
                "type": {"Map": {"hashers": ["Identity"], "key": U16, "value": VECTOR}},
                "default": "\x00",
                "documentation": [f"{name} docs"],
            }
        )
    pallets = [
        {
            "name": pallet,
            "storage": (
                {"prefix": pallet, "entries": storage[pallet]}
                if pallet in storage
                else None
            ),
            **holders[pallet],
        }
        for pallet in pallet_names
    ]
    return ("0x6d657461", {"V15": {"types": {"types": registry}, "pallets": pallets}})


def body(value: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    return value[1]["V15"]


def clone(value: tuple[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    return copy.deepcopy(value)


def entry(value: tuple[str, dict[str, Any]], pallet: str, name: str) -> dict[str, Any]:
    for item in next(p for p in body(value)["pallets"] if p["name"] == pallet)[
        "storage"
    ]["entries"]:
        if item["name"] == name:
            return item
    raise KeyError(name)


def type_def(value: tuple[str, dict[str, Any]], type_id: int) -> dict[str, Any]:
    return next(t for t in body(value)["types"]["types"] if t["id"] == type_id)["type"]


def unaudited_treasury_runtime() -> Any:
    """A runtime whose treasury surface matches no pinned audited interface."""
    from types import SimpleNamespace

    from ditto.chain.runtime_interface import TREASURY_RECEIPT_SURFACE

    return SimpleNamespace(metadata=metadata(surface=TREASURY_RECEIPT_SURFACE))
