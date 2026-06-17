"""``ditto verify``: local pre-flight without paying.

Wraps :func:`ditto.miner_cli.tar_validator.run_preflight`, prints a
per-check status table to stdout, and exits 0 on clean / 1 on any real
check failing. Deferred checks are printed distinctly (``DEFERRED``)
so the miner sees what is not yet enforced without treating it as a
failure.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from ditto.miner_cli.errors import TarStructureError
from ditto.miner_cli.models import PreflightCheckResult, PreflightResult
from ditto.miner_cli.tar_validator import run_preflight

logger = logging.getLogger(__name__)


def add_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``verify`` subparser on the top-level argparse layout."""
    parser = subparsers.add_parser(
        "verify",
        help="Run local pre-flight checks on a tarball without paying.",
        description=(
            "Run every pre-flight check on the tarball and print a table "
            "of results. Exits 0 if every non-deferred check passed."
        ),
    )
    parser.add_argument(
        "tar_path",
        type=Path,
        help="Path to the gzipped tarball to verify.",
    )
    parser.set_defaults(func=run)
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute the verify subcommand and return an exit code."""
    try:
        result = run_preflight(args.tar_path)
    except TarStructureError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    _print_result(result)
    return 0 if result.passed else 1


def _print_result(result: PreflightResult) -> None:
    """Print a fixed-width per-check status table to stdout."""
    name_width = max(len(c.name) for c in result.checks)
    print(f"sha256: {result.sha256}")
    print(f"size:   {result.file_size_bytes} bytes")
    print()
    print(f"{'CHECK':<{name_width}}  STATUS    DETAIL")
    print(f"{'-' * name_width}  --------  ------")
    for check in result.checks:
        print(f"{check.name:<{name_width}}  {_status_label(check):<8}  {check.detail}")
    print()
    print(f"result: {'PASS' if result.passed else 'FAIL'}")


def _status_label(check: PreflightCheckResult) -> str:
    if check.deferred:
        return "DEFERRED"
    return "PASS" if check.passed else "FAIL"
