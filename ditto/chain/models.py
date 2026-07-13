"""Frozen dataclass models + env builder for the chain access layer."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# Bittensor chains (finney, testnet, and the standard subtensor localnet
# image) all use the generic substrate SS58 prefix 42. Pylon sometimes
# returns addresses as raw hex pubkeys (``0x...``) on recent subtensor
# versions; we encode them back to SS58 so downstream string compares
# (signer == coldkey, dest == send_address) work uniformly.
_BITTENSOR_SS58_PREFIX = 42


def normalize_address_to_ss58(value: str) -> str:
    """Return ``value`` as an SS58 string.

    Pylon returns extrinsic signer / dest addresses in two shapes
    depending on the subtensor release:

    - Plain SS58 (``"5..."``): returned as-is.
    - ``0x``-prefixed 32-byte raw account ID: encoded to SS58 with the
      bittensor prefix.

    Empty / unknown shapes return an empty string so downstream
    equality checks fail cleanly rather than raising.

    Args:
        value: The raw address string from Pylon.

    Returns:
        SS58-encoded string, or empty on malformed input.
    """
    if not value:
        return ""
    if not value.startswith("0x"):
        return value
    try:
        from scalecodec.utils.ss58 import ss58_encode

        return ss58_encode(bytes.fromhex(value[2:]), ss58_format=_BITTENSOR_SS58_PREFIX)
    except Exception:
        return ""


@dataclass(frozen=True)
class ChainConfig:
    """Configuration for the chain access client.

    Holds Pylon connection parameters plus the subtensor network identifier
    used for events that Pylon does not surface (e.g. ExtrinsicSuccess /
    ExtrinsicFailed). Loaded from a service's argparse + env config.

    Auth modes:
    - **Open-access** (read-only): set ``open_access_token``. Sufficient for
      ``get_recent_neurons``, ``get_latest_block``, ``get_extrinsic``.
    - **Identity** (read + write): set both ``identity_name`` and
      ``identity_token``. Required for ``put_weights``.

    At least one mode must be configured; both can be set side by side.
    """

    pylon_url: str
    """HTTP URL for the Pylon container (e.g. ``http://pylon:8000``).

    Passed to Pylon SDK as ``AsyncConfig.address``.
    """

    netuid: int
    """Bittensor subnet netuid this client operates against (Ditto is 118).

    Pylon binds netuid at the service level rather than per call, so this
    is informational on the client side. Kept here so consumers have a
    single source for "what subnet are we on".
    """

    open_access_token: str | None = None
    """Token for Pylon's open-access read endpoints. Optional if identity is set."""

    identity_name: str | None = None
    """Pylon identity name. Required for write operations like ``put_weights``."""

    identity_token: str | None = None
    """Pylon identity token paired with ``identity_name``."""

    subtensor_network: str = "finney"
    """Subtensor network identifier passed to async-substrate-interface.

    Used only for event reads (the Pylon gap). Mainnet = ``finney``,
    testnet = ``test``, local = ``local``. Any other value is treated as a
    full WebSocket URL.
    """

    archive_blocks_cutoff: int = 300
    """Recent-block window served from the live node; older blocks go to archive.

    Mirrors Pylon's default. Reads for blocks older than ``current - cutoff``
    automatically fall back to an archive node inside Pylon.
    """

    def __post_init__(self) -> None:
        """Validate that at least one Pylon auth mode is configured."""
        if bool(self.identity_name) != bool(self.identity_token):
            raise ValueError(
                "identity_name and identity_token must be provided together"
            )
        has_open_access = bool(self.open_access_token)
        has_identity = bool(self.identity_name) and bool(self.identity_token)
        if not (has_open_access or has_identity):
            raise ValueError(
                "ChainConfig requires either open_access_token or "
                "(identity_name + identity_token); none provided"
            )


def parse_chain_config_from_env() -> ChainConfig:
    """Build a :class:`ChainConfig` from the ``PYLON_*`` / ``NETUID`` /
    ``SUBTENSOR_NETWORK`` environment variables.

    Defaults match the local docker-compose stack: Pylon on
    ``http://localhost:8001`` (post the API-server port shift), subnet
    netuid 118, finney mainnet for the substrate-interface event reader.
    One token (``PYLON_TOKEN``) guards both the open-access reads and the identity
    write, so it feeds ``open_access_token`` and, when an identity name is set,
    ``identity_token``. Empty strings normalise to ``None``.

    Raises:
        ValueError: When ``PYLON_TOKEN`` is unset (no auth mode).
            Surfaces from :meth:`ChainConfig.__post_init__`.
    """
    token = os.environ.get("PYLON_TOKEN") or None
    identity_name = os.environ.get("PYLON_IDENTITY_NAME") or None
    return ChainConfig(
        pylon_url=os.environ.get("PYLON_URL", "http://localhost:8001"),
        netuid=int(os.environ.get("NETUID", "118")),
        open_access_token=token,
        identity_name=identity_name,
        # Same token; only in identity mode (a bare read client sets no identity).
        identity_token=token if identity_name else None,
        subtensor_network=os.environ.get("SUBTENSOR_NETWORK", "finney"),
        archive_blocks_cutoff=int(os.environ.get("ARCHIVE_BLOCKS_CUTOFF", "300")),
    )


@dataclass(frozen=True)
class NeuronInfo:
    """A neuron registered on the subnet at a point in time.

    Mirrors the subset of Pylon's :class:`pylon_client.artanis.Neuron` that
    Ditto's validator and platform code actually use. Extra Pylon fields
    (``rank``, ``trust``, ``consensus``, ``emission``, ``last_update``,
    ``pruning_score``, etc.) are deliberately omitted until a consumer
    needs them.
    """

    hotkey: str
    """SS58-encoded hotkey address."""

    coldkey: str
    """SS58-encoded coldkey address that owns the hotkey."""

    uid: int
    """Subnet-local UID assigned at registration."""

    stake: float
    """Stake on this neuron's hotkey, in TAO units."""

    axon_info: dict[str, Any] = field(default_factory=dict)
    """Raw axon metadata as returned by Pylon (ip, port, version)."""

    is_active: bool = False
    """Whether the neuron is currently marked active on the metagraph."""

    validator_permit: bool = False
    """Whether this hotkey holds a validator permit and may call ``put_weights``."""

    @classmethod
    def from_pylon(cls, raw: Any, hotkey: str | None = None) -> NeuronInfo:
        """Build a :class:`NeuronInfo` from a Pylon ``Neuron``.

        Args:
            raw: Pylon ``Neuron`` object.
            hotkey: Hotkey override. ``GetNeuronsResponse.neurons`` is a
                ``dict[Hotkey, Neuron]`` and Pylon's ``Neuron.hotkey`` field
                duplicates the dict key, but callers iterating ``.items()``
                can pass the key here as the authoritative value.
        """
        return cls(
            hotkey=str(hotkey if hotkey is not None else getattr(raw, "hotkey", "")),
            coldkey=str(getattr(raw, "coldkey", "") or ""),
            uid=int(getattr(raw, "uid", 0) or 0),
            stake=float(getattr(raw, "stake", 0.0) or 0.0),
            axon_info=_axon_info_to_dict(getattr(raw, "axon_info", None)),
            is_active=bool(getattr(raw, "active", False)),
            validator_permit=bool(getattr(raw, "validator_permit", False)),
        )


def _axon_info_to_dict(axon: Any) -> dict[str, Any]:
    """Flatten a Pylon ``AxonInfo`` (Pydantic model) into a plain dict.

    Returns an empty dict for ``None`` or unrecognised shapes.
    """
    if axon is None:
        return {}
    if isinstance(axon, dict):
        return dict(axon)
    dump = getattr(axon, "model_dump", None)
    if callable(dump):
        return dict(dump())
    return {}


@dataclass(frozen=True)
class ExtrinsicInfo:
    """A single extrinsic at a known ``(block_number, extrinsic_index)``.

    Pylon's ``Extrinsic`` response does NOT include the block hash, so
    ``succeeded`` cannot be auto-resolved from a ``get_extrinsic`` call
    alone. Callers that already hold the block hash (typical for miner
    upload-payment verification, where the hash comes back from
    ``transfer_keep_alive`` finalisation) should call
    :meth:`ChainClient.check_extrinsic_success` separately.
    """

    block_number: int
    """Block number containing the extrinsic."""

    extrinsic_index: int
    """Zero-based index of the extrinsic within the block."""

    extrinsic_hash: str
    """Hash of the extrinsic itself (NOT the block hash)."""

    call_module: str
    """Pallet name the call targets (e.g. ``Balances``)."""

    call_function: str
    """Call function within the pallet (e.g. ``transfer_keep_alive``)."""

    call_args: dict[str, Any] = field(default_factory=dict)
    """Decoded call arguments flattened to ``{name: value}``.

    Pylon returns a ``list[ExtrinsicCallArg]`` with ``name``, ``type``,
    ``value`` per arg; we drop the type info and keep the name → value
    mapping for caller convenience. Order is not preserved.
    """

    signer_address: str = ""
    """SS58-encoded address of the signer (empty for unsigned extrinsics)."""

    succeeded: bool | None = None
    """Whether ``system.ExtrinsicSuccess`` was emitted for this extrinsic.

    Populated by :meth:`ChainClient.check_extrinsic_success`. Stays ``None``
    if the check has not been run.
    """

    @classmethod
    def from_pylon(
        cls,
        raw: Any,
        succeeded: bool | None = None,
    ) -> ExtrinsicInfo:
        """Build an :class:`ExtrinsicInfo` from a Pylon ``Extrinsic``.

        Args:
            raw: Pylon ``Extrinsic`` response (block_number, extrinsic_index,
                extrinsic_hash, address, call all present on the response itself).
            succeeded: Pre-resolved success status from
                :meth:`ChainClient.check_extrinsic_success`, when the caller
                holds the block hash.
        """
        call = getattr(raw, "call", None)
        return cls(
            block_number=int(getattr(raw, "block_number", 0) or 0),
            extrinsic_index=int(getattr(raw, "extrinsic_index", 0) or 0),
            extrinsic_hash=str(getattr(raw, "extrinsic_hash", "") or ""),
            call_module=str(getattr(call, "call_module", "") or ""),
            call_function=str(getattr(call, "call_function", "") or ""),
            call_args=_call_args_to_dict(getattr(call, "call_args", None)),
            signer_address=normalize_address_to_ss58(
                str(getattr(raw, "address", "") or "")
            ),
            succeeded=succeeded,
        )


def _call_args_to_dict(args: Any) -> dict[str, Any]:
    """Flatten a list of ``ExtrinsicCallArg`` into ``{name: value}``.

    Tolerates ``None``, an already-flattened dict, or a list of either
    ``ExtrinsicCallArg`` instances or plain dicts.
    """
    if args is None:
        return {}
    if isinstance(args, dict):
        return dict(args)
    if isinstance(args, list):
        out: dict[str, Any] = {}
        for arg in args:
            if isinstance(arg, dict):
                name = arg.get("name")
                value = arg.get("value")
            else:
                name = getattr(arg, "name", None)
                value = getattr(arg, "value", None)
            if name is not None:
                out[str(name)] = value
        return out
    return {}


@dataclass(frozen=True)
class BlockInfo:
    """A block on the chain identified by number, hash, and timestamp.

    Maps onto Pylon's ``BlockInfoBag`` (the response shape of
    ``get_latest_block_info``).
    """

    number: int
    """Block number counting from genesis (block 0)."""

    hash: str
    """Block hash as a hex string (with or without the ``0x`` prefix per Pylon)."""

    timestamp: int = 0
    """Unix timestamp in seconds at which the block was produced."""

    @classmethod
    def from_pylon(cls, raw: Any) -> BlockInfo:
        """Build a :class:`BlockInfo` from a Pylon ``BlockInfoBag`` / ``Block``."""
        return cls(
            number=int(getattr(raw, "number", 0) or 0),
            hash=str(getattr(raw, "hash", "") or ""),
            timestamp=int(getattr(raw, "timestamp", 0) or 0),
        )
