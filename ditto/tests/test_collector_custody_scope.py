"""Scope regressions: never execute cloud calls or generate key material."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CUSTODY = ROOT / "infra/terraform/stacks/gcp-collector-custody"
CHECKER = ROOT / "scripts/check-collector-custody-plan.py"
spec = importlib.util.spec_from_file_location("custody_scope", CHECKER)
assert spec and spec.loader
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


def fixture() -> dict:
    return {
        "complete": True,
        "variables": {
            "project": {"value": "ditto-app-dev"},
            "region": {"value": "us-central1"},
            "zone": {"value": "us-central1-a"},
            "enable_treasury_host": {"value": False},
            "collector_custody_revision": {"value": "a" * 40},
            "collector_custody_offline_addresses": {
                "value": ["5" + char * 47 for char in "ABCDE"]
            },
        },
        "planned_values": {"root_module": {"resources": []}},
        "resource_changes": [
            {
                "address": 'google_compute_instance.collector_delegate["registration"]',
                "mode": "managed",
                "change": {"actions": ["create"]},
            },
            {
                "address": (
                    'google_secret_manager_secret.collector_delegate["transfer"]'
                ),
                "mode": "managed",
                "change": {"actions": ["create"]},
            },
        ],
    }


class PrivatePlanScope(unittest.TestCase):
    @staticmethod
    def rpc_fixture():
        plan = fixture()
        plan["variables"].update(
            {
                "collector_runtime_rpc_egress": {"value": True},
                "enable_collector_custody": {"value": True},
                "collector_custody_phases": {
                    "value": {"registration": "sealed", "transfer": "sealed"}
                },
            }
        )
        plan["planned_values"]["root_module"]["resources"] = [
            {
                "address": "google_compute_firewall.collector_runtime_rpc[0]",
                "values": {
                    "project": "ditto-app-dev",
                    "name": "sn118-collector-finney-rpc",
                    "direction": "EGRESS",
                    "priority": 750,
                    "target_tags": [
                        "collector-registration-sealed",
                        "collector-transfer-sealed",
                    ],
                    "destination_ranges": ["65.109.251.221/32"],
                    "allow": [{"protocol": "tcp", "ports": ["443"]}],
                    "deny": [],
                    "disabled": False,
                },
            }
        ]
        return plan

    def test_accepts_exact_sealed_finney_tls_rule(self):
        self.assertEqual(scope.validate(self.rpc_fixture()), 2)

    def test_refuses_rpc_intent_or_sealing_mismatch(self):
        for name, value in (
            ("collector_runtime_rpc_egress", False),
            ("collector_runtime_rpc_egress", "true"),
            ("enable_collector_custody", False),
            (
                "collector_custody_phases",
                {"registration": "bootstrap", "transfer": "sealed"},
            ),
        ):
            with self.subTest(name=name, value=value):
                plan = self.rpc_fixture()
                plan["variables"][name]["value"] = value
                with self.assertRaises(ValueError):
                    scope.validate(plan)
        plan = self.rpc_fixture()
        plan["planned_values"]["root_module"]["resources"] = []
        with self.assertRaises(ValueError):
            scope.validate(plan)

    def test_refuses_broadened_rpc_rule(self):
        for name, value in (
            ("destination_ranges", ["0.0.0.0/0"]),
            ("destination_ranges", ["65.109.251.221/32", "1.1.1.1/32"]),
            ("target_tags", ["collector-custody"]),
            ("allow", [{"protocol": "all", "ports": []}]),
            ("allow", [{"protocol": "tcp", "ports": ["443", "22"]}]),
            ("priority", 500),
            ("disabled", True),
            ("project", "other"),
        ):
            with self.subTest(name=name):
                plan = self.rpc_fixture()
                plan["planned_values"]["root_module"]["resources"][0]["values"][
                    name
                ] = value
                with self.assertRaises(ValueError):
                    scope.validate(plan)

    def test_accepts_only_custody_resources(self):
        self.assertEqual(scope.validate(fixture()), 2)

    def test_rejects_original_full_root_deletions_and_fleet_update(self):
        for address in (
            "google_service_account.screening_untrusted",
            "google_compute_region_instance_group_manager.screener_fleet[0]",
            "google_secret_manager_secret.backroom_platform_operator_proof",
            'google_secret_manager_secret_version.collector_delegate["transfer"]',
        ):
            with self.subTest(address=address):
                plan = fixture()
                plan["resource_changes"].append({"address": address, "mode": "managed"})
                with self.assertRaises(ValueError):
                    scope.validate(plan)

    def test_rejects_unrelated_noop_or_old_state(self):
        plan = fixture()
        plan["prior_state"] = {
            "values": {
                "root_module": {
                    "resources": [{"address": "google_service_account.image_builder"}]
                }
            }
        }
        with self.assertRaises(ValueError):
            scope.validate(plan)

    def test_rejects_nested_ownership_and_unknown_role(self):
        plan = fixture()
        plan["planned_values"]["root_module"]["child_modules"] = [
            {"address": "module.x"}
        ]
        with self.assertRaises(ValueError):
            scope.validate(plan)
        plan = fixture()
        plan["resource_changes"][0]["address"] = (
            'google_compute_instance.collector_delegate["other"]'
        )
        with self.assertRaises(ValueError):
            scope.validate(plan)

    def test_refuses_wrong_project_location_source_and_public_bindings(self):
        for name, value in (
            ("project", "some-other-project"),
            ("region", "europe-west1"),
            ("zone", "us-central1-b"),
            ("enable_treasury_host", True),
            ("collector_custody_revision", "main"),
            ("collector_custody_offline_addresses", ["5" + "A" * 47] * 5),
        ):
            with self.subTest(name=name):
                plan = fixture()
                plan["variables"][name]["value"] = value
                with self.assertRaises(ValueError):
                    scope.validate(plan)

    def test_refuses_failed_or_deferred_plans(self):
        for field, value in (
            ("complete", False),
            ("errored", True),
            ("deferred_changes", [1]),
        ):
            plan = fixture()
            plan[field] = value
            with self.assertRaises(ValueError):
                scope.validate(plan)

    def test_failure_does_not_print_private_values(self):
        plan = fixture()
        plan["variables"]["project"]["value"] = "PRIVATE_SENTINEL"
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "plan.json"
            file.write_text(json.dumps(plan))
            result = subprocess.run(
                ["python3", str(CHECKER), str(file)], capture_output=True, text=True
            )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("PRIVATE_SENTINEL", result.stdout + result.stderr)


class ProductionOwnership(unittest.TestCase):
    def test_platform_has_no_custody_owner_or_enabled_intent(self):
        old = ROOT / "infra/terraform/stacks/gcp-platform"
        for file in (*old.glob("*.tf"), old / "prod.auto.tfvars"):
            self.assertNotRegex(
                file.read_text(), r"collector_(custody|delegate|generator|reader)"
            )
        self.assertFalse((old / "files/collector-custody-startup.sh.tpl").exists())

    def test_state_and_provider_are_dedicated(self):
        self.assertIn(
            'prefix = "gcp-collector-custody"', (CUSTODY / "backend.tf").read_text()
        )
        self.assertIn('version = "6.50.0"', (CUSTODY / "versions.tf").read_text())
        self.assertNotIn("cloudflare", (CUSTODY / "providers.tf").read_text())
        self.assertNotIn("cloudflare", (CUSTODY / ".terraform.lock.hcl").read_text())

    def test_all_sixteen_live_preview_resource_definitions_are_unchanged(self):
        preview = ROOT / "infra/terraform/stacks/gcp-preview"
        baseline = json.loads(
            (
                ROOT
                / "infra/terraform/tests/collector-backend"
                / "live-resource-source-sha256.json"
            ).read_text()
        )["definitions"]
        self.assertEqual(len(baseline), 16)
        source = (preview / "main.tf").read_text()
        for address, digest in baseline.items():
            resource_type, name = address.split(".")
            pattern = (
                r'(?ms)^resource "'
                + re.escape(resource_type)
                + '" "'
                + re.escape(name)
                + r'" \{.*?^}\n'
            )
            match = re.search(pattern, source)
            self.assertIsNotNone(match, address)
            assert match
            self.assertEqual(
                hashlib.sha256(match.group().encode()).hexdigest(), digest, address
            )


class ProtectedWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / ".github/workflows/infra-plan-apply.yml").read_text()

    def test_actual_both_root_selectors_reject_targets_and_other_ceremonies(self):
        selectors = re.findall(
            r"      - name: Select exact root\n.*?        run: \|\n(.*?)(?=\n      - )",
            self.text,
            re.S,
        )
        self.assertEqual(len(selectors), 2)
        for selector in selectors:
            script = "\n".join(line[10:] for line in selector.splitlines())
            script = script.replace("${{ inputs.root }}", "gcp-collector-custody")
            for override, accepted in (
                ({}, True),
                ({"TF_TARGETS": "google_compute_instance.collector_delegate"}, False),
                ({"HOTKEY_ADMIN_PHASE": "armed"}, False),
                ({"HOTKEY_ADMIN_REVISION": "a" * 40}, False),
                ({"SCREENER_DEV_HOST_ENABLED": "true"}, False),
            ):
                with tempfile.TemporaryDirectory() as tmp:
                    env = {
                        "PATH": os.environ["PATH"],
                        "GITHUB_OUTPUT": str(Path(tmp) / "output"),
                        "TF_TARGETS": "",
                        "HOTKEY_ADMIN_PHASE": "absent",
                        "HOTKEY_ADMIN_REVISION": "",
                        "SCREENER_DEV_HOST_ENABLED": "false",
                        **override,
                    }
                    result = subprocess.run(["bash", "-e", "-c", script], env=env)
                    self.assertEqual(result.returncode == 0, accepted)

    def test_custody_steps_never_receive_platform_secret_env(self):
        for name in (
            "Create full isolated custody plan",
            "Apply the reviewed isolated custody binary plan",
        ):
            step = self.text.split(f"      - name: {name}\n", 1)[1].split(
                "\n      - ", 1
            )[0]
            self.assertIn("inputs.root == 'gcp-collector-custody'", step)
            self.assertNotIn("env:", step)
            self.assertNotIn("TF_VAR", step)
            self.assertNotIn("secrets.", step)
        for name in ("Create exact private plan", "Apply the reviewed binary plan"):
            step = self.text.split(f"      - name: {name}\n", 1)[1].split(
                "\n      - ", 1
            )[0]
            self.assertIn("inputs.root != 'gcp-collector-custody'", step)

    def test_same_sealed_binary_is_scoped_before_plan_handoff_and_apply(self):
        self.assertEqual(
            self.text.count(
                'python3 scripts/check-collector-custody-plan.py "$plan_json"'
            ),
            2,
        )
        self.assertEqual(self.text.count("--backend-bootstrap"), 2)
        self.assertLess(
            self.text.index("Verify isolated custody plan scope"),
            self.text.index("Seal plan in private GCS"),
        )
        self.assertLess(
            self.text.index("Fetch and verify exact private plan"),
            self.text.index("Verify isolated custody apply scope"),
        )
        self.assertLess(
            self.text.index("Verify isolated custody apply scope"),
            self.text.index("Apply the reviewed isolated custody binary plan"),
        )
        self.assertIn("infra-plan", self.text)
        self.assertIn("infra-apply", self.text)
        self.assertIn("sha256sum -c tfplan.sha256", self.text)
        self.assertIn(
            'test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"', self.text
        )


class BackendGrantScope(unittest.TestCase):
    @staticmethod
    def plan() -> dict:
        result: dict = {"complete": True, "resource_changes": []}
        for address, (role, suffix) in scope.BACKEND_GRANTS.items():
            result["resource_changes"].append(
                {
                    "address": address,
                    "mode": "managed",
                    "change": {
                        "actions": ["create"],
                        "after": {
                            "bucket": "ditto-app-dev-tfstate",
                            "role": role,
                            "member": (
                                "serviceAccount:github-actions-terraform-plan@"
                                "ditto-app-dev.iam.gserviceaccount.com"
                            ),
                            "condition": [
                                {
                                    "expression": (
                                        'resource.name == "projects/_/buckets/'
                                        "ditto-app-dev-tfstate/"
                                        "objects/gcp-collector-custody/"
                                    )
                                    + suffix
                                    + '"'
                                }
                            ],
                        },
                    },
                }
            )
        return result

    def test_two_expected_creations_only(self):
        self.assertEqual(scope.validate_backend_bootstrap(self.plan()), 2)

    def test_unrelated_bake_create_or_live_delete_refused(self):
        for address, actions in (
            ("google_service_account.bake[0]", ["create"]),
            ("google_compute_network.preview", ["delete"]),
        ):
            plan = self.plan()
            plan["resource_changes"].append(
                {"address": address, "mode": "managed", "change": {"actions": actions}}
            )
            with self.assertRaises(ValueError):
                scope.validate_backend_bootstrap(plan)

    def test_scope_widening_or_new_principal_refused(self):
        for field, value in (
            ("role", "roles/storage.admin"),
            ("member", "allUsers"),
            ("condition", [{"expression": "true"}]),
            ("bucket", "some-other-bucket"),
        ):
            plan = self.plan()
            plan["resource_changes"][0]["change"]["after"][field] = value
            with self.assertRaises(ValueError):
                scope.validate_backend_bootstrap(plan)

    def test_backend_grant_removal_or_update_refused(self):
        for actions in (["delete"], ["update"], ["create", "delete"]):
            plan = self.plan()
            plan["resource_changes"][0]["change"]["actions"] = actions
            with self.assertRaises(ValueError):
                scope.validate_backend_bootstrap(plan)

    def test_incomplete_pair_refused(self):
        plan = self.plan()
        plan["resource_changes"].pop()
        with self.assertRaises(ValueError):
            scope.validate_backend_bootstrap(plan)

    def test_after_bootstrap_ordinary_preview_remains_separate_review_scope(self):
        plan = self.plan()
        for resource in plan["resource_changes"]:
            resource["change"]["actions"] = ["no-op"]
        self.assertEqual(scope.validate_backend_bootstrap(plan), 0)


if __name__ == "__main__":
    unittest.main()
