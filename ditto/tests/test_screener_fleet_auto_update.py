"""Contracts for outbound-only authenticated screener-fleet delivery."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
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
BUILDER_FALLBACK = ROOT / "release/screener-fleet-builder.digest"


def _render(
    tmp_path: Path,
    *,
    builder_image: str | None = BUILDER_IMAGE,
    fallback_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(BUILDER),
        "--output",
        str(tmp_path / "release"),
        "--version",
        "1.2.3",
        "--revision",
        REVISION,
    ]
    if builder_image is not None:
        command.extend(["--submission-builder-image", builder_image])
    if fallback_file is not None:
        command.extend(["--submission-builder-fallback-file", str(fallback_file)])
    return subprocess.run(
        command,
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


def _validate_with_host_script(script: Path, manifest: Path) -> None:
    text = script.read_text()
    functions = []
    for name in ("manifest_value", "is_builder_digest", "validate_manifest"):
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}", text, re.MULTILINE | re.DOTALL)
        if match is not None:
            functions.append(match.group())
    harness = (
        "set -euo pipefail\nEXPECTED_FORMAT_VERSION=1\nEXPECTED_UPDATE_PROTOCOL=1\n"
        + "\n".join(functions)
        + '\nvalidate_manifest "$1"\n'
    )
    result = subprocess.run(
        ["bash", "-c", harness, "validate", str(manifest)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (script, result.stderr)


@pytest.mark.parametrize("primary", [None, "", "invalid", "submission-builder:latest"])
def test_release_builder_fallback_is_accepted_by_both_host_validators(
    tmp_path: Path, primary: str | None
) -> None:
    result = _render(tmp_path, builder_image=primary, fallback_file=BUILDER_FALLBACK)

    assert result.returncode == 0, result.stderr
    assert "::warning::" in result.stdout
    assert str(BUILDER_FALLBACK) in result.stdout
    manifest = tmp_path / "release/manifest.env"
    assert len(manifest.read_text().splitlines()) == 5
    assert (
        f"SUBMISSION_BUILDER_IMAGE={BUILDER_FALLBACK.read_text().strip()}\n"
        in manifest.read_text()
    )
    for script in (
        UPDATER,
        ROOT / "workers/screener/scripts/pull-screener-release.sh",
    ):
        _validate_with_host_script(script, manifest)


@pytest.mark.parametrize("fallback_contents", [BUILDER_IMAGE[:-64] + "c" * 64, "bad"])
def test_release_builder_prefers_valid_job_image(
    tmp_path: Path, fallback_contents: str
) -> None:
    fallback = tmp_path / "fallback.digest"
    fallback.write_text(fallback_contents + "\n")
    result = _render(tmp_path, fallback_file=fallback)

    assert result.returncode == 0, result.stderr
    assert "::warning::" not in result.stdout
    assert (
        f"SUBMISSION_BUILDER_IMAGE={BUILDER_IMAGE}\n"
        in (tmp_path / "release/manifest.env").read_text()
    )


@pytest.mark.parametrize(
    "contents",
    [None, b"", b"submission-builder:latest\n", b"\xff", b"bad\nbad\n"],
)
def test_release_builder_refuses_missing_or_invalid_fallback(
    tmp_path: Path, contents: bytes | None
) -> None:
    fallback = tmp_path / "fallback.digest"
    if contents is not None:
        fallback.write_bytes(contents)
    result = _render(tmp_path, builder_image="", fallback_file=fallback)

    assert result.returncode != 0
    assert "::warning::" not in result.stdout
    assert not (tmp_path / "release/manifest.env").exists()


def test_committed_builder_fallback_is_one_immutable_reference() -> None:
    contents = BUILDER_FALLBACK.read_text()
    assert len(contents.splitlines()) == 1
    assert re.fullmatch(
        r"us-central1-docker\.pkg\.dev/ditto-app-dev/ditto-public-builders/"
        r"submission-builder@sha256:[0-9a-f]{64}\n",
        contents,
    )


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
    assert "trap cleanup_staging_on_exit EXIT" in updater
    assert updater.count("venv --relocatable") == 1
    assert updater.count("sync --frozen --no-editable") == 1
    activation = updater[updater.index("activate_release()") :]
    installed = activation.index('"$release_dir/src/scripts/screener-fleet-drain.py"')
    assert (
        activation.index('"$release_dir/src/scripts/screener-fleet-auto-update.sh"')
        < installed
    )
    # Both activation modes run only after the authenticated release is staged
    # and the updater has installed its own copy from it.
    assert installed < activation.index("    stop_fleet\n")
    assert installed < activation.index('rolling_check "$revision" "$PREV_TARGET"')
    assert installed < activation.index('    roll_release "$revision"')
    assert activation.index("rolling_check") < activation.index("roll_release")
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
        "Environment=SCREENER_GATEWAY_STATE_ROOT="
        "{{ screener_fleet_gateway_state_root }}" in worker
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
    stop_fleet = updater[
        updater.index("worker_indexes()") : updater.index("ensure_worker_state()")
    ]
    awk_programs = [
        program.split("'", 1)[0] for program in stop_fleet.split("awk '")[1:]
    ]

    assert "list-units --all --type=service --plain --no-legend" in updater
    assert "ditto-screener-worker@*.service" in updater
    assert len(awk_programs) == 1
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
    assert (
        "systemctl stop"
        not in updater[updater.index("stop_fleet()") :].split("ditto-screener-worker@")[
            0
        ]
    )
    assert "kill -s SIGKILL" not in updater
    assert "drain-status.env" in updater
    assert "leaving it running" in updater
    assert '"$SYSTEMCTL" enable "ditto-screener-worker@$index.service"' in updater
    assert '"$SYSTEMCTL" restart "ditto-screener-worker@$index.service"' in updater
    stop = updater.index("stop_fleet()")
    start = updater.index("start_fleet()")
    assert stop < start


def test_self_updater_provisions_worker_state_before_scale_up() -> None:
    """Scale-up must create systemd bind-mount paths without an Ansible run."""
    updater = UPDATER.read_text()
    service = (
        ROLE / "templates/ditto-screener-fleet-auto-update.service.j2"
    ).read_text()
    provisioning = updater[
        updater.index("ensure_worker_state()") : updater.index("start_fleet()")
    ]

    assert 'L2_WORKSPACE_ROOT="${SCREENER_FLEET_L2_WORKSPACE_ROOT:-' in updater
    assert 'EXECUTOR_GROUP="${SCREENER_FLEET_EXECUTOR_GROUP:-' in updater
    assert 'install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700' in provisioning
    assert '"$STATE_DIR/workers/$index"' in provisioning
    assert 'install -d -o "$SERVICE_USER" -g "$EXECUTOR_GROUP" -m 0770' in provisioning
    assert '"$L2_WORKSPACE_ROOT/$index"' in provisioning
    start_fleet = updater[
        updater.index("start_fleet()") : updater.index("activate_release()")
    ]
    assert start_fleet.index("ensure_worker_state") < start_fleet.index(
        '"$SYSTEMCTL" enable "ditto-screener-worker@$index.service"'
    )
    assert (
        "Environment=SCREENER_FLEET_L2_WORKSPACE_ROOT="
        "{{ screener_fleet_l2_workspace_root }}" in service
    )
    assert (
        "Environment=SCREENER_FLEET_EXECUTOR_GROUP="
        "{{ screener_fleet_executor_group }}" in service
    )


def test_release_workflow_signs_before_advancing_discovery_channel() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = workflow["jobs"]["assemble-screener-fleet-release"]
    script = next(
        step["run"]
        for step in job["steps"]
        if step.get("name") == "Authenticate and promote the exact fleet descriptor"
    )

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


def test_gce_overflow_workers_pull_the_same_authenticated_release() -> None:
    updater = (ROOT / "workers/screener/scripts/pull-screener-release.sh").read_text()
    bootstrap = (ROOT / "workers/screener/scripts/bootstrap-screener.sh").read_text()
    service = (
        ROOT / "workers/screener/deploy/ditto-screener-release-update.service"
    ).read_text()
    timer = (
        ROOT / "workers/screener/deploy/ditto-screener-release-update.timer"
    ).read_text()

    verify = updater.index("cosign verify \\")
    resolve = updater.index('revision="$(manifest_value')
    activate = updater.index('SCREENER_EXPECTED_SHA="$revision"')
    assert verify < resolve < activate
    assert "screener-fleet-stable-1" in updater
    assert "ditto-subnet/.github/workflows/release.yml@refs/heads/main" in updater
    assert "release manifest failed its closed schema" in updater
    assert "failed-descriptor" in updater
    assert "managed-descriptor" in updater
    assert "gcloud compute ssh" not in updater
    assert "github_token" not in updater.casefold()

    assert "cosign-linux-amd64" in bootstrap
    assert (
        "c956e5dfcac53d52bcf058360d579472f0c1d2d9b69f55209e256fe7783f4c74" in bootstrap
    )
    assert "ditto-screener-release-update.timer" in bootstrap
    assert "ExecStart=/opt/ditto/screener/src/" in service
    assert "OnUnitActiveSec=10min" in timer


# A stateful systemctl double. Each worker unit has an ActiveState, a MainPID,
# and a behavior: "idle" exits on its first SIGTERM, "busy" keeps its review
# through the drain, and "finishes" ends its review on the second SIGTERM.
# Once a process exits the unit parks in auto-restart, exactly like
# Restart=always, and only a stop job clears that.
_FAKE_SYSTEMCTL = r"""#!/usr/bin/env -S python3 -S
import os
import sys
from pathlib import Path

