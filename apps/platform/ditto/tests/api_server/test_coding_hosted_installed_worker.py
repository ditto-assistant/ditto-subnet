import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from ditto.api_server import coding_hosted_installed_worker as installed
from ditto.api_server.coding_hosted_runtime_io import (
    HostedRuntimeError,
    protected_helper,
)

REVISION = "a" * 40
WORKER = installed.ROOT / REVISION / "bin" / installed.NAME


@pytest.fixture
def root_record(monkeypatch):
    receipt = {
        "schema": installed.SCHEMA,
        "source_revision": REVISION,
        "archive_sha256": "0" * 64,
        "manifest_sha256": "0" * 64,
        "worker_sha256": "0" * 64,
        "shadow_only": True,
        "weight_eligible": False,
        "worker_started": False,
    }
    monkeypatch.setattr(Path, "resolve", lambda self, **_kwargs: self)
    metadata = SimpleNamespace(st_mode=stat.S_IFDIR | 0o555, st_uid=0)
    monkeypatch.setattr(Path, "lstat", lambda _self: metadata)
    monkeypatch.setattr(Path, "stat", lambda _self: metadata)

    def read(path, _maximum, _mode):
        if path.name == "bundle-receipt.json":
            return json.dumps(
                receipt, sort_keys=True, separators=(",", ":")
            ).encode(), "0" * 64
        return b"\x7fELF\x02\x01" + bytes(12) + b"\x3e\x00", "0" * 64

    monkeypatch.setattr(installed, "read_root_file", read)
    return receipt, metadata


def test_only_the_fixed_installed_worker_shape_is_selected():
    assert installed.installed_worker_path(WORKER)
    for path in (
        Path("/usr/bin/python3"),
        WORKER.with_name("sh"),
        WORKER.parent / "nested" / installed.NAME,
    ):
        assert not installed.installed_worker_path(path)


@pytest.mark.usefixtures("root_record")
def test_installed_receipt_binds_the_root_worker():
    installed.require_installed_worker(WORKER)
    protected_helper(WORKER)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_revision", "b" * 40),
        ("worker_sha256", "1" * 64),
        ("archive_sha256", "invalid"),
        ("manifest_sha256", 1),
        ("shadow_only", False),
        ("weight_eligible", True),
        ("worker_started", True),
        ("extra", "unapproved"),
    ],
)
def test_receipt_drift_is_refused(root_record, field, value):
    receipt, _ = root_record
    receipt[field] = value
    with pytest.raises(HostedRuntimeError, match="installed hosted worker is unsafe"):
        protected_helper(WORKER)


@pytest.mark.parametrize(
    "mode,uid", [(stat.S_IFDIR | 0o777, 0), (stat.S_IFDIR | 0o555, 1001)]
)
def test_writable_or_nonroot_parents_are_refused(root_record, mode, uid):
    _, info = root_record
    info.st_mode, info.st_uid = mode, uid
    with pytest.raises(HostedRuntimeError):
        protected_helper(WORKER)
