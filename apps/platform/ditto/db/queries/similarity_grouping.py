"""When two submissions are near-identical enough to share one queue budget.

The whole decision, as pure functions over sketches. No session, no clock, no
policy lookup: give it two fingerprints and a threshold and it tells you whether
the queue should treat them as one submission, and *why* -- so the rule can be
unit-tested against real production sketches and an operator can be shown the
numbers behind a refusal rather than a verdict.

This is a fairness rule, not a copy rule. See
:mod:`ditto.db.queries.similarity_budget` for why that distinction is
load-bearing; the short version is that being grouped costs a submission
nothing but simultaneity, so the rule may act on evidence far weaker than
anything that would justify holding a submission for review.

The rule
========

Two submissions share a budget when **all** the guards pass and **either**
channel fires:

guard: comparable evidence
    Both sketches exist, are non-empty, carry the same sketch version and the
    same reference-corpus id. The estimator enforces the last two itself (a
    cross-format Jaccard is meaningless), so a cross-generation pair simply
    scores zero and is never grouped.

guard: enough residual to judge
    Both cardinalities are at least :data:`MIN_COMPARABLE_SHINGLES`. Below that
    the residual is a couple of edited regions and two miners can coincide on it
    by pasting the same community fix.

guard: comparable scale
    ``min(card) / max(card) >= size_ratio_floor``. This exists for the
    containment channel specifically: containment is ``|A n B| / min(|A|,|B|)``,
    so a tiny residual that happens to sit inside a large one scores 1.0 while
    being nothing like it. Requiring the two residuals to be within a factor of
    two of each other removes that whole failure mode -- and it removes it for
    the right reason, since two submissions of wildly different size are not
    "the same submission twice" no matter how the smaller one embeds.

channel: Jaccard >= ``jaccard_threshold``
    ``|A n B| / |A u B|``. The symmetric measure, and the primary one: two
    crates that differ in one file out of thirty-two land here.

channel: containment >= ``containment_threshold``
    ``|A n B| / min(|A|, |B|)``. Catches the same submission re-shipped with
    extra files bolted on, which dilutes Jaccard. Only reachable because the
    scale guard above already forbids its degenerate case.

Where the defaults come from
============================

Measured, not guessed, against every production submission carrying a usable
sketch (675 of 745) using this exact estimator.

The family the gate was built for -- 53 submissions, 25 hotkeys, 6 coldkeys --
and 14 independent miners identified by submission lineage were compared
pairwise. Submissions the anti-copy gate had *already* flagged as duplicates
were excluded from the honest set, since grouping a known duplicate is the rule
working, not a false positive.

    independent miners, 13,464 cross-miner pairs
        Jaccard    p50 0.000   p90 0.277   p99 0.465   p99.9 0.625   max 0.891
        containment p50 0.000  p90 0.482   p99 0.720                 max 0.941

    the target family, cross-coldkey pairs among live evaluations
        Jaccard    min 0.559   p10 0.652   p50 0.719                 max 1.000
        containment min 0.793  p10 0.858   p50 0.961                 max 1.000

Those two distributions barely touch, which is the reference subtraction
earning its keep: the miners in the first table demonstrably share whole
byte-identical starter-kit files, and still score a median of zero, because the
scaffold is not in the sketch.

:data:`DEFAULT_JACCARD_THRESHOLD` = 0.90 and
:data:`DEFAULT_CONTAINMENT_THRESHOLD` = 0.95 sit in the gap. Replayed against
the live queue at that operating point, 76 concurrent evaluations resolve to 37
budgets: the target family's 25 submissions across 15 hotkeys and 6 coldkeys
collapse to one, and **zero** of the 13,464 independent-miner pairs are grouped
(the nearest miss is 0.891 Jaccard / 0.941 containment, on both sides of the
line).

The neighbouring operating points are why these are the defaults rather than a
range. Loosening to 0.85/0.95 admits one independent-miner pair and merges no
additional family submissions -- strictly worse. Tightening to 0.95/0.99
fragments the family from one budget of 25 into a largest budget of 11, handing
most of the queue back to it. The chosen point is the widest one that is still
clean.

Single linkage, and why the gate does not use it
================================================

:func:`similarity_budgets` groups by single linkage (A~B and B~C puts all three
together) because that is the honest way to *display* a family: a lineage of
daily edits is a chain, not a clique, and reporting it as five overlapping pairs
would tell an operator nothing.

The gate does not use it. :func:`similar_submissions` compares the candidate
directly against each live lease and never chains, because chaining on the
enforcement path would let two bridge submissions drag an unrelated third into
somebody else's budget. Grouping is for the operator's eyes; the refusal is
always a direct pairwise fact about the row being refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from uuid import UUID

    from ditto.api_models.queue_policy_settings import SimilarityBudgetSettings
    from ditto.db.queries.similarity_budget import SubmissionSketch

# See "Where the defaults come from" above. Both are operator-tunable; these are
# the shipped values and the ones the bounds below are centred on.
DEFAULT_JACCARD_THRESHOLD = 0.90
DEFAULT_CONTAINMENT_THRESHOLD = 0.95

# Neither threshold may be set below this. At 0.50 a rule stops describing
# "near-identical" and starts describing "same genre", and the measured
# independent-miner p99.9 is already 0.625 -- so a floor beneath that would let
# an operator configure the false grouping this module exists to avoid.
MIN_SIMILARITY_THRESHOLD = 0.70
MAX_SIMILARITY_THRESHOLD = 1.0

# Residuals smaller than this are not evidence. Mirrors the eight-shingle floor
# `ditto.api_server.fingerprint` applies when it empties a sketch, with headroom:
# a shingle spans four normalized lines, so 64 is roughly sixteen edited regions.
MIN_COMPARABLE_SHINGLES = 64

# The containment channel's scale guard. 0.5 means "within a factor of two".
DEFAULT_SIZE_RATIO_FLOOR = 0.5
MIN_SIZE_RATIO_FLOOR = 0.1


@dataclass(frozen=True)
class SimilarityBudgetPolicy:
    """The tunable half of the rule, as a plain value object.

    Separate from the wire model deliberately: this is what the gate holds, and
    it must stay constructible without pydantic so the pure rule can be tested
    with nothing but two dicts.
    """

    jaccard_threshold: float = DEFAULT_JACCARD_THRESHOLD
    containment_threshold: float = DEFAULT_CONTAINMENT_THRESHOLD
    size_ratio_floor: float = DEFAULT_SIZE_RATIO_FLOOR
    min_shingles: int = MIN_COMPARABLE_SHINGLES

    def __post_init__(self) -> None:
        for name in ("jaccard_threshold", "containment_threshold"):
            value = getattr(self, name)
            if not MIN_SIMILARITY_THRESHOLD <= value <= MAX_SIMILARITY_THRESHOLD:
                raise ValueError(
                    f"{name} must be within "
                    f"[{MIN_SIMILARITY_THRESHOLD}, {MAX_SIMILARITY_THRESHOLD}], "
                    f"got {value}: below the floor the rule stops describing "
                    "near-identical submissions and starts grouping independent "
                    "miners, whose measured 99.9th-percentile similarity is 0.625"
                )
        if not MIN_SIZE_RATIO_FLOOR <= self.size_ratio_floor <= 1.0:
            raise ValueError(
                "size_ratio_floor must be within "
                f"[{MIN_SIZE_RATIO_FLOOR}, 1.0], got {self.size_ratio_floor}"
            )
        if self.min_shingles < 1:
            raise ValueError(f"min_shingles must be positive, got {self.min_shingles}")


def policy_from_settings(
    settings: SimilarityBudgetSettings,
) -> SimilarityBudgetPolicy | None:
    """The operator board, as the value object the gate holds.

    Returns ``None`` when the board is disabled, because that is the one signal
    the whole path already understands: a ``None`` policy means no sketch is
    read and the allocator behaves exactly as it did before the rail existed.
    Expressing "off" as an absent policy rather than an unreachable threshold
    keeps the kill switch total rather than merely conservative.

    The board's field-level bounds and this module's constructor bounds are the
    same numbers by construction (``test_queue_policy_bounds_match_queue_
    constants`` asserts it), so a value that validated on the wire cannot be
    refused here.
    """
    if not settings.enabled:
        return None
    return SimilarityBudgetPolicy(
        jaccard_threshold=settings.jaccard_threshold,
        containment_threshold=settings.containment_threshold,
    )


@dataclass(frozen=True)
class SimilarityMatch:
    """One pairwise finding, carrying the numbers that produced it.

    The gate turns this into the reason string an operator reads, so it holds
    the estimates and the channel rather than just a boolean: "grouped with
    <id> (jaccard 0.98)" is actionable, "grouped" is not.
    """

    agent_id: UUID
    jaccard: float
    containment: float
    channel: str
    """``"jaccard"`` or ``"containment"`` -- which channel crossed its
    threshold. When both did, the Jaccard channel is named, because it is the
    symmetric one and therefore the stronger claim."""

    def describe(self) -> str:
        """One human-readable clause naming the evidence."""
        return (
            f"{self.agent_id} (jaccard {self.jaccard:.2f}, "
            f"containment {self.containment:.2f}, via {self.channel})"
        )


def similarity_scores(
    left: SubmissionSketch, right: SubmissionSketch
) -> tuple[float, float]:
    """``(jaccard, containment)`` for two sketches, or ``(0.0, 0.0)``.

    Delegates to :func:`ditto.api_server.fingerprint.content_similarity` rather
    than restating its estimator. That matters more than it looks: the
    containment estimate is deliberately *not* derived algebraically from
    Jaccard there, because the algebraic form multiplies the sampling error by
    the set sizes exactly in the asymmetric regime -- and this rule reads
    containment precisely in that regime.

    The import is function-local because it has to be. ``ditto.api_server``'s
    package ``__init__`` builds the whole FastAPI app, which imports the
    endpoints, which reach back into ``ditto.db.queries`` -- so a module-level
    import here closes a cycle through ``queue_order``. Importing inside the
    call keeps the dependency pointing the direction it already points at
    runtime, and ``sys.modules`` makes the repeat cost a dict lookup.
    """
    from ditto.api_server.fingerprint import content_similarity

    return content_similarity(left.fingerprint, right.fingerprint)


def same_budget(
    left: SubmissionSketch,
    right: SubmissionSketch,
    *,
    policy: SimilarityBudgetPolicy | None = None,
) -> SimilarityMatch | None:
    """Whether two submissions should share one concurrency budget.

    Returns the evidence when they should and ``None`` when they should not. A
    submission is never its own near-twin: comparing a sketch with itself
    returns ``None``, so the arithmetic in the gate needs no special case.
    """
    policy = policy or SimilarityBudgetPolicy()
    if left.agent_id == right.agent_id:
        return None
    if not (left.comparable and right.comparable):
        return None
    left_card = int((left.fingerprint or {}).get("card", 0))
    right_card = int((right.fingerprint or {}).get("card", 0))
    smaller, larger = min(left_card, right_card), max(left_card, right_card)
    if smaller < policy.min_shingles:
        return None
    if larger <= 0 or smaller / larger < policy.size_ratio_floor:
        return None
    jaccard, containment = similarity_scores(left, right)
    if jaccard >= policy.jaccard_threshold:
        channel = "jaccard"
    elif containment >= policy.containment_threshold:
        channel = "containment"
    else:
        return None
    return SimilarityMatch(
        agent_id=right.agent_id,
        jaccard=jaccard,
        containment=containment,
        channel=channel,
    )


def similar_submissions(
    candidate: SubmissionSketch,
    others: Iterable[SubmissionSketch],
    *,
    policy: SimilarityBudgetPolicy | None = None,
) -> tuple[SimilarityMatch, ...]:
    """Every submission in ``others`` that shares ``candidate``'s budget.

    The enforcement primitive: strictly pairwise against the candidate, with no
    transitive closure, so a refusal is always a direct fact about the row being
    refused. Ordered strongest-first so a truncated reason names the best
    evidence.
    """
    policy = policy or SimilarityBudgetPolicy()
    matches = [
        match
        for other in others
        if (match := same_budget(candidate, other, policy=policy)) is not None
    ]
    matches.sort(
        key=lambda match: (-match.jaccard, -match.containment, str(match.agent_id))
    )
    return tuple(matches)


def similarity_budgets(
    sketches: Sequence[SubmissionSketch],
    *,
    policy: SimilarityBudgetPolicy | None = None,
) -> tuple[tuple[UUID, ...], ...]:
    """Partition ``sketches`` into single-linkage budgets, for operator display.

    Reporting only. A family iterating daily forms a *chain* of near-identical
    steps rather than a clique, so pairwise output would show an operator a
    hundred edges instead of one group. The gate deliberately does not use this
    -- see the module docstring.

    Groups come back largest-first, then by their first member, so the output is
    stable enough to diff between reads.
    """
    policy = policy or SimilarityBudgetPolicy()
    parent = {sketch.agent_id: sketch.agent_id for sketch in sketches}

    def find(node: UUID) -> UUID:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for index, left in enumerate(sketches):
        for right in sketches[index + 1 :]:
            if same_budget(left, right, policy=policy) is None:
                continue
            left_root, right_root = find(left.agent_id), find(right.agent_id)
            if left_root != right_root:
                parent[left_root] = right_root
    grouped: dict[UUID, list[UUID]] = {}
    for sketch in sketches:
        grouped.setdefault(find(sketch.agent_id), []).append(sketch.agent_id)
    return tuple(
        tuple(members)
        for members in sorted(
            grouped.values(), key=lambda members: (-len(members), str(members[0]))
        )
    )
