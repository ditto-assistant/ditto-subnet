"""Frozen dataclass models + env builder for the chain access layer."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, cast

ArchiveRpcAuthMode = Literal["none", "query", "path"]

_FINNEY_PUBLIC_ARCHIVE_RPC_URLS = (
    "wss://archive.chain.opentensor.ai:443",
    "wss://bittensor-finney.api.onfinality.io/public-ws",
)


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

    archive_rpc_url: str | None = None
    """Optional configured archive WebSocket URL tried before public archives.

    Pylon already selects an archive node for its block-number APIs.  Ditto's
    payment verifier also performs three block-hash storage reads directly via
    ``async-substrate-interface``; those reads must use an archive endpoint or
    finalized payment proofs stop working once the live node prunes the block.
    """

    archive_rpc_api_key: str | None = field(default=None, repr=False)
    """Optional archive RPC API key, excluded from dataclass representations."""

    archive_rpc_auth_mode: ArchiveRpcAuthMode = "query"
    """How ``archive_rpc_api_key`` is attached to the configured endpoint."""

    public_archive_rpc_urls: tuple[str, ...] = ()
    """Credential-free fallbacks used when the configured provider is absent
    or fails."""

    archive_rpc_timeout_seconds: float = 10.0
    """Maximum connection-plus-query time allowed for each archive provider."""

    metagraph_lag_check_seconds: float | None = None
    """How often a consumed Pylon metagraph snapshot is compared with a fresh
    finalized head; ``None`` disables the check.

    Pylon ages its recent-neurons cache by when it wrote it, not by the block
    it read, so a refresh over a lagging node connection still looks fresh.
    The check is one header read; registrations are re-read from chain storage
    only when the snapshot has fallen behind.
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
        if self.archive_rpc_auth_mode not in {"none", "query", "path"}:
            raise ValueError("archive_rpc_auth_mode must be one of: none, query, path")
        if self.archive_rpc_timeout_seconds <= 0:
            raise ValueError("archive_rpc_timeout_seconds must be positive")
        if (
            self.metagraph_lag_check_seconds is not None
            and self.metagraph_lag_check_seconds <= 0
        ):
            raise ValueError("metagraph_lag_check_seconds must be positive or None")


