#!/usr/bin/env python3
"""Credential-free workflow and two-resource snapshot-plan guards."""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "snapshot_plan", Path(__file__).with_name("check-postgres-snapshot-plan.py")
)
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


class WorkflowTest(unittest.TestCase):
    def test_snapshot_plan_has_exactly_two_creates(self):
        changes = [
            {"address": address, "change": {"actions": ["create"]}}
            for address in plan.EXPECTED
        ]
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
            "--locked",
            "persist-credentials: false",
        ):
            self.assertIn(marker, workflow)
        self.assertNotIn("platform-pg-backup-hippius-access-key-id", workflow)
        self.assertNotIn("upload-artifact", workflow)
        self.assertNotIn("GCP_PLATFORM_DEPLOY_SERVICE_ACCOUNT", workflow)

    def test_db_vm_never_receives_private_age_identity(self):
        role = ROOT / "infra/ansible/roles/postgres_backup"
        secrets = (role / "defaults/main.yml").read_text()
        self.assertNotIn("platform-pg-backup-age-identity", secrets)
        writer = (role / "files/backup.py").read_text()
        self.assertNotIn("age-identity", writer)
        self.assertIn("dir=staging_base", writer)


if __name__ == "__main__":
    unittest.main()
