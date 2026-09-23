#!/usr/bin/env python3
"""Synchronize canonical SN118 bounty labels to the GitHub repository."""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ditto.bounty.board import ALL_BOUNTY_LABELS, generate_gh_label_commands


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for label synchronization."""
    parser = argparse.ArgumentParser(
        description="Sync canonical SN118 bounty labels using GitHub CLI."
    )
    parser.add_argument(
        "--repo",
        default="ditto-assistant/ditto-subnet",
        help="Target GitHub repository (default: ditto-assistant/ditto-subnet)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print gh label commands without executing them",
    )
    return parser.parse_args()


def main() -> int:
    """Execute label synchronization."""
    args = parse_args()
    commands = generate_gh_label_commands(repo=args.repo)

    if args.dry_run:
        for cmd in commands:
            print(cmd)
        return 0

    for label in ALL_BOUNTY_LABELS.values():
        cmd = [
            "gh",
            "label",
            "create",
            label.name,
            "--color",
            label.color,
            "--description",
            label.description,
            "--repo",
            args.repo,
            "--force",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print(
                f"Failed to create/update label {label.name}: {result.stderr.strip()}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
