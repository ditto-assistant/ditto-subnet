"""Bounded operator-only artifact observations. Never used for admission.

No archive is extracted or executed. Profiles are temporary hashes/sketches and
are never exposed in responses or persisted. Comparison always uses the current
reference corpus and reports its exact identity; it is not source clearance.
"""

from __future__ import annotations

import codecs
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import PurePosixPath
from typing import Any

from ditto.api_models.submission_attempts import AttemptKind, AttemptObservationPolicy
from ditto.api_server.fingerprint import (
    compute_content_fingerprint,
    content_similarity,
    reference_corpus_provenance,
)

CLASSIFIER_VERSION = 2
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_UNPACKED_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 512
MAX_OWNER_LINKS = 100
SMALL_DELTA_JACCARD = 0.98
_PACKAGING_FILES = frozenset({"Dockerfile", ".dockerignore"})
REPAIR_REASONS = frozenset(
    {
        "docker-build",
        "health-contract",
        "seed-ack-invalid",
        "seed-memory-cap",
        "seed-exit",
        "seed-readonly-write",
        "seed-http-error",
        "seed-oversized-response",
        "seed-unreachable",
    }
)


class ProfileUnavailable(ValueError):
    """Invalid, oversized, or incompatible data cannot support a comparison."""


class _BoundedReader(io.RawIOBase):
    def __init__(self, stream: gzip.GzipFile) -> None:
        self.stream = stream
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        remaining = MAX_UNPACKED_BYTES - self.total
        chunk = self.stream.read(
            min(size if size >= 0 else remaining + 1, remaining + 1)
        )
        self.total += len(chunk)
        if self.total > MAX_UNPACKED_BYTES:
            raise ProfileUnavailable(
                "Observation exceeds the decompressed byte budget."
            )
        return chunk


def observation_policy(source_build: str) -> AttemptObservationPolicy:
    corpus = reference_corpus_provenance()
    settings = {
        "classifier_version": CLASSIFIER_VERSION,
        "reference_corpus": corpus,
        "small_delta_jaccard": SMALL_DELTA_JACCARD,
        "max_archive_bytes": MAX_ARCHIVE_BYTES,
        "max_unpacked_bytes": MAX_UNPACKED_BYTES,
        "max_members": MAX_MEMBERS,
        "max_owner_links": MAX_OWNER_LINKS,
    }
    return AttemptObservationPolicy(
        source_build=source_build,
        settings_digest=hashlib.sha256(
            json.dumps(settings, sort_keys=True).encode()
        ).hexdigest(),
        classifier_version=CLASSIFIER_VERSION,
        reference_corpus=corpus,
        small_delta_jaccard=SMALL_DELTA_JACCARD,
        max_archive_bytes=MAX_ARCHIVE_BYTES,
        max_unpacked_bytes=MAX_UNPACKED_BYTES,
        max_members=MAX_MEMBERS,
        max_owner_links=MAX_OWNER_LINKS,
    )


def artifact_profile(tar_bytes: bytes) -> dict[str, Any]:
    if len(tar_bytes) > MAX_ARCHIVE_BYTES:
        raise ProfileUnavailable("Observation exceeds the archive byte budget.")
    members: dict[str, str] = {}
    opaque: list[str] = []
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(tar_bytes)) as compressed:
            reader = _BoundedReader(compressed)
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                for count, member in enumerate(archive, 1):
                    if count > MAX_MEMBERS:
                        raise ProfileUnavailable(
                            "Observation exceeds the member budget."
                        )
                    path = member.name.removeprefix("./")
                    normalized_path = str(PurePosixPath(path))
                    if member.isdir() and normalized_path == ".":
                        continue
                    if (
                        len(path) > 512
                        or path.startswith("/")
                        or ".." in PurePosixPath(path).parts
                        or "\\" in path
                        or not path
                        or normalized_path == "."
                        or not (member.isfile() or member.isdir())
                    ):
                        raise ProfileUnavailable(
                            "Observation contains an unsafe member."
                        )
                    if member.isdir():
                        continue
                    path = normalized_path
                    if (
                        path in members
                        or member.size < 0
                        or member.size > MAX_UNPACKED_BYTES
                    ):
                        raise ProfileUnavailable(
                            "Observation contains an invalid member."
                        )
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ProfileUnavailable("Observation member is unreadable.")
                    digest = hashlib.sha256()
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    is_text = True
                    while chunk := stream.read(64 * 1024):
                        digest.update(chunk)
                        if is_text:
                            try:
                                decoder.decode(chunk)
                                is_text = b"\x00" not in chunk
                            except UnicodeDecodeError:
                                is_text = False
                    if is_text:
                        try:
                            decoder.decode(b"", final=True)
                        except UnicodeDecodeError:
                            is_text = False
                    members[path] = digest.hexdigest()
                    if not is_text and path not in _PACKAGING_FILES:
                        opaque.append(digest.hexdigest())
            # Include trailing padding/metadata in the budget and verify gzip CRC.
            while reader.read(64 * 1024):
                pass
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ProfileUnavailable("Observation archive is unreadable.") from error
    runtime = sorted(
        value for path, value in members.items() if path not in _PACKAGING_FILES
    )
    packaging = sorted(
        (path, value) for path, value in members.items() if path in _PACKAGING_FILES
    )
    if not runtime:
        raise ProfileUnavailable("Observation has no runtime files.")
    return {
        "runtime_hash": hashlib.sha256(json.dumps(runtime).encode()).hexdigest(),
        "packaging_hash": hashlib.sha256(json.dumps(packaging).encode()).hexdigest(),
        "opaque_hash": hashlib.sha256(json.dumps(sorted(opaque)).encode()).hexdigest(),
        "fingerprint": compute_content_fingerprint(tar_bytes),
    }


def classify_pair(
    candidate: dict[str, Any],
    reference: dict[str, Any],
    *,
    infrastructure_failure: bool,
) -> tuple[AttemptKind, str]:
    same_runtime = candidate["runtime_hash"] == reference["runtime_hash"]
    if same_runtime and infrastructure_failure:
        return (
            "infrastructure_retry",
            "Same runtime after recorded infrastructure failure.",
        )
    if same_runtime and candidate["packaging_hash"] != reference["packaging_hash"]:
        return (
            "packaging_only_repair",
            "Only packaging inputs changed; runtime hashes match.",
        )
    if same_runtime:
        return (
            "small_source_delta",
            "Runtime hashes match across repacking or renaming.",
        )
    if candidate["opaque_hash"] != reference["opaque_hash"]:
        return (
            "inconclusive",
            "Opaque runtime inputs changed; lexical overlap is insufficient.",
        )
    left, right = candidate["fingerprint"] or {}, reference["fingerprint"] or {}
    if (
        not left.get("m")
        or not right.get("m")
        or left.get("v") is None
        or left.get("v") != right.get("v")
        or left.get("corpus") != right.get("corpus")
    ):
        return "inconclusive", "Compatible residual source evidence is unavailable."
    similarity, _ = content_similarity(left, right)
    if similarity >= SMALL_DELTA_JACCARD:
        return (
            "small_source_delta",
            "Residual lexical overlap is above the observation threshold.",
        )
    return (
        "material_new_work",
        "Residual lexical overlap is below the observation threshold.",
    )
