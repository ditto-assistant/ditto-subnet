"""Deterministic identity for the validator code installed in this process."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ditto import __version__

HEARTBEAT_PROTOCOL_VERSION = 8


@dataclass(frozen=True)
class ValidatorBuildInfo:
    software_version: str
    protocol_version: int
    code_digest: str


def source_digest(package_root: Path | None = None) -> str:
    """Hash installed Python paths and bytes in stable lexical order."""
    root = package_root or Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@lru_cache(maxsize=1)
def validator_build_info() -> ValidatorBuildInfo:
    """Return the immutable identity reported for this running installation."""
    return ValidatorBuildInfo(
        software_version=__version__,
        protocol_version=HEARTBEAT_PROTOCOL_VERSION,
        code_digest=source_digest(),
    )
