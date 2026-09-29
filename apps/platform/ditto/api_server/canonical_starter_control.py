"""Immutable public source-control artifact packaged from v0.325.3."""

from __future__ import annotations

import hashlib
from importlib import resources

RELEASE = "v0.325.3"
RELEASE_COMMIT = "7b297ae96488cfa4790b8b9d9b788cc82c7d0442"
SOURCE_TREE = "7c8044a1cc77e342b5f58a24fe31c9a86cc4fd6b"
ARCHIVE_SHA256 = "6f0fb811e08558aab56f63dd13ea1d2e1462d85e711fd31b0362d0de5c611fef"
ARCHIVE_BYTES = 4_912_491
DOCKERFILE_SHA256 = "d3a1a2a1e5d43b0465c28712457d95432942ac8f017fd10d538859a901a54641"
OBJECT_KEY = "source-controls/canonical-starter-v0.325.3.tgz"


def archive_bytes() -> bytes:
    data = (
        resources.files("ditto.api_server")
        .joinpath("data/canonical-starter-v0.325.3.tgz")
        .read_bytes()
    )
    if len(data) != ARCHIVE_BYTES or hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError("packaged canonical starter control does not match release")
    return data
