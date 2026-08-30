"""Curator-only verification plans for protected Coding catalog publication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.api_models.coding_selection import CodingPrivateCatalogRecord
from ditto.api_server.coding_private_catalog import (
    coding_private_catalog_record_key,
    validate_coding_private_catalog_record,
)
from ditto.coding_selection import CodingSelectionCatalogIntegrityError

_MAX_RECORD_BYTES = 2 << 20
# The commitment schema permits 1_000_000 tasks. Each plan object is a small
# known-field envelope (indexes, object key, two SHA-256 fields, size, and a
# 256-byte task_version_id). 4 KiB/object plus 1 MiB of wrapping is above the
# canonical serialization of that maximum catalog.
_MAX_PLAN_OBJECT_BYTES = 4 << 10
_MAX_PLAN_BYTES = (1_000_000 * _MAX_PLAN_OBJECT_BYTES) + (1 << 20)
_MAX_JSON_DEPTH = 32
_RECORD_NAME = re.compile(r"^(?P<index>[0-9]{6})\.json$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PLAN_SCHEMA = "dittobench-coding-private-catalog-publication-v1"
ModelT = TypeVar("ModelT", bound=BaseModel)


class CodingCatalogPublicationError(ValueError):
    """The protected curator input cannot become a private catalog plan."""


@dataclass(frozen=True)
class CodingCatalogPublicationObject:
    catalog_index: int
    object_key: str
    record_sha256: str
    record_size_bytes: int
    task_commitment_sha256: str
    task_version_id: str

    def as_json(self) -> dict[str, Any]:
        return {
            "catalog_index": self.catalog_index,
            "object_key": self.object_key,
            "record_sha256": self.record_sha256,
            "record_size_bytes": self.record_size_bytes,
            "task_commitment_sha256": self.task_commitment_sha256,
            "task_version_id": self.task_version_id,
        }


@dataclass(frozen=True)
class CodingCatalogPublicationPlan:
    commitment: CodingCatalogCommitment
    objects: tuple[CodingCatalogPublicationObject, ...]
    publication_sha256: str

    def as_json(self) -> dict[str, Any]:
        return {
            "catalog_commitment_sha256": self.commitment.commitment_sha256,
            "coding_contract_version": self.commitment.coding_contract_version,
            "corpus_release_id": self.commitment.corpus_release_id,
            "objects": [item.as_json() for item in self.objects],
            "publication_sha256": self.publication_sha256,
            "schema": _PLAN_SCHEMA,
            "task_version_count": self.commitment.task_version_count,
            "weight_eligible": False,
        }


def plan_private_catalog_publication(
    *,
    commitment_path: Path,
    records_dir: Path,
) -> CodingCatalogPublicationPlan:
    """Validate all protected records and return their only permitted keys."""

    commitment, _ = _load_canonical_model(
        commitment_path,
        CodingCatalogCommitment,
        maximum_bytes=1 << 20,
        label="catalog commitment",
    )
    if records_dir.is_symlink() or not records_dir.is_dir():
        raise CodingCatalogPublicationError("catalog records directory is invalid")
    paths = sorted(records_dir.iterdir(), key=lambda value: value.name)
    objects: list[CodingCatalogPublicationObject] = []
    seen_task_versions: set[str] = set()
    for expected_index, path in enumerate(paths):
        match = _RECORD_NAME.fullmatch(path.name)
        if path.is_symlink() or not path.is_file() or match is None:
            raise CodingCatalogPublicationError(
                "catalog records use an invalid filename"
            )
        catalog_index = int(match.group("index"))
        if catalog_index != expected_index:
            raise CodingCatalogPublicationError(
                "catalog record indexes are not contiguous"
            )
        record, body = _load_canonical_model(
            path,
            CodingPrivateCatalogRecord,
            maximum_bytes=_MAX_RECORD_BYTES,
            label="private catalog record",
        )
        try:
            record = validate_coding_private_catalog_record(
                commitment=commitment,
                catalog_index=catalog_index,
                record=record,
            )
        except CodingSelectionCatalogIntegrityError as error:
            raise CodingCatalogPublicationError(
                "catalog record does not match the commitment"
            ) from error
        task_version_id = record.task_version.payload.task_version_id
        if task_version_id in seen_task_versions:
            raise CodingCatalogPublicationError("catalog task version is duplicated")
        seen_task_versions.add(task_version_id)
        objects.append(
            CodingCatalogPublicationObject(
                catalog_index=catalog_index,
                object_key=coding_private_catalog_record_key(
                    catalog_commitment_sha256=commitment.commitment_sha256,
                    catalog_index=catalog_index,
                ),
                record_sha256=hashlib.sha256(body).hexdigest(),
                record_size_bytes=len(body),
                task_commitment_sha256=record.task_version.task_commitment_sha256,
                task_version_id=task_version_id,
            )
        )
    if len(objects) != commitment.task_version_count:
        raise CodingCatalogPublicationError(
            "catalog record count disagrees with commitment"
        )
    publication_sha256 = _publication_sha256(commitment, objects)
    return CodingCatalogPublicationPlan(
        commitment=commitment,
        objects=tuple(objects),
        publication_sha256=publication_sha256,
    )


def write_private_catalog_publication_plan(
    *,
    plan: CodingCatalogPublicationPlan,
    output: Path,
) -> Path:
    """Atomically write a canonical plan; this function never contacts storage."""

    plan = _validated_plan(plan)
    if output.is_symlink():
        raise CodingCatalogPublicationError("catalog publication output is a symlink")
    output = output.resolve()
    if output.exists():
        raise CodingCatalogPublicationError("catalog publication output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    body = _plan_bytes(plan)
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{output.name}.", dir=output.parent, delete=False
    ) as staged:
        staged.write(body)
        staged.flush()
        os.fsync(staged.fileno())
        staged_name = Path(staged.name)
    try:
        os.link(staged_name, output)
    except FileExistsError as error:
        staged_name.unlink(missing_ok=True)
        raise CodingCatalogPublicationError(
            "catalog publication output already exists"
        ) from error
    except OSError:
        staged_name.unlink(missing_ok=True)
        raise
    staged_name.unlink(missing_ok=True)
    return output


def _load_canonical_model(
    path: Path,
    model: type[ModelT],
    *,
    maximum_bytes: int,
    label: str,
) -> tuple[ModelT, bytes]:
    if path.is_symlink() or not path.is_file():
        raise CodingCatalogPublicationError(f"{label} input is invalid")
    body = _read_bounded_regular_file(path, maximum_bytes=maximum_bytes, label=label)
    try:
        raw = json.loads(
            body,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite value {value}")
            ),
        )
        _check_json_depth(raw)
        parsed = model.model_validate(raw)
        canonical = coding_canonical_json_bytes(
            parsed.model_dump(mode="json", by_alias=True),
            maximum_bytes=maximum_bytes,
            label=label,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValidationError,
        ValueError,
    ) as error:
        raise CodingCatalogPublicationError(
            f"{label} input is not canonical"
        ) from error
    if body != canonical:
        raise CodingCatalogPublicationError(f"{label} input is not canonical")
    return parsed, body


def _read_bounded_regular_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise CodingCatalogPublicationError(f"{label} input is unreadable") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CodingCatalogPublicationError(f"{label} input is invalid")
        if info.st_size < 1 or info.st_size > maximum_bytes:
            raise CodingCatalogPublicationError(f"{label} input exceeds bounds")
        body = bytearray()
        while len(body) < maximum_bytes + 1:
            chunk = os.read(fd, maximum_bytes + 1 - len(body))
            if not chunk:
                break
            body.extend(chunk)
        body = bytes(body)
    except CodingCatalogPublicationError:
        raise
    except OSError as error:
        raise CodingCatalogPublicationError(f"{label} input is unreadable") from error
    finally:
        os.close(fd)
    if not body or len(body) > maximum_bytes:
        raise CodingCatalogPublicationError(f"{label} input exceeds bounds")
    return body


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _publication_projection(
    commitment: CodingCatalogCommitment,
    objects: list[CodingCatalogPublicationObject]
    | tuple[CodingCatalogPublicationObject, ...],
) -> dict[str, Any]:
    return {
        "catalog_commitment_sha256": commitment.commitment_sha256,
        "coding_contract_version": commitment.coding_contract_version,
        "corpus_release_id": commitment.corpus_release_id,
        "objects": [item.as_json() for item in objects],
        "schema": _PLAN_SCHEMA,
        "task_version_count": commitment.task_version_count,
        "weight_eligible": False,
    }


def _publication_sha256(
    commitment: CodingCatalogCommitment,
    objects: list[CodingCatalogPublicationObject]
    | tuple[CodingCatalogPublicationObject, ...],
) -> str:
    try:
        body = coding_canonical_json_bytes(
            _publication_projection(commitment, objects),
            maximum_bytes=_MAX_PLAN_BYTES,
            label="private catalog publication plan",
        )
    except ValueError as error:
        raise CodingCatalogPublicationError(
            "catalog publication plan exceeds bounds"
        ) from error
    return hashlib.sha256(body).hexdigest()


def _validated_plan(plan: CodingCatalogPublicationPlan) -> CodingCatalogPublicationPlan:
    try:
        commitment = CodingCatalogCommitment.model_validate_json(
            plan.commitment.model_dump_json(by_alias=True)
        )
    except (ValidationError, ValueError) as error:
        raise CodingCatalogPublicationError(
            "catalog publication plan is invalid"
        ) from error
    if len(plan.objects) != commitment.task_version_count:
        raise CodingCatalogPublicationError("catalog publication plan count is invalid")
    task_versions: set[str] = set()
    for expected_index, item in enumerate(plan.objects):
        if (
            item.catalog_index != expected_index
            or item.object_key
            != coding_private_catalog_record_key(
                catalog_commitment_sha256=commitment.commitment_sha256,
                catalog_index=expected_index,
            )
            or item.record_size_bytes < 1
            or not _SHA256.fullmatch(item.record_sha256)
            or not _SHA256.fullmatch(item.task_commitment_sha256)
            or not item.task_version_id
            or len(item.task_version_id.encode()) > 256
            or any(character.isspace() for character in item.task_version_id)
        ):
            raise CodingCatalogPublicationError(
                "catalog publication plan object is invalid"
            )
        if item.task_version_id in task_versions:
            raise CodingCatalogPublicationError(
                "catalog publication task version is duplicated"
            )
        task_versions.add(item.task_version_id)
    if plan.publication_sha256 != _publication_sha256(commitment, plan.objects):
        raise CodingCatalogPublicationError(
            "catalog publication plan digest is invalid"
        )
    return CodingCatalogPublicationPlan(
        commitment=commitment,
        objects=plan.objects,
        publication_sha256=plan.publication_sha256,
    )


def _plan_bytes(plan: CodingCatalogPublicationPlan) -> bytes:
    try:
        body = plan.as_json()
        if body["publication_sha256"] != _publication_sha256(
            plan.commitment, plan.objects
        ):
            raise CodingCatalogPublicationError(
                "catalog publication plan digest is invalid"
            )
        return coding_canonical_json_bytes(
            body,
            maximum_bytes=_MAX_PLAN_BYTES,
            label="private catalog publication plan",
        )
    except ValueError as error:
        raise CodingCatalogPublicationError(
            "catalog publication plan exceeds bounds"
        ) from error


def _check_json_depth(value: Any, *, depth: int = 1) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON nesting exceeds bounds")
    if isinstance(value, dict):
        for child in value.values():
            _check_json_depth(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_json_depth(child, depth=depth + 1)
