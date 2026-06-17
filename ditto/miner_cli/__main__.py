"""Process entry point for the ``ditto`` miner CLI.

Builds the argparse subparser layout, sets up stdlib logging (off by
default, on with ``--verbose`` so the miner sees diagnostics on
demand), and dispatches to the matching subcommand handler in
:mod:`ditto.miner_cli.commands`.

Mirrors :mod:`ditto.api_server.__main__` posture: argparse + env-var
defaults per :file:`context-docs/practices/CODE-QUALITY-STANDARDS.md`
§argparse rule, no click / typer / rich.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from ditto.miner_cli.commands import status as status_cmd
from ditto.miner_cli.commands import upload as upload_cmd
from ditto.miner_cli.commands import verify as verify_cmd
from ditto.miner_cli.errors import MinerCliError
from ditto.miner_cli.network import NETWORKS

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ditto",
        description=(
            "Miner-side CLI for Ditto Bittensor Subnet 118. "
            "Submit agent harnesses, poll lifecycle, and pre-flight tarballs."
        ),
    )
    parser.add_argument(
        "--network",
        choices=sorted(NETWORKS),
        default=os.environ.get("DITTO_NETWORK", "finney"),
        help=(
            "Deployment network. Couples API URL + subtensor network "
            "from a locked lookup table; cannot desync. Values match the "
            "bittensor SDK identifiers: 'finney' is mainnet, 'test' is "
            "testnet, 'local' is the developer's own localnet. "
            "Defaults to finney / $DITTO_NETWORK."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable INFO/DEBUG logs to stderr.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    upload_cmd.add_subparser(subparsers)
    status_cmd.add_subparser(subparsers)
    verify_cmd.add_subparser(subparsers)

    return parser


def _configure_logging(verbose: bool) -> None:
    """Stdlib logging dictConfig-equivalent for the CLI.

    Off by default (only WARNING and up reach stderr) so happy-path
    output stays clean. ``--verbose`` enables DEBUG so miners can
    diagnose flow without dropping into a debugger.
    """
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    try:
        return int(args.func(args))
    except MinerCliError as e:
        logger.debug(f"miner cli error: {e!r}")
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
