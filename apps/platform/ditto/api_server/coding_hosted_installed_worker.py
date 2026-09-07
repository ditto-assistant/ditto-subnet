"""Admission for the one root-owned worker binary in a sealed runtime bundle."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from ditto.api_models.coding_inference import _decode_json_document

ROOT = Path("/opt/ditto-coding-hosted")
NAME = "dittobench-coding-hosted-worker"
SCHEMA = "dittobench-coding-hosted-runtime-bundle-v2"


def installed_worker_path(path: Path) -> bool:
    return (
        path.parent.parent.parent == ROOT
        and path.parent.name == "bin"
        and path.name == NAME
    )


def require(condition):
    if not condition:
        raise ValueError("installed worker unavailable")


def read_root_file(path: Path, maximum: int, mode: int) -> tuple[bytes, str]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1)
        require(stat.S_IMODE(info.st_mode) == mode and 0 < info.st_size <= maximum)
        remaining, checksum, prefix = info.st_size, hashlib.sha256(), b""
        while remaining:
            body = source.read(min(remaining, 65536))
            require(body)
            if len(prefix) < 4096:
                prefix += body[: 4096 - len(prefix)]
            checksum.update(body)
            remaining -= len(body)
        current = os.fstat(source.fileno())
        require(
            (current.st_size, current.st_mtime_ns, current.st_ctime_ns)
            == (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        )
    return prefix, checksum.hexdigest()


def require_installed_worker(path: Path) -> None:
    require(
        installed_worker_path(path) and path.is_absolute() and path.resolve() == path
    )
    revision = path.parent.parent.name
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None)
    for parent in path.parents:
        info = parent.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
        )
    for parent in (path.parent, path.parent.parent):
        require(stat.S_IMODE(parent.stat().st_mode) == 0o555)
    raw, _ = read_root_file(path.parent.parent / "bundle-receipt.json", 4096, 0o444)
    receipt = _decode_json_document(raw, maximum_bytes=4096)
    require(
        type(receipt) is dict
        and set(receipt)
        == {
            "schema",
            "source_revision",
            "archive_sha256",
            "manifest_sha256",
            "worker_sha256",
            "shadow_only",
            "weight_eligible",
            "worker_started",
        }
    )
    require(receipt["schema"] == SCHEMA and receipt["source_revision"] == revision)
    require(
        receipt["shadow_only"] is True
        and receipt["weight_eligible"] is False
        and receipt["worker_started"] is False
    )
    require(
        json.dumps(
            receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        == raw
    )
    for name in ("archive_sha256", "manifest_sha256", "worker_sha256"):
        require(
            type(receipt[name]) is str
            and re.fullmatch(r"[0-9a-f]{64}", receipt[name]) is not None
        )
    header, digest = read_root_file(path, 256 << 20, 0o555)
    require(
        header[:6] == b"\x7fELF\x02\x01"
        and int.from_bytes(header[18:20], "little") == 62
    )
    require(digest == receipt["worker_sha256"])
