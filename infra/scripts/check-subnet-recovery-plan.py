#!/usr/bin/env python3
"""Fence isolated recovery plans without printing resource or secret values."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = "ditto-subnet"
BILLING = "01279D-184F4C-3102C7"
SECRETS = {
    "platform-pg-backup-hippius-access-key-id",
    "platform-pg-backup-hippius-secret-access-key",
    "platform-pg-backup-age-recipient",
    "platform-pg-backup-age-identity",
    "platform-pg-backup-reader-access-key-id",
    "platform-pg-backup-reader-secret-access-key",
}
READERS = {
    "platform-pg-backup-reader-access-key-id",
    "platform-pg-backup-reader-secret-access-key",
}
SERVICES = {
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "storage.googleapis.com",
}
STATE_BUCKET = "ditto-subnet-recovery-tfstate"
CI_POOL = (
    "projects/286408627661/locations/global/workloadIdentityPools/subnet-recovery-infra"
)
CI_ACCOUNTS = {
    mode: f"subnet-recovery-tf-{mode}@ditto-subnet.iam.gserviceaccount.com"
    for mode in ("plan", "apply")
}
CI_SUBJECTS = {
    mode: f"repo:ditto-assistant/ditto-subnet:environment:infra-{mode}"
    for mode in ("plan", "apply")
}
CI_CONDITIONS = {
    "assertion.repository_id == '1224630318'",
    "assertion.repository_owner_id == '148669063'",
    "assertion.ref == 'refs/heads/main'",
    "assertion.event_name == 'workflow_dispatch'",
    "assertion.workflow_ref == 'ditto-assistant/ditto-subnet/"
    ".github/workflows/infra-plan-apply.yml@refs/heads/main'",
    f"assertion.sub in ['{CI_SUBJECTS['plan']}', '{CI_SUBJECTS['apply']}']",
}
RESTORE = "github-platform-pg-restore@ditto-subnet.iam.gserviceaccount.com"
API = "ditto-platform-api@ditto-app-dev.iam.gserviceaccount.com"
SUBJECT = "repo:ditto-assistant/ditto-subnet:environment:prod"
PRINCIPAL = (
    "principal://iam.googleapis.com/projects/286408627661/locations/global/"
    f"workloadIdentityPools/platform-pg-restore/subject/{SUBJECT}"
)
CONDITIONS = {
    "assertion.repository_id == '1224630318'",
    "assertion.repository_owner_id == '148669063'",
    f"assertion.sub == '{SUBJECT}'",
    "assertion.ref == 'refs/heads/main'",
    "assertion.event_name in ['schedule', 'workflow_dispatch']",
    "assertion.workflow_ref == 'ditto-assistant/ditto-subnet/"
    ".github/workflows/platform-pg-restore-drill.yml@refs/heads/main'",
}
# Exact custom role permissions; administration remains a trusted control
# plane, but neither CI identity receives payload access or token minting.
READ = {
    "resourcemanager.projects.get",
    "serviceusage.services.get",
    "serviceusage.services.list",
    "serviceusage.services.use",
    "secretmanager.secrets.get",
    "secretmanager.secrets.list",
    "secretmanager.secrets.getIamPolicy",
    "iam.serviceAccounts.get",
    "iam.serviceAccounts.list",
    "iam.serviceAccounts.getIamPolicy",
    "iam.googleapis.com/workloadIdentityPools.get",
    "iam.googleapis.com/workloadIdentityPools.list",
    "iam.googleapis.com/workloadIdentityPoolProviders.get",
    "iam.googleapis.com/workloadIdentityPoolProviders.list",
    "iam.googleapis.com/workloadIdentityPools.getIamPolicy",
}
WRITE = {
    "secretmanager.secrets.create",
    "secretmanager.secrets.update",
    "secretmanager.secrets.setIamPolicy",
    "iam.serviceAccounts.create",
    "iam.serviceAccounts.update",
    "iam.serviceAccounts.setIamPolicy",
    "iam.googleapis.com/workloadIdentityPools.create",
    "iam.googleapis.com/workloadIdentityPools.update",
    "iam.googleapis.com/workloadIdentityPoolProviders.create",
    "iam.googleapis.com/workloadIdentityPoolProviders.update",
}


def expectations(phase):
    if phase == "bootstrap":
        result = {"google_billing_project_info.subnet": {"billing_account": BILLING}}
        result.update(
            {
                f'google_project_service.recovery["{service}"]': {"service": service}
                for service in SERVICES
            }
        )
        for mode in ("plan", "apply"):
            role = f"subnetRecoveryTerraform{mode.title()}"
            result[f"google_project_iam_custom_role.{mode}"] = {"role_id": role}
            result[f"google_project_iam_member.{mode}"] = {
                "role": f"projects/{PROJECT}/roles/{role}",
                "member": f"serviceAccount:{CI_ACCOUNTS[mode]}",
            }
            result[f'google_service_account.terraform["{mode}"]'] = {
                "account_id": f"subnet-recovery-tf-{mode}"
            }
            result[f'google_service_account_iam_member.terraform_wif["{mode}"]'] = {
                "service_account_id": (
                    f"projects/{PROJECT}/serviceAccounts/{CI_ACCOUNTS[mode]}"
                ),
                "role": "roles/iam.workloadIdentityUser",
                "member": f"principal://iam.googleapis.com/{CI_POOL}/subject/{CI_SUBJECTS[mode]}",
            }
            result[f'google_storage_bucket_iam_member.terraform_state["{mode}"]'] = {
                "bucket": STATE_BUCKET,
                "role": "roles/storage.objectAdmin",
                "member": f"serviceAccount:{CI_ACCOUNTS[mode]}",
            }
        result.update(
            {
                "google_iam_workload_identity_pool.terraform": {
                    "workload_identity_pool_id": "subnet-recovery-infra"
                },
                "google_iam_workload_identity_pool_provider.terraform": {
                    "workload_identity_pool_id": "subnet-recovery-infra",
                    "workload_identity_pool_provider_id": "github",
                },
                "google_storage_bucket.recovery_state": {
                    "name": STATE_BUCKET,
                    "location": "US",
                    "uniform_bucket_level_access": True,
                    "public_access_prevention": "enforced",
                    "force_destroy": False,
                    "versioning": [{"enabled": True}],
                },
            }
        )
        return result
    if phase != "recovery":
        raise ValueError("unknown recovery phase")
    result = {
        f'google_secret_manager_secret.pg_backup["{secret}"]': {"secret_id": secret}
        for secret in SECRETS
    }
    for owner, member, names in (
        ("pg_backup_platform_reader", API, READERS),
        ("pg_restore", RESTORE, READERS | {"platform-pg-backup-age-identity"}),
    ):
        for secret in names:
            result[f'google_secret_manager_secret_iam_member.{owner}["{secret}"]'] = {
                "secret_id": secret,
                "role": "roles/secretmanager.secretAccessor",
                "member": f"serviceAccount:{member}",
            }
    result.update(
        {
            "google_service_account.pg_restore": {
                "account_id": "github-platform-pg-restore"
            },
            "google_iam_workload_identity_pool.pg_restore": {
                "workload_identity_pool_id": "platform-pg-restore"
            },
            "google_iam_workload_identity_pool_provider.pg_restore": {
                "workload_identity_pool_id": "platform-pg-restore",
                "workload_identity_pool_provider_id": "github",
            },
            "google_service_account_iam_member.pg_restore_wif": {
                "service_account_id": f"projects/{PROJECT}/serviceAccounts/{RESTORE}",
                "role": "roles/iam.workloadIdentityUser",
                "member": PRINCIPAL,
            },
        }
    )
    return result


def check(plan, phase):
    if (
        plan.get("errored")
        or plan.get("complete") is False
        or plan.get("deferred_changes")
    ):
        raise ValueError("incomplete recovery plan")
    expected = expectations(phase)
    observed, changes, replacements = set(), 0, 0
    for row in plan.get("resource_changes", []):
        address, change = row["address"], row["change"]
        if row.get("mode") == "data":
            if phase != "recovery" or address != "data.google_project.this":
                raise ValueError("unexpected recovery data source")
            if change["after"]["project_id"] != PROJECT:
                raise ValueError("wrong recovery project")
            continue
        if address not in expected or address in observed:
            raise ValueError("unexpected recovery resource")
        observed.add(address)
        if row.get("type") != address.split(".", 1)[0]:
            raise ValueError("unexpected recovery resource type")
        if change["actions"] in (["delete", "create"], ["create", "delete"]):
            if phase != "bootstrap" or address not in {
                "google_project_iam_member.plan",
                "google_project_iam_member.apply",
            }:
                raise ValueError("destructive recovery plan")
            mode = address.rsplit(".", 1)[1]
            before = change["before"]
            if (
                before.get("project") != PROJECT
                or before.get("role") != expected[address]["role"]
                or before.get("member")
                != (
                    f"serviceAccount:github-actions-terraform-{mode}"
                    "@ditto-app-dev.iam.gserviceaccount.com"
                )
                or before.get("condition") not in (None, [])
            ):
                raise ValueError("unexpected old CI delegation")
            replacements += 1
        elif change["actions"] not in (["no-op"], ["create"], ["update"]):
            raise ValueError("destructive recovery plan")
        after = change["after"]
        if (
            row["type"]
            not in {
                "google_service_account_iam_member",
                "google_storage_bucket_iam_member",
            }
            and after.get("project") != PROJECT
        ):
            raise ValueError("wrong recovery project")
        for key, value in expected[address].items():
            if after.get(key) != value:
                raise ValueError("recovery scope or principal differs")
        if row["type"].endswith("_iam_member") and after.get("condition") not in (
            None,
            [],
        ):
            raise ValueError("unexpected recovery IAM condition")
        if row["type"] == "google_project_iam_custom_role":
            permissions = READ | (WRITE if address.endswith(".apply") else set())
            if set(after["permissions"]) != permissions:
                raise ValueError("unexpected bootstrap authority")
        if row["type"] == "google_iam_workload_identity_pool_provider":
            conditions = CI_CONDITIONS if phase == "bootstrap" else CONDITIONS
            if set(after["attribute_condition"].split(" && ")) != conditions:
                raise ValueError("unexpected restore federation")
            if after["attribute_mapping"] != {"google.subject": "assertion.sub"}:
                raise ValueError("unexpected federation mapping")
            oidc = after["oidc"]
            if (
                len(oidc) != 1
                or oidc[0].get("issuer_uri")
                != "https://token.actions.githubusercontent.com"
                or oidc[0].get("allowed_audiences") not in (None, [])
                or oidc[0].get("jwks_json") not in (None, "")
            ):
                raise ValueError("unexpected federation issuer")
        changes += change["actions"] != ["no-op"]
    if observed != set(expected):
        raise ValueError("incomplete recovery resources")
    if replacements not in (0, 2):
        raise ValueError("both old CI delegations must migrate together")
    detail = "exactly two old CI grant replacements" if replacements else "no deletes"
    print(f"subnet {phase} plan: {changes} changes; no compute or payloads; {detail}")


if __name__ == "__main__":
    try:
        check(json.loads(Path(sys.argv[2]).read_text()), sys.argv[1])
    except (ValueError, KeyError, TypeError, IndexError):
        print(
            "subnet recovery plan rejected; inspect the private plan", file=sys.stderr
        )
        raise SystemExit(1) from None
