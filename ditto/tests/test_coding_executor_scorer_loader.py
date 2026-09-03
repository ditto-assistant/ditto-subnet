"""Security checks for the dormant coding-executor scorer image loader."""

import hashlib
import importlib.util
import io
import json
import stat
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
LOADER_PATH = ROLE / "files/load-scorer-bundle.py"
LOADER_TEXT = LOADER_PATH.read_text().lower()
SPEC = importlib.util.spec_from_file_location("scorer_bundle_loader", LOADER_PATH)
assert SPEC is not None and SPEC.loader is not None
LOADER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = LOADER
SPEC.loader.exec_module(LOADER)

IMAGE_REPOSITORY = "ghcr.io/ditto-assistant/dittobench-coding-executor-scorer"
DOCKER_HOST = "unix:///run/ditto-coding-executor/docker.sock"


def _root_owned_metadata(path: Path, *, maximum: int) -> Any:
    metadata = path.lstat()
    assert 0 < metadata.st_size <= maximum
    return SimpleNamespace(
        st_mode=metadata.st_mode,
        st_uid=0,
        st_gid=0,
        st_size=metadata.st_size,
        st_dev=metadata.st_dev,
        st_ino=metadata.st_ino,
        st_mtime_ns=metadata.st_mtime_ns,
    )


def _write_archive(path: Path) -> None:
    with tarfile.open(path, "w") as archive:
        body = b'{"synthetic":"scorer"}'
        member = tarfile.TarInfo("manifest.json")
        member.size = len(body)
        archive.addfile(member, io.BytesIO(body))
    path.chmod(0o600)


