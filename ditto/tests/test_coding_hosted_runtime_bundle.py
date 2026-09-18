"""Synthetic software-bundle tests; no root, Docker or private material."""

import hashlib
import importlib.util
import io
import os
import stat
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_runtime"
spec = importlib.util.spec_from_file_location(
    "runtime_bundle", ROLE / "files/runtime-bundle.py"
)
assert spec is not None and spec.loader is not None
BUNDLE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(BUNDLE)
REVISION = "a" * 40


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(BUNDLE, "BASE", tmp_path / "build")
    monkeypatch.setattr(BUNDLE, "PYTHON", Path("/usr/bin/python3").resolve())
    monkeypatch.setattr(
        BUNDLE, "base_packages", lambda: dict.fromkeys(BUNDLE.PACKAGES, "1.0")
    )
    source = BUNDLE.BASE / REVISION
    bodies = {
        "bin/dittobench-coding-hosted-worker": b"\x7fELF\x02\x01"
        + bytes(12)
        + b"\x3e\x00"
        + b"synthetic",
        "bin/dittobench-coding-router-listener": b"\x7fELF\x02\x01"
        + bytes(12)
        + b"\x3e\x00"
        + b"synthetic helper",
        "apps/platform/ditto/coding_hosted_worker.py": b"# synthetic source\n",
        "apps/platform/uv.lock": b"synthetic lock\n",
    }
    for name, body in bodies.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    for name in BUNDLE.EXECUTABLES[BUNDLE.SCHEMA]:
        (source / name).chmod(0o755)
    python = source / "apps/platform/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(BUNDLE.PYTHON)
    archive = tmp_path / "runtime.tar"
    BUNDLE.pack(source, archive, REVISION)
    yield source, archive
    # Restore only our fixture directories for pytest cleanup. Never chmod a
    # symlink target (the fixture includes the real system Python link).
    for directory, _dirs, _files in os.walk(tmp_path, followlinks=False):
        Path(directory).chmod(0o700)


def inspect(archive):
    with archive.open("rb") as source:
        return BUNDLE.inspect(source, BUNDLE.file_hash(archive), REVISION)


def rewrite(archive, mutate, *, format=tarfile.USTAR_FORMAT):
    with tarfile.open(archive) as source:
        members = [
            (item, source.extractfile(item).read() if item.isreg() else None)
            for item in source
        ]
    mutate(members)
    with tarfile.open(archive, "w", format=format) as target:
        for item, body in members:
            if body is not None:
                item.size = len(body)
            target.addfile(item, io.BytesIO(body) if body is not None else None)


def test_install_preserves_pinned_files_and_refuses_overwrite(bundle, tmp_path):
    _, archive = bundle
    value, records = inspect(archive)
    destination = tmp_path / "installed"
    with archive.open("rb") as source:
        receipt = BUNDLE.materialize(
            source, value, records, destination, BUNDLE.file_hash(archive)
        )
        assert (
            receipt["worker_started"] is False and receipt["weight_eligible"] is False
        )
        with pytest.raises(FileExistsError):
            BUNDLE.materialize(
                source, value, records, destination, BUNDLE.file_hash(archive)
            )
    BUNDLE.verify_tree(value, destination, BUNDLE.file_hash(archive))
    assert stat.S_IMODE(destination.stat().st_mode) == 0o555
    assert (destination / "apps/platform/.venv/bin/python").resolve() == BUNDLE.PYTHON
    changed = destination / "apps/platform/uv.lock"
    changed.chmod(0o644)
    changed.write_bytes(b"drift")
    with pytest.raises(ValueError):
        BUNDLE.verify_tree(value, destination, BUNDLE.file_hash(archive))


@pytest.mark.parametrize(
    "name",
    [
        "/etc/shadow",
        "../outside",
        "bin/../outside",
        "bin//worker",
        "bin/.env",
        "bin/.git/config",
        "bin/bad\nname",
        "bin\\worker",
    ],
)
def test_unsafe_paths_are_refused(name):
    with pytest.raises(ValueError):
        BUNDLE.relative(name)


def test_wrong_archive_hash_and_revision_are_refused(bundle):
    _, archive = bundle
    with archive.open("rb") as source:
        with pytest.raises(ValueError):
            BUNDLE.inspect(source, "0" * 64, REVISION)
        with pytest.raises(ValueError):
            BUNDLE.inspect(source, BUNDLE.file_hash(archive), "b" * 40)


@pytest.mark.parametrize(
    "change",
    ["duplicate", "extra", "hardlink", "device", "mode", "bytes", "missing", "pax"],
)
def test_untrusted_archive_structure_refused(bundle, change):
    _, archive = bundle

    def mutate(items):
        if change == "duplicate":
            items.append(items[-1])
        elif change == "extra":
            item = tarfile.TarInfo("bin/extra")
            item.mode = 0o444
            items.append((item, b"extra"))
        elif change == "missing":
            items.pop()
        else:
            item, body = items[-1]
            if change == "hardlink":
                item.type, item.linkname, item.size = tarfile.LNKTYPE, "bin/worker", 0
                items[-1] = (item, None)
            elif change == "device":
                item.type, item.size = tarfile.CHRTYPE, 0
                items[-1] = (item, None)
            elif change == "mode":
                item.mode = 0o6777
            elif change == "bytes":
                items[-1] = (item, body + b"changed")
            elif change == "pax":
                item.pax_headers = {"comment": "extension forbidden"}

    rewrite(
        archive,
        mutate,
        format=tarfile.PAX_FORMAT if change == "pax" else tarfile.USTAR_FORMAT,
    )
    with pytest.raises((ValueError, tarfile.TarError)):
        inspect(archive)


