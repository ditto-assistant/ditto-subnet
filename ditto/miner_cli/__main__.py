"""Process entry point for the ``ditto`` miner CLI.

Builds the argparse subparser layout, sets up stdlib logging (off by
default, on with ``--verbose`` so the miner sees diagnostics on
demand), and dispatches to the matching subcommand handler in
:mod:`ditto.miner_cli.commands`.

Mirrors :mod:`ditto.api_server.__main__` posture: argparse + env-var
defaults, stdlib logging, no click / typer / rich.
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


# Help strings shared between the top-level parser (real defaults) and the
# subcommand parent parser (SUPPRESS defaults). Centralised so the help text
# stays identical regardless of where the flag is parsed.
_NETWORK_HELP = (
    "Deployment network. Couples API URL + subtensor network "
    "from a locked lookup table; cannot desync. Values match the "
    "bittensor SDK identifiers: 'finney' is mainnet, 'test' is "
    "testnet, 'local' is localnet. Flag "
    "aliases: --subtensor.network / --network. Env: "
    "DITTO_NETWORK. Defaults to finney."
)
_CHAIN_ENDPOINT_HELP = (
    "Override the chain target URL for the selected network. "
    "When set, used in place of --network's chain identifier when "
    "constructing bittensor.Subtensor; the API URL side of the "
    "--network pair is unaffected. Useful for smoke testing against "
    "a non-default chain endpoint (a hosted local subtensor at a "
    "specific IP, a testnet endpoint pre-DNS). Flag aliases: "
    "--subtensor.chain_endpoint / --chain-endpoint. Env: "
    "DITTO_SUBTENSOR_CHAIN_ENDPOINT. Unset by default."
)
_VERBOSE_HELP = "Enable INFO/DEBUG logs to stderr."


def _build_subcommand_parent() -> argparse.ArgumentParser:
    """Parent parser used to register the shared flags on each subparser.

    Defaults are ``argparse.SUPPRESS`` so that a subparser inheriting
    this parent does NOT clobber an attribute already set by the
    top-level parser when the flag was supplied before the subcommand.
    The real defaults live on the top-level parser via
    :func:`_build_parser`'s ``add_argument`` calls, where action objects
    are NOT shared with this parent.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--subtensor.network",
        "--network",
        dest="network",
        choices=sorted(NETWORKS),
        default=argparse.SUPPRESS,
        help=_NETWORK_HELP,
    )
    parent.add_argument(
        "--subtensor.chain_endpoint",
        "--chain-endpoint",
        dest="chain_endpoint",
        default=argparse.SUPPRESS,
        help=_CHAIN_ENDPOINT_HELP,
    )
    parent.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help=_VERBOSE_HELP,
    )
    return parent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ditto",
        description=(
            "Miner-side CLI for Ditto Bittensor Subnet 118. "
            "Submit agent harnesses, poll lifecycle, and pre-flight tarballs."
        ),
    )
    # Top-level registrations carry the real defaults. NOT defined via
    # ``parents=`` because that would share action objects with the
    # subcommand parent below; mutating defaults on shared actions
    # propagates into subparsers and breaks the SUPPRESS-on-subparser
    # contract.
    parser.add_argument(
        "--subtensor.network",
        "--network",
        dest="network",
        choices=sorted(NETWORKS),
        default=os.environ.get("DITTO_NETWORK", "finney"),
        help=_NETWORK_HELP,
    )
    parser.add_argument(
        "--subtensor.chain_endpoint",
        "--chain-endpoint",
        dest="chain_endpoint",
        default=os.environ.get("DITTO_SUBTENSOR_CHAIN_ENDPOINT"),
        help=_CHAIN_ENDPOINT_HELP,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help=_VERBOSE_HELP,
    )

    # Subparsers inherit a SEPARATE parent with SUPPRESS defaults so
    # they accept the same flags after the subcommand without clobbering
    # whatever the top-level parser set.
    sub_parent = _build_subcommand_parent()
    subparsers = parser.add_subparsers(dest="command", required=True)
    upload_cmd.add_subparser(subparsers, parents=[sub_parent])
    status_cmd.add_subparser(subparsers, parents=[sub_parent])
    verify_cmd.add_subparser(subparsers, parents=[sub_parent])

    return parser


def _configure_logging(verbose: bool) -> None:
    """Stdlib logging dictConfig-equivalent for the CLI.

    Off by default (only WARNING and up reach stderr) so happy-path
    output stays clean. ``--verbose`` switches to DEBUG so miners can
    diagnose flow without dropping into a debugger.
    """
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    """Process entry point: parse argv, dispatch to a subcommand, map exit codes.

    Args:
        argv: Override the argv passed to argparse. Defaults to
            ``sys.argv[1:]`` when ``None``.

    Returns:
        Process exit code:
        ``0`` on success, ``1`` on any :class:`MinerCliError`,
        ``130`` on user interrupt (Ctrl-C). Subcommands that raise
        unhandled non-:class:`MinerCliError` exceptions propagate
        to argparse / Python's default handling so tracebacks are
        not swallowed during development.
    """
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
