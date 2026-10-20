"""Decide whether a screener release may leave a worker's current review.

``ready`` means the worker is idle and may be stopped.
``wait`` means a signed review is still inside its lease.
``held`` means the lease has ended. A long review is not an orphan, and this
command never signals the process.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# Two heartbeat intervals. A review still publishing progress is not stuck.
PROGRESS_FRESH_SECONDS = 300


def classify(lease: dict[str, object] | None, *, now: int) -> str:
    if not lease:
        return "ready"
    try:
        deadline = int(lease["lease_deadline"])  # type: ignore[arg-type]
        progress = int(lease["progress_at"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return "held"
    if now < deadline:
        return "wait"
    return "held" if now - progress > PROGRESS_FRESH_SECONDS else "wait"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lease", type=Path)
    parser.add_argument("--now", type=int, required=True)
    args = parser.parse_args()
    lease = None
    if args.lease is not None and args.lease.is_file():
        lease = json.loads(args.lease.read_text(encoding="utf-8"))
    print(classify(lease, now=args.now))


if __name__ == "__main__":
    main()
