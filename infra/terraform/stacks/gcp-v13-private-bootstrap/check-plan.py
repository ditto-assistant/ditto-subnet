"""Fail closed unless a saved bootstrap plan matches the private approval record.

This runs in both protected GitHub environments. Never print planned values or
the approval manifest; either can contain private principals and billing data.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ID = "ditto-v13-private-verifier"
BUCKET = f"{PROJECT_ID}-tfstate"
EXPECTED_ADDRESSES = {
    "google_project.verifier": {
        "project_id": PROJECT_ID,
        "name": "Ditto V13 private verifier",
        "auto_create_network": False,
    },
    "google_project_service.storage": {
        "project": PROJECT_ID,
        "service": "storage.googleapis.com",
        "disable_on_destroy": False,
    },
    "google_storage_bucket.state": {
        "project": PROJECT_ID,
        "name": BUCKET,
        "location": "US-CENTRAL1",
        "storage_class": "STANDARD",
        "uniform_bucket_level_access": True,
        "public_access_prevention": "enforced",
        "force_destroy": False,
    },
    "google_storage_bucket_iam_member.state_custodian": {
        "bucket": BUCKET,
        "role": "roles/storage.objectAdmin",
    },
}


def check(plan: dict, approval: dict) -> bool:
    if set(approval) != {
        "project_id",
        "state_bucket",
        "organization_id",
        "billing_account_id",
        "state_custodian",
    }:
        return False
    if approval["project_id"] != PROJECT_ID or approval["state_bucket"] != BUCKET:
        return False
    if not isinstance(approval["organization_id"], str) or not re.fullmatch(
        r"[0-9]+", approval["organization_id"]
    ):
        return False
    if not isinstance(approval["billing_account_id"], str) or not re.fullmatch(
        r"[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}",
        approval["billing_account_id"],
    ):
        return False
    if not isinstance(approval["state_custodian"], str) or not re.fullmatch(
        r"user:[^@]+@[^@]+", approval["state_custodian"]
    ):
        return False

    changes = plan.get("resource_changes", [])
    if not isinstance(changes, list):
        return False
    changes = [c for c in changes if c.get("change", {}).get("actions") != ["no-op"]]
    if len(changes) != len(EXPECTED_ADDRESSES):
        return False
    seen = set()
    for resource in changes:
        address = re.sub(r"\[.*$", "", resource.get("address", ""))
        if address in seen or address not in EXPECTED_ADDRESSES:
            return False
        seen.add(address)
        change = resource.get("change", {})
        if change.get("actions") != ["create"] or change.get("before") is not None:
            return False
        after = change.get("after")
        if not isinstance(after, dict):
            return False
        expected = EXPECTED_ADDRESSES[address]
        if any(after.get(key) != value for key, value in expected.items()):
            return False
        if address == "google_project.verifier":
            if after.get("org_id") != approval["organization_id"]:
                return False
            if after.get("billing_account") != approval["billing_account_id"]:
                return False
        if address == "google_storage_bucket.state" and after.get("versioning") != [
            {"enabled": True}
        ]:
            return False
        if (
            address == "google_storage_bucket_iam_member.state_custodian"
            and after.get("member") != approval["state_custodian"]
        ):
            return False
    return seen == set(EXPECTED_ADDRESSES)


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        plan = json.loads(Path(sys.argv[1]).read_text())
        approval = json.loads(Path(sys.argv[2]).read_text())
        valid = check(plan, approval)
    except (OSError, ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        print(
            "V13 bootstrap plan differs from the protected approval record.",
            file=sys.stderr,
        )
        return 1
    print("V13 bootstrap plan matches the protected approval record: four creates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
