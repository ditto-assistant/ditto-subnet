"""Dueling bench (bench_version 16) season, slice and composite primitives.

From :data:`DUELING_MIN_BENCH_VERSION` a canonical agent run is a datagen half
plus a generator slice: :data:`DUELING_SLICE_COLUMNS` pinned generator columns
of :data:`DUELING_SLICE_CASES_PER_COLUMN` cases each, frozen per season
(``research/dittobench-datagen/docs/dueling-bench.md`` section 7). This module
is the Python mirror of the Go contract in ``research/dittobench-datagen``:

- ``challenge/season.go``: :func:`season_commitment`, :func:`season_seed` and
  :func:`slice_seed` (``challenge/testdata/season_vectors.json``);
- ``challenge/dueling_score.go``: :func:`dueling_composite` and
  :func:`dueling_composite_stderr`
  (``challenge/testdata/dueling_composite_vectors.json``);
- the ``details.dueling`` report shape the scorer signs
  (:class:`DuelingReport`) and the season column pin
  (:class:`DuelingColumnPin`) Platform and the validator share.

Every derivation returns lowercase hex and every composite reproduces the Go
result bit for bit: the arithmetic is float64 in a fixed order (columns in
``column_id`` order, every product rounded before it is added). Every dueling
gate is a floor on :data:`DUELING_MIN_BENCH_VERSION`, never an equality.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from collections.abc import Sequence, Set
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto_screening_protocol.crn_block_binding import normalize_block_hash

DUELING_MIN_BENCH_VERSION = 16
"""First bench version whose canonical run carries a generator slice."""

DUELING_SLICE_COLUMNS = 5
"""Fixed number of generator columns in a slice. The public default generator
fills every slot no eligible miner generator takes."""

DUELING_SLICE_CASES_PER_COLUMN = 35
"""Case count every slice column's generate request asks for."""

DUELING_COLUMN_WEIGHT = 0.1
"""One column's share of the composite (0.5 / DUELING_SLICE_COLUMNS). A masked
column's weight returns to the datagen half, so no column carries more."""

DUELING_SLICE_CONTRACT = "dittobench-dueling-slice/v1"
"""Slice dataset contract (``challenge.SliceContractV1``)."""

DUELING_SLICE_MODE = "dueling-slice-v1"
"""``/v2/score`` private dataset mode that carries a slice."""

SEASON_SECRET_BYTES = 32

_SEASON_COMMIT_DOMAIN = b"dittobench-dueling-season-commit/v1"
_SEASON_SEED_DOMAIN = b"dittobench-dueling-season-seed/v1"
_SLICE_SEED_DOMAIN = b"dittobench-dueling-slice/v1"

_U64_MAX = (1 << 64) - 1
_I64_MAX = (1 << 63) - 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_COLUMN_ID_PATTERN = r"^c[0-9]{1,2}$"
_IDENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_HOTKEY_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_MAX_OWNER_KEY = 256
_MIN_GRADING_BENCH_VERSION = 13


def is_dueling_bench_version(bench_version: int) -> bool:
    """Whether ``bench_version`` carries a generator slice (a floor)."""
    return bench_version >= DUELING_MIN_BENCH_VERSION


def _u64be(value: int) -> bytes:
    return struct.pack(">Q", value)


def _check_season(season_index: int, secret: bytes) -> None:
    if type(season_index) is not int or not 0 <= season_index <= _U64_MAX:
        raise ValueError("season index must be a u64")
    if len(secret) != SEASON_SECRET_BYTES:
        raise ValueError("season secret must be 32 bytes")


def season_commitment(season_index: int, secret: bytes) -> str:
    """``sha256(domain || 0x00 || u64be(season) || secret)`` as lowercase hex."""
    _check_season(season_index, secret)
    h = hashlib.sha256(_SEASON_COMMIT_DOMAIN)
    h.update(b"\x00")
    h.update(_u64be(season_index))
    h.update(secret)
    return h.hexdigest()