units = Path(os.environ["FAKE_UNITS"])
log = Path(os.environ["SCREENER_TEST_SYSTEMCTL_LOG"])
fleet = Path(os.environ["SCREENER_FLEET_STATE_DIR"])
args = sys.argv[1:]
with log.open("a") as handle:
    handle.write(" ".join(args) + "\n")


def note(line):
    with log.open("a") as handle:
        handle.write(line + "\n")


def read(unit, key, default):
    path = units / f"{unit}.{key}"
    return path.read_text().strip() if path.exists() else default


def write(unit, key, value):
    (units / f"{unit}.{key}").write_text(str(value))


def worker_index(unit):
    return unit.split("@", 1)[1].split(".", 1)[0]


def exit_process(unit):
    write(unit, "state", "activating")
    write(unit, "pid", 0)
    write(unit, "seen", 0)
    lease = fleet / "workers" / worker_index(unit) / "active-lease.json"
    lease.unlink(missing_ok=True)


def spawn(unit):
    # systemd resolves the `current` WorkingDirectory when it execs a worker.
    next_pid = int(read("next", "pid", "9000")) + 1
    write("next", "pid", next_pid)
    write(unit, "state", "active")
    write(unit, "pid", next_pid)
    write(unit, "seen", 0)
    behavior = "idle"
    proc = os.environ.get("SCREENER_FLEET_PROC_ROOT")
    current = os.environ.get("SCREENER_FLEET_CURRENT_LINK")
    if proc and current:
        release = Path(os.path.realpath(current))
        if release.name == os.environ.get("FAKE_CRASH_RELEASE"):
            behavior = "crashes"
        entry = Path(proc) / str(next_pid)
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "cwd").symlink_to(release / "src/workers/screener")
    write(unit, "behavior", behavior)


command = args[0]
unit = args[-1]
if command == "list-units":
    for path in sorted(units.glob("ditto-screener-worker@*.service.state")):
        print(path.name[: -len(".state")] + " loaded active running")
elif command == "show":
    key = args[2]
    if os.environ.get("FAKE_AUTO_RESTART") and "worker@" in unit:
        # Restart=always: a parked unit starts again after RestartSec, and a
        # crashing candidate exits soon after each start.
        state = read(unit, "state", "inactive")
        seen = read(unit, "seen", "0") == "1"
        if state == "activating":
            spawn(unit) if seen else write(unit, "seen", 1)
        elif state == "active" and read(unit, "behavior", "idle") == "crashes":
            exit_process(unit) if seen else write(unit, "seen", 1)
    print(read(unit, "state" if key == "ActiveState" else "pid",
               "inactive" if key == "ActiveState" else "0"))
elif command == "kill":
    if "worker@" in unit:
        if "--kill-whom=main" not in args:
            note("child-signaled")
            sys.exit(1)
        if read(unit, "state", "inactive") == "active":
            kills = int(read(unit, "kills", "0")) + 1
            write(unit, "kills", kills)
            behavior = read(unit, "behavior", "idle")
            if behavior in {"idle", "crashes"} or (
                behavior == "finishes" and kills >= 2
            ):
                exit_process(unit)
    elif "--kill-whom=main" not in args:
        note("agent-children-signaled")
elif command == "stop":
    if "worker@" in unit:
        if read(unit, "state", "inactive") == "active":
            if "--no-block" in args:
                sys.exit(0)
            note(f"interrupted-review {unit}")
        write(unit, "state", "inactive")
        write(unit, "pid", 0)
    else:
        write(unit, "state", "inactive")
elif command in {"start", "restart"}:
    if command == "restart" or read(unit, "state", "inactive") != "active":
        spawn(unit)
elif command == "is-active":
    sys.exit(0 if read(unit, "state", "inactive") == "active" else 3)
elif command == "is-enabled":
    sys.exit(0 if read(unit, "enabled", "disabled") == "enabled" else 1)
