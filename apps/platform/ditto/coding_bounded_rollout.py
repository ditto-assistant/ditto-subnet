"""Explicit bounded shadow cohort, only inside the approved manual worker unit."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path


async def run(path: Path, approval_sha: str, connectivity_sha: str) -> dict:
    from ditto.api_server.coding_bounded_rollout import run as execute

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        return await execute(path, approval_sha, connectivity_sha, stop)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--bounded-shadow-once", action="store_true")
    modes.add_argument("--inspect", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--approval-sha256", required=True)
    parser.add_argument("--connectivity-sha256")
    args = parser.parse_args()
    if args.inspect:
        if args.config is not None or args.connectivity_sha256 is not None:
            parser.error("inspection takes only an approval identity")
        try:
            from ditto.api_server.coding_bounded_rollout import STATE
            from ditto.api_server.coding_rollout_governor import inspect_state

            print(
                json.dumps(inspect_state(STATE, args.approval_sha256), sort_keys=True),
                flush=True,
            )
            return 0
        except Exception:
            print("bounded rollout status unavailable", file=sys.stderr, flush=True)
            return 70
    if args.config is None or args.connectivity_sha256 is None:
        parser.error("execution requires both independent approvals")
    logging.disable(logging.CRITICAL)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            run(args.config, args.approval_sha256, args.connectivity_sha256)
        )
        print("bounded shadow cohort completed; weight_eligible=false", flush=True)
        return 0
    except BaseException:
        print(
            "bounded rollout failed; preserve state for reconciliation",
            file=sys.stderr,
            flush=True,
        )
        return 70
    finally:
        # govern already attempted its bounded drain. Never wait indefinitely
        # in asyncio.run shutdown on cancellation-resistant background tasks.
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.close()


if __name__ == "__main__":
    # Dedicated service process. Acceptance was fsynced before success; failure
    # retains active state and systemd owns remaining cgroup cleanup.
    os._exit(main())
