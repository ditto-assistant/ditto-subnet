"""Synthetic custody lifecycle tests; never start a unit or read private material."""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_custody_service"
HELPER = ROLE / "files/custody-run.py"
spec = importlib.util.spec_from_file_location("custody_run", HELPER)
assert spec is not None and spec.loader is not None
RUN = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RUN)

WORKER = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
OTHER = "16fd2706-8baf-433b-82eb-8c7fada847da"


def stable_config(tmp_path: Path) -> dict:
    release = "/var/lib/ditto-coding-custody/release/coding-private-v2-r2"
    return {
        "schema": "dittobench-coding-private-v2-custody-config-v1",
        "shadow_only": True,
        "weight_eligible": False,
        "registration_file": f"{release}/registration.json",
        "transport_manifest": f"{release}/transport-manifest.json",
        "payload_authority": f"{release}/payload-authority.json",
        "publication_receipt": f"{release}/publication-receipt.json",
        "curator_public_key": f"{release}/curator-public.pem",
        "reader_authority_sha256": "ab" * 32,
        "private_key_file": "/var/lib/ditto-coding-custody/keys/private-input-rsa.pem",
        "postgres_environment_file": str(tmp_path / "private/postgres.json"),
        "socket_path": str(tmp_path / "run/custody.sock"),
        "client_uid": os.getuid() + 1,
    }


def write_base(layout: SimpleNamespace, config: dict, **extra) -> None:
    Path(layout.base).write_text(
        json.dumps(
            {"schema": "ditto-coding-custody-base-v1", "config": config, **extra}
        )
    )
    os.chmod(layout.base, 0o600)


@pytest.fixture
def layout(tmp_path: Path) -> SimpleNamespace:
    for name, mode in (("etc", 0o700), ("runs", 0o700), ("run", 0o755)):
        (tmp_path / name).mkdir()
        os.chmod(tmp_path / name, mode)
    units: list[str] = []

    def systemctl(arguments: list[str]) -> str:
        # The lifecycle only ever issues this read-only unit query.
        assert arguments == [
            "list-units",
            "--all",
            "--plain",
            "--no-legend",
            "--full",
            "ditto-coding-custody@*.service",
        ]
        return "\n".join(units)

    value = SimpleNamespace(
        base=str(tmp_path / "etc/base.json"),
        runs=str(tmp_path / "runs"),
        socket=str(tmp_path / "run/custody.sock"),
        root_uid=os.getuid(),
        custody_uid=os.getuid(),
        custody_gid=os.getgid(),
        units=units,
        systemctl=systemctl,
        tmp=tmp_path,
    )
    write_base(value, stable_config(tmp_path))
    return value


def live(layout: SimpleNamespace, worker: str, state: str = "active") -> None:
    layout.units.append(
        f"ditto-coding-custody@{worker}.service loaded {state} running Native custody"
    )


def config_path(layout: SimpleNamespace, worker: str = WORKER) -> Path:
    return Path(layout.runs) / worker / "config.json"


def test_prepare_binds_one_owner_only_config_to_the_exact_worker(layout) -> None:
    receipt = RUN.prepare(layout, WORKER)
    path = config_path(layout)
    raw = path.read_bytes()
    assert json.loads(raw) == {**stable_config(layout.tmp), "worker_id": WORKER}
    assert raw == RUN.canonical(json.loads(raw))
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert receipt == {
        "schema": "ditto-coding-custody-run-v1",
        "action": "prepared",
        "worker_id": WORKER,
        "unit": f"ditto-coding-custody@{WORKER}.service",
        "config_sha256": hashlib.sha256(raw).hexdigest(),
        "postgres_environment_present": False,
        "started": False,
        "shadow_only": True,
        "weight_eligible": False,
    }
    assert "/" not in json.dumps({k: v for k, v in receipt.items() if k != "unit"})


