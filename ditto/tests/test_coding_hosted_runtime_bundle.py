"""Synthetic software-bundle tests; no root, Docker or private material."""

import hashlib
import importlib.util
import io
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
        "apps/platform/ditto/coding_hosted_worker.py": b"# synthetic source\n",
        "apps/platform/uv.lock": b"synthetic lock\n",
    }
    for name, body in bodies.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    (source / "bin/dittobench-coding-hosted-worker").chmod(0o755)
    python = source / "apps/platform/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(BUNDLE.PYTHON)
    archive = tmp_path / "runtime.tar"
    BUNDLE.pack(source, archive, REVISION)
    return source, archive


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
