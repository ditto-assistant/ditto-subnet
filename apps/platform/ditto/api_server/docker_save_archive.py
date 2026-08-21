"""Read the image-config digest from a Kaniko/docker-save tarball.

GCE screeners pin ``screened_image_id`` to the config digest and leave
``manifest.json`` ``Config`` as ``{configDigest}.json``. Targon/Kaniko
``--tar-path`` writes that same classic layout, but Platform historically
pinned the Artifact Registry *manifest* digest from smoke. DittoBench then
looks for ``{manifestDigest}.json``, misses, and fails closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from pathlib import Path
from typing import Literal

_HEX = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 1 << 20
_MAX_CONFIG_BYTES = 4 << 20


def config_digest_from_docker_save(path: Path) -> str | None:
    """Return ``sha256:<config digest>`` when the archive is a classic docker save.

    Accepts uncompressed or gzip tars. Returns ``None`` when the archive is
    not a single-image docker save with a content-addressed config member.
    """
    for mode in ("r:", "r:gz"):
        try:
            digest = _config_digest(path, mode)
        except (OSError, tarfile.TarError, ValueError, UnicodeError):
            continue
        if digest is not None:
            return digest
    return None


def _config_digest(path: Path, mode: Literal["r:", "r:gz"]) -> str | None:
    hashed: dict[str, str] = {}
    manifest: object | None = None
    with tarfile.open(path, mode=mode) as archive:
        for info in archive:
            name = str(info.name).lstrip("./")
            if not info.isfile():
                continue
            if name == "manifest.json":
                if manifest is not None:
                    return None
                if info.size <= 0 or info.size > _MAX_MANIFEST_BYTES:
                    return None
                raw = archive.extractfile(info)
                if raw is None:
                    return None
                manifest = json.loads(raw.read(info.size))
                continue
            digest_hex = _named_config_digest(name)
            if digest_hex is None:
                continue
            if info.size <= 0 or info.size > _MAX_CONFIG_BYTES:
                continue
            config_file = archive.extractfile(info)
            if config_file is None:
                continue
            config_bytes = config_file.read(info.size + 1)
            if len(config_bytes) != info.size:
                return None
            got = hashlib.sha256(config_bytes).hexdigest()
            if got == digest_hex:
                hashed[name] = digest_hex
    if not isinstance(manifest, list) or len(manifest) != 1:
        return None
    entry = manifest[0]
    if not isinstance(entry, dict):
        return None
    config_name = entry.get("Config")
    if not isinstance(config_name, str):
        return None
    digest_hex = hashed.get(config_name.lstrip("./"))
    if digest_hex is None:
        return None
    return "sha256:" + digest_hex


def _named_config_digest(name: str) -> str | None:
    if name.endswith(".json"):
        stem = name[: -len(".json")]
        if "/" not in stem and _HEX.fullmatch(stem):
            return stem
    prefix = "blobs/sha256/"
    if name.startswith(prefix):
        digest = name[len(prefix) :]
        if _HEX.fullmatch(digest):
            return digest
    # go-containerregistry (Kaniko --tar-path) names the config "sha256:<hex>".
    algo_prefix = "sha256:"
    if name.startswith(algo_prefix) and "/" not in name:
        digest = name[len(algo_prefix) :]
        if _HEX.fullmatch(digest):
            return digest
    return None
