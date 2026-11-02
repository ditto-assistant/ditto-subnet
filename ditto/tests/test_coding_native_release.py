"""Public synthetic release artifacts only; no host, Docker or private inputs."""

import copy
import importlib.util
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

from ditto.tests.test_coding_hosted_image_import import (
    configuration,
    fixture_entries,
    tar_bytes,
)

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "infra/scripts/build-coding-native-release.py"
spec = importlib.util.spec_from_file_location("native_release", SCRIPT)
assert spec is not None and spec.loader is not None
RELEASE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RELEASE)
REVISION = "1" * 40


@pytest.fixture(autouse=True)
def protected_fixture_permissions():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def runtime(directory, revision=REVISION):
    directory.mkdir()
    bodies = {
        "bin/dittobench-coding-hosted-worker": b"\x7fELF\x02\x01"
        + bytes(12)
        + b"\x3e\x00",
        "apps/platform/ditto/coding_hosted_worker.py": b"# public synthetic fixture\n",
        "apps/platform/uv.lock": b"synthetic lock",
        "apps/platform/.venv/bin/python": b"not an interpreter",
    }
    manifest = {
        "schema": RELEASE.RUNTIME.SCHEMA,
        "source_revision": revision,
        "python_sha256": "a" * 64,
        "debian_packages": dict.fromkeys(RELEASE.RUNTIME.PACKAGES, "1.0"),
        "files": {
            name: {
                "sha256": RELEASE.sha(body),
                "size": len(body),
                "executable": name.startswith("bin/"),
            }
            for name, body in bodies.items()
        },
        "shadow_only": True,
        "weight_eligible": False,
    }
    with tarfile.open(
        directory / "runtime.tar", "w", format=tarfile.USTAR_FORMAT
    ) as archive:
        for name, body in {
            "manifest.json": RELEASE.RUNTIME.canonical(manifest),
            **bodies,
        }.items():
            item = tarfile.TarInfo(name)
            item.size = len(body)
            item.mode = 0o555 if name.startswith("bin/") else 0o444
            archive.addfile(item, io.BytesIO(body))


def image(language, revision, directory, profile=None):
    config = configuration(profile or RELEASE.PROFILES[language])
    config["Labels"]["org.opencontainers.image.revision"] = revision
    (directory / "source.oci.tar").write_bytes(
        tar_bytes(fixture_entries(config).items())
    )
    RELEASE.IMAGE.prepare(
        directory / "source.oci.tar",
        directory / "runtime.oci.tar",
        directory / "approval.json",
        f"coding-runtime.invalid/{language}/runtime",
        revision,
    )


@pytest.fixture
def release(tmp_path):
    directory = tmp_path / "release"
    directory.mkdir()
    runtime(directory / "native")
    for language in RELEASE.PROFILES:
        child = directory / language
        child.mkdir()
        image(language, REVISION, child)
    raw = RELEASE.IMAGE.json_bytes(RELEASE.describe(directory, REVISION))
    (directory / "release.json").write_bytes(raw)
    return directory, RELEASE.sha(raw)


def test_complete_release_binds_every_artifact_without_execution(release, monkeypatch):
    directory, checksum = release

    def forbidden(*_args, **_kwargs):
        pytest.fail("offline verification must not start any subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    value = RELEASE.verify(directory, REVISION, checksum)
    assert set(value["images"]) == set(RELEASE.PROFILES)
    assert value["runtime"]["worker_sha256"]
    assert value["independent_approval_required"] is True
    for name in (
        "native_imported",
        "runtime_qualification",
        "canary_completed",
        "weight_eligible",
    ):
        assert value[name] is False


@pytest.mark.parametrize(
    "path",
    [
        "native/runtime.tar",
        "python/runtime.oci.tar",
        "node/approval.json",
        "go/runtime.oci.tar",
        "rust/runtime.oci.tar",
        "release.json",
    ],
)
def test_artifact_corruption_refused(release, path):
    directory, checksum = release
    with (directory / path).open("r+b") as stream:
        stream.seek(0)
        stream.write(b"corrupt!")
    with pytest.raises((ValueError, tarfile.TarError)):
        RELEASE.verify(directory, REVISION, checksum)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_revision", "2" * 40),
        ("weight_eligible", True),
        ("shadow_only", 1),
        ("native_imported", True),
        ("runtime_qualification", True),
        ("extra", "authority"),
    ],
)
def test_rehashed_index_cannot_change_artifacts_or_claim_readiness(
    release, field, value
):
    directory, _ = release
    index = json.loads((directory / "release.json").read_bytes())
    index[field] = value
    raw = RELEASE.IMAGE.json_bytes(index)
    (directory / "release.json").write_bytes(raw)
    with pytest.raises(ValueError):
        RELEASE.verify(directory, REVISION, RELEASE.sha(raw))


def test_index_requires_exact_digest_canonical_json_and_unique_keys(release):
    directory, checksum = release
    with pytest.raises(ValueError):
        RELEASE.verify(directory, REVISION, "0" * 64)
    original = (directory / "release.json").read_bytes()
    for raw in (original + b"\n", b'{"shadow_only":true,' + original[1:]):
        (directory / "release.json").write_bytes(raw)
        with pytest.raises(ValueError):
            RELEASE.verify(directory, REVISION, RELEASE.sha(raw))