def _write_bundle(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, Any], str]:
    archive = tmp_path / "scorer.oci.tar"
    _write_archive(archive)
    image_digest = "sha256:" + "1" * 64
    release = {
        "image_digest": image_digest,
        "image_reference": f"{IMAGE_REPOSITORY}@{image_digest}",
        "locked_policy_sha256": "2" * 64,
        "platform": "linux/amd64",
        "schema": "dittobench-coding-executor-scorer-release-v1",
        "scorer_contract": "1",
        "source_revision": "a" * 40,
    }
    release_raw = (
        json.dumps(release, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    release_path = tmp_path / "scorer.release.json"
    release_path.write_bytes(release_raw)
    release_path.chmod(0o600)
    bundle = dict(release)
    bundle.update(
        {
            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "image_id": "sha256:" + "3" * 64,
            "release_manifest_sha256": hashlib.sha256(release_raw).hexdigest(),
            "schema": "dittobench-coding-executor-scorer-bundle-v1",
        }
    )
    bundle_raw = (
        json.dumps(bundle, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    bundle_path = tmp_path / "scorer.bundle.json"
    bundle_path.write_bytes(bundle_raw)
    bundle_path.chmod(0o600)
    return (
        release_path,
        bundle_path,
        archive,
        release,
        hashlib.sha256(bundle_raw).hexdigest(),
    )


def _loaded_image(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "Architecture": "amd64",
        "Config": {
            "Cmd": None,
            "Entrypoint": ["/dittobench-coding-executor-scorer"],
            "Env": ["PATH=/", "DITTOBENCH_SOURCE_SHA=" + manifest["source_revision"]],
            "ExposedPorts": None,
            "Healthcheck": None,
            "Labels": {
                "io.heyditto.dittobench.coding-executor-scorer-contract": "1",
                "io.heyditto.dittobench.coding-executor-locked-policy-sha256": (
                    manifest["locked_policy_sha256"]
                ),
                "org.opencontainers.image.revision": manifest["source_revision"],
            },
            "User": "65532:65532",
            "Volumes": None,
        },
        "Id": "sha256:" + "3" * 64,
        "Os": "linux",
        "RepoDigests": [manifest["image_reference"]],
    }


def test_scorer_image_loading_is_separately_default_off() -> None:
    assert "coding_executor_scorer_bundle_enabled: false" in DEFAULTS
    assert "coding_executor_scorer_image_load_enabled: false" in DEFAULTS
    assert "when: coding_executor_scorer_image_load_enabled | bool" in TASKS
    assert (
        "not (coding_executor_scorer_image_load_enabled | bool) or "
        "coding_executor_scorer_bundle_enabled | bool" in TASKS
    )
    assert "load-scorer-bundle.py" in TASKS
    assert "/var/lib/ditto-coding-executor/attestations" in DEFAULTS
    assert "image load" in LOADER_TEXT
    assert "image pull" not in LOADER_TEXT
    for command in ("container create", "container start", "container run"):
        assert command not in LOADER_TEXT


def test_scorer_loader_attests_only_the_exact_safe_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_path, bundle_path, archive, release, bundle_sha256 = _write_bundle(tmp_path)
    attestation_path = tmp_path / "scorer-image-attestation.json"
    commands: list[list[str]] = []

    monkeypatch.setattr(
        LOADER.BUNDLE_VERIFIER, "regular_root_owned_file", _root_owned_metadata
    )
    monkeypatch.setattr(LOADER.os, "geteuid", lambda: 0)
    monkeypatch.setattr(LOADER.os, "chown", lambda *_args: None)
    monkeypatch.setattr(LOADER, "client_group_id", lambda: 123)
    monkeypatch.setattr(LOADER, "EXPECTED_RELEASE_MANIFEST_PATH", release_path)
    monkeypatch.setattr(LOADER, "EXPECTED_BUNDLE_MANIFEST_PATH", bundle_path)
    monkeypatch.setattr(LOADER, "EXPECTED_ARCHIVE_PATH", archive)
    monkeypatch.setattr(LOADER, "EXPECTED_ATTESTATION_PATH", attestation_path)
    monkeypatch.setattr(LOADER, "secure_attestation_directory", lambda _path: None)

    def fake_docker_output(
        docker_host: str,
        arguments: list[str],
        *,
        timeout: int,
    ) -> bytes:
        assert docker_host == DOCKER_HOST
        assert timeout > 0
        commands.append(arguments)
        if arguments == ["info", "--format", "{{json .SecurityOptions}}"]:
            return b'["name=rootless"]'
        if arguments == ["info", "--format", "{{json .Labels}}"]:
            return b'["io.heyditto.dittobench.isolated=true"]'
        if arguments == ["image", "load", "--input", str(archive)]:
            return b"Loaded image\n"
        assert arguments == ["image", "inspect", "sha256:" + "3" * 64]
        return json.dumps([_loaded_image(release)]).encode()

    monkeypatch.setattr(LOADER, "docker_output", fake_docker_output)
    changed = LOADER.load_scorer_bundle(
        release_path,
        bundle_path,
        archive,
        bundle_sha256,
        DOCKER_HOST,
        attestation_path,
    )

    assert changed is True
    assert stat.S_IMODE(attestation_path.stat().st_mode) == 0o640
    assert json.loads(attestation_path.read_bytes()) == {
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "bundle_manifest_sha256": bundle_sha256,
        "image_id": "sha256:" + "3" * 64,
        "image_reference": release["image_reference"],
        "locked_policy_sha256": release["locked_policy_sha256"],
        "platform": "linux/amd64",
        "release_manifest_sha256": hashlib.sha256(
            release_path.read_bytes()
        ).hexdigest(),
        "schema": "dittobench-coding-executor-scorer-image-attestation-v1",
        "scorer_contract": "1",
        "source_revision": release["source_revision"],
    }
    assert commands == [
        ["info", "--format", "{{json .SecurityOptions}}"],
        ["info", "--format", "{{json .Labels}}"],
        ["image", "load", "--input", str(archive)],
        ["image", "inspect", "sha256:" + "3" * 64],
    ]


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda image: image.__setitem__("Id", "sha256:" + "4" * 64),
            "verified export bundle",
        ),
        (
            lambda image: image["Config"].__setitem__("User", "0"),
            "fixed non-root identity",
        ),
        (
            lambda image: image["Config"]["Labels"].__setitem__(
                "io.heyditto.dittobench.coding-executor-locked-policy-sha256",
                "3" * 64,
            ),
            "locked policy",
        ),
        (
            lambda image: image["Config"]["Labels"].__setitem__(
                "org.opencontainers.image.revision", "b" * 40
            ),
            "source revision",
        ),
        (
            lambda image: image["Config"]["Env"].append("CONTROL_TOKEN=forbidden"),
            "credential-shaped environment",
        ),
        (
            lambda image: image["Config"].__setitem__("ExposedPorts", {"8080/tcp": {}}),
            "exposed port",
        ),
    ],
)
def test_scorer_loader_rejects_unsafe_loaded_image(
    mutate: Callable[[dict[str, Any]], None],
    error: str,
) -> None:
    manifest = {
        "image_reference": f"{IMAGE_REPOSITORY}@sha256:" + "1" * 64,
        "locked_policy_sha256": "2" * 64,
        "scorer_contract": "1",
        "source_revision": "a" * 40,
    }
    image = _loaded_image(manifest)
    mutate(image)

    with pytest.raises(LOADER.LoaderError, match=error):
        LOADER.validate_loaded_image(image, manifest, "sha256:" + "3" * 64)


def test_scorer_loader_rejects_any_socket_but_the_dedicated_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(LOADER.os, "geteuid", lambda: 0)

    with pytest.raises(LOADER.LoaderError, match="fixed dedicated Unix socket"):
        LOADER.load_scorer_bundle(
            Path("/not-used-release.json"),
            Path("/not-used-bundle.json"),
            Path("/not-used-scorer.tar"),
            "0" * 64,
            "unix:///var/run/docker.sock",
            Path("/not-used-attestation.json"),
        )


def test_scorer_loader_rejects_a_non_tar_archive_before_docker_load(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "not-an-archive.tar"
    archive.write_bytes(b"not a tar archive")

    with pytest.raises(LOADER.LoaderError, match="not a readable tar archive"):
        LOADER.validate_archive_layout(archive)


def test_scorer_attestation_write_is_content_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation = {"schema": "example", "sha256": "1" * 64}
    encoded = (
        json.dumps(attestation, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    path = tmp_path / "attestation.json"
    path.write_bytes(encoded)
    path.chmod(0o640)
    before = path.stat()
    monkeypatch.setattr(LOADER, "regular_existing_attestation", lambda _path: before)

    assert LOADER.write_attestation(path, attestation) is False
    after = path.stat()
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
