"""The challenger queue: which never-scored generator gets a challenger column.

Challenger columns measure qualified generators that have never been scored.
Ordered by qualification time alone, one miner's batch of near-identical
builds takes one challenger column per round until every build has run,
ahead of every generator that qualified after the batch's first build. On
2026-11-11 one coldkey qualified ten kinbind/kinlist builds in three hours
(SN-443, SN-474).

The queue instead interleaves template families. A family's next unmeasured
build competes with every other family's next build: its position is how
many of its members are already measured (scored, or holding a column in a
round still in flight) plus its members placed ahead of it in the queue.

Within a position, the more novel generator goes first, then the earlier
qualification. Novelty is the share of a generator's probe cases (its case
fingerprints on the probe seed every generator of the epoch shares) that no
measured generator and no generator ahead of it in the queue emitted. The
probe copy check already rejects a generator reproducing half or more of one
other owner's probe cases; novelty also ranks a partial copy, or a mosaic of
several generators' cases, behind a generator whose cases are new. It is
computed on emitted cases, not source, so padding an archive cannot raise
it. The queue is built greedily, so a generator ahead of another counts
toward the other's family position and novelty.

A family is the generators under common control (one owner key), joined
with every generator built from the same template under another owner: the
same Go module with the same file count and an archive size within
:data:`TEMPLATE_SIZE_TOLERANCE`. The module alone is not a family: unrelated
miners keep the starter kit's module path. Exact duplicates are already
refused at upload by tree digest, and the probe copy check rejects
behavioral copies. This grouping only orders the queue; it never rejects or
drops a generator.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from ditto.db.models import GeneratorSubmission

# Two archives of one module and file count whose sizes differ by at most this
# share of the larger are builds of one template. Builds of one template that
# differ in a seed, a salt or a constant differ by a few bytes.
TEMPLATE_SIZE_TOLERANCE = 0.01


def _qualified_key(row: GeneratorSubmission) -> tuple[datetime, datetime, str]:
    return (
        row.qualified_at or row.submitted_at,
        row.submitted_at,
        str(row.submission_id),
    )


def same_template(a: GeneratorSubmission, b: GeneratorSubmission) -> bool:
    """Whether two archives are builds of one template (see module docs)."""
    if a.module != b.module or a.file_count != b.file_count:
        return False
    larger = max(a.size_bytes, b.size_bytes)
    return abs(a.size_bytes - b.size_bytes) <= TEMPLATE_SIZE_TOLERANCE * larger


def template_families(rows: Iterable[GeneratorSubmission]) -> dict[UUID, str]:
    """Each generator's family: the submission id of its earliest member.

    Joins rows under one owner key and rows that are builds of one template
    (:func:`same_template`). Deterministic for a given set of rows.
    """
    ordered = sorted(rows, key=_qualified_key)
    parent = list(range(len(ordered)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a: int, b: int) -> None:
        # The earlier member stays the root, so the family key is its
        # earliest-qualified member whatever order the rows are joined in.
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    first_by_owner: dict[str, int] = {}
    # Builds of one template share a module and a file count, so only rows in
    # one such bucket are compared, in size order: a row joins the bucket's
    # previous row when its size is within the tolerance, which chains a run
    # of near-equal sizes into one family without comparing every pair.
    buckets: dict[tuple[str, int], list[int]] = {}
    for index, row in enumerate(ordered):
        owner = first_by_owner.setdefault(row.owner_key, index)
        union(owner, index)
        buckets.setdefault((row.module, row.file_count), []).append(index)
    for members in buckets.values():
        by_size = sorted(members, key=lambda index: ordered[index].size_bytes)
        for previous, current in zip(by_size, by_size[1:], strict=False):
            if same_template(ordered[previous], ordered[current]):
                union(previous, current)
    return {
        row.submission_id: str(ordered[find(index)].submission_id)
        for index, row in enumerate(ordered)
    }


@dataclass(frozen=True)
class QueueEntry:
    """One never-scored generator's place in the challenger queue."""

    row: GeneratorSubmission
    family: str
    """The submission id of the family's earliest-qualified member."""
    family_size: int
    family_measured: int
    """Members scored or holding a column in a round still in flight."""
    family_position: int
    """Measured members plus this family's members ahead of it in the queue."""
    in_flight_round: int | None
    """The unfinished round this generator holds a column in, if any."""
    novelty: float | None = None
    """Share of its probe cases new to every measured generator and every
    generator ahead of it; ``None`` while it is in flight."""


def probe_novelty(fingerprints: Sequence[str] | None, known: set[str]) -> float:
    """Share of ``fingerprints`` not in ``known``; 1.0 without a probe result."""
    if not fingerprints:
        return 1.0
    unique = set(fingerprints)
    return len(unique - known) / len(unique)


def challenger_queue(
    qualified: Iterable[GeneratorSubmission],
    in_flight: Mapping[UUID, int],
    probes: Mapping[UUID, Sequence[str]] | None = None,
) -> list[QueueEntry]:
    """Every never-scored generator in ``qualified``, in challenger pick order.

    ``qualified`` is every qualified miner generator, scored ones included
    (they count toward their family's measured members and their probe cases
    are known). ``in_flight`` maps a generator holding a column in an
    unfinished round to that round. Those generators are already being
    measured: they come last, unpickable, and count as measured. ``probes``
    maps a generator to its current-epoch probe fingerprints.
    """
    rows = list(qualified)
    probes = probes or {}
    families = template_families(rows)
    size: dict[str, int] = {}
    measured: dict[str, int] = {}
    known: set[str] = set()
    for row in rows:
        family = families[row.submission_id]
        size[family] = size.get(family, 0) + 1
        if row.round_score is not None or row.submission_id in in_flight:
            measured[family] = measured.get(family, 0) + 1
            known.update(probes.get(row.submission_id) or ())
    placed = dict(measured)
    remaining = sorted(
        (
            row
            for row in rows
            if row.round_score is None and row.submission_id not in in_flight
        ),
        key=_qualified_key,
    )
    entries: list[QueueEntry] = []
    while remaining:
        # Greedy: the next pick depends on every pick before it, through its
        # family's count and the probe cases already known.
        def rank(row: GeneratorSubmission) -> tuple[int, float, tuple]:
            novelty = probe_novelty(probes.get(row.submission_id), known)
            return (
                placed.get(families[row.submission_id], 0),
                -novelty,
                _qualified_key(row),
            )

        row = min(remaining, key=rank)
        remaining.remove(row)
        family = families[row.submission_id]
        entries.append(
            QueueEntry(
                row=row,
                family=family,
                family_size=size[family],
                family_measured=measured.get(family, 0),
                family_position=placed.get(family, 0),
                in_flight_round=None,
                novelty=probe_novelty(probes.get(row.submission_id), known),
            )
        )
        placed[family] = placed.get(family, 0) + 1
        known.update(probes.get(row.submission_id) or ())
    for row in sorted(rows, key=_qualified_key):
        held = in_flight.get(row.submission_id)
        if row.round_score is None and held is not None:
            family = families[row.submission_id]
            entries.append(
                QueueEntry(
                    row=row,
                    family=family,
                    family_size=size[family],
                    family_measured=measured.get(family, 0),
                    family_position=measured.get(family, 0),
                    in_flight_round=held,
                )
            )
    return entries