def season_seed(season_index: int, secret: bytes, anchor_block_hash: str) -> str:
    """``sha256(domain || 0x00 || u64be(season) || secret || block32)``.

    The anchor block hash is 32 bytes of hex, with or without ``0x``, in
    either case.
    """
    _check_season(season_index, secret)
    block = normalize_block_hash(anchor_block_hash)
    if not _SHA256_RE.fullmatch(block):
        raise ValueError("season anchor block hash must be 32 bytes of hex")
    h = hashlib.sha256(_SEASON_SEED_DOMAIN)
    h.update(b"\x00")
    h.update(_u64be(season_index))
    h.update(secret)
    h.update(bytes.fromhex(block))
    return h.hexdigest()


def slice_seed(season_seed_hex: str, run_seed: int, column_id: str) -> str:
    """One slice column's generate seed for one run.

    ``sha256(domain || 0x00 || season_seed_hex || 0x00 || u64be(run_seed) ||
    0x00 || column_id)``; the season seed is hashed as its 64-character hex
    text and ``run_seed`` is the ticket's datagen seed (a non-negative int64).
    """
    if not _SHA256_RE.fullmatch(season_seed_hex):
        raise ValueError("season seed must be 64 lowercase hex characters")
    if type(run_seed) is not int or not 0 <= run_seed <= _I64_MAX:
        raise ValueError("slice run seed must be a non-negative int64")
    if not column_id:
        raise ValueError("slice column id must be non-empty")
    h = hashlib.sha256(_SLICE_SEED_DOMAIN)
    h.update(b"\x00")
    h.update(season_seed_hex.encode("ascii"))
    h.update(b"\x00")
    h.update(_u64be(run_seed))
    h.update(b"\x00")
    h.update(column_id.encode("utf-8"))
    return h.hexdigest()


class DuelingColumnAggregate(BaseModel):
    """One slice column's per-run case statistics (``details.dueling``).

    ``n`` counts the column's executed cases after the slice dedupe; ``sum``
    and ``sum_sq`` are the sums of their scores and squared scores, a failed
    ``/run`` counting as 0. ``owner_key`` is ``""`` for the public default
    generator, including a substituted column, and is never masked.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, allow_inf_nan=False)

    column_id: Annotated[str, Field(pattern=_COLUMN_ID_PATTERN)]
    owner_key: Annotated[str, Field(max_length=_MAX_OWNER_KEY)]
    submission_id: Annotated[str, Field(pattern=_IDENT_PATTERN)] | None = None
    grading_bench_version: (
        Annotated[int, Field(ge=_MIN_GRADING_BENCH_VERSION)] | None
    ) = None
    n: Annotated[int, Field(ge=0)]
    sum: float
    sum_sq: Annotated[float, Field(ge=0)]
    substituted: bool = False

    @model_validator(mode="after")
    def _substituted_is_unowned(self) -> Self:
        if self.substituted and self.owner_key:
            raise ValueError(
                "a substituted column carries the public default and has no owner"
            )
        if self.n == 0 and (self.sum != 0 or self.sum_sq != 0):
            raise ValueError("a column with no cases has zero sums")
        return self

    def mean(self) -> float:
        """The column's mean case score; a column with no cases scores 0."""
        if self.n <= 0:
            return 0.0
        return self.sum / self.n


def _counted(
    columns: Sequence[DuelingColumnAggregate], mask: Set[str]
) -> list[DuelingColumnAggregate]:
    kept = [c for c in columns if c.owner_key == "" or c.owner_key not in mask]
    return sorted(kept, key=lambda c: c.column_id)


def dueling_composite(
    datagen_raw: float,
    datagen_gate: float,
    columns: Sequence[DuelingColumnAggregate],
    mask: Set[str],
) -> float:
    """``gate x ((1 - w x |U|) x datagen + w x sum_{c in U} col_c)``.

    ``U`` is the columns whose owner is not in ``mask`` (``""`` is never
    masked) and ``col_c = sum / n`` (0 when ``n = 0``). The reported composite
    masks the agent's own owner; the paired dethrone test masks both agents'
    owners. Mirrors ``challenge.DuelingComposite`` bit for bit.
    """
    w = DUELING_COLUMN_WEIGHT
    counted = _counted(columns, mask)
    generator = 0.0
    for c in counted:
        generator = generator + w * c.mean()
    share = 1 - w * float(len(counted))
    inner = share * datagen_raw + generator
    return datagen_gate * inner


