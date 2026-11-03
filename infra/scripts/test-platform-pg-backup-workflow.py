#!/usr/bin/env python3
"""Credential-free workflow and two-resource snapshot-plan guards."""

import copy
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "snapshot_plan", Path(__file__).with_name("check-postgres-snapshot-plan.py")
)
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


def snapshot_rows():
    policy = {
        "name": "ditto-pg-platform-daily",
        "project": "ditto-app-dev",
        "region": "us-central1",
        "snapshot_schedule_policy": [
            {
                "retention_policy": [
                    {
                        "max_retention_days": 14,
                        "on_source_disk_delete": "KEEP_AUTO_SNAPSHOTS",
                    }
                ],
                "schedule": [
                    {
                        "daily_schedule": [{"days_in_cycle": 1, "start_time": "06:00"}],
                        "hourly_schedule": [],
                        "weekly_schedule": [],
                    }
                ],
                "snapshot_properties": [
                    {"guest_flush": False, "storage_locations": ["us"]}
                ],
            }
        ],
    }
    attachment = {
        "name": "ditto-pg-platform-daily",
        "project": "ditto-app-dev",
        "disk": "ditto-pg-platform",
        "zone": "us-central1-a",
    }
    return [
        {
            "address": address,
            "change": {
                "actions": ["create"],
                "after": policy
                if address.startswith("google_compute_resource_policy.")
                else attachment,
            },
        }
        for address in plan.EXPECTED
    ]


class WorkflowTest(unittest.TestCase):
    def test_snapshot_plan_has_exactly_two_creates(self):
        changes = snapshot_rows()
        plan.check({"resource_changes": changes})
        for actions in (["delete", "create"], ["update"]):
            broken = [
                {"address": address, "change": {"actions": actions}}
                for address in plan.EXPECTED
            ]
            with self.assertRaises(ValueError):
                plan.check({"resource_changes": broken})
        with self.assertRaises(ValueError):
            plan.check(
                {
                    "resource_changes": changes
                    + [
                        {
                            "address": "module.pg_vm.google_compute_instance.this",
                            "change": {"actions": ["update"]},
                        },
                    ]
                }
            )

    def test_snapshot_fence_uses_plan_resources_even_for_parent_module_targets(self):
        valid = {"resource_changes": snapshot_rows()}
        plan.check_when_changed(valid)
        valid["resource_changes"].append(
            {
                "address": "module.pg_vm.google_compute_instance.this",
                "change": {"actions": ["update"]},
            }
        )
        with self.assertRaises(ValueError):
            plan.check_when_changed(valid)
        plan.check_when_changed(
            {
                "resource_changes": [
                    {"address": "unrelated.thing", "change": {"actions": ["create"]}}
                ]
            }
        )

    def test_snapshot_fence_rejects_renamed_or_reindexed_resources(self):
        for address in (
            "module.pg_vm.google_compute_disk_resource_policy_attachment.boot_snapshot[1]",
            "google_compute_resource_policy.renamed_daily",
            "module.nested.google_compute_resource_policy.renamed_daily",
        ):
            with self.subTest(address=address), self.assertRaises(ValueError):
                plan.check_when_changed(
                    {
                        "resource_changes": [
                            {"address": address, "change": {"actions": ["create"]}}
                        ]
                    }
                )

    def test_attachment_can_finish_after_policy_was_created(self):
        rows = snapshot_rows()
        policy = next(
            row
            for row in rows
            if row["address"].startswith("google_compute_resource_policy.")
        )
        policy["change"]["actions"] = ["no-op"]
        plan.check({"resource_changes": rows})
        for field, value in (("name", "other"), ("project", "other")):
            broken = copy.deepcopy(rows)
            broken[0]["change"]["after"][field] = value
            with self.assertRaises(ValueError):
                plan.check({"resource_changes": broken})
        attached = next(row for row in rows if row["address"].startswith("module."))
        attached["change"]["after"]["name"] = (
            "projects/ditto-app-dev/regions/us-central1/resourcePolicies/ditto-pg-platform-daily"
        )
        with self.assertRaises(ValueError):
            plan.check({"resource_changes": rows})

    def test_snapshot_fence_requires_the_change_list(self):
        with self.assertRaises(KeyError):
            plan.check_when_changed({})
        for invalid in (None, {}, ""):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                plan.check_when_changed({"resource_changes": invalid})
        plan.check_when_changed({"resource_changes": []})

    def test_workflow_is_main_only_and_restorer_has_only_reader_credentials(self):
        workflow = (
            ROOT / ".github/workflows/platform-pg-restore-drill.yml"
        ).read_text()
        for marker in (
            "17 8 * * 0",
            "environment: prod",
            "github.ref == 'refs/heads/main'",
            "GCP_PG_RESTORE_SERVICE_ACCOUNT",
            "platform-pg-backup-age-identity",
            "platform-pg-backup-reader-access-key-id",
            "if: always()",
            "shred -u",
            "--require-hashes",
            "--only-binary=:all:",
            'bin/python" -I',
            "persist-credentials: false",
        ):
            self.assertIn(marker, workflow)
        self.assertNotIn("platform-pg-backup-hippius-access-key-id", workflow)
        self.assertNotIn("platform-pg-backup-hippius-secret-access-key", workflow)
        self.assertNotIn("upload-artifact", workflow)
        self.assertNotIn("GCP_PLATFORM_DEPLOY_SERVICE_ACCOUNT", workflow)
        self.assertNotIn("--project apps/platform", workflow)

    def test_db_vm_never_receives_private_age_identity(self):
        role = ROOT / "infra/ansible/roles/postgres_backup"
        secrets = (role / "defaults/main.yml").read_text()
        self.assertNotIn("platform-pg-backup-age-identity", secrets)
        writer = (role / "files/backup.py").read_text()
        self.assertNotIn("age-identity", writer)
        self.assertIn("dir=staging_base", writer)


if __name__ == "__main__":
    unittest.main()
