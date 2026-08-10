from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
GCP_ROOT = ROOT / "infra" / "terraform" / "stacks" / "gcp-platform"


def test_production_intent_explicitly_sets_every_live_resource_toggle() -> None:
    intent = (GCP_ROOT / "prod.auto.tfvars").read_text()
    declared = set(
        re.findall(
            r'^variable "(enable_[a-z0-9_]+|manage_dns)"',
            "\n".join(path.read_text() for path in GCP_ROOT.glob("*.tf")),
            re.MULTILINE,
        )
    )
    assigned = set(
        re.findall(r"^(enable_[a-z0-9_]+|manage_dns)\s*=", intent, re.MULTILINE)
    )
    assert declared <= assigned


def test_fleet_boot_is_bound_to_a_protected_release_sha() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "infra-plan-apply.yml").read_text()
    )
    checkout = workflow["jobs"]["plan"]["steps"][0]
    assert checkout["with"]["ref"] == "main"
    assert checkout["with"]["fetch-depth"] == 0
    bind_step = next(
        step
        for step in workflow["jobs"]["plan"]["steps"]
        if step.get("name") == "Bind the screener fleet to the latest semantic release"
    )
    assert bind_step["if"] == "inputs.root == 'gcp-platform'"
    assert "gh release view" in bind_step["run"]
    assert "git rev-list -n 1" in bind_step["run"]
    assert "git merge-base --is-ancestor" in bind_step["run"]
    assert "TF_VAR_screener_fleet_release_sha=$revision" in bind_step["run"]
    fleet = (GCP_ROOT / "screener-fleet.tf").read_text()
    startup = (GCP_ROOT / "files" / "screener-fleet-startup.sh.tpl").read_text()
    bootstrap = (
        ROOT / "workers" / "screener" / "scripts" / "bootstrap-screener.sh"
    ).read_text()
    assert "git_revision      = var.screener_fleet_release_sha" in fleet
    assert 'git_ref           = "main"' not in fleet
    assert (
        'git -C /opt/ditto/bootstrap-src fetch --depth 1 origin "${git_revision}"'
        in startup
    )
    assert 'SCREENER_EXPECTED_SHA="${git_revision}"' in startup
    assert 'test "$target_sha" = "$SCREENER_EXPECTED_SHA"' in bootstrap


def test_controller_deploy_proves_exact_fresh_controller_heartbeat() -> None:
    updater = (
        ROOT / "services" / "screener-orchestrator" / "scripts" / "update-controller.sh"
    ).read_text()
    for contract in (
        ".controller_stale == false",
        ".controller_source_sha == $revision",
        ".controller_epoch != $prior_epoch",
    ):
        assert contract in updater
    assert ".activate_fallback == false" not in updater
    assert ".provider_ready == true" not in updater


def test_capacity_controller_cannot_administer_compute_project_wide() -> None:
    terraform = (GCP_ROOT / "screener-capacity-controller.tf").read_text()
    assert 'role    = "roles/compute.instanceAdmin.v1"' not in terraform
    assert '"compute.autoscalers.list"' in terraform
    assert '"compute.instanceGroupManagers.get"' in terraform
    assert '"compute.instanceGroupManagers.update"' in terraform
    assert "compute.regionInstanceGroupManagers" not in terraform
    assert "only_ditto_screener_fleet" in terraform


def test_capacity_controller_activation_uses_org_scoped_targon_v3() -> None:
    intent = (GCP_ROOT / "prod.auto.tfvars").read_text()
    assert re.search(
        r"^enable_screener_capacity_controller\s*=\s*true$", intent, re.MULTILINE
    )
    role = ROOT / "infra" / "ansible" / "roles" / "screener_capacity_controller"
    defaults = (role / "defaults" / "main.yml").read_text()
    controller_unit = (
        role / "templates" / "ditto-screener-capacity.service.j2"
    ).read_text()
    builder_unit = (role / "templates" / "ditto-image-builder.service.j2").read_text()
    assert "screener_capacity_targon_org_slug: ditto" in defaults
    assert (
        "--targon-org-slug {{ screener_capacity_targon_org_slug }}" in controller_unit
    )
    assert "--targon-org-slug {{ screener_capacity_targon_org_slug }}" in builder_unit

    targon_client = (
        ROOT / "services" / "screener-orchestrator" / "screener_capacity" / "targon.py"
    ).read_text()
    assert 'base_url: str = "https://api.targon.com/tha/v3"' in targon_client
    assert 'return f"/orgs/{slug}/workloads{suffix}"' in targon_client

    platform_prod = (
        ROOT / "infra" / "ansible" / "host_vars" / "ditto-platform-prod.yml"
    ).read_text()
    assert (
        'secret_screener_controller_api_token: "screener-controller-api-token-prod"'
        in platform_prod
    )
