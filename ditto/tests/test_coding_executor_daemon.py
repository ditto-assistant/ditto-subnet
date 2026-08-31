"""Regression checks for the dormant dedicated rootless coding executor role."""

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_executor"
DEFAULTS = (ROLE / "defaults/main.yml").read_text()
TASKS = (ROLE / "tasks/main.yml").read_text()
INSTALLER = (ROLE / "files/install-rootless-docker.sh").read_text()
GUARD = (ROLE / "files/executor-egress-guard.sh").read_text()
GUARD_UNIT = ROLE / "templates/ditto-coding-executor-egress-guard.service.j2"
INGRESS_GUARD = (ROLE / "files/executor-capability-ingress-guard.sh").read_text()
INGRESS_GUARD_UNIT = (
    ROLE / "templates/ditto-coding-executor-capability-ingress-guard.service.j2"
)
DAEMON = (ROLE / "files/rootless-daemon.json").read_text()
PLAYBOOK = (ROOT / "infra/ansible/playbooks/gcp-coding-executor.yml").read_text()
WORKFLOW = (ROOT / ".github/workflows/infra-ci.yml").read_text()
DOC = (ROOT / "infra/docs/coding-executor-hosts.md").read_text()
RUNTIME_VERIFIER = ROLE / "files/verify-runtime-bundle.py"
RUNTIME_STAGER = ROLE / "files/stage-runtime-bundle.sh"
RUNTIME_LOADER = ROLE / "files/load-runtime-bundle.py"
CLIENT_GUARD = ROLE / "files/coding-executor-client-guard.py"
CLIENT_GUARD_UNIT = ROLE / "templates/ditto-coding-executor-client-guard.service.j2"
_spec = importlib.util.spec_from_file_location(
    "verify_runtime_bundle", RUNTIME_VERIFIER
)
assert _spec is not None and _spec.loader is not None
VERIFIER = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = VERIFIER
_spec.loader.exec_module(VERIFIER)
_loader_spec = importlib.util.spec_from_file_location(
    "runtime_bundle_loader", RUNTIME_LOADER
)
assert _loader_spec is not None and _loader_spec.loader is not None
LOADER = importlib.util.module_from_spec(_loader_spec)
sys.modules[_loader_spec.name] = LOADER
_loader_spec.loader.exec_module(LOADER)
_guard_spec = importlib.util.spec_from_file_location(
    "coding_executor_client_guard", CLIENT_GUARD
)
assert _guard_spec is not None and _guard_spec.loader is not None
CLIENT_GUARD_MODULE = importlib.util.module_from_spec(_guard_spec)
sys.modules[_guard_spec.name] = CLIENT_GUARD_MODULE
_guard_spec.loader.exec_module(CLIENT_GUARD_MODULE)


def test_coding_executor_daemon_is_default_off_and_has_no_client() -> None:
    assert "coding_executor_daemon_enabled: false" in DEFAULTS
    assert "when: coding_executor_daemon_enabled | bool" in TASKS
    assert "when: not (coding_executor_daemon_enabled | bool)" in TASKS
    assert "coding_executor_user_unit.stat.exists" in TASKS
    assert "coding_executor_policy_files.changed" in TASKS
    assert "ditto-coding-executor" in DEFAULTS
    assert "ditto-coding-client" in DEFAULTS
    assert "task-serving scorer, validator, wallet" in TASKS
    assert "or coding gate has been installed or enabled" in TASKS


