#!/usr/bin/env python3
"""Refuse a custody binary plan that contains another root's resources.

Read private Terraform JSON from a file; never print values or exception text.
This is a scope fence, not approval of a plan or proof of effective cloud IAM.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SINGLE = {
    "google_compute_network.collector_custody",
    "google_compute_subnetwork.collector_custody",
    "google_compute_router.collector_custody",
    "google_compute_router_nat.collector_custody",
    "google_compute_firewall.collector_iap",
    "google_compute_firewall.collector_deny_private",
    "google_compute_firewall.collector_googleapis",
    "google_compute_firewall.collector_deny_other",
    "google_project_iam_custom_role.collector_generator",
}
PER_ROLE = {
    "google_compute_firewall.collector_bootstrap",
    "google_compute_instance.collector_delegate",
    "google_compute_instance_iam_member.collector_operator",
    "google_iap_tunnel_instance_iam_member.collector_operator",
    "google_service_account.collector_delegate",
    "google_service_account_iam_member.collector_operator",
    "google_secret_manager_secret.collector_delegate",
    "google_secret_manager_secret_iam_member.collector_generator",
    "google_secret_manager_secret_iam_member.collector_reader",
}
ALLOWED = {f"{name}[0]" for name in SINGLE} | {
    f'{name}["{role}"]' for name in PER_ROLE for role in ("registration", "transfer")
}
ALLOWED_DATA = "data.google_project.collector_custody[0]"
BACKEND_GRANTS = {
    "google_storage_bucket_iam_member.collector_custody_state_lock[0]": (
        "roles/storage.objectAdmin",
        "default.tflock",
    ),
    "google_storage_bucket_iam_member.collector_custody_state_initial_create[0]": (
        "roles/storage.objectCreator",
        "default.tfstate",
    ),
}


def resources(module: dict) -> list[dict]:
    if module.get("child_modules"):
        raise ValueError("unexpected nested owner")
    return module.get("resources", [])


def validate(plan: dict) -> int:
    if (
        plan.get("errored")
        or plan.get("complete") is False
        or plan.get("deferred_changes")
    ):
        raise ValueError("incomplete plan")
    variables = {name: item["value"] for name, item in plan["variables"].items()}
    if any(
        variables.get(name) != value
        for name, value in {
            "project": "ditto-app-dev",
            "region": "us-central1",
            "zone": "us-central1-a",
            "enable_treasury_host": False,
        }.items()
    ):
        raise ValueError("wrong deployment scope")
    if not re.fullmatch(
        "[0-9a-f]{40}", variables.get("collector_custody_revision", "")
    ):
        raise ValueError("unbound source")
    addresses = variables.get("collector_custody_offline_addresses", [])
    if (
        len(addresses) != 5
        or len(set(addresses)) != 5
        or any(not re.fullmatch("5[1-9A-HJ-NP-Za-km-z]{47}", a) for a in addresses)
    ):
        raise ValueError("unbound public identities")
    for resource in plan.get("resource_changes", []):
        address = resource["address"]
        if resource["mode"] == "managed":
            if address not in ALLOWED:
                raise ValueError("unrelated change")
        elif resource["mode"] != "data" or address != ALLOWED_DATA:
            raise ValueError("unrelated data")
    for tree in (
        plan["planned_values"]["root_module"],
        plan.get("prior_state", {}).get("values", {}).get("root_module", {}),
    ):
        for resource in resources(tree):
            if resource["address"] not in ALLOWED | {ALLOWED_DATA}:
                raise ValueError("unrelated state")
    return sum(r["mode"] == "managed" for r in plan.get("resource_changes", []))


def validate_backend_bootstrap(plan: dict) -> int:
    """Fence initial grants, leaving ordinary preview changes separately reviewed.

    When either backend grant changes, both must be pure creations and every
    other managed preview resource must be unchanged. It cannot delete/change
    these grants, create preview bake resources or smuggle other drift through
    a custody backend bootstrap. Normal preview plans after bootstrap retain
    their existing independent review requirement.
    """
    if (
        plan.get("errored")
        or plan.get("complete") is False
        or plan.get("deferred_changes")
    ):
        raise ValueError("incomplete plan")
    changes = [
        resource
        for resource in plan.get("resource_changes", [])
        if resource["mode"] == "managed" and resource["change"]["actions"] != ["no-op"]
    ]
    if not any(resource["address"] in BACKEND_GRANTS for resource in changes):
        return 0
    if {resource["address"] for resource in changes} != set(BACKEND_GRANTS):
        raise ValueError("unrelated backend bootstrap drift")
    for resource in changes:
        if resource["change"]["actions"] != ["create"]:
            raise ValueError("backend grants may only be added")
        after = resource["change"]["after"]
        role, suffix = BACKEND_GRANTS[resource["address"]]
        expression = (
            "resource.name == "
            f'"projects/_/buckets/ditto-app-dev-tfstate/objects/gcp-collector-custody/{suffix}"'
        )
        if (
            after["bucket"] != "ditto-app-dev-tfstate"
            or after["role"] != role
            or after["member"]
            != (
                "serviceAccount:github-actions-terraform-plan@"
                "ditto-app-dev.iam.gserviceaccount.com"
            )
            or len(after["condition"]) != 1
            or after["condition"][0]["expression"] != expression
        ):
            raise ValueError("unbounded backend authority")
    return 2


def main() -> int:
    try:
        backend = len(sys.argv) == 3 and sys.argv[1] == "--backend-bootstrap"
        if len(sys.argv) != 2 and not backend:
            raise ValueError("one private file required")
        plan = json.loads(Path(sys.argv[-1]).read_text())
        count = validate_backend_bootstrap(plan) if backend else validate(plan)
    except (OSError, ValueError, KeyError, TypeError):
        print("Custody plan scope refused; inspect private evidence.", file=sys.stderr)
        return 2
    label = "Backend bootstrap scope" if backend else "Custody-only plan scope"
    print(f"{label} verified: {count} managed resource addresses.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