@pytest.mark.parametrize(
    "value",
    [
        WORKER.upper(),
        "00000000-0000-0000-0000-000000000000",
        f"{{{WORKER}}}",
        WORKER.replace("-", ""),
        f"../{WORKER}",
        f"{WORKER}\n",
        WORKER[:-1],
        7,
    ],
)
def test_only_canonical_nonzero_worker_uuids_are_accepted(layout, value) -> None:
    with pytest.raises(ValueError):
        RUN.prepare(layout, value)
    assert os.listdir(layout.runs) == []


def test_prepare_refuses_a_second_run_live_instance_or_stale_socket(layout) -> None:
    RUN.prepare(layout, WORKER)
    with pytest.raises(ValueError):
        RUN.prepare(layout, OTHER)
    RUN.release(layout, WORKER)
    live(layout, OTHER)
    with pytest.raises(ValueError):
        RUN.prepare(layout, WORKER)
    layout.units.clear()
    Path(layout.socket).write_text("")
    with pytest.raises(ValueError):
        RUN.prepare(layout, WORKER)
    assert os.listdir(layout.runs) == []


def test_check_admits_only_the_matching_unit_instance(layout) -> None:
    RUN.prepare(layout, WORKER)
    live(layout, WORKER, "activating")
    assert RUN.check(layout, WORKER)["action"] == "checked"
    with pytest.raises(ValueError):
        RUN.check(layout, OTHER)
    live(layout, OTHER)
    with pytest.raises(ValueError):
        RUN.check(layout, WORKER)


@pytest.mark.parametrize(
    "tamper",
    ["worker", "field", "format", "mode", "symlink", "socket"],
)
def test_check_refuses_any_config_or_socket_drift(layout, tamper) -> None:
    RUN.prepare(layout, WORKER)
    path = config_path(layout)
    document = json.loads(path.read_text())
    if tamper == "worker":
        path.write_bytes(RUN.canonical({**document, "worker_id": OTHER}))
    elif tamper == "field":
        path.write_bytes(RUN.canonical({**document, "client_uid": os.getuid() + 2}))
    elif tamper == "format":
        path.write_text(json.dumps(document, indent=2))
    elif tamper == "mode":
        os.chmod(path, 0o644)
    elif tamper == "symlink":
        target = layout.tmp / "elsewhere.json"
        target.write_bytes(path.read_bytes())
        os.chmod(target, 0o600)
        path.unlink()
        path.symlink_to(target)
    else:
        Path(layout.socket).write_text("")
    with pytest.raises((ValueError, OSError)):
        RUN.check(layout, WORKER)


def test_release_removes_only_the_run_and_is_idempotent(layout) -> None:
    RUN.prepare(layout, WORKER)
    assert RUN.release(layout, WORKER)["removed"] is True
    assert os.listdir(layout.runs) == []
    assert RUN.release(layout, WORKER)["removed"] is False


@pytest.mark.parametrize("unexpected", ["extra-file", "symlink-config"])
def test_release_refuses_unexpected_run_contents(layout, unexpected) -> None:
    RUN.prepare(layout, WORKER)
    path = config_path(layout)
    if unexpected == "extra-file":
        (path.parent / "other").write_text("")
    else:
        path.unlink()
        path.symlink_to(layout.base)
    with pytest.raises(ValueError):
        RUN.release(layout, WORKER)
    assert Path(layout.base).exists()


def test_discard_requires_every_custody_instance_to_be_stopped(layout) -> None:
    RUN.prepare(layout, WORKER)
    live(layout, WORKER, "deactivating")
    with pytest.raises(ValueError):
        RUN.discard(layout, WORKER)
    layout.units[:] = [
        f"ditto-coding-custody@{WORKER}.service loaded failed failed Native custody"
    ]
    assert RUN.discard(layout, WORKER)["removed"] is True


MUTATIONS = {
    "worker_id": WORKER,
    "client_uid": os.getuid(),
    "private_key_file": "keys/private-input-rsa.pem",
    "registration_file": "/var/lib/../etc/registration.json",
    "weight_eligible": True,
    "reader_authority_sha256": "AB" * 32,
}


