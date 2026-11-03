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

from typing import Any

from ditto.chain.errors import ChainConnectionError
from ditto_screening_protocol.runtime_interface import (
    RuntimeInterface as RuntimeInterface,
)
from ditto_screening_protocol.runtime_interface import (
    RuntimeSurface as RuntimeSurface,
)
from ditto_screening_protocol.runtime_interface import (
    nearest_audited_change as nearest_audited_change,
)
from ditto_screening_protocol.runtime_interface import (
    runtime_call_digests as runtime_call_digests,
)
from ditto_screening_protocol.runtime_interface import (
    runtime_interface as _shared_runtime_interface,
)

# Storage the verifier (source_emission_verifier), its catch-up range planners
# and the payout receipt reader (ChainClient.get_miner_emission_receipt) decode.
# ``SubnetEpochConsensus`` is absent before v475; its absence is part of the
# fingerprint.
CONSUMED_STORAGE: tuple[tuple[str, str], ...] = (
    ("SubtensorModule", "Active"),
    ("SubtensorModule", "BlockAtRegistration"),
    ("SubtensorModule", "CommitRevealWeightsEnabled"),
    ("SubtensorModule", "Keys"),
    ("SubtensorModule", "LastHotkeySwapOnNetuid"),
    ("SubtensorModule", "LastMechansimStepBlock"),
    ("SubtensorModule", "LastUpdate"),
    ("SubtensorModule", "MechanismCountCurrent"),
    ("SubtensorModule", "OwnedHotkeys"),
    ("SubtensorModule", "OwnerHyperparamRateLimit"),
    ("SubtensorModule", "RevealPeriodEpochs"),
    ("SubtensorModule", "StakeWeight"),
    ("SubtensorModule", "SubnetEpochConsensus"),
    ("SubtensorModule", "SubnetEpochIndex"),
    ("SubtensorModule", "SubnetOwner"),
    ("SubtensorModule", "SubnetOwnerHotkey"),
    ("SubtensorModule", "Tempo"),
    ("SubtensorModule", "TimelockedWeightCommits"),
    ("SubtensorModule", "ValidatorPermit"),
    ("SubtensorModule", "WeightCommits"),
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

CONSUMED_CALLS: tuple[tuple[str, str], ...] = (
    ("SubtensorModule", "commit_timelocked_mechanism_weights"),
    ("SubtensorModule", "commit_timelocked_weights"),
)

SOURCE_EMISSION_SURFACE = RuntimeSurface(
    "source_emission", CONSUMED_STORAGE, CONSUMED_EVENTS, CONSUMED_CALLS
)

# Platform's read-only treasury receipt reader (chain/treasury_receipts.py)
# and the shared decoders it calls (ditto_screening_protocol.collector_receipts
# and treasury_identity). This admits receipt *reading* only; custody signs on
# a runtime only when its own COLLECTOR_SIGNER_SURFACE matches the runtime its
# cold-signed policy names.
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


def runtime_interface(
    metadata: Any, surface: RuntimeSurface = SOURCE_EMISSION_SURFACE
) -> RuntimeInterface:
    """Fingerprint ``surface``, source attribution by default."""
    return _shared_runtime_interface(metadata, surface)


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