def parse_chain_config_from_env() -> ChainConfig:
    """Build a :class:`ChainConfig` from the ``PYLON_*`` / ``NETUID`` /
    ``SUBTENSOR_NETWORK`` environment variables.

    Defaults match the local docker-compose stack: Pylon on
    ``http://localhost:8001`` (post the API-server port shift), subnet
    netuid 118, finney mainnet for the substrate-interface event reader.
    Empty token strings are normalised to ``None`` so a partially-filled
    ``.env`` does not mask required-auth detection.

    Raises:
        ValueError: When neither ``PYLON_OPEN_ACCESS_TOKEN`` nor a paired
            (``PYLON_IDENTITY_NAME``, ``PYLON_IDENTITY_TOKEN``) is set.
            Surfaces from :meth:`ChainConfig.__post_init__`.
    """
    subtensor_network = os.environ.get("SUBTENSOR_NETWORK", "finney")
    archive_rpc_url = os.environ.get("SUBTENSOR_ARCHIVE_RPC_URL") or None
    archive_rpc_api_key = os.environ.get("SUBTENSOR_ARCHIVE_RPC_API_KEY") or None
    archive_rpc_auth_mode = os.environ.get("SUBTENSOR_ARCHIVE_RPC_AUTH_MODE", "query")
    if (
        archive_rpc_url == "wss://api.taostats.io/api/v1/rpc/ws/finney_archive"
        and archive_rpc_api_key is None
    ):
        # Reuse the existing Taostats secret only for Taostats' exact hostname;
        # never forward that credential to an arbitrary configured provider.
        archive_rpc_api_key = os.environ.get("DITTO_TAOSTATS_API_KEY") or None
    public_archive_rpc_urls = (
        _FINNEY_PUBLIC_ARCHIVE_RPC_URLS if subtensor_network == "finney" else ()
    )
    # Zero disables the check.
    metagraph_lag_check_seconds = (
        float(os.environ.get("METAGRAPH_LAG_CHECK_SECONDS", "300")) or None
    )

    return ChainConfig(
        pylon_url=os.environ.get("PYLON_URL", "http://localhost:8001"),
        netuid=int(os.environ.get("NETUID", "118")),
        open_access_token=os.environ.get("PYLON_OPEN_ACCESS_TOKEN") or None,
        identity_name=os.environ.get("PYLON_IDENTITY_NAME") or None,
        identity_token=os.environ.get("PYLON_IDENTITY_TOKEN") or None,
        subtensor_network=subtensor_network,
        archive_blocks_cutoff=int(os.environ.get("ARCHIVE_BLOCKS_CUTOFF", "300")),
        archive_rpc_url=archive_rpc_url,
        archive_rpc_api_key=archive_rpc_api_key,
        archive_rpc_auth_mode=cast(ArchiveRpcAuthMode, archive_rpc_auth_mode),
        public_archive_rpc_urls=public_archive_rpc_urls,
        archive_rpc_timeout_seconds=float(
            os.environ.get("SUBTENSOR_ARCHIVE_RPC_TIMEOUT_SECONDS", "10")
        ),
        metagraph_lag_check_seconds=metagraph_lag_check_seconds,
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


@dataclass(frozen=True)
class ChainRegistration:
    """One uid's hotkey and that hotkey's owner coldkey, from chain storage."""

    uid: int
    hotkey: str
    coldkey: str


@dataclass(frozen=True)
class ChainRegistrations:
    """Every uid's registration on a subnet at one finalized block.

    Read from ``SubtensorModule.Keys`` and ``Owner`` on a fresh connection, so
    it shares neither Pylon's long-lived node connection nor its refresh cycle.
    """

    netuid: int
    block: int
    block_timestamp: int | None
    """Unix seconds of ``block``; ``None`` when the timestamp read failed."""

    provider: str
    """Hostname that served the read. Never carries a credential."""

    observed_at: datetime
    registrations: tuple[ChainRegistration, ...]

    def lookup(self, hotkey: str) -> ChainRegistration | None:
        """The registration ``hotkey`` holds at :attr:`block`, if any."""
        return next((r for r in self.registrations if r.hotkey == hotkey), None)

    def head_age_seconds(self, now: float) -> float | None:
        """How old :attr:`block` was at unix time ``now``, when known."""
        if self.block_timestamp is None:
            return None
        return max(0.0, now - self.block_timestamp)


@dataclass(frozen=True)
class NeuronSnapshot:
    """A subnet's neurons and the chain block they describe.

    ``block`` is the block Pylon read its recent-neurons snapshot at. Pylon
    ages that cache by when it wrote it, not by this block, so a refresh over
    a lagging node connection is served as fresh; the block is the only height
    signal. ``registrations_block`` is newer than ``block`` when uid, hotkey and
    coldkey were taken from a newer read of chain storage.
    """

    block: int
    neurons: tuple[NeuronInfo, ...]
    observed_at: datetime
    """When Platform fetched the snapshot from Pylon."""

    registrations_block: int

    def neuron(self, hotkey: str) -> NeuronInfo | None:
        """The neuron registered to ``hotkey`` in this snapshot, if any."""
        return next((n for n in self.neurons if n.hotkey == hotkey), None)

    def with_registrations(self, chain: ChainRegistrations | None) -> NeuronSnapshot:
        """This snapshot with each uid's registration taken from a newer chain read.

        A neuron whose uid still holds the same hotkey keeps Pylon's other
        fields. A uid re-registered since the snapshot carries the chain's
        hotkey and coldkey with neutral defaults (no stake, no validator
        permit), and the hotkey it replaced is gone. A chain read no newer than
        the snapshot changes nothing.
        """
        if chain is None or chain.block <= self.registrations_block:
            return self
        current = {(n.uid, n.hotkey): n for n in self.neurons}
        neurons = tuple(
            replace(neuron, coldkey=entry.coldkey)
            if (neuron := current.get((entry.uid, entry.hotkey))) is not None
            else NeuronInfo(
                hotkey=entry.hotkey, coldkey=entry.coldkey, uid=entry.uid, stake=0.0
            )
            for entry in chain.registrations
        )
        return replace(self, neurons=neurons, registrations_block=chain.block)


@dataclass(frozen=True)
class HotkeyRegistration:
    """Whether a hotkey is registered on a subnet, with the evidence used."""

    hotkey: str
    netuid: int
    uid: int | None
    coldkey: str | None
    """Owner coldkey; ``None`` exactly when the hotkey is not registered."""

    snapshot_block: int
    """Block of the Pylon metagraph snapshot that was consulted."""

    chain_block: int | None = None
    """Finalized block of the chain-storage read made because the snapshot
    lacked the hotkey; ``None`` when the snapshot alone answered."""

    @property
    def registered(self) -> bool:
        """Whether the hotkey holds a uid on :attr:`netuid`."""
        return self.coldkey is not None


@dataclass(frozen=True)
class RegistrationChange:
    """A uid whose hotkey differs between Pylon's snapshot and chain storage."""

    uid: int
    pylon_hotkey: str | None
    chain_hotkey: str | None


@dataclass(frozen=True)
class RegistrationFreshness:
    """How far Pylon's metagraph snapshot trails the chain, for operators."""

    netuid: int
    snapshot_block: int
    snapshot_observed_at: datetime
    finalized_block: int | None
    """A fresh finalized head; ``None`` when no provider answered."""

    finalized_block_error: str | None
    chain_registrations: ChainRegistrations | None
    """The newest chain-storage registration read held in memory, if any."""

    serving_registrations_block: int
    """Block the registrations every consumer currently sees describe."""

    changes: tuple[RegistrationChange, ...]
    """Uids where what consumers see differs from Pylon's snapshot."""


@dataclass(frozen=True)
class ChainWeight:
    """One revealed destination weight from a validator's on-chain vector."""

    uid: int
    hotkey: str
    value: int


@dataclass(frozen=True)
class ChainWeightVector:
    """One validator's latest publicly revealed weight vector."""

    validator_uid: int
    validator_hotkey: str
    weights: tuple[ChainWeight, ...]


@dataclass(frozen=True)
class ChainEpoch:
    """Where the subnet sits in its tempo cycle, read at one block.

    Subtensor runs a subnet's epoch every ``tempo`` blocks, and that tick — not
    any individual validator's submission — is when Yuma consensus folds the
    weight matrix and the accumulated emission is paid out. Validators commit
    weights asynchronously (each throttled only by ``WeightsSetRateLimit``), so
    "when do weights get set" has no single answer; "when does the next fold
    and payout happen" does, and it is the same instant for every miner on the
    subnet.

    The phase comes from ``LastMechansimStepBlock`` (the chain's own record of
    the last epoch tick, misspelling included) rather than from reimplementing
    Subtensor's epoch predicate, so a runtime change to that predicate cannot
    silently desynchronise this from the chain.
    """

    tempo: int
    """Blocks between epoch ticks (``SubtensorModule.Tempo``). SN118 runs 360."""

    last_step_block: int
    """Block at which the subnet's last epoch tick ran."""

    blocks_since_last_step: int
    """Blocks elapsed since that tick, at the snapshot block."""

    next_epoch_block: int
    """Block at which the next tick is due: ``last_step_block + tempo``."""

    blocks_until_next_epoch: int
    """Blocks remaining to that tick, at the snapshot block. Never negative."""

    commit_reveal_enabled: bool | None
    """Whether weight commitments are timelock-encrypted (``None`` if unread)."""

    reveal_period_epochs: int | None
    """Epochs between a commit and its reveal (``None`` if unread)."""

    weights_rate_limit: int | None
    """Chain-enforced blocks between one hotkey's weight submissions."""


@dataclass(frozen=True)
class EpochSchedule:
    """The subnet's stateful epoch position, read at one block.

    Subtensor's stateful scheduler (``run_coinbase::should_run_epoch``) fires an
    epoch when a pending owner-triggered block is reached, when
    ``BlocksSinceLastStep`` overruns the tempo ceiling, or ``tempo`` blocks
    after ``LastEpochBlock``; ``SubnetEpochIndex`` counts every fire. That
    counter is the identity the epoch-pinned ledger keys on: unlike a block
    modulus it survives deferred and manually triggered epochs, and it is the
    same integer the chain itself stamps on every timelocked weight commit.
    """

    netuid: int
    subnet_epoch_index: int
    """Monotonic epoch counter (``SubtensorModule.SubnetEpochIndex``)."""

    last_epoch_block: int
    """Block at which the subnet's last epoch slot was consumed."""

    pending_epoch_at: int
    """Owner-triggered epoch block, or ``0`` when none is pending."""

    tempo: int
    blocks_since_last_step: int
    block: int
    """Head block every field above was read at."""

    block_hash: str
    block_timestamp: int | None
    """Unix seconds of ``block`` (``Timestamp.Now``), or ``None`` if unread."""

    next_epoch_block: int
    """First block after ``block`` at which the chain will step the epoch."""


@dataclass(frozen=True)
class ChainWeightsSnapshot:
    """A block-consistent read of the subnet's public weight matrix."""

    netuid: int
    block: int
    block_hash: str
    owner_hotkey: str | None
    vectors: tuple[ChainWeightVector, ...]
    epoch: ChainEpoch | None = None
    """Tempo position at ``block``, or ``None`` when those reads failed.

    Optional because the matrix is the endpoint's contract and the epoch is
    decoration on it: a hyperparameter read that fails must degrade the
    countdown, never take the weight matrix down with it.
    """

    block_timestamp: int | None = None
    """Unix seconds of ``block``, the anchor a countdown is measured from."""


@dataclass(frozen=True)
class ChainMinerEarning:
    """Miner incentive credited as stake/collateral, excluding owner recycling."""

    uid: int
    hotkey: str
    amount_rao: int


@dataclass(frozen=True)
class ChainMinerEmissionReceipt:
    """Finalized successful distribution and its unambiguous consumed matrix.

    This proves hotkey earnings, not which off-chain submission earned them.
    Consumers must bind every relevant validator vector to an immutable ledger.
    Timestamps are Unix seconds. All vectors are retained conservatively; this
    reader does not infer that an offline validator contributed zero stake.
    """

    netuid: int
    block: int
    block_hash: str
    block_timestamp: int
    epoch_index: int
    previous_distribution_block: int
    owner_hotkey: str
    earnings: tuple[ChainMinerEarning, ...]
    vectors: tuple[ChainWeightVector, ...]
    validator_last_updates: tuple[tuple[int, int], ...]
    validator_last_update_timestamps: tuple[tuple[int, int], ...]


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
            signer_address=str(getattr(raw, "address", "") or ""),
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
