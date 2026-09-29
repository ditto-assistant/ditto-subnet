"""Immutable public source-control artifact packaged from v0.330.3."""

from __future__ import annotations

import hashlib
from importlib import resources

RELEASE = "v0.330.3"
RELEASE_COMMIT = "b20b065b133ecbe1a8e0e9c350169bfcb19e4be2"
SOURCE_TREE = "733904ee190b9a11e7d5023f83559cb48bac0d01"
ARCHIVE_SHA256 = "047aaa870a6c6b8ea4da1da802792f3f44991e5c8d5698d6cb0be3334a8122d3"
ARCHIVE_BYTES = 4_915_314
DOCKERFILE_SHA256 = "d3a1a2a1e5d43b0465c28712457d95432942ac8f017fd10d538859a901a54641"
OBJECT_KEY = "source-controls/canonical-starter-v0.330.3.tgz"


def archive_bytes() -> bytes:
    data = (
        resources.files("ditto.api_server")
        .joinpath("data/canonical-starter-v0.330.3.tgz")
        .read_bytes()
    )
    if len(data) != ARCHIVE_BYTES or hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError("packaged canonical starter control does not match release")
    return data
