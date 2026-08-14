"""Regression coverage for the staged Depot deployment identity cutover."""

from pathlib import Path

ROOT = Path(__file__).parents[2]
GCP_ROOT = ROOT / "infra/terraform/stacks/gcp-platform"
SECRET_BOOTSTRAP = ROOT / ".github/workflows/depot-deployment-secrets.yml"


def test_depot_provider_is_isolated_and_main_only() -> None:
    identity = (GCP_ROOT / "identity.tf").read_text()
    variables = (GCP_ROOT / "variables.tf").read_text()

    assert 'default     = "depot-ditto-subnet"' in variables
    assert 'default     = "4q2czr6whg"' in variables
    assert 'default     = "ditto-assistant/ditto-subnet"' in variables
    assert 'default     = "refs/heads/main"' in variables
    assert 'issuer_uri = "https://identity.depot.dev"' in identity
    assert "assertion.org_id == '${var.depot_org_id}'" in identity
    assert "assertion.repository == '${var.depot_deploy_repository}'" in identity
    assert "assertion.ref == '${var.depot_deploy_ref}'" in identity


def test_every_automatic_gcp_deploy_identity_accepts_depot_wif() -> None:
    terraform = "\n".join(path.read_text() for path in GCP_ROOT.glob("*.tf"))
    expected_bindings = {
        "platform_deploy_depot_wif",
        "datagen_release_depot_wif",
        "screener_bake_depot_wif",
        "subnet_build_depot_wif",
        "dittobench_deploy_depot_wif",
        "screener_deploy_depot_wif",
    }

    for binding in expected_bindings:
        assert f'google_service_account_iam_member" "{binding}"' in terraform
    assert terraform.count("member             = local.depot_deploy_principal") == len(
        expected_bindings
    )


def test_release_stays_on_github_until_external_cutover_gates_are_met() -> None:
    # The release still publishes public GHCR images and relies on GitHub
    # Environment enforcement. Depot CI supports neither GitHub App package
    # pushes nor Environment protection rules, so moving it before WIF is
    # applied and a registry credential/distribution plan exists would turn a
    # successful merge into a broken or less-protected production release.
    assert (ROOT / ".github/workflows/release.yml").is_file()
    assert not (ROOT / ".depot/workflows/release.yml").exists()


def test_secret_bootstrap_uses_protected_environments_and_new_provider() -> None:
    workflow = SECRET_BOOTSTRAP.read_text()
    assert "environment: prod" in workflow
    assert "environment: dev" in workflow
    assert workflow.count("--branch main") >= 4
    assert "providers/depot-ditto-subnet" in workflow
    assert "secrets.GCP_WIF_PROVIDER" not in workflow
    for name in (
        "CLOUDFLARE_API_TOKEN",
        "GCP_DATAGEN_RELEASE_SA",
        "GCP_DITTOBENCH_DEPLOY_SA",
        "GCP_PLATFORM_DEPLOY_SA",
        "GCP_SCREENER_BAKE_SA",
        "GCP_SCREENER_DEPLOY_SA",
        "GCP_SUBNET_BUILD_SA",
    ):
        assert f"secrets.{name}" in workflow
