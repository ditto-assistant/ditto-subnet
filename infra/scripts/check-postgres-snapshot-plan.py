#!/usr/bin/env python3
"""Reject any snapshot activation plan beyond the reviewed additive resources."""

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
    attachment = (
        "module.pg_vm.google_compute_disk_resource_policy_attachment.boot_snapshot[0]"
    )
    policy = "google_compute_resource_policy.platform_postgres_daily"
    if set(changes) not in (EXPECTED, {attachment}) or any(
        actions != ["create"] for actions in changes.values()
    ):
        raise ValueError("snapshot plan permits only policy/attachment creates")
    # A failed first apply may already have created the policy. Require that
    # exact policy to be present and unchanged before finishing its attachment.
    rows = {row["address"]: row for row in plan["resource_changes"]}
    if policy not in rows or attachment not in rows:
        raise ValueError("snapshot plan requires both reviewed resources")
    for address, values in {
        policy: {
            "name": "ditto-pg-platform-daily",
            "project": "ditto-app-dev",
            "region": "us-central1",
        },
        attachment: {
            "name": "ditto-pg-platform-daily",
            "project": "ditto-app-dev",
            "disk": "ditto-pg-platform",
            "zone": "us-central1-a",
        },
    }.items():
        after = rows[address]["change"]["after"]
        if any(after.get(key) != value for key, value in values.items()):
            raise ValueError("snapshot target or policy differs")
    if rows[policy]["change"]["actions"] not in (["create"], ["no-op"]):
        raise ValueError("snapshot policy must be created or already unchanged")
    schedule = rows[policy]["change"]["after"]["snapshot_schedule_policy"]
    if len(schedule) != 1:
        raise ValueError("unexpected snapshot schedule")
    item = schedule[0]
    if (
        item["retention_policy"]
        != [{"max_retention_days": 14, "on_source_disk_delete": "KEEP_AUTO_SNAPSHOTS"}]
        or item["schedule"]
        != [
            {
                "daily_schedule": [{"days_in_cycle": 1, "start_time": "06:00"}],
                "hourly_schedule": [],
                "weekly_schedule": [],
            }
        ]
        or len(item["snapshot_properties"]) != 1
        or item["snapshot_properties"][0]["guest_flush"] is not False
        or item["snapshot_properties"][0]["storage_locations"] != ["us"]
    ):
        raise ValueError("unexpected snapshot retention or consistency")
    print(
        f"snapshot plan: {len(changes)} creates, 0 updates, 0 deletes; "
        "instances/disks unchanged"
    )


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