@pytest.mark.parametrize("change", ["missing", "not_executable", "not_elf"])
def test_router_listener_helper_is_a_required_executable_elf(bundle, tmp_path, change):
    source, _ = bundle
    helper = source / "bin/dittobench-coding-router-listener"
    if change == "missing":
        helper.unlink()
    elif change == "not_executable":
        helper.chmod(0o644)
    else:
        helper.write_bytes(b"#!/bin/sh\nexit 0\n")
    archive = tmp_path / "changed.tar"
    with pytest.raises(ValueError):
        BUNDLE.pack(source, archive, REVISION)
        inspect(archive)


def install_and_verify(archive, destination):
    value, records = inspect(archive)
    checksum = BUNDLE.file_hash(archive)
    with archive.open("rb") as source:
        receipt = BUNDLE.materialize(source, value, records, destination, checksum)
    BUNDLE.verify_tree(value, destination, checksum)
    return value, receipt


def test_new_bundles_pin_the_router_listener_in_the_receipt(bundle, tmp_path):
    source, archive = bundle
    value, receipt = install_and_verify(archive, tmp_path / "installed")
    helper = hashlib.sha256(
        (source / "bin/dittobench-coding-router-listener").read_bytes()
    ).hexdigest()
    assert value["schema"] == receipt["schema"] == BUNDLE.SCHEMA
    assert BUNDLE.SCHEMA == "dittobench-coding-hosted-runtime-bundle-v3"
    assert receipt["router_listener_sha256"] == helper
    assert (tmp_path / "installed/bundle-receipt.json").read_bytes() == (
        BUNDLE.canonical(receipt)
    )
    # Helper drift after installation fails verification like worker drift.
    installed = tmp_path / "installed/bin/dittobench-coding-router-listener"
    installed.parent.chmod(0o755)
    installed.chmod(0o755)
    installed.write_bytes(installed.read_bytes() + b"drift")
    installed.chmod(0o555)
    installed.parent.chmod(0o555)
    with pytest.raises(ValueError):
        BUNDLE.verify_tree(value, tmp_path / "installed", BUNDLE.file_hash(archive))


def test_previously_approved_v2_bundles_still_install_and_verify(
    bundle, tmp_path, monkeypatch
):
    source, _ = bundle
    helper = source / "bin/dittobench-coding-router-listener"
    body = helper.read_bytes()
    helper.unlink()
    legacy = tmp_path / "legacy.tar"
    # A v2 bundle is exactly what the earlier packer wrote: no helper.
    with monkeypatch.context() as patch:
        patch.setattr(BUNDLE, "SCHEMA", BUNDLE.SCHEMA_V2)
        BUNDLE.pack(source, legacy, REVISION)
    value, receipt = install_and_verify(legacy, tmp_path / "installed")
    assert value["schema"] == receipt["schema"] == BUNDLE.SCHEMA_V2
    assert set(receipt) == {
        "schema",
        "source_revision",
        "archive_sha256",
        "manifest_sha256",
        "worker_sha256",
        "shadow_only",
        "weight_eligible",
        "worker_started",
    }
    # The current packer cannot produce a v3 bundle without the helper.
    with pytest.raises(ValueError):
        BUNDLE.pack(source, tmp_path / "missing.tar", REVISION)
    # A v2 manifest cannot carry a helper its receipt would not pin.
    helper.write_bytes(body)
    helper.chmod(0o755)
    with monkeypatch.context() as patch:
        patch.setattr(BUNDLE, "SCHEMA", BUNDLE.SCHEMA_V2)
        with pytest.raises(ValueError):
            BUNDLE.pack(source, tmp_path / "unpinned.tar", REVISION)
    unknown = dict(value, schema="dittobench-coding-hosted-runtime-bundle-v4")
    with pytest.raises(ValueError):
        BUNDLE.metadata(unknown, REVISION)


def test_duplicate_manifest_keys_and_link_escape_refused(bundle):
    _, archive = bundle
    value, _ = inspect(archive)
    value["files"]["apps/platform/.venv/bin/python"] = {"link": "/etc/passwd"}
    with pytest.raises(ValueError):
        BUNDLE.metadata(value, REVISION)
    with pytest.raises(ValueError):
        BUNDLE.unique([("schema", "a"), ("schema", "b")])


def test_nonroot_cannot_install_even_with_confirmation(bundle, monkeypatch):
    _, archive = bundle
    monkeypatch.setattr(BUNDLE.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(
        "sys.argv",
        [
            "bundle",
            "install",
            "--revision",
            REVISION,
            "--archive",
            str(archive),
            "--sha256",
            "0" * 64,
            "--confirm",
            "INSTALL VERIFIED CODING RUNTIME",
        ],
    )
    with pytest.raises(ValueError):
        BUNDLE.main()


def test_install_mutation_after_validation_cannot_finalize(bundle, tmp_path):
    _, archive = bundle
    value, records = inspect(archive)
    destination = tmp_path / "partial"
    name, offset, size = records[0]
    assert size and name
    with archive.open("r+b") as source:
        source.seek(offset)
        source.write(b"X")
        source.flush()
        with pytest.raises(ValueError):
            BUNDLE.materialize(
                source,
                value,
                records,
                destination,
                hashlib.sha256(b"synthetic").hexdigest(),
            )
    assert destination.exists() and not (destination / "bundle-receipt.json").exists()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700


def test_role_is_off_by_default_and_never_activates_worker():
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["coding_hosted_runtime_enabled"] is False
    assert defaults["coding_hosted_runtime_sha256"] == ""
    source = (ROLE / "tasks/main.yml").read_text()
    assert "coding_hosted_runtime_enabled | bool" in source
    assert "INSTALL VERIFIED CODING RUNTIME" in source and "- verify" in source
    assert "systemd" not in source and "state: started" not in source
    assert "get_url" not in source and "unarchive" not in source
