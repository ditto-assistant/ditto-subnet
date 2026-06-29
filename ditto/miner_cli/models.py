"""Frozen dataclass value objects shared across the miner CLI subpackage.

Pydantic is reserved for HTTP wire shapes (``ditto.api_models``);
internal value carriers go through ``@dataclass(frozen=True)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class NetworkConfig:
    """A locked pair of API URL + subtensor network identifier.

    Each Ditto deployment binds an API server to exactly one chain at
    boot, so the CLI presents miners with named pairs rather than two
    independently settable strings. ``ditto.miner_cli.network.NETWORKS``
    holds the canonical entries.
    """

    name: str
    """One of ``"finney"`` (mainnet), ``"test"`` (testnet), ``"local"``."""

    api_url: str
    """Base URL of the Ditto API server for this network."""

    subtensor_network: str
    """Bittensor network identifier passed to ``bittensor.Subtensor``.

    Valid values: ``"finney"``, ``"test"``, ``"local"``, or a full
    WebSocket URL. The bittensor SDK handles parsing.
    """


@dataclass(frozen=True)
class WalletHandle:
    """Immutable identifying record for a loaded wallet.

    Holds only the strings needed for logging and signing-payload
    construction. The mutable :class:`bittensor.wallet.Wallet` object
    is deliberately NOT a field on this dataclass: keeping the frozen
    contract intact means callers pass the live wallet object alongside
    a :class:`WalletHandle` through function signatures (see
    :func:`ditto.miner_cli.signing.sign_upload_payload`) rather than
    embedding mutable state in a frozen container.
    """

    coldkey_name: str
    """Coldkey wallet name (``--wallet.name`` / ``--coldkey`` / ``WALLET_NAME``)."""

    hotkey_name: str
    """Hotkey name (from ``--wallet.hotkey`` / ``--hotkey`` / env ``HOTKEY_NAME``)."""

    hotkey_ss58: str
    """SS58-encoded address derived from the loaded hotkey keyfile."""


@dataclass(frozen=True)
class PreflightCheckResult:
    """Result of a single named pre-flight check.

    The ``deferred`` flag distinguishes checks that are stubbed pending
    artifacts the harness team still owns (manifest spec, approved
    dependencies, reference schema) from real pass/fail results.
    Deferred checks always set ``passed=True`` but surface in the
    printed table so miners see what is not yet enforced.
    """

    name: str
    """Stable identifier for the check, e.g. ``"gzip_valid"``."""

    passed: bool
    """``True`` when the check passed (or is deferred)."""

    detail: str
    """Human-readable message describing the outcome."""

    deferred: bool = False
    """``True`` if the check is a placeholder pending external artifacts."""


@dataclass(frozen=True)
class PreflightResult:
    """Aggregate result of every pre-flight check run on a tarball."""

    sha256: str
    """SHA-256 of the tarball, lowercase hex, no ``0x`` prefix."""

    file_size_bytes: int
    """Tarball size on disk."""

    checks: tuple[PreflightCheckResult, ...]
    """All checks run, in execution order. Frozen tuple for dataclass safety."""

    @property
    def passed(self) -> bool:
        """``True`` when every non-deferred check passed."""
        return all(c.passed for c in self.checks if not c.deferred)


@dataclass(frozen=True)
class PaymentReceipt:
    """Proof of payment returned by the chain after extrinsic finalization."""

    block_hash: str
    """``0x``-prefixed 64-hex block hash of the block including the extrinsic."""

    block_number: int
    """Block number containing the extrinsic."""

    extrinsic_index: int
    """Zero-based extrinsic index within the block."""


@dataclass(frozen=True)
class UploadResult:
    """Server response from ``POST /upload/agent``."""

    agent_id: UUID
    """Server-generated agent identifier."""

    status: str
    """Lifecycle state as a string (always ``"uploaded"`` on success today)."""


@dataclass(frozen=True)
class MinerCliConfig:
    """Resolved configuration for one CLI invocation.

    Built once at process start by
    :func:`ditto.miner_cli.factory.create_miner_cli_config` from the
    argparse namespace plus environment variables. Subcommand handlers
    receive this object and pull what they need from it.
    """

    network: NetworkConfig
    """Resolved network pair (API URL + subtensor network)."""

    chain_endpoint: str | None = None
    """Optional chain URL override (``--subtensor.chain_endpoint`` /
    ``DITTO_SUBTENSOR_CHAIN_ENDPOINT``).

    When set, used in place of ``network.subtensor_network`` as the
    target passed to ``bittensor.Subtensor(network=...)`` (the SDK
    accepts either a known identifier or a full WebSocket URL on that
    arg). The API URL piece of the locked ``network`` pair is
    unaffected. ``None`` means use the SDK's default URL for
    ``network.subtensor_network``.
    """
