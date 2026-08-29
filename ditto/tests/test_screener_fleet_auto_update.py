"""Contracts for outbound-only authenticated screener-fleet delivery."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
BUILDER = ROOT / "scripts/build-screener-fleet-release.py"
UPDATER = ROOT / "scripts/screener-fleet-auto-update.sh"
WORKFLOW = ROOT / ".github/workflows/release.yml"
ROLE = ROOT / "infra/ansible/roles/hetzner_screener_fleet"
REVISION = "a" * 40
BUILDER_IMAGE = (
    "us-central1-docker.pkg.dev/ditto-app-dev/ditto-public-builders/"
    "submission-builder@sha256:" + "b" * 64
)


def _render(
    tmp_path: Path, *, builder_image: str = BUILDER_IMAGE
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--output",
            str(tmp_path / "release"),
            "--version",
            "1.2.3",
            "--revision",
            REVISION,
            "--submission-builder-image",
            builder_image,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_release_builder_renders_closed_immutable_manifest(tmp_path: Path) -> None:
    result = _render(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "release/manifest.env").read_text().splitlines() == [
        "FLEET_FORMAT_VERSION=1",
        "FLEET_VERSION=1.2.3",
        f"FLEET_REVISION={REVISION}",
        "FLEET_UPDATE_PROTOCOL=1",
        f"SUBMISSION_BUILDER_IMAGE={BUILDER_IMAGE}",
    ]


@pytest.mark.parametrize(
    "builder_image",
    [
        "submission-builder:latest",
        "ghcr.io/attacker/submission-builder@sha256:" + "b" * 64,
        BUILDER_IMAGE.removesuffix("b"),
    ],
)
def test_release_builder_rejects_mutable_or_wrong_builder(
    tmp_path: Path, builder_image: str
) -> None:
    result = _render(tmp_path, builder_image=builder_image)

    assert result.returncode != 0
    assert not (tmp_path / "release/manifest.env").exists()


def test_updater_authenticates_before_fetch_or_drain() -> None:
    updater = UPDATER.read_text()

    verify = updater.index("cosign verify \\")
    fetch = updater.index('prepare_release "$revision"')
    activate = updater.rindex('activate_release "$revision"')
    assert verify < fetch < activate
    assert (
        "--certificate-oidc-issuer https://token.actions.githubusercontent.com"
        in updater
    )
    assert "ditto-subnet/.github/workflows/release.yml@refs/heads/main" in updater
    assert "refs/heads/main:refs/remotes/origin/main" in updater
    assert "merge-base --is-ancestor" in updater
    assert 'setpriv --reuid="$SERVICE_USER"' in updater
    assert 'env HOME="$FLEET_ROOT"' in updater
    assert "runuser" not in updater
    assert "trap cleanup_staging RETURN" in updater
    assert updater.count("venv --relocatable") == 2
    assert updater.count("sync --frozen --no-editable") == 2
    activation = updater[updater.index("activate_release()") :]
    assert activation.index('"$release_dir/src/scripts/screener-fleet-auto-update.sh"') < (
        activation.index("stop_fleet\n")
    )


def test_updater_has_no_inbound_deploy_or_long_lived_cloud_credential() -> None:
    updater = UPDATER.read_text().casefold()
    service = (
        (ROLE / "templates/ditto-screener-fleet-auto-update.service.j2")
        .read_text()
        .casefold()
    )

    for forbidden in ("ssh", "github_token", "service-account-key", "credentials.json"):
        assert forbidden not in updater
        assert forbidden not in service
    assert "user=root" not in service.splitlines()
    assert "nonewprivileges=true" in service
    assert "protectsystem=strict" in service
    assert "readwritepaths=" in service
    assert "/usr/local/sbin/ditto-screener-fleet-auto-update" in service
    assert "environment=home={{ screener_fleet_update_state_dir }}" in service
    assert "restrictsuidsgid=true" in service


def test_updater_reports_the_debian_13_docker_cli_package() -> None:
    updater = UPDATER.read_text()

    assert "install the Docker CLI; Debian 13 package: docker-cli" in updater


def test_role_installs_setpriv_provider() -> None:
    tasks = (ROLE / "tasks/main.yml").read_text()

    assert "- util-linux" in tasks


def test_release_workflow_signs_before_advancing_discovery_channel() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = workflow["jobs"]["assemble-screener-fleet-release"]
    script = job["steps"][-1]["run"]

    assert job["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
    }
    assert script.index("cosign sign --yes") < script.index(
        "docker buildx imagetools create"
    )
    assert "screener-fleet-stable-$SCREENER_FLEET_UPDATE_PROTOCOL" in script
    assert "HETZNER_SSH" not in WORKFLOW.read_text()