@pytest.mark.parametrize(
    "mutation",
    [*MUTATIONS, "missing_client_uid", "boolean_client_uid", "other_socket"],
)
def test_base_must_be_exact_worker_free_and_consistent(layout, mutation) -> None:
    config = stable_config(layout.tmp)
    if mutation == "missing_client_uid":
        config.pop("client_uid")
    elif mutation == "boolean_client_uid":
        config["client_uid"] = True
    elif mutation == "other_socket":
        config["socket_path"] = str(layout.tmp / "other.sock")
    else:
        config[mutation] = MUTATIONS[mutation]
    write_base(layout, config)
    with pytest.raises((ValueError, KeyError)):
        RUN.prepare(layout, WORKER)
    assert os.listdir(layout.runs) == []


@pytest.mark.parametrize("document", ["[]", '"base"', "{}", "not json"])
def test_malformed_base_fails_closed_as_a_rejection(layout, document) -> None:
    Path(layout.base).write_text(document)
    with pytest.raises(ValueError):
        RUN.prepare(layout, WORKER)


def test_base_must_be_owner_only(layout) -> None:
    os.chmod(layout.base, 0o640)
    with pytest.raises(ValueError):
        RUN.prepare(layout, WORKER)
    os.chmod(layout.base, 0o600)
    os.chmod(os.path.dirname(layout.base), 0o750)
    with pytest.raises(ValueError):
        RUN.prepare(layout, WORKER)


def test_main_accepts_only_fixed_actions(layout, capsys) -> None:
    RUN.main(["prepare", WORKER], layout)
    assert json.loads(capsys.readouterr().out)["action"] == "prepared"
    for argv in (["start", WORKER], ["prepare"], ["prepare", WORKER, "extra"]):
        with pytest.raises(ValueError):
            RUN.main(argv, layout)


def test_cli_off_host_fails_with_a_fixed_message() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(HELPER), "prepare", WORKER],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "native custody lifecycle failed\n"


def test_lifecycle_fields_match_exactly_what_the_custody_service_reads() -> None:
    service = (ROOT / "apps/platform/ditto/coding_private_v2_custody.py").read_text()
    serve = service.split("async def serve", 1)[1].split("\ndef main", 1)[0]
    read = set(re.findall(r'config(?:\.get\()?\[?\(?"([a-z0-9_]+)"', serve))
    read |= set(
        re.findall(
            r'^\s+"([a-z_]+)",$', serve.split("paths = {", 1)[1].split("}", 1)[0], re.M
        )
    )
    assert read == RUN.STABLE_FIELDS | {"worker_id"}
    template = (ROLE / "templates/base.json.j2").read_text()
    config = template.split('"config": {', 1)[1].split("}", 1)[0]
    assert set(re.findall(r'^\s+"([a-z0-9_]+)":', config, re.M)) == RUN.STABLE_FIELDS


def test_install_is_default_off_confirmation_gated_and_never_starts_custody() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults == {
        "coding_hosted_custody_service_enabled": False,
        "coding_hosted_custody_service_confirmation": "",
        "coding_hosted_custody_service_source_revision": "",
        "coding_hosted_custody_runtime_revision": "",
        "coding_hosted_custody_postgres_ip": "",
        "coding_hosted_custody_release_id": "",
        "coding_hosted_custody_reader_authority_sha256": "",
        "coding_hosted_custody_release_inputs": {},
    }
    tasks = (ROLE / "tasks/main.yml").read_text()
    assert "INSTALL NATIVE CODING CUSTODY SERVICE" in tasks
    assert "ansible_facts['hostname'] == 'ditto-coding-hosted-v2'" in tasks
    assert "checksum_algorithm: sha256" in tasks
    assert "force: false" in tasks
    assert "daemon_reload: true" in tasks
    for forbidden in (
        "state: started",
        "state: restarted",
        "enabled: true",
        "systemctl start",
        "secretmanager",
        "gcloud",
        "postgres-environment.json\n",
        "private-input-rsa.pem\n        dest",
        "slurp",
    ):
        assert forbidden not in tasks
    assert tasks.count("check_mode: false") == 1
    assert "argv: [/usr/bin/systemctl, list-units," in tasks


