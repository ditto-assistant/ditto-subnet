"""Atomic private artifact pinning. No lease, public reveal or activation rights.

Callers own their transaction; never issue a lease until it has committed.
Transform/semantic validation must happen before this module is called, outside
any long-lived database transaction. A receipt is provenance, not qualification.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import PrivateBenchmarkDataset

MAX_ARTIFACT_BYTES = 32 << 20
MAX_RECEIPT_BYTES = 4 << 20
_DIGEST = re.compile(r"[0-9a-f]{64}")


class PrivateDatasetError(ValueError):
    """Sanitized private-artifact validation error."""


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class PrivateDatasetIdentity:
    """Scope is a trusted work-set identity, shared by both sides of CRN.

    It must not be derived from a miner-selected name, or reused for unrelated
    work. A transformation-profile change deliberately creates a new identity.
    """

    scope: str
    seed: int
    run_size: str
    transform_profile_sha256: str
    bench_version: int = 13

    def digest(self) -> str:
        if (
            self.bench_version != 13
            or type(self.seed) is not int
            or not 0 <= self.seed < 2**63
            or self.run_size not in {"small", "medium", "full"}
            or not 1 <= len(self.scope) <= 256
            or any(ord(c) < 32 for c in self.scope)
            or not _DIGEST.fullmatch(self.transform_profile_sha256)
        ):
            raise PrivateDatasetError("invalid private dataset identity")
        return _sha(
            json.dumps(
                [
                    "private-benchmark-dataset-v1",
                    self.scope,
                    self.bench_version,
                    self.seed,
                    self.run_size,
                    self.transform_profile_sha256,
                ],
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
        )


@dataclass(frozen=True)
class PrivateDatasetBytes:
    dataset_id: UUID
    identity: PrivateDatasetIdentity
    dataset_sha256: str
    base_sha256: str
    validation_receipt_sha256: str
    dataset_bytes: bytes = field(repr=False)
    base_bytes: bytes = field(repr=False)
    validation_receipt_bytes: bytes = field(repr=False)


def _object(body: bytes, maximum: int) -> dict:
    if not 0 < len(body) <= maximum:
        raise PrivateDatasetError("private artifact size invalid")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PrivateDatasetError("duplicate private artifact field")
            result[key] = value
        return result

    def constant(_value):
        raise PrivateDatasetError("non-finite private artifact value")

    try:
        value = json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise PrivateDatasetError("private artifact JSON invalid") from None
    if not isinstance(value, dict):
        raise PrivateDatasetError("private artifact must be an object")
    return value


def _validate(identity, base_bytes, dataset_bytes, receipt_bytes) -> None:
    identity.digest()
    base = _object(base_bytes, MAX_ARTIFACT_BYTES)
    dataset = _object(dataset_bytes, MAX_ARTIFACT_BYTES)
    for obj in (base, dataset):
        if (
            type(obj.get("seed")) is not int
            or obj["seed"] != identity.seed
            or obj.get("bench_version") != identity.bench_version
            or type(obj.get("surface_salt")) is not int
            or not 0 < obj["surface_salt"] < 2**64
        ):
            raise PrivateDatasetError("private artifact identity mismatch")
    if base["surface_salt"] != dataset["surface_salt"] or base == dataset:
        raise PrivateDatasetError("private artifact transformation missing or invalid")
    receipt = _object(receipt_bytes, MAX_RECEIPT_BYTES)
    if (
        receipt.get("schema") != "private-surface-validation-v1"
        or receipt.get("accepted") is not True
        or receipt.get("base_sha256") != _sha(base_bytes)
        or receipt.get("dataset_sha256") != _sha(dataset_bytes)
        or receipt.get("transform_profile_sha256") != identity.transform_profile_sha256
    ):
        raise PrivateDatasetError("private artifact validation receipt mismatch")


def _read(
    row: PrivateBenchmarkDataset, identity: PrivateDatasetIdentity
) -> PrivateDatasetBytes:
    if (
        row.identity_sha256 != identity.digest()
        or row.scope != identity.scope
        or row.seed != identity.seed
        or row.bench_version != identity.bench_version
        or row.run_size != identity.run_size
        or row.transform_profile_sha256 != identity.transform_profile_sha256
        or _sha(row.base_bytes) != row.base_sha256
        or _sha(row.dataset_bytes) != row.dataset_sha256
        or _sha(row.validation_receipt_bytes) != row.validation_receipt_sha256
    ):
        raise PrivateDatasetError("stored private artifact integrity mismatch")
    _validate(identity, row.base_bytes, row.dataset_bytes, row.validation_receipt_bytes)
    return PrivateDatasetBytes(
        dataset_id=row.dataset_id,
        identity=identity,
        dataset_sha256=row.dataset_sha256,
        base_sha256=row.base_sha256,
        validation_receipt_sha256=row.validation_receipt_sha256,
        dataset_bytes=row.dataset_bytes,
        base_bytes=row.base_bytes,
        validation_receipt_bytes=row.validation_receipt_bytes,
    )


async def find_private_dataset(
    session: AsyncSession, *, identity: PrivateDatasetIdentity
) -> PrivateDatasetBytes | None:
    row = await session.scalar(
        select(PrivateBenchmarkDataset).where(
            PrivateBenchmarkDataset.identity_sha256 == identity.digest()
        )
    )
    return _read(row, identity) if row is not None else None


async def pin_private_dataset(
    session: AsyncSession,
    *,
    identity: PrivateDatasetIdentity,
    base_bytes: bytes,
    dataset_bytes: bytes,
    validation_receipt_bytes: bytes,
) -> PrivateDatasetBytes:
    """First committed candidate wins; every concurrent loser reads its bytes.

    READ COMMITTED is required (Platform's default). Do not report success from
    an uncommitted transaction. A lost commit response is recovered by lookup,
    not by replacing the existing dataset with another provider response.
    """
    _validate(identity, base_bytes, dataset_bytes, validation_receipt_bytes)
    await _insert_private(
        session,
        insert(PrivateBenchmarkDataset)
        .values(
            dataset_id=uuid4(),
            identity_sha256=identity.digest(),
            scope=identity.scope,
            bench_version=identity.bench_version,
            seed=identity.seed,
            run_size=identity.run_size,
            transform_profile_sha256=identity.transform_profile_sha256,
            validation_receipt_sha256=_sha(validation_receipt_bytes),
            validation_receipt_bytes=validation_receipt_bytes,
            base_sha256=_sha(base_bytes),
            dataset_sha256=_sha(dataset_bytes),
            base_bytes=base_bytes,
            dataset_bytes=dataset_bytes,
        )
        .on_conflict_do_nothing(index_elements=["identity_sha256"])
    )
    winner = await find_private_dataset(session, identity=identity)
    if winner is None:
        raise PrivateDatasetError("private artifact pin unavailable")
    return winner


async def _insert_private(session: AsyncSession, statement) -> None:
    # Driver errors can contain a failing-row DETAIL even when SQLAlchemy bind
    # logging is disabled. Do not propagate those bytes into HTTP error logs.
    try:
        await session.execute(statement)
    except SQLAlchemyError:
        raise PrivateDatasetError("private artifact storage unavailable") from None
