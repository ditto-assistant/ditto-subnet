"""Contracts for outbound-only authenticated screener-fleet delivery."""

from __future__ import annotations

import json
import os
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
    assert activation.index('"$release_dir/src/scripts/screener-fleet-drain.py"') < (
        activation.index("stop_fleet\n")
    )
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
        '"$SYSTEMCTL" start ditto-screener-fleet-agent.service'
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
    lease = fleet / "workers" / worker_index(unit) / "active-lease.json"
    lease.unlink(missing_ok=True)


command = args[0]
unit = args[-1]
if command == "list-units":
    for path in sorted(units.glob("ditto-screener-worker@*.service.state")):
        print(path.name[: -len(".state")] + " loaded active running")
elif command == "show":
    key = args[2]
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
            if behavior == "idle" or (behavior == "finishes" and kills >= 2):
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
        next_pid = int(read("next", "pid", "9000")) + 1
        write("next", "pid", next_pid)
        write(unit, "state", "active")
        write(unit, "pid", next_pid)
        write(unit, "behavior", "idle")
elif command == "is-active":
    sys.exit(0 if read(unit, "state", "inactive") == "active" else 3)
elif command == "disable" and os.environ.get("FAKE_FAIL_DISABLE"):
    sys.exit(1)
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
    assert "start ditto-screener-fleet-agent.service" in recorded
    for index in (1, 2, 3):
        assert f"enable ditto-screener-worker@{index}.service" in recorded
    assert "restart ditto-screener-worker@1.service" not in recorded
    assert _unit_value(units, 1, "pid") == "101"
    assert "restart ditto-screener-worker@2.service" in recorded
    assert "restart ditto-screener-worker@3.service" in recorded
    assert "PHASE=active" in (state / "updater/drain-status.env").read_text()


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


def test_aborted_drain_restores_the_fleet_agent(tmp_path: Path) -> None:
    """A failure after the agent stopped must not leave the node down."""
    env, units, state, log = _fleet_harness(tmp_path)
    env["FAKE_FAIL_DISABLE"] = "1"
    env["SCREENER_FLEET_WORKER_PROCESSES"] = "1"
    _worker_unit(units, state, 1, behavior="idle", pid=101)
    _worker_unit(units, state, 4, behavior="idle", pid=104)

    result = _run(env, "stop_fleet")

    assert result.returncode != 0
    recorded = log.read_text().splitlines()
    assert "start --no-block ditto-screener-fleet-agent.service" in recorded
    assert "start --no-block ditto-screener-worker@1.service" in recorded
    assert "PHASE=aborted" in (state / "updater/drain-status.env").read_text()


def test_timeout_sigterm_during_drain_restores_the_fleet_agent(
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
    assert "start --no-block ditto-screener-fleet-agent.service" in recorded
    assert "interrupted-review" not in log.read_text()


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
    assert partition.count("TimeoutStopSec=120min") == 3
    assert "TimeoutStartSec=180min" in partition
    assert "TimeoutStartSec=180min" in service
    assert (
        'DRAIN_BOUND_SECONDS="${SCREENER_FLEET_DRAIN_BOUND_SECONDS:-4200}"' in updater
    )
    # 115 min worst-case review < 120 min stop; prep + 2 x 70 min drain < 180.
    assert 4200 * 2 + 20 * 60 < 180 * 60
