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
    "google_compute_firewall.collector_runtime_rpc",
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
MAILBOX = {
    "google_project_service.manual_pubsub[0]",
    *{
        f'google_pubsub_{kind}.manual["{name}"]'
        for kind in ("topic", "subscription")
        for name in ("requests", "reports")
    },
    *{
        f"google_pubsub_{kind}_iam_member.manual_{name}[0]"
        for kind in ("topic", "subscription")
        for name in ("requests", "reports")
    },
}
ALLOWED |= MAILBOX
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


def validate(plan: dict, *, project: str = "ditto-app-dev") -> int:
    if project not in ("ditto-app-dev", "sn118-gamma-custody"):
        raise ValueError("unapproved custody project")
    if (
        plan.get("errored")
        or plan.get("complete") is False
        or plan.get("deferred_changes")
    ):
        raise ValueError("incomplete plan")
    variables = {name: item["value"] for name, item in plan["variables"].items()}
    mailbox = variables.get("enable_manual_mailbox", False)
    if type(mailbox) is not bool or (
        mailbox
        and (
            project != "sn118-gamma-custody"
            or variables.get("collector_custody_phases")
            != {"registration": "sealed", "transfer": "sealed"}
            or variables.get("manual_mailbox_platform_service_account")
            != "ditto-platform-api@ditto-app-dev.iam.gserviceaccount.com"
        )
    ):
        raise ValueError("unapproved manual mailbox intent")
    if any(
        variables.get(name) != value
        for name, value in {
            "project": project,
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
        if address in MAILBOX:
            change = resource["change"]
            if "delete" in change["actions"] or not mailbox:
                raise ValueError("manual mailbox removal/unapproved update refused")
            value = change["after"]
            if value.get("project") != "sn118-gamma-custody":
                raise ValueError("mailbox project differs")
            if address == "google_project_service.manual_pubsub[0]":
                if (
                    value.get("service") != "pubsub.googleapis.com"
                    or value.get("disable_on_destroy") is not False
                ):
                    raise ValueError("mailbox service differs")
            elif "_iam_member." not in address:
                name = "requests" if '["requests"]' in address else "reports"
                if value.get("name") != f"sn118-manual-{name}":
                    raise ValueError("mailbox resource differs")
                if address.startswith("google_pubsub_topic."):
                    if value.get("message_storage_policy") != [
                        {"allowed_persistence_regions": ["us-central1"]}
                    ]:
                        raise ValueError("mailbox persistence differs")
                elif (
                    value.get("topic")
                    != f"projects/sn118-gamma-custody/topics/sn118-manual-{name}"
                    or value.get("ack_deadline_seconds") != 600
                    or value.get("message_retention_duration") != "604800s"
                    or value.get("expiration_policy") != [{"ttl": ""}]
                    or value.get("retry_policy")
                    != [{"minimum_backoff": "15s", "maximum_backoff": "300s"}]
                    or value.get("push_config")
                ):
                    raise ValueError("mailbox delivery differs")
            if "_iam_member." in address:
                platform = (
                    "serviceAccount:ditto-platform-api@"
                    "ditto-app-dev.iam.gserviceaccount.com"
                )
                signer = (
                    "serviceAccount:sn118-collector-transfer@"
                    "sn118-gamma-custody.iam.gserviceaccount.com"
                )
                kind = (
                    "topic"
                    if address.startswith("google_pubsub_topic_")
                    else "subscription"
                )
                name = "requests" if ".manual_requests" in address else "reports"
                expected = (
                    platform
                    if (kind, name)
                    in {("topic", "requests"), ("subscription", "reports")}
                    else signer
                )
                if (
                    value.get("member") != expected
                    or value.get("role")
                    != (
                        "roles/pubsub.publisher"
                        if kind == "topic"
                        else "roles/pubsub.subscriber"
                    )
                    or value.get(kind) != f"sn118-manual-{name}"
                ):
                    raise ValueError(
                        "mailbox authority is broader than the reviewed direction"
                    )
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
    rpc_enabled = variables.get("collector_runtime_rpc_egress", False)
    if type(rpc_enabled) is not bool:
        raise ValueError("unknown RPC intent")
    if rpc_enabled and (
        variables.get("enable_collector_custody") is not True
        or variables.get("collector_custody_phases")
        != {"registration": "sealed", "transfer": "sealed"}
    ):
        raise ValueError("RPC requires sealed roles")
    rpc_address = "google_compute_firewall.collector_runtime_rpc[0]"
    rpc_resources = [
        item
        for item in resources(plan["planned_values"]["root_module"])
        if item["address"] == rpc_address
    ]
    if len(rpc_resources) != int(rpc_enabled):
        raise ValueError("RPC intent and plan differ")
    if rpc_resources:
        value = rpc_resources[0]["values"]
        if (
            value.get("project") != project
            or value.get("name") != "sn118-collector-finney-rpc"
            or not re.fullmatch(
                rf"(?:https://www\.googleapis\.com/compute/v1/)?projects/{re.escape(project)}/global/networks/sn118-collector-custody",
                value.get("network", ""),
            )
            or value.get("direction") != "EGRESS"
            or value.get("priority") != 750
            or set(value.get("destination_ranges", []))
            != {"65.109.251.221/32", "65.109.254.0/32"}
            or set(value.get("target_tags", []))
            != {"collector-registration-sealed", "collector-transfer-sealed"}
            or value.get("allow") != [{"protocol": "tcp", "ports": ["443"]}]
            or value.get("deny")
            or value.get("disabled") is not False
        ):
            raise ValueError("RPC rule differs from reviewed endpoint")
    if (
        project == "sn118-gamma-custody"
        and variables.get("enable_collector_custody") is True
    ):
        phases = variables.get("collector_custody_phases")
        order = {"bootstrap": 0, "armed": 1, "locked": 2, "sealed": 3}
        if not isinstance(phases, dict) or set(phases) != {"registration", "transfer"}:
            raise ValueError("unbound phases")
        prior = {
            r["address"]: r
            for r in resources(
                plan.get("prior_state", {}).get("values", {}).get("root_module", {})
            )
        }
        planned = {
            r["address"]: r for r in resources(plan["planned_values"]["root_module"])
        }
        for role, phase in phases.items():
            if phase not in order:
                raise ValueError("unknown phase")
            address = f'google_compute_instance.collector_delegate["{role}"]'
            new_host = planned.get(address, {}).get("values", {})
            if new_host.get(
                "project"
            ) != project or f"collector-{role}-{phase}" not in new_host.get("tags", []):
                raise ValueError("phase and host differ")
            old_host = prior.get(address)
            if old_host is None:
                if phase != "bootstrap":
                    raise ValueError("first apply must bootstrap")
            else:
                old = old_host.get("values", {})
                matches = [
                    p for p in order if f"collector-{role}-{p}" in old.get("tags", [])
                ]
                if (
                    old.get("project") != project
                    or len(matches) != 1
                    or order[phase] not in (order[matches[0]], order[matches[0]] + 1)
                ):
                    raise ValueError("phase skipped or reversed")
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
        gamma = len(sys.argv) == 3 and sys.argv[1] == "--gamma-project"
        if len(sys.argv) != 2 and not backend and not gamma:
            raise ValueError("one private file required")
        plan = json.loads(Path(sys.argv[-1]).read_text())
        count = (
            validate_backend_bootstrap(plan)
            if backend
            else validate(
                plan, project="sn118-gamma-custody" if gamma else "ditto-app-dev"
            )
        )
    except (OSError, ValueError, KeyError, TypeError):
        print("Custody plan scope refused; inspect private evidence.", file=sys.stderr)
        return 2
    label = "Backend bootstrap scope" if backend else "Custody-only plan scope"
    print(f"{label} verified: {count} managed resource addresses.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