def dueling_composite_stderr(
    datagen_stderr: float,
    datagen_gate: float,
    columns: Sequence[DuelingColumnAggregate],
    mask: Set[str],
) -> float:
    """Stratified standard error of :func:`dueling_composite`.

    ``SE^2 = (1 - w x |U|)^2 x SE_datagen^2 + w^2 x sum_{c in U} s_c^2 / n_c``,
    scaled by the datagen gate; ``s_c^2`` is the sample variance (clamped at
    0, no contribution when ``n < 2``). Mirrors
    ``challenge.DuelingCompositeStderr`` bit for bit.
    """
    w = DUELING_COLUMN_WEIGHT
    counted = _counted(columns, mask)
    strata = 0.0
    for c in counted:
        if c.n < 2:
            continue
        n = float(c.n)
        ss = c.sum_sq - (c.sum * c.sum) / n
        variance = ss / (n - 1)
        if variance < 0 or math.isnan(variance):
            variance = 0.0
        strata = strata + variance / n
    share = 1 - w * float(len(counted))
    datagen_var = (share * share) * (datagen_stderr * datagen_stderr)
    total = datagen_var + (w * w) * strata
    return datagen_gate * math.sqrt(total)


class DuelingReport(BaseModel):
    """The ``details.dueling`` block of a bench >= 16 score report.

    ``owner_key`` is the scored agent's owner; the reported composite is
    :meth:`self_composite` (the agent's own columns masked).
    """

    model_config = ConfigDict(extra="ignore", frozen=True, allow_inf_nan=False)

    season_id: Annotated[str, Field(pattern=_IDENT_PATTERN)]
    season_index: Annotated[int, Field(ge=0, le=_U64_MAX)]
    slice_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    owner_key: Annotated[str, Field(max_length=_MAX_OWNER_KEY)]
    datagen_raw: Annotated[float, Field(ge=0, le=1)]
    datagen_gate: Annotated[float, Field(ge=0, le=1)]
    column_weight: float
    columns: Annotated[
        list[DuelingColumnAggregate],
        Field(min_length=DUELING_SLICE_COLUMNS, max_length=DUELING_SLICE_COLUMNS),
    ]
    composite_stderr: Annotated[float, Field(ge=0)]

    @model_validator(mode="after")
    def _contract(self) -> Self:
        if self.column_weight != DUELING_COLUMN_WEIGHT:
            raise ValueError(
                f"column_weight must be the contract weight {DUELING_COLUMN_WEIGHT}"
            )
        ids = sorted(c.column_id for c in self.columns)
        if ids != [f"c{i}" for i in range(DUELING_SLICE_COLUMNS)]:
            raise ValueError(
                "dueling columns must be the unique ids c0..c"
                f"{DUELING_SLICE_COLUMNS - 1} (challenge.CheckDuelingColumns)"
            )
        return self

    def composite(self, mask: Set[str]) -> float:
        """The composite with every column owned by an owner in ``mask`` dropped."""
        return dueling_composite(
            self.datagen_raw, self.datagen_gate, self.columns, mask
        )

    def self_composite(self) -> float:
        """The reported composite: the agent's own owner masked."""
        return self.composite(frozenset({self.owner_key}))


class DuelingColumnPin(BaseModel):
    """One column of a season's frozen generator set.

    The pin is the exact submission and tree digest, never the hotkey. The
    public default generator is owned by nobody (empty ``owner_key``); every
    other column names its owner so self- and pair-masks can drop it.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    column_id: Annotated[str, Field(pattern=_COLUMN_ID_PATTERN)]
    submission_id: Annotated[str, Field(pattern=_IDENT_PATTERN)]
    miner_hotkey: Annotated[str, Field(pattern=_HOTKEY_PATTERN)]
    owner_key: Annotated[str, Field(max_length=_MAX_OWNER_KEY)]
    tree_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    source_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)] | None = None
    grading_bench_version: Annotated[int, Field(ge=_MIN_GRADING_BENCH_VERSION)]
    public_default: bool

    @model_validator(mode="after")
    def _ownership(self) -> Self:
        if self.public_default and self.owner_key:
            raise ValueError("the public default generator has no owner_key")
        if not self.public_default and not self.owner_key:
            raise ValueError("a miner generator column must name its owner_key")
        return self
