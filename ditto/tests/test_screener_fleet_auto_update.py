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
    assert activation.index(
        '"$release_dir/src/scripts/screener-fleet-auto-update.sh"'
    ) < (activation.index("stop_fleet\n"))
    assert '"$SELF_PATH"' in activation
    assert '[[ "$SELF_PATH" = "$STATE_DIR/"* ]]' in updater


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
    assert "/usr/local/sbin/ditto-screener-fleet-auto-update" not in service
    assert (
        "environment=screener_fleet_self_path={{ screener_fleet_updater_path }}"
        in service
    )
    assert "execstart={{ screener_fleet_updater_path }}" in service
    assert "environment=home={{ screener_fleet_update_state_dir }}" in service
    assert "restrictsuidsgid=true" in service


def test_role_keeps_updater_self_replacement_in_its_private_state_dir() -> None:
    defaults = (ROLE / "defaults/main.yml").read_text()
    tasks = (ROLE / "tasks/main.yml").read_text()

    assert (
        "{{ screener_fleet_update_state_dir }}/ditto-screener-fleet-auto-update"
        in defaults
    )
    assert 'dest: "{{ screener_fleet_updater_path }}"' in tasks


def test_updater_reports_the_debian_13_docker_cli_package() -> None:
    updater = UPDATER.read_text()

    assert "install the Docker CLI; Debian 13 package: docker-cli" in updater


def test_role_installs_setpriv_provider() -> None:
    tasks = (ROLE / "tasks/main.yml").read_text()

    assert "- util-linux" in tasks


def test_hetzner_workers_use_release_bound_rootless_analyzer() -> None:
    tasks = (ROLE / "tasks/main.yml").read_text()
    defaults = (ROLE / "defaults/main.yml").read_text()
    environment = (ROLE / "templates/fleet.env.j2").read_text()
    installer = (
        ROOT / "workers/screener/scripts/install-rootless-docker.sh"
    ).read_text()
    updater = UPDATER.read_text()
    worker = (ROLE / "templates/ditto-screener-worker@.service.j2").read_text()

    for package in ("uidmap", "slirp4netns", "rootlesskit", "dbus-user-session"):
        assert f"- {package}" in tasks
    assert "unix:///run/ditto-screener-docker/docker.sock" in defaults
    assert "ditto-screener-no-local-docker.sock" not in environment
    assert "SCREENER_REQUIRE_ROOTLESS_DOCKER=1" in environment
    assert 'rootless_dockerd="$(command -v dockerd-rootless.sh)"' in installer
    assert "ExecStart=${rootless_dockerd}" in installer
    assert 'prepare_l2_analyzer "$revision"' in updater
    assert '"$release_dir/src/workers/screener" >&2' in updater
    assert 'docker tag "$l2_candidate" "$L2_ANALYZER_ACTIVE"' in updater
    assert "rootless analyzer executor is unavailable" in updater
    assert (
        "screener_fleet_l2_workspace_root: /var/lib/ditto-screener-l2-workspaces"
        in defaults
    )
    assert "Ensure rootless-analyzer workspace parents" in tasks
    assert 'group: "{{ screener_fleet_executor_group }}"' in tasks
    assert "Ensure the rootless gateway bind-mount root" in tasks
    assert (
        "screener_fleet_gateway_state_root: /var/lib/ditto-screener-gateway-state"
        in defaults
    )
    assert (
        "Environment=DOCKER_CONFIG={{ screener_fleet_state_dir }}/workers/%i/docker"
        in worker
    )
    assert (
        "Environment=SCREENER_GATEWAY_STATE_ROOT={{ screener_fleet_gateway_state_root }}"
        in worker
    )
    assert "{{ screener_fleet_gateway_state_root }}" in worker
    assert (
        "SCREENER_L2_WORKSPACE_ROOT={{ screener_fleet_l2_workspace_root }}/%i" in worker
    )
    assert "{{ screener_fleet_l2_workspace_root }}/%i" in worker
    assert "PrivateTmp=true" in worker


def test_role_stops_workers_above_configured_capacity() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    task = next(
        item
        for item in tasks
        if item["name"]
        == "Stop and disable screening workers above configured capacity"
    )

    assert task["ansible.builtin.systemd"] == {
        "name": "ditto-screener-worker@{{ item }}",
        "enabled": False,
        "state": "stopped",
    }
    assert task["loop"] == (
        "{{ range(screener_fleet_worker_processes + 1, 17) | list }}"
    )
    assert task["when"] == "screener_fleet_runtime_enabled"


def test_each_hetzner_worker_has_a_distinct_heartbeat_identity() -> None:
    worker = (ROLE / "templates/ditto-screener-worker@.service.j2").read_text()

    assert (
        "Environment=SCREENER_INSTANCE_ID={{ screener_fleet_node_id }}-worker-%i"
        in worker
    )


def test_self_updater_reconciles_stale_workers_when_canary_shrinks() -> None:
    """A pull release must not leave an old higher-index poller alive.

    Ansible is not part of every release. The updater therefore enumerates the
    currently loaded numeric worker instances, drains all of them, disables
    the ones above the requested canary count, and re-enables only the desired
    workers on the activated release.
    """
    updater = UPDATER.read_text()
    stop_fleet = updater[updater.index("stop_fleet()") : updater.index("start_fleet()")]
    awk_programs = [
        program.split("'", 1)[0] for program in stop_fleet.split("awk '")[1:]
    ]

    assert "list-units --all --type=service --plain --no-legend" in updater
    assert "ditto-screener-worker@*.service" in updater
    assert len(awk_programs) == 2
    unit_listing = "\n".join(
        (
            "ditto-screener-worker@1.service loaded active running",
            "ditto-screener-worker@12.service loaded inactive dead",
            "ditto-screener-worker@0.service loaded active running",
            "ditto-screener-worker@1.service.bak loaded inactive dead",
        )
    )
    for program in awk_programs:
        result = subprocess.run(
            ["awk", program],
            input=unit_listing,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["1", "12"]
    assert '"$SYSTEMCTL" disable "ditto-screener-worker@$index.service"' in updater
    assert '"$SYSTEMCTL" enable --now "ditto-screener-worker@$index.service"' in updater
    stop = updater.index("stop_fleet()")
    start = updater.index("start_fleet()")
    assert stop < start


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
