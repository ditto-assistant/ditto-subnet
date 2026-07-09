"""Wire argparse + env into a frozen :class:`MinerCliConfig`."""

from __future__ import annotations

import argparse

from ditto.miner_cli.models import MinerCliConfig
from ditto.miner_cli.network import resolve_network


def create_miner_cli_config(args: argparse.Namespace) -> MinerCliConfig:
    """Build a :class:`MinerCliConfig` from the parsed argparse namespace.

    The argparse layer is the single ingestion point for env-var
    fallbacks (each ``add_argument(... default=os.environ.get(...))``
    line in ``__main__.py``), so this function just consumes the
    already-resolved namespace fields.

    Args:
        args: Top-level argparse namespace. Must carry a ``network``
            attribute populated from ``--network`` / ``DITTO_NETWORK``.
            ``chain_endpoint`` is optional; ``getattr(args,
            "chain_endpoint", None)`` keeps the factory tolerant of
            test namespaces that do not register the flag.

    Raises:
        ValueError: When ``args.network`` is not one of the names in
            :data:`ditto.miner_cli.network.NETWORKS`. argparse
            ``choices=...`` should normally prevent this.
    """
    return MinerCliConfig(
        network=resolve_network(args.network),
        chain_endpoint=getattr(args, "chain_endpoint", None) or None,
    )
