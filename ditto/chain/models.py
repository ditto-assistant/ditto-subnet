"""Frozen dataclass models for the chain access layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChainConfig:
    """Configuration for the chain access client.

    Holds Pylon connection parameters plus the subtensor network identifier
    used for events that Pylon does not surface (e.g. ExtrinsicSuccess /
    ExtrinsicFailed). Loaded from a service's argparse + env config.
    """

    pylon_url: str
    """HTTP URL for the Pylon container (e.g. ``http://pylon:8080``)."""

    identity_name: str
    """Pylon identity name used to authenticate this client."""

    identity_token: str
    """Pylon identity token paired with ``identity_name``."""

    netuid: int
    """Bittensor subnet netuid this client operates against (Ditto is 118)."""

    subtensor_network: str = "finney"
    """Subtensor network identifier passed to async-substrate-interface.

    Used only for event reads (the Pylon gap). Mainnet = ``finney``,
    testnet = ``test``, local = ``local``. Any other value is treated as a
    full WebSocket URL.
    """

    archive_blocks_cutoff: int = 300
    """Number of recent blocks served from the live node; older blocks go to the archive.

    Mirrors Pylon's default. Reads for blocks older than ``current - cutoff``
    automatically fall back to an archive node inside Pylon.
    """


@dataclass(frozen=True)
class NeuronInfo:
    """A neuron registered on the subnet at a point in time."""

    hotkey: str
    """SS58-encoded hotkey address."""

    coldkey: str
    """SS58-encoded coldkey address that owns the hotkey."""

    uid: int
    """Subnet-local UID assigned at registration."""

    stake: float
    """Alpha stake on this neuron's hotkey, in TAO units."""

    axon_info: dict[str, Any] = field(default_factory=dict)
    """Raw axon metadata as returned by Pylon (ip, port, version)."""

    registered_at_block: int = 0
    """Block number at which this hotkey was registered on the subnet."""

    @classmethod
    def from_pylon(cls, raw: Any) -> NeuronInfo:
        """Build a :class:`NeuronInfo` from a Pylon-shaped neuron object.

        Tolerant of missing fields and ``None`` values — defaults are applied
        so partial responses still produce a well-formed dataclass.
        """
        return cls(
            hotkey=str(getattr(raw, "hotkey", "") or ""),
            coldkey=str(getattr(raw, "coldkey", "") or ""),
            uid=int(getattr(raw, "uid", 0) or 0),
            stake=float(getattr(raw, "stake", 0.0) or 0.0),
            axon_info=dict(getattr(raw, "axon_info", {}) or {}),
            registered_at_block=int(getattr(raw, "registered_at_block", 0) or 0),
        )


@dataclass(frozen=True)
class ExtrinsicInfo:
    """A single extrinsic at a known ``(block_number, extrinsic_index)``.

    ``succeeded`` is ``None`` when the extrinsic has been fetched but its
    success status has not been resolved yet. ``ChainClient.get_extrinsic``
    populates ``succeeded`` automatically by reading ``system.Events`` at the
    matching block, so callers do not need to make two calls.
    """

    block_number: int
    """Block number containing the extrinsic."""

    extrinsic_index: int
    """Zero-based index of the extrinsic within the block."""

    call_module: str
    """Pallet name the call targets (e.g. ``Balances``)."""

    call_function: str
    """Call function within the pallet (e.g. ``transfer_keep_alive``)."""

    call_args: dict[str, Any] = field(default_factory=dict)
    """Decoded call arguments as returned by Pylon."""

    signer_address: str = ""
    """SS58-encoded address of the signer."""

    succeeded: bool | None = None
    """Whether ``system.ExtrinsicSuccess`` was emitted for this extrinsic.

    ``None`` if the success check has not been run or could not be resolved,
    ``True`` on ``ExtrinsicSuccess``, ``False`` on ``ExtrinsicFailed``.
    """

    @classmethod
    def from_pylon(
        cls,
        raw: Any,
        block_number: int,
        extrinsic_index: int,
        succeeded: bool | None = None,
    ) -> ExtrinsicInfo:
        """Build an :class:`ExtrinsicInfo` from a Pylon-shaped extrinsic object.

        Args:
            raw: Pylon response object exposing ``call.call_module``,
                ``call.call_function``, ``call.call_args``, and ``address``.
            block_number: Block the extrinsic was found in (Pylon's response
                does not include it; caller passes the value used in the lookup).
            extrinsic_index: Index within the block.
            succeeded: Pre-resolved success status from ``check_extrinsic_success``.
        """
        call = getattr(raw, "call", raw)
        return cls(
            block_number=block_number,
            extrinsic_index=extrinsic_index,
            call_module=str(getattr(call, "call_module", "") or ""),
            call_function=str(getattr(call, "call_function", "") or ""),
            call_args=dict(getattr(call, "call_args", {}) or {}),
            signer_address=str(getattr(raw, "address", "") or ""),
            succeeded=succeeded,
        )


@dataclass(frozen=True)
class BlockInfo:
    """A block on the chain identified by number, hash, and timestamp."""

    number: int
    """Block number."""

    hash: str
    """Block hash as a hex string (with or without the ``0x`` prefix per Pylon)."""

    timestamp: int = 0
    """Unix timestamp in seconds at which the block was produced (best-effort)."""

    @classmethod
    def from_pylon(cls, raw: Any) -> BlockInfo:
        """Build a :class:`BlockInfo` from a Pylon-shaped block object."""
        return cls(
            number=int(getattr(raw, "number", 0) or 0),
            hash=str(getattr(raw, "hash", "") or ""),
            timestamp=int(getattr(raw, "timestamp", 0) or 0),
        )
