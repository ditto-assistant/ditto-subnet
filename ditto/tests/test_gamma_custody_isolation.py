"""No-cloud controls for isolated project selection and deployment authority."""

from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "gamma_scope", ROOT / "scripts/check-collector-custody-plan.py"
)
assert spec and spec.loader
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


def plan():
    return {
        "complete": True,
        "variables": {
            name: {"value": value}
            for name, value in {
                "project": "sn118-gamma-custody",
                "region": "us-central1",
                "zone": "us-central1-a",
                "enable_treasury_host": False,
                "collector_custody_revision": "a" * 40,
                "collector_custody_offline_addresses": [
                    "5" + char * 47 for char in "ABCDE"
                ],
            }.items()
        },
        "planned_values": {"root_module": {"resources": []}},
        "resource_changes": [],
    }


class Isolation(unittest.TestCase):
    @staticmethod
    def phase_plan(new, old=None):
        result = plan()
        result["variables"]["enable_collector_custody"] = {"value": True}
        result["variables"]["collector_custody_phases"] = {
            "value": {"registration": new, "transfer": new}
        }

        def hosts(phase):
            return [
                {
                    "address": f'google_compute_instance.collector_delegate["{role}"]',
                    "values": {
                        "project": "sn118-gamma-custody",
                        "tags": [f"collector-{role}-{phase}"],
                    },
                }
                for role in ("registration", "transfer")
            ]

        result["planned_values"]["root_module"]["resources"] = hosts(new)
        if old is not None:
            result["prior_state"] = {
                "values": {"root_module": {"resources": hosts(old)}}
            }
        return result

    def test_first_apply_cannot_skip_bootstrap_or_reverse_phase(self):
        for new, old, accepted in (
            ("bootstrap", None, True),
            ("armed", None, False),
            ("sealed", None, False),
            ("armed", "bootstrap", True),
            ("locked", "armed", True),
            ("sealed", "locked", True),
            ("sealed", "bootstrap", False),
            ("armed", "sealed", False),
            ("bootstrap", "locked", False),
            ("sealed", "sealed", True),
        ):
            with self.subTest(new=new, old=old):
                if accepted:
                    scope.validate(
                        self.phase_plan(new, old), project="sn118-gamma-custody"
                    )
                else:
                    with self.assertRaises(ValueError):
                        scope.validate(
                            self.phase_plan(new, old), project="sn118-gamma-custody"
                        )

    def test_phase_guard_refuses_missing_host_or_old_project(self):
        for prior in (False, True):
            altered = self.phase_plan("armed", "bootstrap")
            items = (
                altered["prior_state"]["values"]["root_module"]["resources"]
                if prior
                else altered["planned_values"]["root_module"]["resources"]
            )
            items[0]["values"]["project"] = "ditto-app-dev"
            with self.assertRaises(ValueError):
                scope.validate(altered, project="sn118-gamma-custody")

    def test_old_scope_does_not_silently_accept_new_project(self):
        with self.assertRaises(ValueError):
            scope.validate(plan())
        self.assertEqual(scope.validate(plan(), project="sn118-gamma-custody"), 0)

    def test_new_scope_rejects_old_project_and_other_resources(self):
        original = plan()
        original["variables"]["project"]["value"] = "ditto-app-dev"
        with self.assertRaises(ValueError):
            scope.validate(original, project="sn118-gamma-custody")
        for address in (
            "google_project.other",
            "google_secret_manager_secret_version.key",
            "google_compute_instance.platform",
        ):
            altered = copy.deepcopy(plan())
            altered["resource_changes"] = [{"address": address, "mode": "managed"}]
            with self.assertRaises(ValueError):
                scope.validate(altered, project="sn118-gamma-custody")

    def test_state_and_live_legacy_root_stay_separate(self):
        root = ROOT / "infra/terraform/stacks/gcp-gamma-custody"
        self.assertIn(
            'bucket = "sn118-gamma-custody-tfstate"', (root / "backend.tf").read_text()
        )
        self.assertIn('prefix = "gcp-gamma-custody"', (root / "backend.tf").read_text())
        self.assertTrue((root / "collector-custody.tf").is_symlink())
        self.assertIn(
            'project = "ditto-app-dev"',
            (
                ROOT / "infra/terraform/stacks/gcp-collector-custody/prod.auto.tfvars"
            ).read_text(),
        )

    def test_project_deployment_roles_have_no_payload_or_version_writer(self):
        bootstrap = (
            ROOT / "infra/terraform/stacks/gcp-gamma-custody-bootstrap/main.tf"
        ).read_text()
        self.assertNotIn('"secretmanager.versions.access"', bootstrap)
        self.assertNotIn('"secretmanager.versions.add"', bootstrap)
        self.assertNotIn('"roles/secretmanager.secretAccessor"', bootstrap)
        self.assertNotIn('"roles/owner"', bootstrap)
        self.assertIn("assertion.actor_id == '6766068'", bootstrap)
        self.assertIn(
            "assertion.workflow_ref == 'ditto-assistant/ditto-subnet/.github/workflows/infra-plan-apply.yml@refs/heads/main'",
            bootstrap,
        )
        self.assertIn('rules { enforce = "TRUE" }', bootstrap)

    def test_existing_identity_is_not_used_for_gamma_auth(self):
        workflow = (ROOT / ".github/workflows/infra-plan-apply.yml").read_text()
        for role in ("plan", "apply"):
            self.assertIn(
                f"gamma-custody-tf-{role}@sn118-gamma-custody.iam.gserviceaccount.com",
                workflow,
            )
        self.assertIn("scope+=(--gamma-project)", workflow)
        self.assertIn("PLAN_BUCKET=sn118-gamma-custody-tfstate", workflow)
        self.assertIn("environment: infra-apply", workflow)
        self.assertIn(
            'test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"', workflow
        )


if __name__ == "__main__":
    unittest.main()