def test_runtime_bundle_staging_and_loading_are_separately_default_off() -> None:
    assert "coding_executor_runtime_bundle_enabled: false" in DEFAULTS
    assert "coding_executor_runtime_image_load_enabled: false" in DEFAULTS
    assert 'coding_executor_runtime_manifest_sha256: ""' in DEFAULTS
    assert "when: coding_executor_runtime_bundle_enabled | bool" in TASKS
    assert "when: coding_executor_runtime_image_load_enabled | bool" in TASKS
    assert (
        "not (coding_executor_runtime_bundle_enabled | bool) or "
        "coding_executor_daemon_enabled | bool" in TASKS
    )
    assert (
        "not (coding_executor_runtime_image_load_enabled | bool) or "
        "coding_executor_runtime_bundle_enabled | bool" in TASKS
    )
    assert "/var/lib/ditto-coding-executor/staged" in DEFAULTS
    assert "complete protected manifest SHA-256" in TASKS
    assert "verify-runtime-bundle.py" in TASKS
    assert "stage-runtime-bundle.sh" in TASKS
    assert "load-runtime-bundle.py" in TASKS
    assert "docker pull" not in TASKS.lower()
    assert "gcloud" not in RUNTIME_STAGER.read_text().lower()
    assert "docker load" not in RUNTIME_STAGER.read_text().lower()
    assert "docker pull" not in RUNTIME_STAGER.read_text().lower()
    assert "--expected-manifest-sha256" in RUNTIME_STAGER.read_text()
    assert "image load" in RUNTIME_LOADER.read_text()
    assert "image pull" not in RUNTIME_LOADER.read_text().lower()
    assert "container create" not in RUNTIME_LOADER.read_text().lower()


def test_client_guard_is_default_off_and_cannot_execute_or_listen() -> None:
    text = CLIENT_GUARD.read_text().lower()
    assert "coding_executor_client_guard_enabled: false" in DEFAULTS
    assert "when: coding_executor_client_guard_enabled | bool" in TASKS
    assert (
        "not (coding_executor_client_guard_enabled | bool) or "
        "coding_executor_runtime_image_load_enabled | bool" in TASKS
    )
    assert "ditto-coding-scorer" in DEFAULTS
    assert "coding-executor-client-guard.py" in TASKS
    assert (
        "supplementarygroups={{ coding_executor_client_group }}"
        in CLIENT_GUARD_UNIT.read_text().lower()
    )
    assert "restrictaddressfamilies=af_unix" in CLIENT_GUARD_UNIT.read_text().lower()
    for command in (
        "image pull",
        "image load",
        "container create",
        "container start",
        "container run",
        "image rm",
    ):
        assert command not in text


def test_attestations_are_reachable_without_exposing_staged_bundles() -> None:
    attestation_directory = "/var/lib/ditto-coding-executor/attestations"
    assert f"coding_executor_attestation_dir: {attestation_directory}" in DEFAULTS
    assert (
        "coding_executor_runtime_image_attestation_path: "
        f"{attestation_directory}/runtime-image-attestation.json"
    ) in DEFAULTS
    assert 'group: "{{ coding_executor_client_group }}"' in TASKS
    assert 'mode: "0750"' in TASKS
    assert "/var/lib/ditto-coding-executor/staged/runtime-manifest.json" in DEFAULTS
    assert 'mode: "0700"' in TASKS
    assert attestation_directory in RUNTIME_LOADER.read_text()
    assert attestation_directory in CLIENT_GUARD.read_text()


def test_rootless_daemon_is_pinned_to_the_isolated_empty_identity() -> None:
    assert '"io.heyditto.dittobench.isolated=true"' in DAEMON
    assert "no-new-privileges" in DAEMON
    assert "CODING_EXECUTOR_HOME" in INSTALLER
    assert "dockerd-rootless.sh" in INSTALLER
    assert "systemctl disable --now docker.service docker.socket" in INSTALLER
    assert '"${user_systemctl[@]}" disable --now "$unit"' in INSTALLER
    assert "io.heyditto.dittobench.isolated=true" in INSTALLER
    assert "CODING_EXECUTOR_CLIENT_GROUP" in INSTALLER
    assert "gcloud secrets" not in INSTALLER.lower()
    assert "openrouter_api_key" not in INSTALLER.lower()
    assert "VALIDATOR_MNEMONIC" not in INSTALLER


