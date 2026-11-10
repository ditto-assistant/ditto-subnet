"""Contracts for the on-demand AWS EC2 mode of the screener fleet role.

The Hetzner fleet role also converges a self-bootstrapping EC2 node. Every new
variable must default to today's Hetzner behaviour, so these tests pin both
the AWS-only additions and their absence from a default render.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/hetzner_screener_fleet"
PLAYBOOK = ROOT / "infra/ansible/playbooks/aws-screener-node.yml"
IDLE_SHUTDOWN = ROLE / "files/idle-shutdown.sh"
IDLE_IF = "{% if screener_fleet_idle_exit_seconds | string | length > 0 %}"


def _tasks(name: str = "main.yml") -> list[dict[str, Any]]:
    return yaml.safe_load((ROLE / "tasks" / name).read_text())


def _task(name: str, file: str = "main.yml") -> dict[str, Any]:
    return next(task for task in _tasks(file) if task["name"] == name)


def _when(task: dict[str, Any]) -> list[str]:
    when = task.get("when", [])
    return [when] if isinstance(when, str) else list(when)


def _defaults() -> dict[str, Any]:
    return yaml.safe_load((ROLE / "defaults/main.yml").read_text())


def _block(template: str, opening: str) -> str:
    """Return the text between an exact `{% if %}` and its `{% endif %}`."""
    start = template.index(opening) + len(opening)
    return template[start : template.index("{% endif %}", start)]


# --- defaults keep today's Hetzner behaviour --------------------------------


def test_new_variables_default_to_hetzner_behaviour() -> None:
    defaults = _defaults()

    assert defaults["screener_fleet_provider"] == "hetzner"
    assert defaults["screener_fleet_google_identity"] == "x509"
    assert defaults["screener_fleet_kvm_enabled"] is True
    assert defaults["screener_fleet_idle_exit_seconds"] == ""
    assert defaults["screener_fleet_worker_processes"] == 1
    assert defaults["screener_fleet_runtime_enabled"] is True
    assert defaults["screener_fleet_auto_update_on_boot_sec"] == "10min"
    assert defaults["screener_fleet_auto_update_randomized_delay"] == "3min"
    assert defaults["screener_fleet_aws_wif_provider"] == ""
    assert defaults["screener_fleet_registration_token_secret"] == ""
    # The source-review key stays where a vault-key Hetzner host keeps it.
    assert (
        "screener_fleet_secret_agent_enabled | bool"
        in (defaults["screener_fleet_source_review_key_file"])
    )
    assert (
        "screener_fleet_x509_enabled | bool"
        in (defaults["screener_fleet_secret_agent_enabled"])
    )
    assert (
        "screener_fleet_google_identity == 'aws'"
        in (defaults["screener_fleet_secret_agent_enabled"])
    )


def test_updater_timer_boot_delay_is_a_variable_with_the_old_default() -> None:
    timer = (ROLE / "templates/ditto-screener-fleet-auto-update.timer.j2").read_text()

    assert "OnBootSec={{ screener_fleet_auto_update_on_boot_sec }}" in timer
    assert (
        "RandomizedDelaySec={{ screener_fleet_auto_update_randomized_delay }}" in timer
    )
    assert "OnUnitActiveSec=10min" in timer


# --- provider ---------------------------------------------------------------


def test_fleet_env_renders_the_configured_provider() -> None:
    env = (ROLE / "templates/fleet.env.j2").read_text()

    assert "SCREENER_NODE_PROVIDER={{ screener_fleet_provider }}" in env
    assert "SCREENER_NODE_PROVIDER=hetzner" not in env
    fleet = _task("Assert immutable screener fleet inputs")["ansible.builtin.assert"]
    assert "screener_fleet_provider in ['hetzner', 'aws']" in fleet["that"]
    assert "screener_fleet_google_identity in ['x509', 'aws']" in fleet["that"]
    assert (
        "(screener_fleet_provider == 'aws') == "
        "(screener_fleet_google_identity == 'aws')"
    ) in fleet["that"]


def test_runtime_resource_id_is_provider_aware() -> None:
    runtime = _task("Assert immutable screener runtime inputs")
    that = "\n".join(runtime["ansible.builtin.assert"]["that"])

    assert "screener_fleet_provider == 'hetzner'" in that
    assert "screener_fleet_provider_resource_id is regex('^i-[0-9a-f]{8,17}$')" in that
    assert "EC2 instance id" in runtime["ansible.builtin.assert"]["fail_msg"]


# --- KVM gating -------------------------------------------------------------

_KVM_MARKERS = (
    "qemu",
    "libvirt",
    "virsh",
    "virt-customize",
    "debian_cloud_image",
    "screener_fleet_base_image",
)


def test_every_kvm_task_is_gated() -> None:
    gated = []
    for task in _tasks():
        text = yaml.safe_dump(task)
        name = task.get("name", "<unnamed>")
        if name.startswith("Assert") or not any(
            marker in text for marker in _KVM_MARKERS
        ):
            continue
        gated.append(name)
        assert "screener_fleet_kvm_enabled" in text, name
    # The guest-image pipeline, NAT network, packages, and group membership.
    assert len(gated) >= 13
    service_user = _task("Ensure the dedicated screener service user")
    assert service_user["ansible.builtin.user"]["groups"] == (
        "{{ 'kvm,libvirt,docker' if screener_fleet_kvm_enabled | bool else 'docker' }}"
    )
    container_tools = _task("Install trusted-container tools")
    assert "qemu-system-x86" not in container_tools["ansible.builtin.apt"]["name"]
    assert "docker.io" in container_tools["ansible.builtin.apt"]["name"]
    assert "when" not in container_tools


def test_guest_image_asserts_only_apply_with_kvm() -> None:
    that = _task("Assert immutable screener fleet inputs")["ansible.builtin.assert"][
        "that"
    ]
    image = next(item for item in that if "debian_cloud_image_url" in item)

    assert image.startswith("not screener_fleet_kvm_enabled | bool or")
    assert "screener_fleet_debian_cloud_image_sha256 | length == 64" in image


# --- Google identity --------------------------------------------------------


def test_x509_key_and_certificate_tasks_stay_x509_only() -> None:
    for task in _tasks():
        text = yaml.safe_dump(task)
        if any(
            marker in text
            for marker in (
                "x509_private_key_file",
                "x509_certificate_file",
                "x509_ca_certificate",
                "credential-cert",
                "certificate_config.json",
            )
        ):
            assert "screener_fleet_x509_enabled" in _when(task), task["name"]


def test_secret_agent_and_materializer_run_in_both_identity_modes() -> None:
    for name in (
        "Ensure the dedicated Google secret materializer user",
        "Ensure private Google identity and shared secret directories",
        "Install the source-review secret materializer",
        "Install source-review secret materializer units",
        "Materialize the source-review secret without exposing its value",
        "Enable periodic source-review secret refresh",
        "Require an owner-only source-review key after Google materialization",
    ):
        assert "screener_fleet_secret_agent_enabled | bool" in _when(_task(name)), name
    for name in (
        "Require source-review authority before initial converge",
        "Write the source-review API key",
    ):
        assert "not screener_fleet_secret_agent_enabled | bool" in _when(_task(name))


def test_aws_identity_renders_an_instance_role_credential_config() -> None:
    include = _task("Render the AWS instance-role Google identity")
    assert include["ansible.builtin.include_tasks"] == "google_identity_aws.yml"
    assert "screener_fleet_google_identity == 'aws'" in _when(include)

    render = _task(
        "Render the non-secret AWS external-account configuration",
        "google_identity_aws.yml",
    )
    argv = render["ansible.builtin.command"]["argv"]
    assert argv[:4] == [
        "gcloud",
        "iam",
        "workload-identity-pools",
        "create-cred-config",
    ]
    assert "{{ screener_fleet_aws_wif_provider }}" in argv
    assert "--aws" in argv
    assert "--enable-imdsv2" in argv
    # The GCP side grants the role principalSet directly: no impersonation.
    assert not any(arg.startswith("--service-account") for arg in argv)
    assert not any(arg.startswith("--credential-cert") for arg in argv)
    assert render["become_user"] == "{{ screener_fleet_secret_agent_user }}"
    install = _task(
        "Install the AWS external-account configuration", "google_identity_aws.yml"
    )["ansible.builtin.copy"]
    assert install["dest"] == "{{ screener_fleet_x509_credential_file }}"
    assert install["owner"] == "{{ screener_fleet_secret_agent_user }}"
    assert install["mode"] == "0400"
    apt_key = (ROLE / "tasks/google_identity_aws.yml").read_text()
    assert 'checksum: "sha256:{{ screener_fleet_google_cloud_apt_key_sha256 }}"' in (
        apt_key
    )


def test_aws_federation_contract_is_asserted() -> None:
    task = _task("Require the exact AWS federation contract")
    that = task["ansible.builtin.assert"]["that"]

    assert _when(task) == ["screener_fleet_google_identity == 'aws'"]
    assert (
        "screener_fleet_aws_wif_provider is regex('^projects/[0-9]+/locations/global/"
        "workloadIdentityPools/screener-aws/providers/aws-ec2$')"
    ) in that
    assert any("screener_fleet_registration_token_secret" in item for item in that)
    fleet = _task("Assert immutable screener fleet inputs")["ansible.builtin.assert"]
    assert (
        "not (screener_fleet_google_identity == 'aws' and "
        "screener_fleet_x509_enabled | bool)"
    ) in fleet["that"]


def test_materializer_is_identity_mode_aware() -> None:
    script = (ROLE / "templates/materialize-source-review-secret.sh.j2").read_text()
    service = (
        ROLE / "templates/ditto-screener-source-review-secret.service.j2"
    ).read_text()

    x509_only = "{% if screener_fleet_google_identity != 'aws' %}"
    assert "certificate=" in _block(script, x509_only)
    aws_only = "{% if screener_fleet_google_identity == 'aws' %}"
    checkend = script[script.index(aws_only) :]
    assert "openssl x509 -checkend" in checkend[checkend.index("{% else %}") :]
    assert "openssl x509 -checkend" not in checkend[: checkend.index("{% else %}")]
    assert "GOOGLE_API_CERTIFICATE_CONFIG" in _block(service, x509_only)
    assert service.count("GOOGLE_API_CERTIFICATE_CONFIG") == 1
    assert "Environment=GOOGLE_APPLICATION_CREDENTIALS=" in service
    assert 'gcloud auth login --cred-file="$credential"' in script


# --- enrollment -------------------------------------------------------------


def test_aws_enrollment_reads_the_grant_once_without_logging_it() -> None:
    read = _task(
        "Read the single-use registration grant through the federated instance role"
    )
    when = _when(read)

    assert read["no_log"] is True
    assert "not screener_fleet_credential.stat.exists" in when
    assert "screener_fleet_google_identity == 'aws'" in when
    assert read["ansible.builtin.command"]["argv"][:5] == [
        "gcloud",
        "secrets",
        "versions",
        "access",
        "latest",
    ]
    assert (
        "--secret={{ screener_fleet_registration_token_secret }}"
        in read["ansible.builtin.command"]["argv"]
    )
    environment = read["environment"]
    assert environment["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] == (
        "{{ screener_fleet_x509_credential_file }}"
    )
    # Root reads it with throwaway gcloud state, never the secret agent's.
    assert environment["CLOUDSDK_CONFIG"] == (
        "{{ screener_fleet_aws_enrollment_cloudsdk_config }}"
    )

    write = _task("Write the one-time registration token")
    copy = write["ansible.builtin.copy"]
    assert write["no_log"] is True
    assert copy["dest"] == "{{ screener_fleet_registration_token_file }}"
    assert copy["mode"] == "0600"
    assert copy["owner"] == "{{ screener_fleet_user }}"
    assert "not screener_fleet_credential.stat.exists" in _when(write)
    value = write["vars"]["screener_fleet_registration_token_value"]
    assert "screener_fleet_registration_grant.stdout" in value
    assert "else screener_fleet_registration_token" in value
    require = _task("Require one-time registration authority before initial enrollment")
    assert require["no_log"] is True
    assert require["vars"] == write["vars"]

    names = [task["name"] for task in _tasks()]
    assert names.index("Render the AWS instance-role Google identity") < names.index(
        "Inspect enrolled node credential"
    )
    assert names.index("Inspect enrolled node credential") < names.index(
        "Read the single-use registration grant through the federated instance role"
    )
    assert names.index("Write the one-time registration token") < names.index(
        "Enable and run one-time node enrollment"
    )


# --- idle exit --------------------------------------------------------------


def test_idle_exit_is_rendered_only_when_enabled() -> None:
    env = (ROLE / "templates/fleet.env.j2").read_text()
    worker = (ROLE / "templates/ditto-screener-worker@.service.j2").read_text()

    idle_env = "SCREENER_IDLE_EXIT_SECONDS={{ screener_fleet_idle_exit_seconds | int }}"
    assert idle_env in _block(env, IDLE_IF)
    assert env.count("SCREENER_IDLE_EXIT_SECONDS") == 1
    # Every shipped unit leaves an idle exit (85) down, unconditionally; status
    # 85 only occurs when the opt-in env above is rendered.
    lines = worker.splitlines()
    assert "RestartPreventExitStatus=85" in lines
    assert "SuccessExitStatus=85" in lines
    assert worker.count("RestartPreventExitStatus") == 1
    # The release updater depends on Restart=always for every other exit.
    assert "Restart=always" in lines

    fleet = _task("Assert immutable screener fleet inputs")["ansible.builtin.assert"]
    idle = next(item for item in fleet["that"] if "idle_exit_seconds" in item)
    assert "screener_fleet_idle_exit_seconds | int >= 60" in idle
    assert "screener_fleet_idle_exit_seconds | int <= 86400" in idle


def test_idle_shutdown_units_install_only_when_enabled() -> None:
    tasks = _tasks("idle_shutdown.yml")
    service = (ROLE / "templates/ditto-screener-idle-shutdown.service.j2").read_text()
    timer = (ROLE / "templates/ditto-screener-idle-shutdown.timer.j2").read_text()

    enabled = "screener_fleet_idle_exit_seconds | string | length > 0"
    for task in tasks[:3]:
        assert enabled in _when(task), task["name"]
    for task in tasks[3:]:
        assert any(
            f"not (screener_fleet_runtime_enabled and {enabled})" in item
            for item in _when(task)
        ), task["name"]
    assert "OnUnitActiveSec=60s" in timer
    assert "OnBootSec={{ screener_fleet_idle_shutdown_on_boot_sec }}" in timer
    assert _defaults()["screener_fleet_idle_shutdown_on_boot_sec"] == "10min"
    assert "User=" not in service
    assert (
        "Environment=SCREENER_IDLE_WORKER_PROCESSES="
        "{{ screener_fleet_worker_processes }}" in service
    )
    assert (
        "Environment=SCREENER_IDLE_UPDATE_LOCK="
        "{{ screener_fleet_update_state_dir }}/lock" in service
    )
    assert "ExecStart={{ screener_fleet_idle_shutdown_path }}" in service
    include = _task("Converge the on-demand idle shutdown")
    assert include["ansible.builtin.include_tasks"] == "idle_shutdown.yml"


# --- idle shutdown decision logic -------------------------------------------

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  show)
    prop="$3"; unit="$5"
    if [ -f "$FAKE_UNITS/$unit.$prop" ]; then
      cat "$FAKE_UNITS/$unit.$prop"
    else
      case "$prop" in
        ActiveState) echo inactive ;;
        *) echo 0 ;;
      esac
    fi
    ;;
  list-units)
    cat "$FAKE_UNITS/list-units" 2>/dev/null || true
    ;;
  poweroff)
    echo poweroff >>"$FAKE_CALLS"
    ;;
  *)
    echo "unexpected systemctl $*" >&2
    exit 64
    ;;
esac
"""

