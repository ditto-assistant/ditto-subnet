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


HELPER = WORKER.with_name(installed.ROUTER_LISTENER)
ELF = b"\x7fELF\x02\x01" + bytes(12) + b"\x3e\x00"


@pytest.fixture(params=[installed.SCHEMA_V2, installed.SCHEMA])
def root_record(monkeypatch, request):
    receipt = {
        "schema": request.param,
        "source_revision": REVISION,
        "archive_sha256": "0" * 64,
        "manifest_sha256": "0" * 64,
        "worker_sha256": "0" * 64,
        "shadow_only": True,
        "weight_eligible": False,
        "worker_started": False,
    }
    if request.param == installed.SCHEMA:
        receipt["router_listener_sha256"] = "1" * 64
    monkeypatch.setattr(Path, "resolve", lambda self, **_kwargs: self)
    metadata = SimpleNamespace(st_mode=stat.S_IFDIR | 0o555, st_uid=0)
    monkeypatch.setattr(Path, "lstat", lambda _self: metadata)
    monkeypatch.setattr(Path, "stat", lambda _self: metadata)
    files = {WORKER: (ELF, "0" * 64), HELPER: (ELF, "1" * 64)}
    reads = []

    def read(path, _maximum, _mode):
        reads.append(path)
        if path.name == "bundle-receipt.json":
            return json.dumps(
                receipt, sort_keys=True, separators=(",", ":")
            ).encode(), "0" * 64
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    monkeypatch.setattr(installed, "read_root_file", read)
    return SimpleNamespace(receipt=receipt, metadata=metadata, files=files, reads=reads)


def test_only_the_fixed_installed_worker_shape_is_selected():
    assert installed.installed_worker_path(WORKER)
    for path in (
        Path("/usr/bin/python3"),
        WORKER.with_name("sh"),
        WORKER.parent / "nested" / installed.NAME,
    ):
        assert not installed.installed_worker_path(path)


def test_installed_receipt_binds_the_root_worker(root_record):
    installed.require_installed_worker(WORKER)
    protected_helper(WORKER)
    # A v3 receipt also pins the router listener helper beside the worker.
    assert (HELPER in root_record.reads) == (
        root_record.receipt["schema"] == installed.SCHEMA
    )


def test_rootless_router_requires_a_v3_bundle_with_the_pinned_helper(root_record):
    if root_record.receipt["schema"] == installed.SCHEMA_V2:
        installed.require_installed_worker(WORKER)
        with pytest.raises(ValueError):
            installed.require_installed_worker(WORKER, router_listener=True)
        return
    installed.require_installed_worker(WORKER, router_listener=True)
    for change in ("digest", "not_elf", "missing"):
        files = dict(root_record.files)
        if change == "digest":
            root_record.files[HELPER] = (ELF, "2" * 64)
        elif change == "not_elf":
            root_record.files[HELPER] = (b"#!/bin/sh\n", "1" * 64)
        else:
            del root_record.files[HELPER]
        for router_listener in (False, True):
            with pytest.raises((ValueError, OSError)):
                installed.require_installed_worker(
                    WORKER, router_listener=router_listener
                )
        with pytest.raises(HostedRuntimeError):
            protected_helper(WORKER)
        root_record.files.clear()
        root_record.files.update(files)


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
        ("router_listener_sha256", "3" * 64),
        ("schema", "dittobench-coding-hosted-runtime-bundle-v4"),
    ],
)
def test_receipt_drift_is_refused(root_record, field, value):
    receipt = root_record.receipt
    receipt[field] = value
    with pytest.raises(HostedRuntimeError, match="installed hosted worker is unsafe"):
        protected_helper(WORKER)


@pytest.mark.parametrize(
    "mode,uid", [(stat.S_IFDIR | 0o777, 0), (stat.S_IFDIR | 0o555, 1001)]
)
def test_writable_or_nonroot_parents_are_refused(root_record, mode, uid):
    info = root_record.metadata
    info.st_mode, info.st_uid = mode, uid
    with pytest.raises(HostedRuntimeError):
        protected_helper(WORKER)
