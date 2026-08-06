from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]
DATAPIPELINE_TF = ROOT / "infra/terraform/stacks/gcp-platform/datapipeline.tf"
ROOT_PYPROJECT = ROOT / "pyproject.toml"


def test_monorepo_semantic_release_owns_the_datagen_version() -> None:
    semantic_release = tomllib.loads(ROOT_PYPROJECT.read_text())["tool"][
        "semantic_release"
    ]
    assert (
        "research/dittobench-datagen/internal/version/version.go:Version"
        in semantic_release["version_variables"]
    )


def test_terraform_cannot_roll_back_a_semantic_release_image() -> None:
    terraform = DATAPIPELINE_TF.read_text()

    assert 'resource "google_cloud_run_v2_service" "datapipeline"' in terraform
    assert "ignore_changes = [template[0].containers[0].image]" in terraform
    assert "from = module.datapipeline[0].google_cloud_run_v2_service.this" in terraform
    assert "to   = google_cloud_run_v2_service.datapipeline[0]" in terraform
    assert 'role     = "roles/run.developer"' in terraform
    assert 'role               = "roles/iam.serviceAccountUser"' in terraform
    assert "Semantic release owns every subsequent image deployment" in terraform
    assert 'module "datapipeline"' not in terraform