FAKE_FLOCK = """#!/usr/bin/env bash
echo "flock $*" >>"$FAKE_CALLS"
exit "${FAKE_FLOCK_EXIT:-0}"
"""


class IdleHost:
    def __init__(self, tmp_path: Path, workers: int = 2) -> None:
        self.bin = tmp_path / "bin"
        self.units = tmp_path / "units"
        self.state = tmp_path / "state"
        self.calls = tmp_path / "calls"
        for directory in (self.bin, self.units, self.state / "updater"):
            directory.mkdir(parents=True)
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("flock", FAKE_FLOCK)):
            command = self.bin / name
            command.write_text(body)
            command.chmod(0o755)
        self.workers = workers
        self.flock_exit = 0
        # A freshly booted host unless a test says otherwise.
        self.uptime = tmp_path / "uptime"
        self.uptime.write_text("600.25 1200.50\n")
        for index in range(1, workers + 1):
            (self.state / "workers" / str(index)).mkdir(parents=True)
            self.worker(index, "inactive", code=1, status=85)

    def unit(self, unit: str, prop: str, value: str) -> None:
        (self.units / f"{unit}.{prop}").write_text(value + "\n")

    def worker(self, index: int, state: str, *, code: int, status: int) -> None:
        unit = f"ditto-screener-worker@{index}.service"
        self.unit(unit, "ActiveState", state)
        self.unit(unit, "ExecMainCode", str(code))
        self.unit(unit, "ExecMainStatus", str(status))

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ | {
            "SCREENER_IDLE_SYSTEMCTL": str(self.bin / "systemctl"),
            "SCREENER_IDLE_FLOCK": str(self.bin / "flock"),
            "SCREENER_IDLE_STATE_DIR": str(self.state),
            "SCREENER_IDLE_WORKER_PROCESSES": str(self.workers),
            "SCREENER_IDLE_UPTIME_FILE": str(self.uptime),
            "FAKE_UNITS": str(self.units),
            "FAKE_CALLS": str(self.calls),
            "FAKE_FLOCK_EXIT": str(self.flock_exit),
        }
        environment.update(env)
        return subprocess.run(
            ["bash", str(IDLE_SHUTDOWN)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    @property
    def powered_off(self) -> bool:
        return self.calls.exists() and "poweroff" in self.calls.read_text().split()


def test_idle_shutdown_powers_off_when_every_worker_idle_exited(
    tmp_path: Path,
) -> None:
    host = IdleHost(tmp_path)
    host.worker(2, "failed", code=1, status=85)

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert host.powered_off
    assert "powering off: all 2 workers exited idle (85)" in result.stdout
    # The updater lock is taken before deciding and held into the poweroff.
    assert "flock -n 9" in host.calls.read_text()
    assert (host.state / "updater/lock").exists()


@pytest.mark.parametrize(
    ("arrange", "reason"),
    [
        (
            lambda host: host.worker(1, "active", code=0, status=0),
            "worker 1 is active",
        ),
        (
            lambda host: host.worker(2, "activating", code=1, status=1),
            "worker 2 is activating",
        ),
        (
            lambda host: host.worker(1, "inactive", code=1, status=0),
            "worker 1 last exited with status 0 (code 1), not an idle exit 85",
        ),
        (
            lambda host: host.worker(2, "failed", code=1, status=1),
            "worker 2 last exited with status 1 (code 1)",
        ),
        (
            # Killed by a signal (CLD_KILLED) is never an idle exit.
            lambda host: host.worker(1, "inactive", code=2, status=85),
            "worker 1 last exited with status 85 (code 2), not an idle exit",
        ),
        (
            lambda host: (host.state / "workers/2/active-lease.json").write_text("{}"),
            "worker 2 holds an active lease",
        ),
        (
            lambda host: host.unit(
                "ditto-screener-fleet-auto-update.service", "ActiveState", "activating"
            ),
            "release updater is activating",
        ),
        (
            lambda host: setattr(host, "flock_exit", 1),
            "release updater holds its lock",
        ),
        (
            lambda host: host.unit(
                "ditto-screener-enroll.service", "ActiveState", "activating"
            ),
            "enrollment is activating",
        ),
        (
            lambda host: (host.units / "list-units").write_text(
                "ditto-screener-worker@3.service loaded active running Ditto worker 3\n"
            ),
            "worker unit still running: ditto-screener-worker@3.service",
        ),
    ],
)
def test_idle_shutdown_stays_up_and_says_why(
    tmp_path: Path, arrange: Any, reason: str
) -> None:
    host = IdleHost(tmp_path)
    arrange(host)

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert not host.powered_off
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("ditto-screener-idle-shutdown: staying up: ")
    assert reason in lines[0]


def test_idle_shutdown_bounds_the_uptime_of_a_host_that_never_idles(
    tmp_path: Path,
) -> None:
    host = IdleHost(tmp_path)
    # A crash-looping worker never exits with the idle status.
    host.worker(1, "activating", code=1, status=1)
    host.uptime.write_text("21600.10 1.0\n")

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert host.powered_off
    assert "up 21600s (bound 21600s) with no active lease" in result.stdout


@pytest.mark.parametrize(
    "arrange",
    [
        lambda host: (host.state / "workers/1/active-lease.json").write_text("{}"),
        lambda host: setattr(host, "flock_exit", 1),
    ],
)
def test_uptime_bound_never_preempts_a_lease_or_the_updater(
    tmp_path: Path, arrange: Any
) -> None:
    host = IdleHost(tmp_path)
    host.worker(1, "active", code=0, status=0)
    host.uptime.write_text("90000.00 1.0\n")
    arrange(host)

    result = host.run()

    assert not host.powered_off
    assert "staying up" in result.stdout


@pytest.mark.parametrize(
    ("lease", "stays_up"),
    [
        # Expired well past the grace: a worker killed mid-review left it.
        ('{"lease_deadline": 1000, "progress_at": 900}', False),
        ('{"lease_deadline":1000}', False),
        # Still live, or within the grace.
        ('{"lease_deadline": 9999999999, "progress_at": 1}', True),
        # Unreadable deadline: treat as live.
        ('{"progress_at": 1}', True),
        ("not json", True),
    ],
)
def test_only_a_live_lease_blocks_the_uptime_bound(
    tmp_path: Path, lease: str, stays_up: bool
) -> None:
    host = IdleHost(tmp_path)
    host.worker(1, "active", code=0, status=0)
    host.uptime.write_text("90000.00 1.0\n")
    (host.state / "workers/1/active-lease.json").write_text(lease)

    result = host.run()

    assert host.powered_off is not stays_up
    if stays_up:
        assert "worker 1 holds an active lease" in result.stdout
    else:
        assert "ignoring expired lease file" in result.stdout


def test_uptime_bound_can_be_disabled(tmp_path: Path) -> None:
    host = IdleHost(tmp_path)
    host.worker(1, "active", code=0, status=0)
    host.uptime.write_text("90000.00 1.0\n")

    result = host.run(SCREENER_IDLE_MAX_UPTIME_SECONDS="0")

    assert not host.powered_off
    assert "worker 1 is active" in result.stdout


def test_idle_shutdown_stays_up_without_the_updater_state_directory(
    tmp_path: Path,
) -> None:
    host = IdleHost(tmp_path)

    result = host.run(SCREENER_IDLE_UPDATE_LOCK=str(tmp_path / "missing/lock"))

    assert result.returncode == 0
    assert not host.powered_off
    assert "release updater state directory is missing" in result.stdout


@pytest.mark.parametrize("workers", ["", "0", "17", "two"])
def test_idle_shutdown_rejects_an_invalid_worker_count(
    tmp_path: Path, workers: str
) -> None:
    host = IdleHost(tmp_path)

    result = host.run(SCREENER_IDLE_WORKER_PROCESSES=workers)

    assert result.returncode == 2
    assert not host.powered_off


@pytest.mark.skipif(shutil.which("flock") is None, reason="util-linux flock")
def test_idle_shutdown_respects_a_real_held_updater_lock(tmp_path: Path) -> None:
    host = IdleHost(tmp_path)
    lock = host.state / "updater/lock"
    holder = subprocess.Popen(
        ["flock", str(lock), "sleep", "30"],
    )
    try:
        for _ in range(50):
            probe = subprocess.run(
                ["flock", "-n", str(lock), "true"], check=False, capture_output=True
            )
            if probe.returncode != 0:
                break
            time.sleep(0.1)
        else:
            pytest.fail("the lock holder never acquired the updater lock")
        result = host.run(SCREENER_IDLE_FLOCK=shutil.which("flock") or "flock")
    finally:
        holder.kill()
        holder.wait()

    assert not host.powered_off
    assert "release updater holds its lock" in result.stdout


def test_idle_shutdown_script_is_syntactically_valid() -> None:
    subprocess.run(["bash", "-n", str(IDLE_SHUTDOWN)], check=True)
    assert os.access(IDLE_SHUTDOWN, os.X_OK)
    text = IDLE_SHUTDOWN.read_text()
    assert 'exec "$SYSTEMCTL" poweroff' in text
    assert "active-lease.json" in text


# --- playbook ---------------------------------------------------------------


def test_aws_playbook_converges_locally_with_aws_defaults() -> None:
    plays = yaml.safe_load(PLAYBOOK.read_text())
    assert len(plays) == 1
    play = plays[0]

    assert play["hosts"] == "localhost"
    assert play["connection"] == "local"
    assert play["become"] is True
    variables = play["vars"]
    assert variables["screener_fleet_provider"] == "aws"
    assert variables["screener_fleet_google_identity"] == "aws"
    assert variables["screener_fleet_kvm_enabled"] is False
    assert variables["screener_fleet_x509_enabled"] is False
    assert variables["screener_fleet_auto_update_on_boot_sec"] == "1min"
    # Required identity must come from bootstrap-vars.json, never a role default.
    for name in (
        "screener_fleet_node_id",
        "screener_fleet_provider_resource_id",
        "screener_fleet_release_sha",
        "screener_fleet_builder_image",
        "screener_fleet_aws_wif_provider",
        "screener_fleet_registration_token_secret",
    ):
        assert variables[name] == "", name
    # Roles by path: cloud-init runs from a plain checkout without ansible.cfg.
    assert play["roles"] == [{"role": "../roles/hetzner_screener_fleet"}]
    cache = yaml.safe_load((ROLE / "tasks/cache_gc.yml").read_text())
    assert cache[0]["ansible.builtin.include_role"]["name"] == (
        "{{ role_path }}/../screener_worker"
    )


def test_aws_playbook_skips_operator_access_and_hetzner_bootstrap() -> None:
    text = PLAYBOOK.read_text()
    play = yaml.safe_load(text)[0]

    base = next(
        task for task in play["pre_tasks"] if "ansible.builtin.include_role" in task
    )
    assert base["ansible.builtin.include_role"]["name"] == "../roles/base"
    assert base["loop"] == [
        "packages.yml",
        "journald.yml",
        "unattended_upgrades.yml",
        "uv.yml",
    ]
    for forbidden in (
        "screener_fleet_bootstrap_user",
        "sudoers",
        "authorized_key",
        "screener_logs_access",
        "users.yml",
        "ssh.yml",
        "ufw.yml",
    ):
        assert forbidden not in yaml.safe_dump(play), forbidden


def test_aws_playbook_asserts_a_well_formed_bootstrap_contract() -> None:
    play = yaml.safe_load(PLAYBOOK.read_text())[0]
    that = play["pre_tasks"][0]["ansible.builtin.assert"]["that"]
    joined = "\n".join(that)

    for expected in (
        "screener_fleet_provider_resource_id is regex('^i-[0-9a-f]{8,17}$')",
        "screener_fleet_release_sha is regex('^[0-9a-f]{40}$')",
        "screener_fleet_builder_image is regex('^[a-z0-9.-]+(?::[0-9]+)?/"
        "[a-z0-9._/-]+@sha256:[0-9a-f]{64}$')",
        "screener_fleet_node_id is regex('^subnet-screener-[1-9][0-9]*$')",
        "screener_fleet_aws_wif_provider is regex('^projects/[0-9]+/locations/global/"
        "workloadIdentityPools/screener-aws/providers/aws-ec2$')",
        "screener_fleet_registration_token_secret is regex(",
        "screener_fleet_platform_url is regex('^https://[^/]+$')",
        "screener_fleet_idle_exit_seconds | int <= 86400",
    ):
        assert expected in joined, expected
    # Mirror the regexes against representative values.
    assert re.fullmatch(r"i-[0-9a-f]{8,17}", "i-0123456789abcdef0")
    assert not re.fullmatch(r"i-[0-9a-f]{8,17}", "robot-server-id")


# --- optional full render (jinja2 is not a locked test dependency) ----------


def _render_role(overrides: dict[str, Any]) -> dict[str, str]:
    jinja2 = pytest.importorskip("jinja2")

    def to_bool(value: Any) -> bool:
        return value is True or str(value).strip().lower() in {"true", "1", "yes", "on"}

    environment = jinja2.Environment(
        trim_blocks=True, undefined=jinja2.StrictUndefined, keep_trailing_newline=True
    )
    environment.filters["quote"] = lambda value: shlex.quote(str(value))
    environment.filters["bool"] = to_bool
    # Ansible resolves defaults lazily; re-render every original expression
    # against the previous pass until nothing changes.
    original: dict[str, Any] = {**_defaults(), **overrides}
    context = dict(original)
    for _ in range(10):
        resolved = {
            key: environment.from_string(value).render(context).strip()
            if isinstance(value, str) and "{{" in value
            else value
            for key, value in original.items()
        }
        if resolved == context:
            break
        context = resolved
    return {
        path.name.removesuffix(".j2"): environment.from_string(path.read_text()).render(
            context
        )
        for path in (ROLE / "templates").glob("*.j2")
    }


AWS = {
    "screener_fleet_node_id": "subnet-screener-3",
    "screener_fleet_provider": "aws",
    "screener_fleet_provider_resource_id": "i-0123456789abcdef0",
    "screener_fleet_google_identity": "aws",
    "screener_fleet_kvm_enabled": False,
    "screener_fleet_idle_exit_seconds": 900,
    "screener_fleet_auto_update_on_boot_sec": "1min",
    "screener_fleet_release_sha": "a" * 40,
}
HETZNER = {"screener_fleet_release_sha": "a" * 40, "screener_fleet_x509_enabled": True}


def test_full_render_aws_mode() -> None:
    rendered = _render_role(AWS)

    assert "SCREENER_NODE_PROVIDER=aws\n" in rendered["fleet.env"]
    assert "SCREENER_IDLE_EXIT_SECONDS=900\n" in rendered["fleet.env"]
    assert (
        "SCREENER_SOURCE_REVIEW_API_KEY_FILE=/var/lib/ditto-screener-fleet/secrets/"
        in rendered["fleet.env"]
    )
    assert "RestartPreventExitStatus=85\n" in rendered["ditto-screener-worker@.service"]
    assert "Restart=always\n" in rendered["ditto-screener-worker@.service"]
    assert "OnBootSec=1min\n" in rendered["ditto-screener-fleet-auto-update.timer"]
    secret_service = rendered["ditto-screener-source-review-secret.service"]
    assert "GOOGLE_API_CERTIFICATE_CONFIG" not in secret_service
    assert "AWS instance-role federation" in secret_service
    script = rendered["materialize-source-review-secret.sh"]
    assert "openssl" not in script
    assert "certificate=" not in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_full_render_hetzner_defaults_are_unchanged() -> None:
    rendered = _render_role(HETZNER)

    assert "SCREENER_NODE_PROVIDER=hetzner\n" in rendered["fleet.env"]
    assert "SCREENER_IDLE_EXIT_SECONDS" not in rendered["fleet.env"]
    assert "RestartPreventExitStatus" not in rendered["ditto-screener-worker@.service"]
    assert "SuccessExitStatus" not in rendered["ditto-screener-worker@.service"]
    assert "OnBootSec=10min\n" in rendered["ditto-screener-fleet-auto-update.timer"]
    assert (
        "RandomizedDelaySec=3min\n"
        in rendered["ditto-screener-fleet-auto-update.timer"]
    )
    secret_service = rendered["ditto-screener-source-review-secret.service"]
    assert "GOOGLE_API_CERTIFICATE_CONFIG=" in secret_service
    assert "X.509 federation" in secret_service
    assert (
        "openssl x509 -checkend 1209600"
        in rendered["materialize-source-review-secret.sh"]
    )


def test_aws_playbook_refuses_a_release_whose_worker_cannot_idle_out() -> None:
    play = yaml.safe_load(PLAYBOOK.read_text())[0]
    tasks = {task["name"]: task for task in play["pre_tasks"]}
    read = tasks["Read the release worker's configuration source"]
    check = tasks["Require a release whose worker supports idle exit"]

    # The playbook runs from the exact release checkout it installs.
    assert read["ansible.builtin.slurp"]["src"] == (
        "{{ playbook_dir }}/../../../workers/screener/ditto_screener/config.py"
    )
    assert (PLAYBOOK.parent / "../../../workers/screener/ditto_screener").resolve() == (
        ROOT / "workers/screener/ditto_screener"
    )
    assert (
        "'SCREENER_IDLE_EXIT_SECONDS' in" in check["ansible.builtin.assert"]["that"][0]
    )
    for task in (read, check):
        assert task["when"] == "screener_fleet_idle_exit_seconds | string | length > 0"
    names = list(tasks)
    assert names.index(check["name"]) < names.index(
        "Apply the shared base packages, journald, upgrades, and uv"
    )
