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

from ditto_screening_protocol.v13_private_execute import PrivateCasePayload
from ditto_screening_protocol.v13_private_provision import (
    PrivateBlueprintPair,
    PrivateProvisioningUnavailable,
)

_MAX_INDEX_BYTES = 256 * 1024
_MAX_PAIRS = 512
_SHA = frozenset("0123456789abcdef")


def _owner_only_directory(fd: int) -> None:
    metadata = os.fstat(fd)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise PrivateProvisioningUnavailable("protected bank permissions unavailable")


class _OpenedBank:
    """Keep the validated directory handles for the entire bank read."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root_fd: int | None = None
        self.payloads_fd: int | None = None

    def __enter__(self) -> _OpenedBank:
        try:
            self.root_fd = os.open(
                self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            _owner_only_directory(self.root_fd)
            self.payloads_fd = os.open(
                "payloads",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self.root_fd,
            )
            _owner_only_directory(self.payloads_fd)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args: object) -> None:
        try:
            if self.root_fd is not None:
                _owner_only_directory(self.root_fd)
            if self.payloads_fd is not None:
                _owner_only_directory(self.payloads_fd)
        finally:
            for fd in (self.payloads_fd, self.root_fd):
                if fd is not None:
                    os.close(fd)
            self.payloads_fd = None
            self.root_fd = None

    def read_file(self, *, name: str, payload: bool) -> bytes:
        directory_fd = self.payloads_fd if payload else self.root_fd
        if directory_fd is None:
            raise PrivateProvisioningUnavailable("protected bank unavailable")
        limit = 128 * 1024 if payload else _MAX_INDEX_BYTES
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_mode & 0o077
                or before.st_size > limit
            ):
                raise PrivateProvisioningUnavailable("protected bank file unavailable")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(fd)
            if (
                len(raw) != before.st_size
                or after.st_size != before.st_size
                or after.st_uid != before.st_uid
                or after.st_mode != before.st_mode
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns
            ):
                raise PrivateProvisioningUnavailable(
                    "protected bank file changed during read"
                )
            return raw
        finally:
            os.close(fd)


def _case_bytes(bank: _OpenedBank, digest: str, pair_id: UUID, side: str) -> bytes:
    if len(digest) != 64 or not set(digest) <= _SHA:
        raise PrivateProvisioningUnavailable("protected bank digest invalid")
    raw = bank.read_file(name=digest, payload=True)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise PrivateProvisioningUnavailable("protected bank digest invalid")
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
        with _OpenedBank(self._root) as bank:
            return self._load_opened(bank)

    def _load_opened(self, bank: _OpenedBank) -> tuple[PrivateBlueprintPair, ...]:
        parsed = json.loads(bank.read_file(name="bank.json", payload=False))
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
            control = _case_bytes(bank, entry["control_sha256"], pair_id, "control")
            variant = _case_bytes(bank, entry["variant_sha256"], pair_id, "variant")
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