def test_unit_template_is_locked_transient_and_instance_bound() -> None:
    unit = (ROLE / "templates/custody@.service.j2").read_text()
    lines = set(unit.splitlines())
    for directive in (
        "User=ditto-coding-custody",
        "Group=ditto-coding-custody",
        "RuntimeDirectory=ditto-coding-custody",
        "RuntimeDirectoryMode=0755",
        "ExecStartPre=+/usr/bin/python3 -I "
        "/usr/local/lib/ditto-coding-custody/custody-run.py check %i",
        "ExecStopPost=+/usr/bin/python3 -I "
        "/usr/local/lib/ditto-coding-custody/custody-run.py release %i",
        "Restart=no",
        "RuntimeMaxSec=2h",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "RestrictAddressFamilies=AF_UNIX AF_INET",
        "IPAddressDeny=any",
        "IPAddressAllow={{ coding_hosted_custody_postgres_ip }}/32",
        "StandardOutput=null",
        "StandardError=null",
    ):
        assert directive in lines, directive
    assert "--config /var/lib/ditto-coding-custody/runs/%i/config.json" in unit
    assert "--serve-custody" in unit and "--proxy-once" not in unit
    assert "[Install]" not in unit
    assert "\nPrivateUsers" not in unit
    assert "%I" not in unit


def test_proxy_is_fixed_argument_free_and_admissible_by_the_worker() -> None:
    proxy = (ROLE / "templates/custody-unwrap.sh.j2").read_text()
    commands = [line for line in proxy.splitlines() if not line.startswith("#")]
    assert proxy.startswith("#!/bin/sh\n")
    assert len(commands) == 1 and commands[0].startswith("exec /usr/bin/env -i ")
    assert "$" not in proxy
    assert (
        "--proxy-once --socket /run/ditto-coding-custody/custody.sock "
        "--custodian-uid {{ coding_hosted_custody_uid }}" in commands[0]
    )
    tasks = (ROLE / "tasks/main.yml").read_text()
    install = tasks.split("Install the fixed argument-free unwrap proxy", 1)[1]
    install = install.split("- name:", 1)[0]
    assert "dest: /var/lib/ditto-coding-hosted/custody/unwrap" in install
    assert "owner: ditto-coding-hosted" in install and 'mode: "0500"' in install
    directory = tasks.split(
        "Create the worker-owned private unwrap proxy directory", 1
    )[1]
    assert 'mode: "0700"' in directory.split("- name:", 1)[0]


def test_socket_runtime_and_paths_agree_across_every_layer() -> None:
    socket_path = "/run/ditto-coding-custody/custody.sock"
    assert (
        f'"socket_path": "{socket_path}"'
        in (ROLE / "templates/base.json.j2").read_text()
    )
    assert socket_path in (ROLE / "templates/custody-unwrap.sh.j2").read_text()
    assert f'socket="{socket_path}"' in HELPER.read_text()
    assert 'base="/etc/ditto-coding-custody/base.json"' in HELPER.read_text()
    assert 'runs="/var/lib/ditto-coding-custody/runs"' in HELPER.read_text()
    tasks = (ROLE / "tasks/main.yml").read_text()
    assert "dest: /etc/ditto-coding-custody/base.json" in tasks
    assert "dest: /usr/local/lib/ditto-coding-custody/custody-run.py" in tasks
    assert "dest: /etc/systemd/system/ditto-coding-custody@.service" in tasks


def test_playbook_and_disabled_fixture_run_in_infra_ci() -> None:
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert "playbooks/gcp-coding-hosted-custody-service.yml" in workflow
    assert "tests/coding-hosted-custody-service.yml" in workflow
    fixture = yaml.safe_load(
        (ROOT / "infra/ansible/tests/coding-hosted-custody-service.yml").read_text()
    )
    assert "coding_hosted_custody_service_enabled" not in json.dumps(fixture)
