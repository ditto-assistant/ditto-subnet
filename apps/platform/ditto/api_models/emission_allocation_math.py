"""Exact arithmetic for the typed-weights emission allocation.

Standard library only, on purpose: the root validator tests load this file by
path and run its derivation through the real validator fold, so the golden
"derived served fields fold to the allocation" check needs no Platform import.

An allocation splits total SN118 miner emission into integer basis points that
sum to exactly ``10000``::

    burn 5000 | track memory 3500 | track generator 1500 | gamma gm 0

Validators do not read this shape. They fold three nested fields Platform has
always served (``ditto/validator/worker.py``):

* Gamma service buckets are carved first: ``service = sum(gamma) / 10000`` of
  total emission (``service_first_weights``; zero unless Gamma enforces).
* ``burn_share`` burns that fraction of the remainder ``1 - service``.
* ``track_shares_bps`` splits what is left between tracks; bps it leaves
  unclaimed burn.

So track ``i`` receives ``(1 - service) * (1 - burn_share) * T'_i / 10000``.

The canonical derivation puts every burnt basis point in ``burn_share`` and
makes the served track map claim the whole pool:

* ``burn_share = burn / (10000 - gamma)``, exact as a fraction, served as the
  correctly rounded float;
* ``T'_i = track_i * 10000 / sum(track)``, which must be an exact integer.

Keeping the track map at a full 10000 matters: the map is withheld until every
live weight setter reports protocol 33, and a withheld map folds memory at the
whole pool. With the burn carried in ``burn_share`` that fallback still burns
exactly ``burn`` bps; a shortfall riding in the map would be paid to memory.

A split whose track bps do not divide the pool into integers (for example
memory 4000 / generator 2000 of a 6000 pool: 6666.67 / 3333.33) is **refused**,
never rounded: rounding would pay one competition more than the operator typed.
The refusal names the remainder; burn 50% with any integer track split, or a
single paid track, is always representable.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

BASIS_POINTS = 10_000
KIND_BURN = "burn"
KIND_TRACK = "track"
KIND_GAMMA = "gamma"
KINDS = (KIND_BURN, KIND_TRACK, KIND_GAMMA)
KNOWN_TRACKS = ("memory", "coding", "router", "generator")
"""Track ids the validator registry knows, in its registration order."""
PAYABLE_TRACKS = ("memory", "generator")
"""Tracks a shipped validator pays (protocol 33). Others may only appear at 0."""
COMPILED_TRACK_SPLIT = {"memory": BASIS_POINTS}
"""What every validator folds while no track map is served."""
CONFIRMATION_PREFIX = "APPLY EMISSION ALLOCATION"
ROUND_TRIP_TOLERANCE_BPS = 1e-6
"""Float slack when re-reading an exact derivation through the float fold."""


class AllocationError(ValueError):
    """An allocation Platform refuses to store or derive."""


@dataclass(frozen=True)
class AllocationEntry:
    kind: str
    key: str | None
    """Track id for ``track``, bucket id for ``gamma``, ``None`` for ``burn``."""
    bps: int

    @property
    def label(self) -> str:
        if self.kind == KIND_BURN:
            return "burn"
        if self.kind == KIND_GAMMA:
            return f"gamma:{self.key}"
        return str(self.key)


@dataclass(frozen=True)
class DerivedServed:
    """The served fields an allocation folds to, plus the exact burn fraction."""

    burn_share: float
    burn_fraction: Fraction
    track_shares_bps: dict[str, int] | None
    """``None`` only when no track holds a share (the burn takes everything
    miners would get); the caller then keeps whatever map is in force, since
    ``burn_share == 1`` leaves nothing for it to split."""
    gamma_bps: dict[str, int]


def _order(entry: AllocationEntry) -> tuple[int, int, str]:
    kind = KINDS.index(entry.kind)
    if entry.kind == KIND_TRACK:
        return kind, KNOWN_TRACKS.index(str(entry.key)), ""
    return kind, 0, entry.key or ""


def canonical_entries(
    entries: Iterable[AllocationEntry],
    *,
    gamma_buckets: Sequence[str] | None = None,
) -> tuple[AllocationEntry, ...]:
    """Validate and order entries: burn, tracks in registry order, Gamma by id.

    ``gamma_buckets`` is the bucket set of the treasury policy in force. When
    given, every bucket must be named exactly once (zero allowed) and no other
    bucket may appear; ``None`` checks only the shape.
    """
    seen: set[tuple[str, str | None]] = set()
    ordered: list[AllocationEntry] = []
    for entry in entries:
        if entry.kind not in KINDS:
            raise AllocationError(f"unknown kind {entry.kind!r}; known: {list(KINDS)}")
        if type(entry.bps) is not int or not 0 <= entry.bps <= BASIS_POINTS:
            raise AllocationError(f"{entry.label} bps must be an integer in 0..10000")
        if entry.kind == KIND_BURN and entry.key is not None:
            raise AllocationError("the burn entry takes no id or bucket")
        if entry.kind == KIND_TRACK and entry.key not in KNOWN_TRACKS:
            raise AllocationError(
                f"unknown track {entry.key!r}; known: {list(KNOWN_TRACKS)}"
            )
        if entry.kind == KIND_GAMMA and not entry.key:
            raise AllocationError("a gamma entry must name its service bucket")
        identity = (entry.kind, entry.key)
        if identity in seen:
            raise AllocationError(f"duplicate entry {entry.label}")
        seen.add(identity)
        if (
            entry.kind == KIND_TRACK
            and entry.bps > 0
            and entry.key not in PAYABLE_TRACKS
        ):
            raise AllocationError(
                f"no shipped validator pays track {entry.key}; its share would "
                f"burn. Only {list(PAYABLE_TRACKS)} may hold a nonzero share"
            )
        ordered.append(entry)
    if (KIND_BURN, None) not in seen:
        raise AllocationError("name the burn entry, at 0 if nothing burns")
    missing = [t for t in PAYABLE_TRACKS if (KIND_TRACK, t) not in seen]
    if missing:
        raise AllocationError(
            f"name every paid track, at 0 if it takes nothing; missing {missing}"
        )
    if gamma_buckets is not None:
        named = {key for kind, key in seen if kind == KIND_GAMMA}
        unknown = sorted(str(key) for key in named - set(gamma_buckets))
        if unknown:
            raise AllocationError(
                f"unknown Gamma bucket {unknown}; the treasury policy has "
                f"{sorted(gamma_buckets)}"
            )
        absent = sorted(set(gamma_buckets) - named)
        if absent:
            raise AllocationError(
                f"name every Gamma bucket, at 0 if it takes nothing; missing {absent}"
            )
    total = sum(entry.bps for entry in ordered)
    if total != BASIS_POINTS:
        raise AllocationError(
            f"entries sum to {total} bps; an allocation accounts for exactly "
            f"{BASIS_POINTS}"
        )
    return tuple(sorted(ordered, key=_order))


def confirmation_phrase(entries: Iterable[AllocationEntry]) -> str:
    """The exact phrase a write must carry: every nonzero entry, in order.

    ``APPLY EMISSION ALLOCATION burn=5000 memory=3500 generator=1500``; a Gamma
    bucket reads ``gamma:gm=500``. Pass canonical entries.
    """
    parts = [f"{entry.label}={entry.bps}" for entry in entries if entry.bps > 0]
    return " ".join([CONFIRMATION_PREFIX, *parts])


def derive_served(entries: Sequence[AllocationEntry]) -> DerivedServed:
    """The burn share, track map and Gamma buckets validators must be served.

    Refuses (``AllocationError``) a split it cannot express exactly, then
    re-reads the result through the float fold and refuses any drift above
    :data:`ROUND_TRIP_TOLERANCE_BPS`.
    """
    entries = canonical_entries(entries)
    burn = sum(e.bps for e in entries if e.kind == KIND_BURN)
    gamma = {str(e.key): e.bps for e in entries if e.kind == KIND_GAMMA}
    tracks = {
        str(e.key): e.bps
        for e in entries
        if e.kind == KIND_TRACK and e.key in PAYABLE_TRACKS
    }
    service = sum(gamma.values())
    remainder = BASIS_POINTS - service
    if remainder <= 0:
        raise AllocationError("Gamma cannot take the whole emission")
    burn_fraction = Fraction(burn, remainder)
    pool = sum(tracks.values())
    shares: dict[str, int] | None = None
    if pool > 0:
        shares = {}
        for track in PAYABLE_TRACKS:
            numerator = tracks[track] * BASIS_POINTS
            if numerator % pool:
                raise AllocationError(
                    f"{track}={tracks[track]} of a {pool}-bps track pool is "
                    f"{Fraction(numerator, pool)} bps of the served track map, "
                    "not an integer; validators fold integer bps, so this split "
                    "is not exactly representable. Move a few bps between the "
                    "tracks and burn so each track's bps x 10000 divides by the "
                    "track total (burn 5000 makes any split exact)"
                )
            served = numerator // pool
            if track == "memory" or served > 0:
                shares[track] = served
    derived = DerivedServed(
        burn_share=float(burn_fraction),
        burn_fraction=burn_fraction,
        track_shares_bps=shares,
        gamma_bps=gamma,
    )
    if shares is not None:
        folded = effective_split(
            burn_share=derived.burn_share,
            track_shares_bps=shares,
            gamma_bps=gamma,
        )
        expected = {e.label: float(e.bps) for e in entries if e.kind != KIND_TRACK}
        expected.update({track: float(bps) for track, bps in tracks.items()})
        for label, bps in expected.items():
            if abs(folded.get(label, 0.0) - bps) > ROUND_TRIP_TOLERANCE_BPS:
                raise AllocationError(
                    f"derived fields fold {label} to {folded.get(label, 0.0)} bps, "
                    f"not {bps}; refusing to drift"
                )
    return derived


def effective_split(
    *,
    burn_share: float,
    track_shares_bps: Mapping[str, int] | None,
    gamma_bps: Mapping[str, int],
) -> dict[str, float]:
    """Basis points of total emission each destination receives, as validators fold.

    Mirrors ``service_first_weights`` and ``apply_miner_emission_cap`` in float
    arithmetic: Gamma first, ``burn_share`` of the remainder, then the track map
    with any unclaimed bps burning. ``track_shares_bps=None`` is the compiled
    memory-only split. Keys are ``burn``, track ids and ``gamma:<bucket>``.
    Assumes every paid track has a payable miner.
    """
    if not math.isfinite(burn_share) or not 0.0 <= burn_share <= 1.0:
        raise AllocationError("burn_share must be a finite fraction in [0, 1]")
    service = sum(gamma_bps.values()) / BASIS_POINTS
    miner = (1.0 - service) * (1.0 - burn_share)
    shares = dict(track_shares_bps or COMPILED_TRACK_SPLIT)
    result: dict[str, float] = {
        f"gamma:{bucket}": bps for bucket, bps in sorted(gamma_bps.items())
    }
    paid = 0.0
    for track in KNOWN_TRACKS:
        bps = shares.get(track, 0)
        if track in PAYABLE_TRACKS:
            value = miner * bps
            result[track] = value
            paid += value
    result["burn"] = BASIS_POINTS * (1.0 - service) - paid
    return result


def entries_from_split(
    split: Mapping[str, float],
) -> tuple[AllocationEntry, ...] | None:
    """The integer allocation a folded split corresponds to, or ``None``.

    ``None`` when any destination is not within
    :data:`ROUND_TRIP_TOLERANCE_BPS` of an integer, or the integers do not
    account for exactly 10000.
    """
    entries: list[AllocationEntry] = []
    for label, value in split.items():
        rounded = round(value)
        if abs(value - rounded) > ROUND_TRIP_TOLERANCE_BPS or rounded < 0:
            return None
        if label == "burn":
            entries.append(AllocationEntry(KIND_BURN, None, rounded))
        elif label.startswith("gamma:"):
            entries.append(AllocationEntry(KIND_GAMMA, label[6:], rounded))
        else:
            entries.append(AllocationEntry(KIND_TRACK, label, rounded))
    try:
        return canonical_entries(entries)
    except AllocationError:
        return None