def test_rootless_daemon_private_egress_guard_and_ci_coverage_are_present() -> None:
    for cidr in (
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ):
        assert cidr in GUARD
    assert 'chain="DCE-EXEC-EGRESS"' in GUARD
    assert "169.254.169.254/32 --dport 53 -j ACCEPT" in GUARD
    assert 'capability_gateway="${CODING_EXECUTOR_CAPABILITY_GATEWAY:-}"' in GUARD
    assert 'iptables -A "$replacement" -j REJECT' in GUARD
    assert "coding_executor_capability_egress_enabled" in DEFAULTS
    assert "coding_executor_capability_gateway" in GUARD_UNIT.read_text()
    assert "coding_executor_capability_ingress_enabled" in DEFAULTS
    assert 'chain="DCE-EXEC-INGRESS"' in INGRESS_GUARD
    assert "coding_executor_capability_source_cidr" in INGRESS_GUARD_UNIT.read_text()
    assert "hosts: role_coding_executor" in PLAYBOOK
    assert "gcp-coding-executor.yml" in WORKFLOW
    assert "docker-ce-rootless-extras" in TASKS
    assert "coding_executor_daemon_enabled" in DOC
    assert "neither a client service nor a candidate image" in DOC


def test_rootless_egress_guard_allows_only_the_reviewed_capability_gateway(
    tmp_path: Path,
) -> None:
    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    log = tmp_path / "iptables.log"
    iptables = command_dir / "iptables"
    iptables.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == '-D' ]]; then exit 1; fi\n"
        'printf \'%s\\n\' "$*" >> "$IPTABLES_LOG"\n'
    )
    iptables.chmod(0o755)
    identity = command_dir / "id"
    identity.write_text("#!/usr/bin/env bash\nprintf '4242\\n'\n")
    identity.chmod(0o755)
    environment = os.environ | {
        "CODING_EXECUTOR_CAPABILITY_GATEWAY": "10.30.0.5",
        "CODING_EXECUTOR_CAPABILITY_PORT": "11438",
        "IPTABLES_LOG": str(log),
        "PATH": str(command_dir) + ":" + os.environ["PATH"],
    }

    result = subprocess.run(
        ["bash", str(ROLE / "files/executor-egress-guard.sh")],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text()
    assert "-p tcp -d 10.30.0.5/32 --dport 11438 -j ACCEPT" in commands
    assert "-A DCE-EXEC-EGRESS-" in commands
    assert "-j REJECT" in commands


def test_rootless_egress_guard_rejects_an_invalid_capability_gateway(
    tmp_path: Path,
) -> None:
    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    identity = command_dir / "id"
    identity.write_text("#!/usr/bin/env bash\nprintf '4242\\n'\n")
    identity.chmod(0o755)
    environment = os.environ | {
        "CODING_EXECUTOR_CAPABILITY_GATEWAY": "127.0.0.1",
        "CODING_EXECUTOR_CAPABILITY_PORT": "11438",
        "PATH": str(command_dir) + ":" + os.environ["PATH"],
    }

    result = subprocess.run(
        ["bash", str(ROLE / "files/executor-egress-guard.sh")],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert result.returncode == 1
    assert "capability gateway is invalid" in result.stderr


def test_rootless_ingress_guard_allows_only_the_reviewed_candidate_cidr(
    tmp_path: Path,
) -> None:
    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    log = tmp_path / "iptables.log"
    iptables = command_dir / "iptables"
    iptables.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == '-D' ]]; then exit 1; fi\n"
        'printf \'%s\\n\' "$*" >> "$IPTABLES_LOG"\n'
    )
    iptables.chmod(0o755)
    environment = os.environ | {
        "CODING_EXECUTOR_CAPABILITY_GATEWAY": "10.30.0.5",
        "CODING_EXECUTOR_CAPABILITY_SOURCE_CIDR": "10.0.2.0/24",
        "CODING_EXECUTOR_CAPABILITY_PORT": "11438",
        "IPTABLES_LOG": str(log),
        "PATH": str(command_dir) + ":" + os.environ["PATH"],
    }

    result = subprocess.run(
        ["bash", str(ROLE / "files/executor-capability-ingress-guard.sh")],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text()
    assert "-p tcp -s 10.0.2.0/24 -d 10.30.0.5/32 --dport 11438 -j ACCEPT" in commands
    assert "-A DCE-EXEC-INGRESS-" in commands
    assert "-j REJECT" in commands


def test_rootless_ingress_guard_rejects_a_public_candidate_cidr(
    tmp_path: Path,
) -> None:
    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    environment = os.environ | {
        "CODING_EXECUTOR_CAPABILITY_GATEWAY": "10.30.0.5",
        "CODING_EXECUTOR_CAPABILITY_SOURCE_CIDR": "203.0.113.0/24",
        "CODING_EXECUTOR_CAPABILITY_PORT": "11438",
        "PATH": str(command_dir) + ":" + os.environ["PATH"],
    }

    result = subprocess.run(
        ["bash", str(ROLE / "files/executor-capability-ingress-guard.sh")],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert result.returncode == 1
    assert "capability ingress configuration is invalid" in result.stderr


def _manifest(archive_sha256: str, *, fixture: bool = False) -> bytes:
    value = {
        "archive_sha256": archive_sha256,
        "fixture": fixture,
        "image_digest": "sha256:" + "1" * 64,
        "image_repository": "registry.invalid/private/dittobench-coding-supervisor",
        "platform": "linux/amd64",
        "schema": "dittobench-coding-runtime-manifest-v1",
        "source_revision": "a" * 40,
        "supervisor_contract": "1",
        "trusted_test_driver_digest": "sha256:" + "2" * 64,
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _root_owned_metadata(path: Path, *, maximum: int) -> Any:
    metadata = path.lstat()
    assert metadata.st_size <= maximum
    return SimpleNamespace(
        st_mode=metadata.st_mode,
        st_uid=0,
        st_gid=0,
        st_size=metadata.st_size,
        st_dev=metadata.st_dev,
        st_ino=metadata.st_ino,
        st_mtime_ns=metadata.st_mtime_ns,
    )


def _verify_as_root_owned(
    manifest: Path,
    archive: Path,
    monkeypatch: pytest.MonkeyPatch,
    expected_manifest_sha256: str | None = None,
) -> None:
    monkeypatch.setattr(VERIFIER, "regular_root_owned_file", _root_owned_metadata)
    VERIFIER.verify_runtime_bundle(
        manifest,
        archive,
        expected_manifest_sha256
        if expected_manifest_sha256 is not None
        else hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )


def test_runtime_bundle_verifier_accepts_only_a_root_owned_non_fixture_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "supervisor.oci.tar"
    archive.write_bytes(b"synthetic-test-archive")
    archive.chmod(0o600)
    manifest = tmp_path / "runtime-manifest.json"
    manifest.write_bytes(_manifest(hashlib.sha256(archive.read_bytes()).hexdigest()))
    manifest.chmod(0o600)

    _verify_as_root_owned(manifest, archive, monkeypatch)


@pytest.mark.parametrize(
    ("manifest_body", "error"),
    [
        (
            lambda archive_sha256: _manifest(archive_sha256, fixture=True),
            "certification fixture",
        ),
        (
            lambda _archive_sha256: (
                b'{"schema":"dittobench-coding-runtime-manifest-v1",'
                b'"schema":"dittobench-coding-runtime-manifest-v1"}'
            ),
            "duplicate key",
        ),
    ],
)
def test_runtime_bundle_verifier_rejects_untrusted_manifests(
    tmp_path: Path,
    manifest_body: Callable[[str], bytes],
    error: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "supervisor.oci.tar"
    archive.write_bytes(b"synthetic-test-archive")
    archive.chmod(0o600)
    manifest = tmp_path / "runtime-manifest.json"
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest.write_bytes(manifest_body(archive_sha256))
    manifest.chmod(0o600)

    with pytest.raises(VERIFIER.VerificationError, match=error):
        _verify_as_root_owned(manifest, archive, monkeypatch)


def test_runtime_bundle_verifier_rejects_manifest_hash_or_archive_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "supervisor.oci.tar"
    archive.write_bytes(b"synthetic-test-archive")
    archive.chmod(0o600)
    manifest = tmp_path / "runtime-manifest.json"
    manifest.write_bytes(_manifest("0" * 64))
    manifest.chmod(0o600)

    with pytest.raises(
        VERIFIER.VerificationError, match="archive SHA-256 does not match"
    ):
        _verify_as_root_owned(manifest, archive, monkeypatch)


def test_runtime_bundle_verifier_rejects_an_unpinned_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "supervisor.oci.tar"
    archive.write_bytes(b"synthetic-test-archive")
    archive.chmod(0o600)
    manifest = tmp_path / "runtime-manifest.json"
    manifest.write_bytes(_manifest(hashlib.sha256(archive.read_bytes()).hexdigest()))
    manifest.chmod(0o600)

    with pytest.raises(
        VERIFIER.VerificationError,
        match="does not match protected host configuration",
    ):
        _verify_as_root_owned(manifest, archive, monkeypatch, "0" * 64)


def test_runtime_bundle_verifier_rejects_non_root_owned_staging_files(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "supervisor.oci.tar"
    archive.write_bytes(b"synthetic-test-archive")
    archive.chmod(0o600)
    manifest = tmp_path / "runtime-manifest.json"
    manifest.write_bytes(_manifest(hashlib.sha256(archive.read_bytes()).hexdigest()))
    manifest.chmod(0o600)

    result = subprocess.run(
        [
            sys.executable,
            str(RUNTIME_VERIFIER),
            "--manifest",
            str(manifest),
            "--archive",
            str(archive),
            "--expected-manifest-sha256",
            hashlib.sha256(manifest.read_bytes()).hexdigest(),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert "not owned by root" in result.stderr


def test_runtime_bundle_stager_requires_root(tmp_path: Path) -> None:
    manifest = tmp_path / "runtime-manifest.json"
    archive = tmp_path / "supervisor.oci.tar"
    manifest.write_text("{}")
    archive.write_bytes(b"synthetic-test-archive")

    result = subprocess.run(
        [
            "bash",
            str(RUNTIME_STAGER),
            "--manifest-source",
            str(manifest),
            "--archive-source",
            str(archive),
            "--expected-manifest-sha256",
            "0" * 64,
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert "must run as root" in result.stderr


def _loaded_image(manifest: dict[str, Any], *, fixture: bool = False) -> dict[str, Any]:
    labels = {
        "io.heyditto.dittobench.coding-supervisor-contract": "1",
        "io.heyditto.dittobench.trusted-test-driver-sha256": manifest[
            "trusted_test_driver_digest"
        ],
        "io.heyditto.dittobench.trusted-test-driver-name": "dittobench-test-driver",
        "org.opencontainers.image.revision": manifest["source_revision"],
    }
    if fixture:
        labels["io.heyditto.dittobench.coding-supervisor-fixture"] = "true"
    return {
        "Architecture": "amd64",
        "Config": {
            "Entrypoint": ["/usr/local/bin/dittobench-coding-supervisor"],
            "Env": ["PATH=/"],
            "Labels": labels,
            "Volumes": None,
        },
        "Id": "sha256:" + "3" * 64,
        "Os": "linux",
        "RepoDigests": [manifest["image_repository"] + "@" + manifest["image_digest"]],
    }


def _write_runtime_archive(path: Path) -> None:
    with tarfile.open(path, "w") as archive:
        body = b'{"synthetic":"runtime"}'
        member = tarfile.TarInfo("manifest.json")
        member.size = len(body)
        archive.addfile(member, io.BytesIO(body))


def test_runtime_loader_requires_a_safe_exact_image_and_writes_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "supervisor.oci.tar"
    _write_runtime_archive(archive)
    archive.chmod(0o600)
    manifest_path = tmp_path / "runtime-manifest.json"
    manifest_path.write_bytes(
        _manifest(hashlib.sha256(archive.read_bytes()).hexdigest())
    )
    manifest_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_bytes())
    attestation_path = tmp_path / "runtime-image-attestation.json"
    commands: list[list[str]] = []

    monkeypatch.setattr(
        LOADER.BUNDLE_VERIFIER, "regular_root_owned_file", _root_owned_metadata
    )
    monkeypatch.setattr(LOADER.os, "geteuid", lambda: 0)
    monkeypatch.setattr(LOADER.os, "chown", lambda *_args: None)
    monkeypatch.setattr(
        LOADER.grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=123)
    )
    monkeypatch.setattr(LOADER, "EXPECTED_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(LOADER, "EXPECTED_ARCHIVE_PATH", archive)
    monkeypatch.setattr(LOADER, "EXPECTED_ATTESTATION_PATH", attestation_path)
    monkeypatch.setattr(LOADER, "secure_attestation_directory", lambda _path: None)

    def fake_docker_output(
        docker_host: str,
        arguments: list[str],
        *,
        timeout: int,
    ) -> bytes:
        assert docker_host == "unix:///run/ditto-coding-executor/docker.sock"
        assert timeout > 0
        commands.append(arguments)
        if arguments == ["info", "--format", "{{json .SecurityOptions}}"]:
            return b'["name=rootless"]'
        if arguments == ["info", "--format", "{{json .Labels}}"]:
            return b'["io.heyditto.dittobench.isolated=true"]'
        if arguments == ["image", "load", "--input", str(archive)]:
            return b"Loaded image\n"
        assert arguments == [
            "image",
            "inspect",
            manifest["image_repository"] + "@" + manifest["image_digest"],
        ]
        return json.dumps([_loaded_image(manifest)]).encode()

    monkeypatch.setattr(LOADER, "docker_output", fake_docker_output)
    LOADER.load_runtime_bundle(
        manifest_path,
        archive,
        hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "unix:///run/ditto-coding-executor/docker.sock",
        attestation_path,
    )

    attestation = json.loads(attestation_path.read_bytes())
    assert attestation_path.stat().st_mode & 0o777 == 0o640
    assert attestation == {
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "image_id": "sha256:" + "3" * 64,
        "image_reference": manifest["image_repository"]
        + "@"
        + manifest["image_digest"],
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "platform": "linux/amd64",
        "schema": "dittobench-coding-runtime-image-attestation-v1",
        "source_revision": manifest["source_revision"],
        "supervisor_contract": "1",
        "trusted_test_driver_digest": manifest["trusted_test_driver_digest"],
    }
    assert commands == [
        ["info", "--format", "{{json .SecurityOptions}}"],
        ["info", "--format", "{{json .Labels}}"],
        ["image", "load", "--input", str(archive)],
        [
            "image",
            "inspect",
            manifest["image_repository"] + "@" + manifest["image_digest"],
        ],
    ]


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda image: image["RepoDigests"].clear(),
            "exact manifest repository digest",
        ),
        (
            lambda image: image["Config"]["Labels"].__setitem__(
                "io.heyditto.dittobench.coding-supervisor-fixture", "true"
            ),
            "public certification fixture",
        ),
        (
            lambda image: image["Config"]["Env"].append("API_TOKEN=not-allowed"),
            "credential-shaped environment",
        ),
    ],
)
def test_runtime_loader_rejects_unsafe_loaded_image(
    mutate: Callable[[dict[str, Any]], None],
    error: str,
) -> None:
    manifest = json.loads(_manifest("0" * 64))
    image = _loaded_image(manifest)
    mutate(image)

    with pytest.raises(LOADER.LoaderError, match=error):
        LOADER.validate_loaded_image(image, manifest)


def test_runtime_loader_rejects_any_socket_but_the_dedicated_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(LOADER.os, "geteuid", lambda: 0)

    with pytest.raises(LOADER.LoaderError, match="fixed dedicated Unix socket"):
        LOADER.load_runtime_bundle(
            Path("/not-used-manifest.json"),
            Path("/not-used-supervisor.oci.tar"),
            "0" * 64,
            "unix:///var/run/docker.sock",
            Path("/not-used-attestation.json"),
        )


def test_runtime_loader_rejects_a_non_tar_archive_before_docker_load(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "not-an-archive.tar"
    archive.write_bytes(b"not a tar archive")

    with pytest.raises(LOADER.LoaderError, match="not a readable tar archive"):
        LOADER.validate_archive_layout(archive)


def _guard_attestation() -> dict[str, str]:
    return {
        "archive_sha256": "1" * 64,
        "image_id": "sha256:" + "3" * 64,
        "image_reference": (
            "registry.invalid/private/dittobench-coding-supervisor@sha256:" + "2" * 64
        ),
        "manifest_sha256": "4" * 64,
        "platform": "linux/amd64",
        "schema": "dittobench-coding-runtime-image-attestation-v1",
        "source_revision": "a" * 40,
        "supervisor_contract": "1",
        "trusted_test_driver_digest": "sha256:" + "5" * 64,
    }


def _guard_image(attestation: dict[str, str]) -> dict[str, Any]:
    return {
        "Architecture": "amd64",
        "Config": {
            "Entrypoint": ["/usr/local/bin/dittobench-coding-supervisor"],
            "Env": ["PATH=/"],
            "Labels": {
                "io.heyditto.dittobench.coding-supervisor-contract": "1",
                "io.heyditto.dittobench.trusted-test-driver-sha256": attestation[
                    "trusted_test_driver_digest"
                ],
                "io.heyditto.dittobench.trusted-test-driver-name": (
                    "dittobench-test-driver"
                ),
                "org.opencontainers.image.revision": attestation["source_revision"],
            },
            "Volumes": None,
        },
        "Id": attestation["image_id"],
        "Os": "linux",
        "RepoDigests": [attestation["image_reference"]],
    }


def test_client_guard_revalidates_the_attested_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation_path = tmp_path / "runtime-image-attestation.json"
    attestation = _guard_attestation()
    attestation_path.write_text(json.dumps(attestation))
    commands: list[list[str]] = []

    monkeypatch.setattr(
        CLIENT_GUARD_MODULE, "EXPECTED_ATTESTATION_PATH", attestation_path
    )
    monkeypatch.setattr(
        CLIENT_GUARD_MODULE.grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=123)
    )
    monkeypatch.setattr(CLIENT_GUARD_MODULE.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(CLIENT_GUARD_MODULE.os, "getgid", lambda: 1001)
    monkeypatch.setattr(CLIENT_GUARD_MODULE.os, "getgroups", lambda: [123])
    monkeypatch.setattr(
        CLIENT_GUARD_MODULE,
        "regular_client_attestation",
        lambda _path: _root_owned_metadata(attestation_path, maximum=16 << 10),
    )

    def fake_docker_output(arguments: list[str]) -> bytes:
        commands.append(arguments)
        if arguments == ["info", "--format", "{{json .SecurityOptions}}"]:
            return b'["name=rootless"]'
        if arguments == ["info", "--format", "{{json .Labels}}"]:
            return b'["io.heyditto.dittobench.isolated=true"]'
        assert arguments == [
            "image",
            "inspect",
            "--format",
            "{{json .}}",
            attestation["image_reference"],
        ]
        return json.dumps(_guard_image(attestation)).encode()

    monkeypatch.setattr(CLIENT_GUARD_MODULE, "docker_output", fake_docker_output)
    CLIENT_GUARD_MODULE.guard_once()

    assert commands == [
        ["info", "--format", "{{json .SecurityOptions}}"],
        ["info", "--format", "{{json .Labels}}"],
        [
            "image",
            "inspect",
            "--format",
            "{{json .}}",
            attestation["image_reference"],
        ],
    ]


def test_client_guard_rejects_fixture_or_image_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation = _guard_attestation()
    image = _guard_image(attestation)
    image["Config"]["Labels"]["io.heyditto.dittobench.coding-supervisor-fixture"] = (
        "true"
    )
    monkeypatch.setattr(
        CLIENT_GUARD_MODULE,
        "docker_output",
        lambda _arguments: json.dumps(image).encode(),
    )

    with pytest.raises(
        CLIENT_GUARD_MODULE.GuardError,
        match="public certification fixture",
    ):
        CLIENT_GUARD_MODULE.inspect_image(attestation)
