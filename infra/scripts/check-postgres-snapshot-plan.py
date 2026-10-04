#!/usr/bin/env python3
"""Reject any snapshot activation plan beyond the reviewed two additive resources."""

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


if __name__ == "__main__":
    try:
        check(json.loads(Path(sys.argv[1]).read_text()))
    except (ValueError, KeyError, IndexError):
        print("snapshot plan rejected; review the private plan", file=sys.stderr)
        raise SystemExit(1) from None
