"""Regression checks for the dormant dedicated rootless coding executor role."""

import hashlib
import importlib.util
import json
import subprocess
import sys
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
DAEMON = (ROLE / "files/rootless-daemon.json").read_text()
PLAYBOOK = (ROOT / "infra/ansible/playbooks/gcp-coding-executor.yml").read_text()
WORKFLOW = (ROOT / ".github/workflows/infra-ci.yml").read_text()
DOC = (ROOT / "infra/docs/coding-executor-hosts.md").read_text()
RUNTIME_VERIFIER = ROLE / "files/verify-runtime-bundle.py"
RUNTIME_STAGER = ROLE / "files/stage-runtime-bundle.sh"
_spec = importlib.util.spec_from_file_location(
    "verify_runtime_bundle", RUNTIME_VERIFIER
)
assert _spec is not None and _spec.loader is not None
VERIFIER = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(VERIFIER)


def test_coding_executor_daemon_is_default_off_and_has_no_client() -> None:
    assert "coding_executor_daemon_enabled: false" in DEFAULTS
    assert "when: coding_executor_daemon_enabled | bool" in TASKS
    assert "when: not (coding_executor_daemon_enabled | bool)" in TASKS
    assert "coding_executor_user_unit.stat.exists" in TASKS
    assert "coding_executor_policy_files.changed" in TASKS
    assert "ditto-coding-executor" in DEFAULTS
    assert "ditto-coding-client" in DEFAULTS
    assert "no scorer, validator, wallet, image" in TASKS
    assert "or coding gate has been installed or enabled" in TASKS


def test_runtime_bundle_staging_is_default_off_and_cannot_load_an_image() -> None:
    assert "coding_executor_runtime_bundle_enabled: false" in DEFAULTS
    assert 'coding_executor_runtime_manifest_sha256: ""' in DEFAULTS
    assert "when: coding_executor_runtime_bundle_enabled | bool" in TASKS
    assert (
        "not (coding_executor_runtime_bundle_enabled | bool) or "
        "coding_executor_daemon_enabled | bool" in TASKS
    )
    assert "/var/lib/ditto-coding-executor/staged" in DEFAULTS
    assert "complete protected manifest SHA-256" in TASKS
    assert "verify-runtime-bundle.py" in TASKS
    assert "stage-runtime-bundle.sh" in TASKS
    assert "docker load" not in TASKS.lower()
    assert "docker pull" not in TASKS.lower()
    assert "gcloud" not in RUNTIME_STAGER.read_text().lower()
    assert "docker load" not in RUNTIME_STAGER.read_text().lower()
    assert "docker pull" not in RUNTIME_STAGER.read_text().lower()
    assert "--expected-manifest-sha256" in RUNTIME_STAGER.read_text()


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
    assert "hosts: role_coding_executor" in PLAYBOOK
    assert "gcp-coding-executor.yml" in WORKFLOW
    assert "docker-ce-rootless-extras" in TASKS
    assert "coding_executor_daemon_enabled" in DOC
    assert "neither a client service nor a candidate image" in DOC


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
