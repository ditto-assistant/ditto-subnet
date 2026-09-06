"""Explicit private Platform worker process, never started by API boot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import NoReturn


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise ValueError("hosted worker arguments invalid")


async def _run(path: Path) -> str:
    from ditto.api_server.coding_hosted_runtime import run_runtime

    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        return await run_runtime(path)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main() -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--private-shadow-once", action="store_true", required=True)
    parser.add_argument("--config", type=Path, required=True)
    try:
        args = parser.parse_args()
    except ValueError:
        print(
            "requires --private-shadow-once --config <protected-file>", file=sys.stderr
        )
        return 2
    logging.disable(logging.CRITICAL)
    try:
        # Fail cheap before importing the private runtime and database stack.
        # Full owner/type/link checks still happen in the protected file loader.
        if not args.config.is_absolute() or not args.config.is_file():
            raise ValueError("hosted worker configuration unavailable")
        asyncio.run(_run(args.config))
        print("hosted private terminal evidence finalized; weight_eligible=false")
        return 0
    except BaseException:
        print(
            "hosted worker failed; preserve private state for reconciliation",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
