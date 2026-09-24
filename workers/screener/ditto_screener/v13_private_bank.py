"""Read an operator-provisioned V13 blueprint bank without exposing case bytes.

The bank is an owner-only directory outside the release tree. Its manifest
contains digest references, while payloads live at ``payloads/<sha256>``.
Nothing here creates or approves protected cases or registers a package.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path
from uuid import UUID

from ditto_screener.v13_private_adapter import _read_digest_object
from ditto_screening_protocol.v13_private_execute import PrivateCasePayload
from ditto_screening_protocol.v13_private_provision import (
    PrivateBlueprintPair,
    PrivateProvisioningUnavailable,
)

_MAX_INDEX_BYTES = 256 * 1024
_MAX_PAIRS = 512
_SHA = frozenset("0123456789abcdef")


def _owner_only_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise PrivateProvisioningUnavailable("protected bank permissions unavailable")


def _read_index(root: Path) -> bytes:
    root_fd: int | None = None
    index_fd: int | None = None
    try:
        _owner_only_directory(root)
        _owner_only_directory(root / "payloads")
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        index_fd = os.open("bank.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
        metadata = os.fstat(index_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or metadata.st_size > _MAX_INDEX_BYTES
        ):
            raise PrivateProvisioningUnavailable("protected bank index unavailable")
        raw = os.read(index_fd, _MAX_INDEX_BYTES + 1)
        if len(raw) != metadata.st_size:
            raise PrivateProvisioningUnavailable("protected bank index unavailable")
        return raw
    except PrivateProvisioningUnavailable:
        raise
    except OSError:
        raise PrivateProvisioningUnavailable("protected bank unavailable") from None
    finally:
        for descriptor in (index_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def _case_bytes(root: Path, digest: str, pair_id: UUID, side: str) -> bytes:
    if len(digest) != 64 or not set(digest) <= _SHA:
        raise PrivateProvisioningUnavailable("protected bank digest invalid")
    metadata = (root / "payloads" / digest).lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise PrivateProvisioningUnavailable("protected bank payload unavailable")
    raw = _read_digest_object(root, "payloads", digest, 128 * 1024)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or set(parsed) != set(
        PrivateCasePayload.model_fields
    ):
        raise PrivateProvisioningUnavailable("protected bank case shape invalid")
    case = PrivateCasePayload.model_validate(parsed)
    if case.pair_id != pair_id or case.side != side:
        raise PrivateProvisioningUnavailable("protected bank case identity invalid")
    return raw


class FileProtectedBlueprintBank:
    """Fail-closed, read-only source for a private provisioner process.

    The operator must mount a semantically reviewed bank at an owner-only path
    and keep that mount out of screener containers and public build contexts.
    """

    def __init__(self, root: Path | None) -> None:
        self._root = root

    async def load(self) -> tuple[PrivateBlueprintPair, ...]:
        if self._root is None:
            raise PrivateProvisioningUnavailable("protected bank unavailable")
        try:
            return await asyncio.to_thread(self._load)
        except PrivateProvisioningUnavailable:
            raise
        except Exception:
            raise PrivateProvisioningUnavailable("protected bank unavailable") from None

    def _load(self) -> tuple[PrivateBlueprintPair, ...]:
        assert self._root is not None
        parsed = json.loads(_read_index(self._root))
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {"revision", "pairs"}
            or parsed["revision"] != "v13-protected-blueprint-bank-v1"
            or not isinstance(parsed["pairs"], list)
            or not 1 <= len(parsed["pairs"]) <= _MAX_PAIRS
        ):
            raise PrivateProvisioningUnavailable("protected bank index invalid")
        pairs: list[PrivateBlueprintPair] = []
        seen: set[UUID] = set()
        for entry in parsed["pairs"]:
            if not isinstance(entry, dict) or set(entry) != {
                "pair_id",
                "transformation_class",
                "control_sha256",
                "variant_sha256",
            }:
                raise PrivateProvisioningUnavailable("protected bank pair invalid")
            pair_id = UUID(entry["pair_id"])
            if pair_id in seen or entry["control_sha256"] == entry["variant_sha256"]:
                raise PrivateProvisioningUnavailable("protected bank pair invalid")
            seen.add(pair_id)
            control = _case_bytes(
                self._root, entry["control_sha256"], pair_id, "control"
            )
            variant = _case_bytes(
                self._root, entry["variant_sha256"], pair_id, "variant"
            )
            if hashlib.sha256(control).hexdigest() != entry["control_sha256"]:
                raise PrivateProvisioningUnavailable("protected bank digest invalid")
            if hashlib.sha256(variant).hexdigest() != entry["variant_sha256"]:
                raise PrivateProvisioningUnavailable("protected bank digest invalid")
            control_case = PrivateCasePayload.model_validate_json(control)
            variant_case = PrivateCasePayload.model_validate_json(variant)
            if (
                control_case.semantic_contract_sha256
                != variant_case.semantic_contract_sha256
            ):
                raise PrivateProvisioningUnavailable(
                    "protected bank semantics mismatch"
                )
            pairs.append(
                PrivateBlueprintPair(
                    pair_id=pair_id,
                    transformation_class=entry["transformation_class"],
                    control=control,
                    variant=variant,
                )
            )
        return tuple(pairs)
