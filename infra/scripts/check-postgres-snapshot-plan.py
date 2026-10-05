#!/usr/bin/env python3
"""Reject any snapshot activation plan beyond the reviewed two additive resources."""

import argparse
import json
import sys
from pathlib import Path

EXPECTED = {
    "google_compute_resource_policy.platform_postgres_daily",
    "module.pg_vm.google_compute_disk_resource_policy_attachment.boot_snapshot[0]",
}


def check(plan):
    changes = {
        row["address"]: row["change"]["actions"]
        for row in plan.get("resource_changes", [])
        if row["change"]["actions"] not in (["no-op"], ["read"])
    }
    if set(changes) != EXPECTED or any(
        actions != ["create"] for actions in changes.values()
    ):
        raise ValueError(
            "snapshot plan must contain exactly two creates and no other mutations"
        )
    print("snapshot plan: 2 creates, 0 updates, 0 deletes; instances/disks unchanged")


def check_when_changed(plan):
    # Inspect the actual saved plan, including implicit dependencies and parent
    # module targets, rather than trusting the spelling of a target argument.
    rows = plan["resource_changes"]
    if not isinstance(rows, list):
        raise ValueError("snapshot plan requires a resource change list")
    if any(
        (
            "google_compute_resource_policy." in row["address"]
            or "google_compute_disk_resource_policy_attachment." in row["address"]
        )
        and row["change"]["actions"] not in (["no-op"], ["read"])
        for row in rows
    ):
        check(plan)


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("--when-changed", action="store_true")
        parser.add_argument("plan_file")
        args = parser.parse_args()
        inspect = check_when_changed if args.when_changed else check
        inspect(json.loads(Path(args.plan_file).read_text()))
    except (ValueError, KeyError, IndexError, OSError, TypeError):
        print("snapshot plan rejected; review the private plan", file=sys.stderr)
        raise SystemExit(1) from None
