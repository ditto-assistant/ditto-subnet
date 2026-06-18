"""Miner-side CLI process: the ``ditto`` command miners run to upload.

The CLI consumes the HTTP API exposed by :mod:`ditto.api_server` and
submits one chain operation directly via the raw bittensor SDK: a
``Balances.transfer_keep_alive`` extrinsic that funds the upload fee.
Every other chain query (registration status, neuron lookup) is handled
server-side by the API and surfaced through retrieval endpoints, so the
CLI itself never reaches for Pylon or :class:`ditto.chain.ChainClient`.

Three subcommands ship here:

- ``ditto upload <tar>``: full 10-step submission flow
- ``ditto status [agent_id]``: poll lifecycle by id or wallet hotkey
- ``ditto verify <tar>``: pure-local pre-flight without paying

A fourth subcommand (``ditto logs``) is part of the locked CLI surface
in ``context-docs/MVP-SPEC.md §14`` but its target endpoint is not yet
built; it lands in a follow-up PR.
"""

from __future__ import annotations

from ditto.miner_cli.errors import (
    AgentNotFoundError,
    ApiResponseError,
    DependencyAllowlistError,
    HotkeyAgentNotFoundError,
    ManifestError,
    MinerCliError,
    PaymentCancelledError,
    PaymentFinalizationTimeoutError,
    PaymentSubmissionError,
    PreCheckRejectedError,
    SchemaDriftError,
    TarStructureError,
    UploadAgentRejectedError,
    WalletDecryptError,
    WalletNotFoundError,
)
from ditto.miner_cli.factory import create_miner_cli_config
from ditto.miner_cli.models import (
    MinerCliConfig,
    NetworkConfig,
    PaymentReceipt,
    PreflightCheckResult,
    PreflightResult,
    UploadResult,
    WalletHandle,
)
from ditto.miner_cli.network import NETWORKS, resolve_network

__all__ = [
    # Configuration
    "MinerCliConfig",
    "NetworkConfig",
    "NETWORKS",
    "resolve_network",
    # Result / value models
    "PreflightCheckResult",
    "PreflightResult",
    "PaymentReceipt",
    "UploadResult",
    "WalletHandle",
    # Errors
    "MinerCliError",
    "TarStructureError",
    "ManifestError",
    "DependencyAllowlistError",
    "SchemaDriftError",
    "WalletNotFoundError",
    "WalletDecryptError",
    "PaymentSubmissionError",
    "PaymentFinalizationTimeoutError",
    "ApiResponseError",
    "PreCheckRejectedError",
    "UploadAgentRejectedError",
    "AgentNotFoundError",
    "HotkeyAgentNotFoundError",
    "PaymentCancelledError",
    # Factory
    "create_miner_cli_config",
]
