from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
SCRIPT = (
    ROOT
    / "infra"
    / "terraform"
    / "stacks"
    / "gcp-v13-private-bootstrap"
    / "check-plan.py"
)


def _checker():
    spec = importlib.util.spec_from_file_location("v13_bootstrap_plan_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check


def _plan():
    approval = {
        "project_id": "ditto-v13-private-verifier",
        "state_bucket": "ditto-v13-private-verifier-tfstate",
        "organization_id": "123456789",
        "billing_account_id": "ABCDEF-123456-ABCDEF",
        "state_custodian": "user:approved@example.com",
    }
    changes = [
        (
            "google_project.verifier[0]",
            {
                "project_id": approval["project_id"],
                "name": "Ditto V13 private verifier",
                "org_id": approval["organization_id"],
                "billing_account": approval["billing_account_id"],
                "auto_create_network": False,
            },
        ),
        (
            "google_project_service.storage[0]",
            {
                "project": approval["project_id"],
                "service": "storage.googleapis.com",
                "disable_on_destroy": False,
            },
        ),
        (
            "google_storage_bucket.state[0]",
            {
                "project": approval["project_id"],
                "name": approval["state_bucket"],
                "location": "US-CENTRAL1",
                "storage_class": "STANDARD",
                "uniform_bucket_level_access": True,
                "public_access_prevention": "enforced",
                "force_destroy": False,
                "versioning": [{"enabled": True}],
            },
        ),
        (
            'google_storage_bucket_iam_member.state_custodian["user:approved@example.com"]',
            {
                "bucket": approval["state_bucket"],
                "role": "roles/storage.objectAdmin",
                "member": approval["state_custodian"],
            },
        ),
    ]
    plan = {
        "resource_changes": [
            {
                "address": address,
                "change": {"actions": ["create"], "before": None, "after": after},
            }
            for address, after in changes
        ]
    }
    return plan, approval


def test_exact_plan_values_match_independent_approval() -> None:
    plan, approval = _plan()
    assert _checker()(plan, approval)


def test_plan_rejects_different_iam_parent_billing_and_scope() -> None:
    check = _checker()
    plan, approval = _plan()
    for index, field, wrong in (
        (0, "org_id", "987654321"),
        (0, "billing_account", "111111-222222-333333"),
        (0, "project_id", "another-project"),
        (2, "name", "another-bucket"),
        (3, "member", "user:wrong@example.com"),
        (3, "role", "roles/storage.admin"),
    ):
        changed = copy.deepcopy(plan)
        changed["resource_changes"][index]["change"]["after"][field] = wrong
        assert not check(changed, approval)
    changed = copy.deepcopy(plan)
    changed["resource_changes"][3]["change"]["actions"] = ["delete", "create"]
    assert not check(changed, approval)
    changed = copy.deepcopy(plan)
    changed["resource_changes"].append(copy.deepcopy(changed["resource_changes"][3]))
    assert not check(changed, approval)


def test_dispatch_identifiers_are_env_values_in_every_apply_shell_step() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "infra-plan-apply.yml").read_text()
    )
    for step in workflow["jobs"]["apply"]["steps"]:
        script = step.get("run", "")
        for name in ("plan_sha", "plan_run_id", "plan_checksum"):
            assert "${{ inputs." + name + " }}" not in script
    validation = next(
        step
        for step in workflow["jobs"]["apply"]["steps"]
        if step.get("name") == "Validate immutable plan identity"
    )
    assert validation["env"]["PLAN_SHA"] == "${{ inputs.plan_sha }}"
    assert validation["env"]["PLAN_RUN_ID"] == "${{ inputs.plan_run_id }}"
    assert validation["env"]["PLAN_CHECKSUM"] == "${{ inputs.plan_checksum }}"
    assert '[[ "$PLAN_SHA" =~ ^[0-9a-f]{40}$ ]]' in validation["run"]
    assert '[[ "$PLAN_RUN_ID" =~ ^[0-9]+$ ]]' in validation["run"]
    assert '[[ "$PLAN_CHECKSUM" =~ ^[0-9a-f]{64}$ ]]' in validation["run"]
