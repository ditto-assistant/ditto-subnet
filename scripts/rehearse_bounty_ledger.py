#!/usr/bin/env python3
"""CLI utility to rehearse the 5-stage activation order and zero-fund bounty ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ditto.bounty import RAO_PER_TAO, run_activation_order_rehearsal


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Rehearse SN118 bounty activation order and ledger."
    )
    parser.add_argument(
        "--repo",
        default="ditto-assistant/ditto-subnet",
        help="Target GitHub repository (default: ditto-assistant/ditto-subnet)",
    )
    parser.add_argument(
        "--issue",
        type=int,
        default=2054,
        help="Target issue number (default: 2054)",
    )
    parser.add_argument(
        "--commit-sha",
        default="ef1518b0c61947b1928374829102837465819283",
        help="Accepted Git commit SHA",
    )
    parser.add_argument(
        "--amount-tao",
        type=float,
        default=5.0,
        help="Disbursement amount in TAO (default: 5.0)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON instead of human-readable markdown",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional file destination to write the rehearsal report",
    )
    return parser.parse_args()


def main() -> int:
    """Execute the activation rehearsal and report results."""
    args = parse_args()
    amount_rao = int(args.amount_tao * RAO_PER_TAO)

    try:
        report = run_activation_order_rehearsal(
            repo=args.repo,
            issue=args.issue,
            commit_sha=args.commit_sha,
            amount_rao=amount_rao,
        )
    except Exception as exc:
        print(
            f"ERROR: Activation rehearsal aborted with exception: {exc}",
            file=sys.stderr,
        )
        return 1

    if args.json:
        output_content = json.dumps(report.to_dict(), indent=2, sort_keys=True)
    else:
        output_content = report.to_markdown()

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output_content, encoding="utf-8")
        print(f"Rehearsal report written to {args.output}")
    else:
        print(output_content)

    return 0 if report.rehearsal_passed else 1


if __name__ == "__main__":
    sys.exit(main())
