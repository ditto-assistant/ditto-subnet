"""Common Random Numbers (CRN) seed derivation — platform mirror.

The **authoritative** implementation lives in ``ditto-subnet`` at
``ditto/validator/crn.py``; validators derive the seeds they benchmark on from
it. This module is a **byte-for-byte mirror** so the platform can independently
re-derive the champion-anchored seed set and *validate* a submitted confirmation
seed against it (anti-grind: a validator cannot cherry-pick a favourable seed).
Keep it exactly aligned with the subnet — the digest encoding is consensus.

    crn_seed(agent_ids, version, k) = sha256(
        sorted(agent_ids) each ‖ b"\\x00", then str(version) ascii,
        then (if k>0) b"\\x00k" ‖ str(k) ascii
    ) → first 8 bytes little-endian unsigned & (2**63 - 1)

The int63 masking mirrors dittobench-api's ``gen.FreshSeed`` (``int64(uint64 >>
1)``) so the value round-trips through the wire unchanged.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from uuid import UUID

from ditto.api_server.koth import (
    KOTH_DETHRONE_Z,
    KOTH_MARGIN,
    TOP5_MAX_CONFIRMATION_SEEDS,
    TOP5_MIN_CONFIRMATION_SEEDS,
)

# Mask to a non-negative signed-63-bit integer, matching dittobench-api's
# FreshSeed (``int64(uint64 >> 1)``): JSON-clean and never negative.
_INT63_MASK = (1 << 63) - 1


def crn_seed(agent_ids: Iterable[str], *, version: int, k: int = 0) -> int:
    """Deterministic dataset seed for a CRN comparison over ``agent_ids`` at
    ``version``. Order-independent (the *set* of compared agents determines the
    seed) and pure, so every validator computes the same value.

    ``k`` indexes a confirmation replicate: a dethrone-grade comparison runs the
    compared agents on ``K`` common seeds (k = 0..K-1). ``k=0`` is byte-identical
    to the original single-seed derivation.

    Byte-for-byte aligned with ``ditto-subnet/ditto/validator/crn.py::crn_seed``.
    """
    h = hashlib.sha256()
    for aid in sorted(agent_ids):
        h.update(aid.encode("utf-8"))
        h.update(b"\x00")
    h.update(str(int(version)).encode("ascii"))
    if k > 0:
        h.update(b"\x00k")
        h.update(str(int(k)).encode("ascii"))
    return int.from_bytes(h.digest()[:8], "little", signed=False) & _INT63_MASK


def confirmation_seeds(
    agent_ids: Iterable[str], *, version: int, count: int
) -> list[int]:
    """The ``count`` common confirmation seeds for one comparison, k = 0..count-1.

    ``count <= 1`` degrades to the single classic CRN seed. Byte-for-byte aligned
    with ``ditto-subnet/ditto/validator/crn.py::confirmation_seeds``.
    """
    ids = list(agent_ids)
    n = max(1, int(count))
    return [crn_seed(ids, version=version, k=k) for k in range(n)]


def champion_anchored_seeds(
    champion_agent_id: UUID, *, version: int, max_seeds: int
) -> list[int]:
    """The champion-anchored CRN seed set for the top-5 shared-seed rescore lane.

    The seed set keys only on the *champion's* agent id (LOCKED, cf.
    ``docs/top5-rescore-lane.md`` §2), so the shared baseline stays stable as the
    tail churns and moves only when the champion is dethroned. ``version`` is the
    **major** benchmark version, so successive versions get distinct seeds. This
    is exactly what the subnet's ``top5_confirmation_set`` derives via
    ``confirmation_seeds([str(champion_id)], version=current_version, count)`` —
    the platform mirrors it to bound the anti-grind check to the first
    ``max_seeds`` (``TOP5_MAX_CONFIRMATION_SEEDS``) replicate indices.
    """
    return confirmation_seeds(
        [str(champion_agent_id)], version=version, count=max_seeds
    )


def elastic_confirmation_seed_ceiling(
    composites_by_agent: Mapping[UUID, Mapping[int, float]],
    *,
    minimum: int = TOP5_MIN_CONFIRMATION_SEEDS,
    maximum: int = TOP5_MAX_CONFIRMATION_SEEDS,
    target_half_width: float = KOTH_MARGIN,
    z: float = KOTH_DETHRONE_Z,
) -> int:
    """Variance-sized shared-seed target, clamped to a finite work budget.

    Use the noisiest emission member's sample variance so one quiet agent cannot
    prematurely stop evidence collection for a noisy sibling. The unconstrained
    target solves ``z * sample_sd / sqrt(n) <= target_half_width``. With fewer
    than two observations there is no variance estimate, so the lane gathers the
    minimum first. This is scheduling policy only; accepted rows stay append-only
    and the fold keeps using them after the target later shrinks.
    """
    floor = max(2, int(minimum))
    cap = max(floor, int(maximum))
    if target_half_width <= 0.0 or z <= 0.0:
        return cap
    max_variance = 0.0
    measured = False
    for composites in composites_by_agent.values():
        values = [
            float(value)
            for value in composites.values()
            if not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and 0.0 <= value <= 1.0
        ]
        if len(values) < 2:
            continue
        measured = True
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
        max_variance = max(max_variance, variance)
    if not measured:
        return floor
    required = math.ceil(max_variance * (z / target_half_width) ** 2)
    return min(cap, max(floor, required))


def bounded_continual_seed_set(
    champion_agent_id: UUID,
    *,
    version: int,
    composites_by_agent: Mapping[UUID, Mapping[int, float]],
) -> tuple[int, ...]:
    """The elastic cohort target: most-shared durable seeds, then fresh CRNs.

    Existing evidence is ordered by cohort coverage first so a churned member
    catches up to the seeds that buy the largest paired intersection. If the
    durable universe is shallower than the elastic target, deterministic seeds
    from the current champion fill the remainder. Once the target is full no new
    seed is introduced, which is the actual continual-work cap.
    """
    ceiling = elastic_confirmation_seed_ceiling(composites_by_agent)
    anchor = champion_anchored_seeds(
        champion_agent_id, version=version, max_seeds=ceiling
    )
    anchor_order = {seed: index for index, seed in enumerate(anchor)}
    coverage: dict[int, int] = {}
    for composites in composites_by_agent.values():
        for seed in composites:
            coverage[int(seed)] = coverage.get(int(seed), 0) + 1
    targets = sorted(
        coverage,
        key=lambda seed: (-coverage[seed], anchor_order.get(seed, ceiling), seed),
    )[:ceiling]
    if len(targets) < ceiling:
        selected = set(targets)
        for seed in anchor:
            if seed in selected:
                continue
            targets.append(seed)
            selected.add(seed)
            if len(targets) == ceiling:
                break
    return tuple(targets)


def active_confirmation_seed_set(
    seeds_by_agent: Mapping[UUID, Iterable[int]],
    *,
    max_seeds: int = TOP5_MAX_CONFIRMATION_SEEDS,
) -> tuple[int, ...]:
    """The bounded recorded evidence window used by the current fold.

    Accepted rows are immutable audit history, but a historical, larger work
    budget must not keep buying a deeper official mean after the compiled cap
    shrinks. Prefer the seeds held by the most agents so the bounded window
    retains as much paired evidence as possible; seed order is the deterministic
    tie-break shared by Platform and every validator ledger projection.
    """
    coverage: dict[int, int] = {}
    for seeds in seeds_by_agent.values():
        for seed in set(seeds):
            coverage[int(seed)] = coverage.get(int(seed), 0) + 1
    return tuple(
        sorted(coverage, key=lambda seed: (-coverage[seed], seed))[
            : max(0, int(max_seeds))
        ]
    )


def fold_seed_bound(
    *,
    champion_agent_id: UUID,
    anchor_version: int,
    seeds_by_agent: Mapping[UUID, Iterable[int]],
    max_seeds: int = TOP5_MAX_CONFIRMATION_SEEDS,
) -> tuple[int, ...]:
    """The seeds the fold may consider under the current compiled work cap.

    Cross-reign seeds remain valid paired evidence, but only the most widely
    shared ``max_seeds`` recorded datasets are active. This makes a reduced
    current cap authoritative over a larger historical policy without deleting
    its audit trail. The live anchor fills unused slots so a fresh reign can
    still introduce unpredictable seeds until the active window is full.
    """
    active = list(active_confirmation_seed_set(seeds_by_agent, max_seeds=max_seeds))
    selected = set(active)
    anchor = champion_anchored_seeds(
        champion_agent_id, version=anchor_version, max_seeds=max_seeds
    )
    for seed in anchor:
        if seed not in selected and len(active) < max_seeds:
            active.append(seed)
            selected.add(seed)
    return tuple(active)
