"""Subcommand handlers for the ``ditto`` CLI.

Each subcommand exposes a ``run(args)`` function that returns an int
exit code. ``ditto.miner_cli.__main__`` builds the argparse subparser
layout and dispatches to these handlers.
"""

from __future__ import annotations

from ditto.miner_cli.commands.status import run as run_status
from ditto.miner_cli.commands.upload import run as run_upload
from ditto.miner_cli.commands.verify import run as run_verify

__all__ = ["run_status", "run_upload", "run_verify"]
