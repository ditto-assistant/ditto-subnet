"""Fingerprint the slice of a Subtensor runtime that a chain reader decodes.

Pure functions over decoded runtime metadata, shared by Platform's readers and
the Gamma custody signer so both hash a runtime the same way. What a reader
depends on is narrower than the WASM: the SCALE shapes of the storage items,
events and calls it decodes. A new runtime whose consumed surface is identical
to an audited runtime's can be admitted without a per-release artifact audit;
any change to that surface fails closed and names the entries that moved.

Docs and type names are cosmetic and excluded. Storage defaults and modifiers
are included because they change what an absent value decodes to. The outer
``RuntimeCall``/``RuntimeEvent`` enums are hashed by path only: they embed
every pallet, so hashing them whole would move on any unrelated upgrade.
``runtime_call_digests`` reports per-pallet call changes for an operator.

Fetching metadata stays with each caller: it must read ``state_getMetadata``
at the exact block hash, never the SDK's parent-runtime metadata.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeSurface:
    """The storage items, event variants and calls one reader decodes.

    The optional fields default to empty so existing surfaces keep their pinned
    digests. A signer also depends on error and pallet indices it matches by
    number, constant types it reads, and the signed extensions it encodes.
    """

    name: str
    storage: tuple[tuple[str, str], ...]
    events: tuple[tuple[str, str], ...]
    calls: tuple[tuple[str, str], ...]
    errors: tuple[tuple[str, str], ...] = ()
    constants: tuple[tuple[str, str], ...] = ()
    pallet_indices: tuple[str, ...] = ()
    signed_extensions: bool = False


# ``SubnetEpochConsensus`` is absent before v475; its absence is part of the
# fingerprint rather than a missing dependency.
EPOCH_CONSENSUS_ENTRY = "storage:SubtensorModule.SubnetEpochConsensus"
_EPOCH = EPOCH_CONSENSUS_ENTRY

# Outer enums aggregating every pallet; hashed by path, not structure.
_OPAQUE_TYPES = frozenset({"RuntimeCall", "RuntimeEvent"})


@dataclass(frozen=True)
class RuntimeInterface:
    """Canonical consumed surface of one runtime, and its digest."""

    digest: str
    entries: dict[str, str]
    has_epoch_consensus: bool

    def changed_entries(self, other: RuntimeInterface) -> list[str]:
        return sorted(
            name
            for name in self.entries.keys() | other.entries.keys()
            if self.entries.get(name) != other.entries.get(name)
        )


def _metadata_body(metadata: Any) -> dict[str, Any]:
    """Return the V14/V15 body of a decoded ``MetadataVersioned`` value."""
    value = getattr(metadata, "value", metadata)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        value = value[1]
    if not isinstance(value, dict) or len(value) != 1:
        raise ValueError("unsupported runtime metadata envelope")
    version, body = next(iter(value.items()))
    if version not in ("V14", "V15") or not isinstance(body, dict):
        raise ValueError(f"unsupported runtime metadata version: {version}")
    return body


class _Types:
    def __init__(self, registry: Any) -> None:
        rows = registry.get("types") if isinstance(registry, dict) else None
        if not isinstance(rows, list):
            raise ValueError("runtime metadata has no type registry")
        self._by_id: dict[int, dict[str, Any]] = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or type(row.get("id")) is not int
                or not isinstance(row.get("type"), dict)
            ):
                raise ValueError("invalid runtime type registry row")
            self._by_id[row["id"]] = row["type"]

    def get(self, type_id: Any) -> dict[str, Any]:
        if type(type_id) is not int or type_id not in self._by_id:
            raise ValueError(f"unknown runtime type id: {type_id!r}")
        return self._by_id[type_id]

    def canonical(self, type_id: Any, stack: tuple[int, ...] = ()) -> Any:
        """Structure of ``type_id`` with ids replaced by what they mean."""
        node = self.get(type_id)
        path = list(node.get("path") or [])
        if path and path[-1] in _OPAQUE_TYPES:
            return {"opaque": path}
        if type_id in stack:
            # Recursive types: refer back by path rather than recurse forever.
            return {"recursive": path}
        inner = (*stack, type_id)
        return {
            "path": path,
            "params": [
                {
                    "name": param.get("name"),
                    "type": (
                        None
                        if param.get("type") is None
                        else self.canonical(param["type"], inner)
                    ),
                }
                for param in node.get("params") or []
            ],
            "def": self._definition(node.get("def"), inner),
        }

    def _fields(self, fields: Any, stack: tuple[int, ...]) -> list[Any]:
        if not isinstance(fields, list):
            raise ValueError("invalid runtime type fields")
        return [
            {"name": field.get("name"), "type": self.canonical(field["type"], stack)}
            for field in fields
        ]

    def variant(self, variant: dict[str, Any], stack: tuple[int, ...]) -> Any:
        return {
            "name": variant.get("name"),
            "index": variant.get("index"),
            "fields": self._fields(variant.get("fields") or [], stack),
        }

    def variants(self, type_id: Any) -> dict[str, dict[str, Any]]:
        definition = self.get(type_id).get("def")
        variants = (
            definition.get("variant", {}).get("variants")
            if isinstance(definition, dict)
            else None
        )
        if not isinstance(variants, list):
            raise ValueError("pallet call/event type is not an enum")
        return {item["name"]: item for item in variants if isinstance(item, dict)}

    def _definition(self, definition: Any, stack: tuple[int, ...]) -> Any:
        if not isinstance(definition, dict) or len(definition) != 1:
            raise ValueError("invalid runtime type definition")
        kind, body = next(iter(definition.items()))
        if kind == "composite":
            return {kind: self._fields(body.get("fields") or [], stack)}
        if kind == "variant":
            return {
                kind: [self.variant(item, stack) for item in body.get("variants") or []]
            }
        if kind in ("sequence", "compact"):
            return {kind: self.canonical(body["type"], stack)}
        if kind == "array":
            return {
                kind: {"len": body["len"], "type": self.canonical(body["type"], stack)}
            }
        if kind == "tuple":
            return {kind: [self.canonical(item, stack) for item in body]}
        if kind == "bitsequence":
            return {
                kind: {
                    "store": self.canonical(body["bit_store_type"], stack),
                    "order": self.canonical(body["bit_order_type"], stack),
                }
            }
        if kind == "primitive":
            return {kind: body}
        raise ValueError(f"unsupported runtime type kind: {kind}")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# What malformed metadata raises below the explicit shape checks.
_MALFORMED = (KeyError, IndexError, TypeError, AttributeError, RecursionError)


def _pallets(body: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    return {
        pallet.get("name"): pallet
        for pallet in body.get("pallets") or []
        if isinstance(pallet, dict)
    }


def runtime_interface(metadata: Any, surface: RuntimeSurface) -> RuntimeInterface:
    """Fingerprint ``surface`` in decoded runtime metadata.

    Raises ``ValueError`` on any metadata shape it does not understand, so an
    unreadable runtime fails closed exactly like an unaudited one.
    """
    try:
        return _runtime_interface(metadata, surface)
    except _MALFORMED as error:
        # Malformed nodes deeper than the explicit checks still fail closed
        # through the one documented error type.
        raise ValueError(
            f"malformed runtime metadata: {type(error).__name__}"
        ) from error


def _runtime_interface(metadata: Any, surface: RuntimeSurface) -> RuntimeInterface:
    body = _metadata_body(metadata)
    types = _Types(body.get("types"))
    pallets = _pallets(body)
    entries: dict[str, Any] = {}
    for pallet_name, item_name in surface.storage:
        storage = (pallets.get(pallet_name) or {}).get("storage") or {}
        items = {
            item.get("name"): item
            for item in storage.get("entries") or []
            if isinstance(item, dict)
        }
        item = items.get(item_name)
        key = f"storage:{pallet_name}.{item_name}"
        if item is None:
            entries[key] = None
            continue
        shape = item.get("type")
        if not isinstance(shape, dict) or len(shape) != 1:
            raise ValueError(f"invalid storage type for {pallet_name}.{item_name}")
        kind, spec = next(iter(shape.items()))
        if kind == "Plain":
            resolved: Any = {"plain": types.canonical(spec)}
        elif kind == "Map":
            if not isinstance(spec, dict) or "key" not in spec or "value" not in spec:
                raise ValueError(
                    f"invalid map storage type for {pallet_name}.{item_name}"
                )
            resolved = {
                "map": {
                    "hashers": spec.get("hashers"),
                    "key": types.canonical(spec["key"]),
                    "value": types.canonical(spec["value"]),
                }
            }
        else:
            raise ValueError(f"unsupported storage kind for {pallet_name}.{item_name}")
        entries[key] = {
            "modifier": item.get("modifier"),
            "type": resolved,
            "default": item.get("default"),
        }
    for label, consumed, field in (
        ("event", surface.events, "event"),
        ("call", surface.calls, "calls"),
        ("error", surface.errors, "error"),
    ):
        for pallet_name, variant_name in consumed:
            key = f"{label}:{pallet_name}.{variant_name}"
            holder = (pallets.get(pallet_name) or {}).get(field)
            if not isinstance(holder, dict) or "ty" not in holder:
                entries[key] = None
                continue
            variant = types.variants(holder["ty"]).get(variant_name)
            entries[key] = None if variant is None else types.variant(variant, ())
    for pallet_name, constant_name in surface.constants:
        constants = (pallets.get(pallet_name) or {}).get("constants") or []
        constant = next(
            (
                item
                for item in constants
                if isinstance(item, dict) and item.get("name") == constant_name
            ),
            None,
        )
        # The value is policy the caller rereads live; only its shape is pinned.
        entries[f"constant:{pallet_name}.{constant_name}"] = (
            None if constant is None else types.canonical(constant["type"])
        )
    for pallet_name in surface.pallet_indices:
        index = (pallets.get(pallet_name) or {}).get("index")
        entries[f"pallet:{pallet_name}"] = None if type(index) is not int else index
    if surface.signed_extensions:
        extensions = (body.get("extrinsic") or {}).get("signed_extensions")
        if not isinstance(extensions, list) or not extensions:
            raise ValueError("runtime metadata has no signed extensions")
        entries["extrinsic:signed_extensions"] = [
            {
                "identifier": item["identifier"],
                "type": types.canonical(item["ty"]),
                "additional_signed": types.canonical(item["additional_signed"]),
            }
            for item in extensions
        ]
    missing = sorted(
        name for name, value in entries.items() if value is None and name != _EPOCH
    )
    if missing:
        # Everything but the epoch-consensus map exists in every runtime since
        # v448. An absence is a parse failure or a removed dependency; either
        # way a degenerate fingerprint must not be compared.
        raise ValueError("runtime lacks consumed interface: " + ", ".join(missing))
    digests = {name: _digest(value) for name, value in sorted(entries.items())}
    return RuntimeInterface(
        digest=_digest(digests),
        entries=digests,
        has_epoch_consensus=entries.get(_EPOCH) is not None,
    )


def runtime_call_digests(metadata: Any) -> dict[str, str]:
    """Short digest of every ``Pallet.call`` variant, for reporting.

    Never an admission input: an operator reads it to see which calls an
    upgrade added, removed or reshaped behind the opaque outer ``RuntimeCall``
    (for example the calls a proxy may dispatch). 64-bit digests are ample for
    naming what changed between two runtimes. Raises ``ValueError`` on
    metadata it cannot read.
    """
    try:
        return _runtime_call_digests(metadata)
    except _MALFORMED as error:
        raise ValueError(
            f"malformed runtime metadata: {type(error).__name__}"
        ) from error


def _runtime_call_digests(metadata: Any) -> dict[str, str]:
    body = _metadata_body(metadata)
    types = _Types(body.get("types"))
    digests = {}
    for name, pallet in sorted(_pallets(body).items(), key=lambda item: str(item[0])):
        holder = pallet.get("calls")
        if not isinstance(name, str) or not isinstance(holder, dict):
            continue
        if "ty" not in holder:
            continue
        for call, variant in sorted(types.variants(holder["ty"]).items()):
            digests[f"{name}.{call}"] = _digest(types.variant(variant, ()))[:16]
    return digests


def nearest_audited_change(
    interface: RuntimeInterface,
    audited: dict[str, tuple[str, RuntimeInterface]],
) -> tuple[str, list[str]]:
    """The closest audited release and the entries that differ from it."""
    changed, release = min(
        (
            (interface.changed_entries(pinned), release)
            for release, pinned in audited.values()
        ),
        key=lambda item: len(item[0]),
    )
    return release, changed
