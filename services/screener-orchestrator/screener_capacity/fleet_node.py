"""Inert compatibility entrypoint for the installed pre-retirement unit.

The v0.319.11 updater starts this unit after it switches the first retirement
release. Its already-running shell cannot call the new updater functions. Keep
the old argv contract and an active process for that one rolling transition;
this entrypoint never contacts Platform or starts submission code.
"""

from __future__ import annotations

import argparse
import signal
import threading
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-url", required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--base-image", type=Path, required=True)
    parser.add_argument("--builder-image", required=True)
    parser.add_argument("--source-review-api-key-file", type=Path, required=True)
    parser.add_argument("--source-review-env-file", type=Path)
    parser.add_argument("--jobs-root", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--build-timeout-seconds", type=int, default=3000)
    parser.add_argument("--runtime-timeout-seconds", type=int, default=300)
    parser.add_argument("--source-review-timeout-seconds", type=int, default=3600)
    parser.add_argument("--max-workers", type=int, default=16)
    parser.add_argument("--vm-memory-mib", type=int, default=10240)
    parser.add_argument("--vm-vcpus", type=int, default=8)
    parser.add_argument("--vm-disk-gib", type=int, default=80)
    parser.add_argument("--local-sandbox-slots", type=int, default=0)
    parser.add_argument("--local-source-review-slots", type=int, default=0)
    parser.add_argument("--resource-slice", default="")
    parser.add_argument("--once", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.once:
        parser.error("retired fleet jobs cannot run")
    stop = threading.Event()
    for handled in (signal.SIGTERM, signal.SIGINT):
        signal.signal(handled, lambda _signum, _frame: stop.set())
    stop.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