@pytest.mark.parametrize("language", list(RELEASE.PROFILES))
def test_mixed_image_revision_refused(tmp_path, language):
    for name in RELEASE.PROFILES:
        child = tmp_path / name
        child.mkdir()
        image(name, "2" * 40 if name == language else REVISION, child)
    runtime(tmp_path / "native")
    with pytest.raises(ValueError):
        RELEASE.describe(tmp_path, REVISION)


def test_wrong_language_profile_refused(tmp_path):
    child = tmp_path / "python"
    child.mkdir()
    image("python", REVISION, child, profile="rust-call-ast-v1")
    with pytest.raises(ValueError):
        RELEASE.describe(tmp_path, REVISION)


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "fifo", "writable", "parent-link"]
)
def test_unprotected_or_special_inputs_refused(release, tmp_path, kind):
    directory, checksum = release
    path = directory / "release.json"
    if kind == "writable":
        path.chmod(0o666)
    elif kind == "parent-link":
        link = tmp_path / "alias"
        link.symlink_to(directory, target_is_directory=True)
        directory = link
    else:
        saved = directory / "saved.json"
        path.rename(saved)
        if kind == "symlink":
            path.symlink_to(saved)
        elif kind == "hardlink":
            os.link(saved, path)
        else:
            os.mkfifo(path)
    with pytest.raises(ValueError):
        RELEASE.verify(directory, REVISION, checksum)


def test_aggregate_cannot_omit_language_or_redirect_a_path(release):
    directory, _ = release
    original = json.loads((directory / "release.json").read_bytes())
    for omit in (True, False):
        value = copy.deepcopy(original)
        if omit:
            del value["images"]["rust"]
        else:
            value["images"]["rust"]["archive"] = "../outside.tar"
        raw = RELEASE.IMAGE.json_bytes(value)
        (directory / "release.json").write_bytes(raw)
        with pytest.raises(ValueError):
            RELEASE.verify(directory, REVISION, RELEASE.sha(raw))


def test_file_mutation_during_read_is_refused(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"original")
    with pytest.raises(ValueError), RELEASE.artifact(path, 64) as stream:
        assert stream.read() == b"original"
        path.write_bytes(b"changed")


def test_build_composes_existing_tools_and_publishes_index_last(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        RELEASE, "check_source", lambda revision: calls.append(revision)
    )

    def native(args, **_kwargs):
        assert args[1] == "-I"
        assert args[2].endswith("infra/scripts/build-coding-hosted-runtime.py")
        assert args[3:5] == ["--revision", REVISION]
        runtime(Path(args[-1]))

    monkeypatch.setattr(subprocess, "run", native)
    monkeypatch.setattr(RELEASE, "build_image", image)
    output = tmp_path / "new"
    checksum = RELEASE.build(output, REVISION)
    assert calls == [REVISION, REVISION]
    assert RELEASE.verify(output, REVISION, checksum)["source_revision"] == REVISION
    assert (output / "release.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        RELEASE.build(output, REVISION)
    assert RELEASE.sha((output / "release.json").read_bytes()) == checksum


def test_build_failure_retains_partial_state_without_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(RELEASE, "check_source", lambda _revision: None)

    def failed(args, **_kwargs):
        Path(args[-1]).mkdir()
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(subprocess, "run", failed)
    output = tmp_path / "partial"
    with pytest.raises(subprocess.CalledProcessError):
        RELEASE.build(output, REVISION)
    assert (output / "native").is_dir()
    assert not (output / "release.json").exists()


def test_source_gate_refuses_revision_drift_and_dirty_tracked_files(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_k: "2" * 40)
    with pytest.raises(ValueError):
        RELEASE.check_source(REVISION)
    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_k: REVISION)

    def dirty(*args, **_kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(subprocess, "run", dirty)
    with pytest.raises(subprocess.CalledProcessError):
        RELEASE.check_source(REVISION)


def test_build_image_uses_only_git_archive_stdin(tmp_path, monkeypatch):
    class Export:
        stdout = io.BytesIO(b"synthetic Git tar")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def wait(self, **_kwargs):
            return 0

    def popen(args, **_kwargs):
        assert args == ["git", "archive", "--format=tar", REVISION]
        return Export()

    def run(args, **kwargs):
        assert args[:3] == ["docker", "buildx", "build"] and args[-1] == "-"
        assert "--provenance=false" in args
        assert "DITTOBENCH_SOURCE_SHA=" + REVISION in args
        assert "--secret" not in args and "--ssh" not in args and "--load" not in args
        assert kwargs["stdin"].read() == b"synthetic Git tar"

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(RELEASE.IMAGE, "prepare", lambda *_args: None)
    RELEASE.build_image("rust", REVISION, tmp_path)


def test_cli_verify_failure_redacts_paths_and_details(tmp_path):
    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "verify",
            "--directory",
            str(tmp_path / "sensitive-label"),
            "--revision",
            REVISION,
            "--manifest-sha256",
            "0" * 64,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "sensitive-label" not in result.stderr and "Traceback" not in result.stderr


def test_workflow_build_requires_dispatch_and_has_no_production_authority():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/coding-native-release.yml").read_text()
    )
    assert workflow["permissions"] == {"contents": "read"}
    build = workflow["jobs"]["public-build"]
    assert build["if"] == "github.event_name == 'workflow_dispatch'"
    assert build["needs"] == "verify"
    for job in workflow["jobs"].values():
        assert "environment" not in job and "secrets" not in job
        for step in job["steps"]:
            if "uses" in step:
                assert len(step["uses"].split("@")[1]) == 40
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False
