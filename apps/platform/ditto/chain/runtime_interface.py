"""Fingerprint the slice of a Subtensor runtime that a chain reader decodes.

The source-emission collector used to admit a runtime only when its WASM
``:code`` hash matched a hand-audited release artifact. Every Finney upgrade
then stalled source release until someone audited the new artifact, and an
untagged deploy (v472, v479) could not be audited at all.

What the collector actually depends on is narrower than the WASM: the SCALE
shapes of the storage items, events and calls it decodes. This module hashes
exactly that surface from the runtime's own metadata. A new runtime whose
consumed surface is identical to an audited runtime's is admitted
automatically; any change to that surface still fails closed and names the
entries that moved, so a human audits only upgrades that touch it.

Docs and type names are cosmetic and excluded. Storage defaults and modifiers
are included because they change what an absent value decodes to. The outer
``RuntimeCall``/``RuntimeEvent`` enums are hashed by path only: they embed
every pallet, so hashing them whole would move on any unrelated upgrade.
``runtime_call_digests`` reports per-pallet call changes for an operator.

Each reader declares its own ``RuntimeSurface``: source attribution and the
treasury receipt reader decode different items and are pinned separately.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ditto.chain.errors import ChainConnectionError


@dataclass(frozen=True)
class RuntimeSurface:
    """The storage items, event variants and calls one reader decodes."""

    name: str
    storage: tuple[tuple[str, str], ...]
    events: tuple[tuple[str, str], ...]
    calls: tuple[tuple[str, str], ...]


# Storage the verifier (source_emission_verifier) and the payout receipt reader
# (ChainClient.get_miner_emission_receipt) decode. ``SubnetEpochConsensus`` is
# absent before v475; its absence is part of the fingerprint.
CONSUMED_STORAGE: tuple[tuple[str, str], ...] = (
    ("SubtensorModule", "Active"),
    ("SubtensorModule", "CommitRevealWeightsEnabled"),
    ("SubtensorModule", "Keys"),
    ("SubtensorModule", "LastMechansimStepBlock"),
    ("SubtensorModule", "LastUpdate"),
    ("SubtensorModule", "MechanismCountCurrent"),
    ("SubtensorModule", "OwnedHotkeys"),
    ("SubtensorModule", "StakeWeight"),
    ("SubtensorModule", "SubnetEpochConsensus"),
    ("SubtensorModule", "SubnetEpochIndex"),
    ("SubtensorModule", "SubnetOwner"),
    ("SubtensorModule", "SubnetOwnerHotkey"),
    ("SubtensorModule", "TimelockedWeightCommits"),
    ("SubtensorModule", "ValidatorPermit"),
    ("SubtensorModule", "Weights"),
    ("Timestamp", "Now"),
)

# Event variants matched by name. ``System.Events`` itself is not hashed whole:
# it embeds every pallet's events, so any unrelated pallet change would move it.
CONSUMED_EVENTS: tuple[tuple[str, str], ...] = (
    ("SubtensorModule", "IncentiveAlphaEmittedToMiners"),
    ("SubtensorModule", "TimelockedWeightsCommitted"),
    ("SubtensorModule", "TimelockedWeightsRevealed"),
    ("SubtensorModule", "WeightsSet"),
    ("System", "CodeUpdated"),
    ("System", "ExtrinsicFailed"),
    ("System", "ExtrinsicSuccess"),
)

_EPOCH = "storage:SubtensorModule.SubnetEpochConsensus"

CONSUMED_CALLS: tuple[tuple[str, str], ...] = (
    ("SubtensorModule", "commit_timelocked_mechanism_weights"),
    ("SubtensorModule", "commit_timelocked_weights"),
)

SOURCE_EMISSION_SURFACE = RuntimeSurface(
    "source_emission", CONSUMED_STORAGE, CONSUMED_EVENTS, CONSUMED_CALLS
)

# Platform's read-only treasury receipt reader (chain/treasury_receipts.py)
# and the shared decoders it calls (ditto_screening_protocol.collector_receipts
# and treasury_identity). This admits receipt *reading* only; the validator
# signer still requires the runtime named in its cold-signed policy.
TREASURY_RECEIPT_SURFACE = RuntimeSurface(
    "treasury_receipt",
    storage=(
        ("SubtensorModule", "AutoStakeDestination"),
        ("SubtensorModule", "Keys"),
        ("SubtensorModule", "LastEpochBlock"),
        ("SubtensorModule", "Owner"),
        ("SubtensorModule", "SubnetEpochConsensus"),
        ("SubtensorModule", "SubnetEpochIndex"),
        ("SubtensorModule", "SubnetOwner"),
        ("SubtensorModule", "Uids"),
        ("SubtensorModule", "ValidatorPermit"),
        ("Timestamp", "Now"),
    ),
    events=(
        ("Balances", "Transfer"),
        ("Proxy", "ProxyExecuted"),
        ("SubtensorModule", "AutoStakeAdded"),
        ("SubtensorModule", "IncentiveAlphaEmittedToMiners"),
        ("SubtensorModule", "StakeAdded"),
        ("SubtensorModule", "StakeRemoved"),
        ("System", "ExtrinsicFailed"),
        ("System", "ExtrinsicSuccess"),
    ),
    calls=(
        ("Balances", "transfer_keep_alive"),
        ("Proxy", "proxy"),
        ("SubtensorModule", "transfer_stake"),
    ),
)

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


async def metadata_at(substrate: Any, block_hash: str) -> Any:
    """Metadata of the runtime whose ``:code`` is stored at ``block_hash``.

    Never ``init_runtime(block_hash=...)``: the library decodes a block with
    its *parent's* runtime, so at an upgrade block it returns the previous
    runtime's metadata. Fingerprinting that would admit the new code hash on
    the old interface. ``state_getMetadata`` at the hash is the exact runtime,
    decoded with a fresh configuration that shares no session cache.
    """
    from scalecodec.base import RuntimeConfigurationObject
    from scalecodec.type_registry import load_type_registry_preset

    config = RuntimeConfigurationObject()
    config.clear_type_registry()
    config.update_type_registry(load_type_registry_preset(name="core") or {})
    metadata = await substrate.get_block_metadata(
        block_hash=block_hash, runtime_config=config
    )
    if metadata is None:
        raise ChainConnectionError(f"no runtime metadata at {block_hash}")
    return metadata


# What malformed metadata raises below the explicit shape checks.
_MALFORMED = (KeyError, IndexError, TypeError, AttributeError, RecursionError)


def _pallets(body: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    return {
        pallet.get("name"): pallet
        for pallet in body.get("pallets") or []
        if isinstance(pallet, dict)
    }


def runtime_interface(
    metadata: Any, surface: RuntimeSurface = SOURCE_EMISSION_SURFACE
) -> RuntimeInterface:
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
    ):
        for pallet_name, variant_name in consumed:
            key = f"{label}:{pallet_name}.{variant_name}"
            holder = (pallets.get(pallet_name) or {}).get(field)
            if not isinstance(holder, dict) or "ty" not in holder:
                entries[key] = None
                continue
            variant = types.variants(holder["ty"]).get(variant_name)
            entries[key] = None if variant is None else types.variant(variant, ())
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


def require_audited_interface(
    interface: RuntimeInterface,
    audited: dict[str, tuple[str, RuntimeInterface]] | None = None,
) -> None:
    """Raise ``ValueError`` naming what moved unless the surface was audited.

    ``audited`` defaults to the source-emission pins.
    """
    if audited is None:
        from ditto.chain.audited_runtime_interfaces import (
            AUDITED_RUNTIME_INTERFACES,
        )

        audited = AUDITED_RUNTIME_INTERFACES
    if interface.digest in audited:
        return
    release, changed = nearest_audited_change(interface, audited)
    raise ValueError(f"interface changed since {release}: " + ", ".join(changed))
