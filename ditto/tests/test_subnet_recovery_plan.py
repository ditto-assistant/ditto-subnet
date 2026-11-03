"""Fail closed on broad grants, wrong projects and injected recovery resources."""

import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location(
    "recovery_plan", ROOT / "infra/scripts/check-subnet-recovery-plan.py"
)
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def plan(phase):
    rows = []
    for address, values in guard.expectations(phase).items():
        after = {"project": guard.PROJECT, **values}
        kind = address.split(".", 1)[0]
        if kind == "google_project_iam_custom_role":
            after["permissions"] = sorted(
                guard.READ | (guard.WRITE if address.endswith(".apply") else set())
            )
        if kind == "google_iam_workload_identity_pool_provider":
            after.update(
                {
                    "attribute_condition": " && ".join(
                        sorted(
                            guard.CI_CONDITIONS
                            if phase == "bootstrap"
                            else guard.CONDITIONS
                        )
                    ),
                    "attribute_mapping": {"google.subject": "assertion.sub"},
                    "oidc": [
                        {
                            "issuer_uri": "https://token.actions.githubusercontent.com",
                            "allowed_audiences": [],
                        }
                    ],
                }
            )
        rows.append(
            {
                "address": address,
                "type": kind,
                "mode": "managed",
                "change": {"actions": ["create"], "after": after},
            }
        )
    return {"resource_changes": rows, "complete": True}


@pytest.mark.parametrize("phase", ["bootstrap", "recovery"])
def test_complete_isolated_plan_is_allowed(phase):
    guard.check(plan(phase), phase)


@pytest.mark.parametrize("phase", ["bootstrap", "recovery"])
def test_extra_resource_replacement_and_missing_resource_are_rejected(phase):
    original = plan(phase)
    extra = copy.deepcopy(original)
    extra["resource_changes"].append(
        {
            "address": "google_compute_instance.injected",
            "type": "google_compute_instance",
            "mode": "managed",
            "change": {"actions": ["create"], "after": {"project": guard.PROJECT}},
        }
    )
    replaced = copy.deepcopy(original)
    replaced["resource_changes"][0]["change"]["actions"] = ["delete", "create"]
    wrong = copy.deepcopy(original)
    wrong["resource_changes"][0]["change"]["after"]["project"] = "ditto-app-dev"
    incomplete = copy.deepcopy(original)
    incomplete["resource_changes"].pop()
    for broken in (extra, replaced, wrong, incomplete):
        with pytest.raises(ValueError):
            guard.check(broken, phase)


def test_ci_secret_payload_permission_is_rejected():
    broken = plan("bootstrap")
    row = next(
        row
        for row in broken["resource_changes"]
        if row["address"] == "google_project_iam_custom_role.apply"
    )
    row["change"]["after"]["permissions"].append("secretmanager.versions.access")
    with pytest.raises(ValueError):
        guard.check(broken, "bootstrap")


def test_private_key_cannot_be_granted_to_platform():
    broken = plan("recovery")
    row = next(
        row
        for row in broken["resource_changes"]
        if row["address"] == "google_secret_manager_secret_iam_member.pg_restore["
        '"platform-pg-backup-age-identity"]'
    )
    row["change"]["after"]["member"] = f"serviceAccount:{guard.API}"
    with pytest.raises(ValueError):
        guard.check(broken, "recovery")


def test_restore_workflow_ref_cannot_be_broadened():
    broken = plan("recovery")
    row = next(
        row
        for row in broken["resource_changes"]
        if row["type"] == "google_iam_workload_identity_pool_provider"
    )
    row["change"]["after"]["attribute_condition"] = (
        "assertion.repository_id == '1224630318'"
    )
    with pytest.raises(ValueError):
        guard.check(broken, "recovery")


def test_provider_null_defaults_are_allowed_but_custom_issuer_keys_are_not():
    original = plan("recovery")
    row = next(
        row
        for row in original["resource_changes"]
        if row["type"] == "google_iam_workload_identity_pool_provider"
    )
    oidc = row["change"]["after"]["oidc"][0]
    oidc.update(allowed_audiences=None, jwks_json=None)
    guard.check(original, "recovery")
    for key, value in (
        ("issuer_uri", "https://untrusted.example.com"),
        ("allowed_audiences", ["untrusted"]),
        ("jwks_json", '{"keys": []}'),
    ):
        saved = oidc[key]
        oidc[key] = value
        with pytest.raises(ValueError):
            guard.check(original, "recovery")
        oidc[key] = saved


def migrated_plan():
    result = plan("bootstrap")
    for mode in ("plan", "apply"):
        row = next(
            r
            for r in result["resource_changes"]
            if r["address"] == f"google_project_iam_member.{mode}"
        )
        row["change"].update(
            actions=["delete", "create"],
            before={
                "project": guard.PROJECT,
                "role": row["change"]["after"]["role"],
                "member": (
                    f"serviceAccount:github-actions-terraform-{mode}"
                    "@ditto-app-dev.iam.gserviceaccount.com"
                ),
                "condition": [],
            },
        )
    return result


def test_exact_old_ci_delegations_can_migrate_together():
    guard.check(migrated_plan(), "bootstrap")


@pytest.mark.parametrize(
    "field,value",
    [
        ("project", "ditto-app-dev"),
        ("role", "roles/owner"),
        ("member", f"serviceAccount:{guard.API}"),
        ("condition", [{"expression": "true"}]),
    ],
)
def test_migration_rejects_unexpected_old_authority(field, value):
    broken = migrated_plan()
    row = next(
        r
        for r in broken["resource_changes"]
        if r["address"] == "google_project_iam_member.apply"
    )
    row["change"]["before"][field] = value
    with pytest.raises(ValueError):
        guard.check(broken, "bootstrap")


def test_partial_migration_is_rejected():
    broken = migrated_plan()
    row = next(
        r
        for r in broken["resource_changes"]
        if r["address"] == "google_project_iam_member.plan"
    )
    row["change"]["actions"] = ["create"]
    with pytest.raises(ValueError):
        guard.check(broken, "bootstrap")


@pytest.mark.parametrize(
    "address,field,value",
    [
        (
            "google_project_iam_member.apply",
            "member",
            "serviceAccount:github-actions-terraform-apply@ditto-app-dev.iam.gserviceaccount.com",
        ),
        ("google_storage_bucket.recovery_state", "name", "ditto-app-dev-tfstate"),
        ("google_storage_bucket.recovery_state", "project", "ditto-app-dev"),
        (
            "google_storage_bucket.recovery_state",
            "public_access_prevention",
            "inherited",
        ),
        ("google_storage_bucket.recovery_state", "versioning", [{"enabled": False}]),
        ("google_storage_bucket.recovery_state", "uniform_bucket_level_access", False),
        (
            "google_iam_workload_identity_pool_provider.terraform",
            "attribute_condition",
            "assertion.repository_id == '1224630318'",
        ),
        (
            'google_service_account_iam_member.terraform_wif["apply"]',
            "member",
            guard.PRINCIPAL,
        ),
    ],
)
def test_custody_cannot_reuse_shared_infrastructure(address, field, value):
    broken = plan("bootstrap")
    row = next(r for r in broken["resource_changes"] if r["address"] == address)
    row["change"]["after"][field] = value
    with pytest.raises(ValueError):
        guard.check(broken, "bootstrap")