elif command == "disable":
    if os.environ.get("FAKE_FAIL_DISABLE"):
        sys.exit(1)
    write(unit, "enabled", "disabled")
sys.exit(0)
"""


def _fleet_harness(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    units = tmp_path / "units"
    units.mkdir()
    state = tmp_path / "fleet"
    updater_state = state / "updater"
    updater_state.mkdir(parents=True)
    log = tmp_path / "systemctl.log"
    log.write_text("")
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(_FAKE_SYSTEMCTL)
    systemctl.chmod(0o755)
    (bin_dir / "timeout").write_text('#!/bin/sh\nshift\nexec "$@"\n')
    (bin_dir / "timeout").chmod(0o755)
    user = subprocess.run(
        ["id", "-un"], text=True, capture_output=True, check=True
    ).stdout.strip()
    group = subprocess.run(
        ["id", "-gn"], text=True, capture_output=True, check=True
    ).stdout.strip()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "FAKE_UNITS": str(units),
            "SCREENER_TEST_SYSTEMCTL_LOG": str(log),
            "SCREENER_FLEET_UPDATE_STATE_DIR": str(updater_state),
            "SCREENER_FLEET_STATE_DIR": str(state),
            "SCREENER_FLEET_SELF_PATH": str(UPDATER),
            "SCREENER_FLEET_DRAIN_PY": str(ROOT / "scripts/screener-fleet-drain.py"),
            "SCREENER_FLEET_WORKER_PROCESSES": "3",
            "SCREENER_FLEET_DRAIN_BOUND_SECONDS": "2",
            "SCREENER_FLEET_DRAIN_POLL_SECONDS": "0.1",
            "SCREENER_FLEET_START_SETTLE_SECONDS": "0",
            "SCREENER_FLEET_USER": user,
            "SCREENER_FLEET_GROUP": group,
            "SCREENER_FLEET_EXECUTOR_GROUP": group,
            "SCREENER_FLEET_L2_WORKSPACE_ROOT": str(tmp_path / "l2"),
        }
    )
    return env, units, state, log


def _worker_unit(
    units: Path, state: Path, index: int, *, behavior: str, pid: int
) -> None:
    unit = f"ditto-screener-worker@{index}.service"
    (units / f"{unit}.state").write_text("active")
    (units / f"{unit}.pid").write_text(str(pid))
    (units / f"{unit}.behavior").write_text(behavior)
    worker = state / "workers" / str(index)
    worker.mkdir(parents=True, exist_ok=True)
    if behavior != "idle":
        now = int(time.time())
        (worker / "active-lease.json").write_text(
            json.dumps(
                {
                    "agent_id": f"agent-{index}",
                    "attempt_id": f"attempt-{index}",
                    "lease_deadline": now + 600,
                    "progress_at": now,
                    "revision": "a" * 40,
                }
            )
        )


def _unit_value(units: Path, index: int, key: str) -> str:
    return (units / f"ditto-screener-worker@{index}.service.{key}").read_text()


def _run(env: dict[str, str], entrypoint: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(UPDATER)],
        env={**env, "SCREENER_FLEET_TEST_ENTRYPOINT": entrypoint},
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def test_drain_cancels_restarts_and_never_interrupts_a_review(
    tmp_path: Path,
) -> None:
    """A finished worker must not come back on the old release mid-drain.

    SIGTERM reaches only the main process. Workers whose process exits are
    parked by Restart=always, so the drain issues a stop job to cancel that
    restart. A review still open at the bound is held, never stopped, and a
    held worker above the requested count gets a non-blocking stop so it
    cannot restart after its review.
    """
    env, units, state, log = _fleet_harness(tmp_path)
    _worker_unit(units, state, 1, behavior="idle", pid=101)
    _worker_unit(units, state, 2, behavior="busy", pid=102)
    _worker_unit(units, state, 3, behavior="finishes", pid=103)
    _worker_unit(units, state, 12, behavior="busy", pid=112)

    result = _run(env, "stop_fleet")

    assert result.returncode == 0, result.stderr
    recorded = log.read_text()
    assert "child-signaled" not in recorded
    assert "agent-children-signaled" not in recorded
    assert "interrupted-review" not in recorded
    # The idle and the finished worker had restarts pending; both were cleared.
    for index in (1, 3):
        assert f"stop ditto-screener-worker@{index}.service" in recorded
        assert _unit_value(units, index, "state") == "inactive"
    # The finished worker drained inside the bound, not at it.
    assert int(_unit_value(units, 3, "kills")) >= 2
    held = (state / "updater/held-workers").read_text().splitlines()
    assert sorted(held) == ["12 112", "2 102"]
    assert _unit_value(units, 2, "state") == "active"
    assert "stop ditto-screener-worker@2.service" not in recorded
    assert "stop --no-block ditto-screener-worker@12.service" in recorded
    assert "disable ditto-screener-worker@12.service" in recorded
    status = (state / "updater/drain-status.env").read_text()
    assert "PHASE=held" in status


def test_start_restarts_every_worker_except_a_still_held_review(
    tmp_path: Path,
) -> None:
    """Every worker ends on the activated release.

    A held review keeps its process (same MainPID) and restarts on the new
    release by itself. Any other running process predates the symlink flip,
    so start_fleet restarts it rather than trusting that it is active.
    """
    env, units, state, log = _fleet_harness(tmp_path)
    _worker_unit(units, state, 1, behavior="busy", pid=101)
    _worker_unit(units, state, 2, behavior="busy", pid=102)
    assert _run(env, "stop_fleet").returncode == 0
    # Worker 2's held process exited and Restart=always brought it back on
    # the old release before the flip: its MainPID no longer matches.
    (units / "ditto-screener-worker@2.service.pid").write_text("202")
    log.write_text("")

    result = _run(env, "start_fleet")

    assert result.returncode == 0, result.stderr
    recorded = log.read_text().splitlines()
    assert "start ditto-screener-fleet-agent.service" not in recorded
    for index in (1, 2, 3):
        assert f"enable ditto-screener-worker@{index}.service" in recorded
    assert "restart ditto-screener-worker@1.service" not in recorded
    assert _unit_value(units, 1, "pid") == "101"
    assert "restart ditto-screener-worker@2.service" in recorded
    assert "restart ditto-screener-worker@3.service" in recorded
    assert "PHASE=active" in (state / "updater/drain-status.env").read_text()


def test_release_retires_installed_lane_agent_without_restarting_it(
    tmp_path: Path,
) -> None:
    env, units, state, log = _fleet_harness(tmp_path)
    unit = "ditto-screener-fleet-agent.service"
    (units / f"{unit}.state").write_text("active")
    (units / f"{unit}.enabled").write_text("enabled")
    _worker_unit(units, state, 1, behavior="idle", pid=101)

    assert _run(env, "stop_fleet").returncode == 0
    assert (units / f"{unit}.state").read_text() == "inactive"
    assert (units / f"{unit}.enabled").read_text() == "disabled"
    assert _run(env, "start_fleet").returncode == 0
    recorded = log.read_text()
    assert f"stop {unit}" in recorded
    assert f"disable {unit}" in recorded
    assert f"start {unit}" not in recorded
    assert f"restart {unit}" not in recorded


def test_retirement_fails_closed_when_installed_lane_agent_cannot_be_disabled(
    tmp_path: Path,
) -> None:
    env, units, state, log = _fleet_harness(tmp_path)
    unit = "ditto-screener-fleet-agent.service"
    (units / f"{unit}.state").write_text("active")
    (units / f"{unit}.enabled").write_text("enabled")
    env["FAKE_FAIL_DISABLE"] = "1"

    result = _run(env, "stop_fleet")

    assert result.returncode != 0
    assert "retired fleet agent could not be disabled" in result.stderr
    assert (units / f"{unit}.state").read_text() == "inactive"


def test_start_failure_is_reported_to_the_rollback_caller(tmp_path: Path) -> None:
    """``if ! start_fleet`` disables set -e; failures must still return 1."""
    env, units, state, log = _fleet_harness(tmp_path)
    fake = (tmp_path / "bin/systemctl").read_text()
    (tmp_path / "bin/systemctl").write_text(
        fake.replace(
            'elif command == "is-active":',
            'elif command == "is-active" and unit.endswith("@3.service"):\n'
            "    sys.exit(3)\n"
            'elif command == "is-active":',
        )
    )
    result = _run(env, "start_fleet")
    assert result.returncode == 1
    status = state / "updater/drain-status.env"
    assert not status.exists() or "PHASE=active" not in status.read_text()
    updater = UPDATER.read_text()
    assert "if ! start_fleet; then" in updater


def test_failed_lease_check_does_not_abort_the_drain(tmp_path: Path) -> None:
    """A missing drain helper holds the review instead of exiting under set -e."""
    env, units, state, log = _fleet_harness(tmp_path)
    env["SCREENER_FLEET_DRAIN_PY"] = str(tmp_path / "missing-drain.py")
    env["SCREENER_FLEET_DRAIN_BOUND_SECONDS"] = "0"
    _worker_unit(units, state, 1, behavior="busy", pid=101)

    result = _run(env, "stop_fleet")

    assert result.returncode == 0, result.stderr
    assert (state / "updater/held-workers").read_text() == "1 101\n"
    assert "interrupted-review" not in log.read_text()


def test_aborted_drain_restores_signed_workers(tmp_path: Path) -> None:
    """A failure after drain starts must not leave signed workers down."""
    env, units, state, log = _fleet_harness(tmp_path)
    env["FAKE_FAIL_DISABLE"] = "1"
    env["SCREENER_FLEET_WORKER_PROCESSES"] = "1"
    _worker_unit(units, state, 1, behavior="idle", pid=101)
    _worker_unit(units, state, 4, behavior="idle", pid=104)

    result = _run(env, "stop_fleet")

    assert result.returncode != 0
    recorded = log.read_text().splitlines()
    assert "start --no-block ditto-screener-fleet-agent.service" not in recorded
    assert "start --no-block ditto-screener-worker@1.service" in recorded
    assert "PHASE=aborted" in (state / "updater/drain-status.env").read_text()


def test_timeout_sigterm_during_drain_restores_signed_workers(
    tmp_path: Path,
) -> None:
    """systemd's TimeoutStartSec SIGTERM runs the same restore path."""
    env, units, state, log = _fleet_harness(tmp_path)
    env["SCREENER_FLEET_DRAIN_BOUND_SECONDS"] = "600"
    _worker_unit(units, state, 1, behavior="busy", pid=101)
    process = subprocess.Popen(
        ["bash", str(UPDATER)],
        env={**env, "SCREENER_FLEET_TEST_ENTRYPOINT": "stop_fleet"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while "worker 1 wait" not in (
            (state / "updater/drain-status.env").read_text()
            if (state / "updater/drain-status.env").exists()
            else ""
        ):
            assert time.monotonic() < deadline, "drain never started"
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 143
    recorded = log.read_text().splitlines()
    assert "start --no-block ditto-screener-fleet-agent.service" not in recorded
    assert "interrupted-review" not in log.read_text()


# Release preparation doubles. FAKE_FAIL names the one step that fails:
# clone, fetch, cat-file, merge-base, checkout, rev-parse (reports another
# commit), sync, or verify.
_FAKE_GIT = r"""#!/bin/sh
printf '%s\n' "$*" >>"$FAKE_RELEASE_LOG"
[ "$1" = -C ] && shift 2
if [ "$1" = rev-parse ]; then
  [ "${FAKE_FAIL:-}" = rev-parse ] && echo 0000000000000000000000000000000000000000 \
    || printf '%s\n' "$SCREENER_FLEET_TEST_REVISION"
  exit 0
fi
[ "${FAKE_FAIL:-}" != "$1" ] || exit 1
[ "$1" != clone ] || { for dest; do :; done; mkdir -p "$dest/.git"; }
"""
_FAKE_UV = r"""#!/bin/sh
printf 'uv %s\n' "$*" >>"$FAKE_RELEASE_LOG"
case "$1" in
  venv)
    mkdir -p "$3/bin"
    {
      echo '#!/bin/sh'
      echo 'echo "python $*" >>"$FAKE_RELEASE_LOG"'
      echo '[ "${FAKE_FAIL:-}" != verify ]'
    } >"$3/bin/python"
    chmod +x "$3/bin/python"
    ;;
  sync)
    [ -z "${FAKE_SYNC_SLEEP:-}" ] || sleep "$FAKE_SYNC_SLEEP"
    [ "${FAKE_FAIL:-}" != sync ] || exit 1
    ;;
esac
"""
_FAKE_SETPRIV = r"""#!/bin/sh
while [ "$1" != -- ]; do shift; done
shift
exec "$@"
"""
_FAKE_DOCKER = r"""#!/bin/sh
printf 'DOCKER_HOST=%s %s\n' "${DOCKER_HOST:-}" "$*" >>"$FAKE_RELEASE_LOG"
"""


def _release_harness(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    env, _units, _state, _log = _fleet_harness(tmp_path)
    bin_dir = tmp_path / "bin"
    for name, body in (
        ("git", _FAKE_GIT),
        ("uv", _FAKE_UV),
        ("setpriv", _FAKE_SETPRIV),
        ("docker", _FAKE_DOCKER),
    ):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    releases = tmp_path / "fleet-root/releases"
    releases.mkdir(parents=True)
    log = tmp_path / "release.log"
    log.write_text("")
    env.update(
        {
            "FAKE_RELEASE_LOG": str(log),
            "SCREENER_FLEET_ROOT": str(tmp_path / "fleet-root"),
            "SCREENER_FLEET_RELEASES_DIR": str(releases),
            "SCREENER_FLEET_CURRENT_LINK": str(tmp_path / "fleet-root/current"),
            "SCREENER_FLEET_UV_BIN": str(bin_dir / "uv"),
            "SCREENER_FLEET_TEST_REVISION": REVISION,
            "SCREENER_FLEET_PROC_ROOT": str(tmp_path / "proc"),
        }
    )
    return env, releases, log


def _staging_dirs(releases: Path) -> list[str]:
    return sorted(path.name for path in releases.glob("*.staging.*"))


def test_prepare_success_promotes_the_staged_release(tmp_path: Path) -> None:
    env, releases, _log = _release_harness(tmp_path)

    result = _run(env, "prepare_release")

    assert result.returncode == 0, result.stderr
    assert (releases / REVISION / "worker-venv/bin/python").exists()
    assert _staging_dirs(releases) == []


@pytest.mark.parametrize(
    ("step", "marker"),
    [
        ("fetch", "fetch --force origin"),
        ("cat-file", "cat-file -e"),
        ("merge-base", "merge-base --is-ancestor"),
        ("checkout", "checkout --detach"),
        ("rev-parse", "rev-parse HEAD"),
        ("sync", "uv sync --frozen"),
        ("verify", "verify-installed-signing-contract.py"),
    ],
)
def test_prepare_failure_after_clone_leaves_no_staging_dir(
    tmp_path: Path, step: str, marker: str
) -> None:
    """errexit skips RETURN traps; the EXIT trap must remove the staging dir."""
    env, releases, log = _release_harness(tmp_path)
    env["FAKE_FAIL"] = step

    result = _run(env, "prepare_release")

    assert result.returncode != 0
    assert marker in log.read_text()
    assert _staging_dirs(releases) == []
    assert not (releases / REVISION).exists()


def test_prepare_clone_failure_leaves_no_staging_dir(tmp_path: Path) -> None:
    env, releases, log = _release_harness(tmp_path)
    env["FAKE_FAIL"] = "clone"

    result = _run(env, "prepare_release")

    assert result.returncode != 0
    assert "clone --filter=blob:none" in log.read_text()
    assert _staging_dirs(releases) == []
    assert not (releases / REVISION).exists()


def test_sigterm_during_prepare_leaves_no_staging_dir(tmp_path: Path) -> None:
    """systemd's TimeoutStartSec SIGTERM must still run the staging cleanup."""
    env, releases, log = _release_harness(tmp_path)
    env["FAKE_SYNC_SLEEP"] = "1"
    process = subprocess.Popen(
        ["bash", str(UPDATER)],
        env={**env, "SCREENER_FLEET_TEST_ENTRYPOINT": "prepare_release"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while "uv sync" not in log.read_text():
            assert time.monotonic() < deadline, "prepare never reached uv sync"
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 143
    assert _staging_dirs(releases) == []
    assert not (releases / REVISION).exists()


def test_failing_merge_base_never_activates_release(tmp_path: Path) -> None:
    """The provenance checks must stay under errexit, never inside a condition."""
    env, releases, log = _release_harness(tmp_path)
    env["FAKE_FAIL"] = "merge-base"

    result = _run(env, "prepare_release")

    assert result.returncode != 0
    recorded = log.read_text()
    assert "checkout --detach" not in recorded
    assert "uv venv" not in recorded
    assert not (releases / REVISION).exists()
    updater = UPDATER.read_text()
    assert '\nprepare_release "$revision" "$RELEASES_DIR/$revision"\n' in updater
    staged = updater[
        updater.index('STAGING_DIR="$staging"') : updater.index("STAGING_DIR=''\n}")
    ]
    assert staged.count("if !") == 1  # only the clone, which returns 1
    assert "||" not in staged
    assert "&&" not in staged
    assert "RETURN" not in staged


def test_startup_sweeps_stale_staging_dirs(tmp_path: Path) -> None:
    env, releases, _log = _release_harness(tmp_path)
    for name in (f"{REVISION}.staging.123", f"{'b' * 40}.staging.456"):
        (releases / name / "src").mkdir(parents=True)
    (releases / REVISION / "src").mkdir(parents=True)
    (releases / "notes.staging.1").mkdir()

    result = _run(env, "sweep_staging")

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in releases.iterdir()) == [
        REVISION,
        "notes.staging.1",
    ]
    assert "removed 2 stale release staging dir(s)" in result.stderr
    updater = UPDATER.read_text()
    main = updater[updater.index('flock -n "$lock_fd"') :]
    assert main.index("arm_staging_cleanup") < main.index("sweep_staging")
    assert main.index("sweep_staging") < main.index("resolve_descriptor")


def _proc_entry(tmp_path: Path, pid: int, cwd: Path | None) -> None:
    entry = tmp_path / "proc" / str(pid)
    entry.mkdir(parents=True)
    interpreter = tmp_path / "python3"
    interpreter.touch()
    (entry / "exe").symlink_to(interpreter)
    if cwd is not None:
        (entry / "cwd").symlink_to(cwd)


def _pruning_fleet(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    env, releases, _log = _release_harness(tmp_path)
    units = Path(env["FAKE_UNITS"])
    for revision in ("b", "c", "d", "e", "f"):
        (releases / (revision * 40) / "src/workers/screener").mkdir(parents=True)
    (releases / "notes").mkdir()
    (releases / f"{'e' * 40}.staging.7").mkdir()
    (tmp_path / "fleet-root/current").symlink_to(f"releases/{'c' * 40}")
    # Worker 1 runs the activated release, worker 2 is a held review still on
    # an older release, and worker 3 is not running.
    for index, pid in ((1, 101), (2, 102), (3, 0)):
        unit = f"ditto-screener-worker@{index}.service"
        (units / f"{unit}.state").write_text("active" if pid else "inactive")
        (units / f"{unit}.pid").write_text(str(pid))
    _proc_entry(tmp_path, 101, tmp_path / "fleet-root/current/src/workers/screener")
    env["SCREENER_FLEET_TEST_PREVIOUS"] = f"releases/{'b' * 40}"
    return env, releases, units


def test_prune_keeps_current_previous_and_held_worker_releases(
    tmp_path: Path,
) -> None:
    env, releases, _units = _pruning_fleet(tmp_path)
    _proc_entry(tmp_path, 102, releases / ("d" * 40) / "src/workers/screener")

    result = _run(env, "prune_releases")

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in releases.iterdir()) == [
        "b" * 40,
        "c" * 40,
        "d" * 40,
        f"{'e' * 40}.staging.7",
        "notes",
    ]
    assert f"pruned superseded release {'e' * 40}" in result.stderr


def test_prune_skips_when_worker_cwd_unreadable(tmp_path: Path) -> None:
    """A live worker whose release cannot be resolved blocks the whole prune."""
    env, releases, _units = _pruning_fleet(tmp_path)
    _proc_entry(tmp_path, 102, None)

    result = _run(env, "prune_releases")

    assert result.returncode == 0, result.stderr
    assert "worker 2 process 102 cwd is unreadable; not pruning" in result.stderr
    assert (releases / ("e" * 40)).exists()
    assert (releases / ("f" * 40)).exists()


def test_prune_retry_after_held_worker_exits(
    tmp_path: Path,
) -> None:
    env, releases, units = _pruning_fleet(tmp_path)
    _proc_entry(tmp_path, 102, releases / ("d" * 40) / "src/workers/screener")
    state = Path(env["SCREENER_FLEET_UPDATE_STATE_DIR"])
    state.joinpath("managed-release.env").write_text(
        f"DESCRIPTOR=example@sha256:{'a' * 64}\n"
        f"REVISION={'c' * 40}\n"
        f"PREVIOUS_RELEASE=releases/{'b' * 40}\n"
    )

    first = _run(env, "prune_activated_release")
    assert first.returncode == 0, first.stderr
    assert (releases / ("d" * 40)).exists()
    assert not (releases / ("e" * 40)).exists()

    (units / "ditto-screener-worker@2.service.pid").write_text("0")
    second = _run(env, "prune_activated_release")
    assert second.returncode == 0, second.stderr
    assert sorted(path.name for path in releases.iterdir()) == [
        "b" * 40,
        "c" * 40,
        f"{'e' * 40}.staging.7",
        "notes",
    ]


def test_already_current_tick_skips_release_deletion_without_previous_identity(
    tmp_path: Path,
) -> None:
    env, releases, _units = _pruning_fleet(tmp_path)
    _proc_entry(tmp_path, 102, releases / ("d" * 40) / "src/workers/screener")
    state = Path(env["SCREENER_FLEET_UPDATE_STATE_DIR"])
    state.joinpath("managed-release.env").write_text(
        f"DESCRIPTOR=older-release\nREVISION={'c' * 40}\n"
    )

    result = _run(env, "prune_activated_release")

    assert result.returncode == 0, result.stderr
    assert "previous release is unknown; not pruning" in result.stderr
    assert (releases / ("e" * 40)).exists()
    assert (releases / ("f" * 40)).exists()


def test_already_current_tick_skips_release_deletion_after_link_drift(
    tmp_path: Path,
) -> None:
    env, releases, _units = _pruning_fleet(tmp_path)
    _proc_entry(tmp_path, 102, releases / ("d" * 40) / "src/workers/screener")
    state = Path(env["SCREENER_FLEET_UPDATE_STATE_DIR"])
    state.joinpath("managed-release.env").write_text(
        f"REVISION={'a' * 40}\nPREVIOUS_RELEASE=releases/{'b' * 40}\n"
    )

    result = _run(env, "prune_activated_release")

    assert result.returncode == 0, result.stderr
    assert "managed and current releases differ; not pruning releases" in result.stderr
    assert (releases / ("e" * 40)).exists()
    assert (releases / ("f" * 40)).exists()


def test_rollback_path_does_not_prune() -> None:
    """Rollback never prunes; activated releases may retry best-effort cleanup."""
    updater = UPDATER.read_text()
    activation = updater[updater.index("activate_release()") :]
    rollback = activation[
        activation.index("if ! start_fleet; then") : activation.index(
            "    fi\n    disarm_fleet_restore"
        )
    ]
    assert "prune" not in rollback
    rolling = updater[
        updater.index("roll_release()") : updater.index("prune_releases()")
    ]
    rolling_rollback = rolling[rolling.index("restore_previous_release ||") :]
    assert "prune" not in rolling_rollback
    assert "return 1" in rolling_rollback
    success = activation[activation.index('rm -f "$FAILED_CANDIDATE_FILE"') :]
    assert "prune_activated_release" in success
    assert "prune_analyzer_images || log" in updater
    assert updater.count('prune_releases "$previous"') == 1
    restore = updater[
        updater.index("restore_fleet_after_abort()") : updater.index(
            "arm_fleet_restore()"
        )
    ]
    assert restore.index("cleanup_staging_on_exit") < restore.index('exit "$status"')


def test_analyzer_prune_is_dangling_and_label_filtered(tmp_path: Path) -> None:
    env, _releases, log = _release_harness(tmp_path)

    result = _run(env, "prune_analyzer_images")

    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "DOCKER_HOST=unix:///run/ditto-screener-docker/docker.sock image prune "
        "--force --filter dangling=true --filter label=ai.heyditto.screener.sha"
    ]
    updater = UPDATER.read_text()
    assert "image prune --all" not in updater
    assert "prune -a" not in updater
    assert "system prune" not in updater
    assert updater.count("image prune") == 1


def test_drain_timeouts_outlast_the_longest_review() -> None:
    """Unit timeouts must exceed a full review and the bounded drain."""
    partition = (
        ROOT / "infra/ansible/roles/screener_partition/tasks/main.yml"
    ).read_text()
    service = (
        ROLE / "templates/ditto-screener-fleet-auto-update.service.j2"
    ).read_text()
    updater = UPDATER.read_text()
    assert "TimeoutStopSec=infinity" not in partition
    assert "TimeoutStopSec=70min" not in partition
    assert partition.count("TimeoutStopSec=120min") == 2
    assert "TimeoutStartSec=180min" in partition
    assert "TimeoutStartSec=180min" in service
    assert (
        'DRAIN_BOUND_SECONDS="${SCREENER_FLEET_DRAIN_BOUND_SECONDS:-4200}"' in updater
    )
    # Worker stop exceeds a review; prep + 2 x 70 min drain < 180.
    assert 4200 * 2 + 20 * 60 < 180 * 60


# Rolling activation. Releases are real analyzer build contexts (the committed
# Dockerfile and the files it copies). The fake systemctl auto-restarts a
# worker whose process exited, exactly like Restart=always, from whatever
# release `current` names at that moment.
OLD = "c" * 40
NEW = "d" * 40
THIRD = "e" * 40
_ROLL_DOCKER = r"""#!/bin/sh
printf 'DOCKER_HOST=%s %s\n' "${DOCKER_HOST:-}" "$*" >>"$FAKE_RELEASE_LOG"
case "$*" in
  *"image inspect --format {{.Id}} ditto-screener-l2-analyzer:active"*)
    echo sha256:previous ;;
esac
"""


def _analyzer_release(releases: Path, revision: str) -> Path:
    context = releases / revision / "src/workers/screener"
    source = ROOT / "workers/screener"
    for relative in (
        "deploy/l2-analyzer.Dockerfile",
        ".dockerignore",
        "tools/l2_analyzer.py",
        *(
            str(path.relative_to(source))
            for path in (source / "ditto_screener/data").glob(
                "starter-kit-provenance-*.json"
            )
        ),
    ):
        target = context / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source / relative).read_bytes())
    return context


def _rolling_fleet(
    tmp_path: Path, workers: dict[int, str]
) -> tuple[dict[str, str], Path, Path, Path]:
    env, releases, release_log = _release_harness(tmp_path)
    (tmp_path / "bin/docker").write_text(_ROLL_DOCKER)
    units = Path(env["FAKE_UNITS"])
    state = Path(env["SCREENER_FLEET_STATE_DIR"])
    for revision in (OLD, NEW):
        _analyzer_release(releases, revision)
    (tmp_path / "fleet-root/current").symlink_to(f"releases/{OLD}")
    release_env = tmp_path / "config/release.env"
    release_env.parent.mkdir()
    release_env.write_text(
        f"SCREENER_FLEET_BUILDER_IMAGE={BUILDER_IMAGE}\n"
        f"SCREENER_FLEET_REVISION={OLD}\nSCREENER_FLEET_VERSION=1.2.2\n"
    )
    for index, behavior in workers.items():
        _worker_unit(units, state, index, behavior=behavior, pid=100 + index)
        _proc_entry(tmp_path, 100 + index, releases / OLD / "src/workers/screener")
    env.update(
        {
            "FAKE_AUTO_RESTART": "1",
            "SCREENER_FLEET_RELEASE_ENV": str(release_env),
            "SCREENER_FLEET_TEST_REVISION": NEW,
            "SCREENER_FLEET_TEST_PREVIOUS": f"releases/{OLD}",
            "SCREENER_FLEET_TEST_BUILDER": BUILDER_IMAGE,
            "SCREENER_FLEET_TEST_DESCRIPTOR": (
                "ghcr.io/ditto-assistant/ditto-subnet-stack@sha256:" + "f" * 64
            ),
            "SCREENER_FLEET_WORKER_PROCESSES": str(len(workers)),
            "SCREENER_FLEET_DRAIN_BOUND_SECONDS": "30",
            "SCREENER_FLEET_ROLL_SETTLE_SECONDS": "1",
            "SCREENER_FLEET_ROLL_START_SECONDS": "4",
        }
    )
    return env, units, state, release_log


def _worker_release(tmp_path: Path, units: Path, index: int) -> str:
    pid = _unit_value(units, index, "pid")
    return Path(os.path.realpath(tmp_path / "proc" / pid / "cwd")).parents[2].name


def _assert_no_review_interrupted(log: Path) -> None:
    recorded = log.read_text()
    assert "interrupted-review" not in recorded
    assert "child-signaled" not in recorded
    for line in recorded.splitlines():
        if "worker@" not in line:
            continue
        verb = line.split()[0]
        # The roll only signals a worker's main process with SIGTERM (sign the
        # verdict, then exit), starts a unit with no process, or enables units.
        # It never stops, restarts, or SIGKILLs a worker.
        assert verb in {"list-units", "show", "kill", "start", "enable"}, line
        if verb == "kill":
            assert "--kill-whom=main -s SIGTERM" in line, line


def test_rolling_activation_keeps_idle_workers_claiming(tmp_path: Path) -> None:
    """Idle workers move to the candidate while a busy review finishes."""
    env, units, state, release_log = _rolling_fleet(
        tmp_path, {1: "idle", 2: "busy", 3: "idle"}
    )
    log = Path(env["SCREENER_TEST_SYSTEMCTL_LOG"])

    result = _run(env, "roll_release")

    assert result.returncode == 0, result.stderr
    assert os.readlink(tmp_path / "fleet-root/current") == f"releases/{NEW}"
    assert (
        f"SCREENER_FLEET_REVISION={NEW}"
        in Path(env["SCREENER_FLEET_RELEASE_ENV"]).read_text()
    )
    assert (
        f"docker.sock tag ditto-screener-l2-analyzer:candidate-{NEW} "
        "ditto-screener-l2-analyzer:active" in release_log.read_text()
    )
    for index in (1, 3):
        assert _unit_value(units, index, "state") == "active"
        assert _unit_value(units, index, "pid") != str(100 + index)
        assert _worker_release(tmp_path, units, index) == NEW
    # The busy review keeps its process on the release it started from.
    assert _unit_value(units, 2, "state") == "active"
    assert _unit_value(units, 2, "pid") == "102"
    assert _worker_release(tmp_path, units, 2) == OLD
    _assert_no_review_interrupted(log)
    assert (state / "updater/held-workers").read_text() == "2 102\n"
    status = (state / "updater/drain-status.env").read_text()
    assert "PHASE=active" in status
    assert "finishing on the previous release: 2:wait" in status


def test_rolling_busy_worker_moves_only_after_its_review(tmp_path: Path) -> None:
    """SIGTERM-then-exit is the only signal; the review ends on its own."""
    env, units, _state, _release_log = _rolling_fleet(
        tmp_path, {1: "busy", 2: "finishes", 3: "busy"}
    )
    log = Path(env["SCREENER_TEST_SYSTEMCTL_LOG"])

    result = _run(env, "roll_release")

    assert result.returncode == 0, result.stderr
    assert int(_unit_value(units, 2, "kills")) >= 2
    assert _worker_release(tmp_path, units, 2) == NEW
    for index in (1, 3):
        assert _unit_value(units, index, "pid") == str(100 + index)
        assert _worker_release(tmp_path, units, index) == OLD
    _assert_no_review_interrupted(log)


def test_crashing_candidate_rolls_every_worker_back(tmp_path: Path) -> None:
    env, units, state, release_log = _rolling_fleet(
        tmp_path, {1: "idle", 2: "busy", 3: "idle"}
    )
    env["FAKE_CRASH_RELEASE"] = NEW
    log = Path(env["SCREENER_TEST_SYSTEMCTL_LOG"])

    result = _run(env, "roll_release")

    assert result.returncode == 1
    assert "candidate failed to start; restoring the previous release" in result.stderr
    assert os.readlink(tmp_path / "fleet-root/current") == f"releases/{OLD}"
    assert (
        "docker.sock tag sha256:previous ditto-screener-l2-analyzer:active"
        in release_log.read_text()
    )
    release_env = Path(env["SCREENER_FLEET_RELEASE_ENV"]).read_text()
    assert f"SCREENER_FLEET_REVISION={OLD}" in release_env
    assert "SCREENER_FLEET_VERSION=1.2.2" in release_env
    assert (state / "updater/failed-candidate").read_text() == (
        env["SCREENER_FLEET_TEST_DESCRIPTOR"] + "\n"
    )
    for index in (1, 3):
        assert _unit_value(units, index, "state") == "active"
        assert _worker_release(tmp_path, units, index) == OLD
    assert _unit_value(units, 2, "pid") == "102"
    _assert_no_review_interrupted(log)
    assert "PHASE=rolled_back" in (state / "updater/drain-status.env").read_text()


def test_roll_without_a_started_candidate_restores_and_retries(
    tmp_path: Path,
) -> None:
    """Every worker busy past the bound: restore, but do not suppress."""
    env, units, state, _release_log = _rolling_fleet(tmp_path, {1: "busy", 2: "busy"})
    env["SCREENER_FLEET_DRAIN_BOUND_SECONDS"] = "1"
    log = Path(env["SCREENER_TEST_SYSTEMCTL_LOG"])

    result = _run(env, "roll_release")

    assert result.returncode == 1
    assert "no worker started on the candidate before the drain bound" in result.stderr
    assert os.readlink(tmp_path / "fleet-root/current") == f"releases/{OLD}"
    assert not (state / "updater/failed-candidate").exists()
    status = (state / "updater/drain-status.env").read_text()
    assert "PHASE=rolled_back" in status
    assert "retried next run" in status
    for index in (1, 2):
        assert _unit_value(units, index, "pid") == str(100 + index)
    _assert_no_review_interrupted(log)


def test_sigterm_during_roll_restores_the_previous_release(tmp_path: Path) -> None:
    env, units, state, _release_log = _rolling_fleet(tmp_path, {1: "busy"})
    env["SCREENER_FLEET_DRAIN_BOUND_SECONDS"] = "600"
    log = Path(env["SCREENER_TEST_SYSTEMCTL_LOG"])
    status = state / "updater/drain-status.env"
    process = subprocess.Popen(
        ["bash", str(UPDATER)],
        env={**env, "SCREENER_FLEET_TEST_ENTRYPOINT": "roll_release"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while "finishing: 1:wait" not in (
            status.read_text() if status.exists() else ""
        ):
            assert time.monotonic() < deadline, "roll never started"
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 143
    assert os.readlink(tmp_path / "fleet-root/current") == f"releases/{OLD}"
    assert (
        f"SCREENER_FLEET_REVISION={OLD}"
        in Path(env["SCREENER_FLEET_RELEASE_ENV"]).read_text()
    )
    assert _unit_value(units, 1, "pid") == "101"
    _assert_no_review_interrupted(log)
    assert "PHASE=aborted" in status.read_text()


def _rolling_check(env: dict[str, str]) -> str:
    result = _run(env, "rolling_check")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_identical_analyzer_inputs_allow_a_rolling_activation(
    tmp_path: Path,
) -> None:
    env, _units, _state, _log = _rolling_fleet(tmp_path, {1: "idle", 2: "busy"})
    # A worker-only change does not touch the analyzer image.
    releases = Path(env["SCREENER_FLEET_RELEASES_DIR"])
    (releases / NEW / "src/workers/screener/worker.py").write_text("changed\n")

    assert _rolling_check(env) == "eligible"


@pytest.mark.parametrize(
    "relative",
    [
        "tools/l2_analyzer.py",
        "ditto_screener/data/starter-kit-provenance-v5.json",
        "deploy/l2-analyzer.Dockerfile",
        ".dockerignore",
    ],
)
def test_analyzer_input_change_falls_back_to_drain_all(
    tmp_path: Path, relative: str
) -> None:
    env, _units, _state, _log = _rolling_fleet(tmp_path, {1: "idle"})
    releases = Path(env["SCREENER_FLEET_RELEASES_DIR"])
    changed = releases / NEW / "src/workers/screener" / relative
    changed.write_text(changed.read_text() + "\n# changed\n")

    assert _rolling_check(env) == f"analyzer inputs differ from live release {OLD}"


def test_unparsed_analyzer_dockerfile_falls_back_to_drain_all(
    tmp_path: Path,
) -> None:
    env, _units, _state, _log = _rolling_fleet(tmp_path, {1: "idle"})
    releases = Path(env["SCREENER_FLEET_RELEASES_DIR"])
    for revision in (OLD, NEW):
        dockerfile = (
            releases / revision / "src/workers/screener/deploy/l2-analyzer.Dockerfile"
        )
        dockerfile.write_text(dockerfile.read_text() + "ADD tools /opt/tools\n")

    assert _rolling_check(env) == f"analyzer inputs differ from live release {OLD}"


def test_worker_still_on_an_older_release_gates_the_roll(tmp_path: Path) -> None:
    """Back-to-back releases: a review from two releases ago still counts."""
    env, _units, _state, _log = _rolling_fleet(tmp_path, {1: "idle", 2: "busy"})
    releases = Path(env["SCREENER_FLEET_RELEASES_DIR"])
    third = _analyzer_release(releases, THIRD)
    (third / "tools/l2_analyzer.py").write_text("# older analyzer\n")
    (tmp_path / "proc/102/cwd").unlink()
    (tmp_path / "proc/102/cwd").symlink_to(third)

    assert _rolling_check(env) == f"analyzer inputs differ from live release {THIRD}"


def test_operator_drain_all_mode_disables_rolling(tmp_path: Path) -> None:
    env, _units, _state, _log = _rolling_fleet(tmp_path, {1: "idle"})
    env["SCREENER_FLEET_ROLLOUT_MODE"] = "drain-all"

    assert _rolling_check(env) == "rollout mode is drain-all"
    updater = UPDATER.read_text()
    activation = updater[updater.index("activate_release()") :]
    drain = activation[activation.index('if [ -z "$ROLLING_BLOCKER" ]; then') :]
    drain = drain[drain.index("  else\n") :]
    assert drain.index("arm_fleet_restore") < drain.index("stop_fleet")
    assert drain.index("stop_fleet") < drain.index("start_fleet")


@pytest.mark.parametrize(
    ("setup", "blocker"),
    [
        ("no-previous", "no previous release to roll from"),
        ("unknown-release", "worker 1 release is unknown"),
        ("above-count", "worker 9 is above the requested count"),
    ],
)
def test_unprovable_fleet_state_falls_back_to_drain_all(
    tmp_path: Path, setup: str, blocker: str
) -> None:
    env, units, state, _log = _rolling_fleet(tmp_path, {1: "idle"})
    if setup == "no-previous":
        env["SCREENER_FLEET_TEST_PREVIOUS"] = ""
    elif setup == "unknown-release":
        (tmp_path / "proc/101/cwd").unlink()
    else:
        _worker_unit(units, state, 9, behavior="busy", pid=109)

    assert _rolling_check(env) == blocker
