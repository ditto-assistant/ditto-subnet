"""Mutations + reads against the ``scores`` table.

A score is upserted per ``(agent_id, validator_hotkey)``: a validator
re-scoring an agent overwrites its prior row rather than appending. The
upsert is a read-then-write inside the caller's transaction (portable
across the Postgres runtime and the SQLite unit-test fallback) rather than
a dialect-specific ``ON CONFLICT``. Under the k=3 quorum several validators
score the same agent, so the ``(agent_id, validator_hotkey)`` PK is what holds
each validator to one row; finalization takes the median across the rows
(:func:`list_eligible_ledger`).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    and_,
    case,
    func,
    literal,
    null,
    or_,
    select,
    union,
)
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.orm.util import AliasedClass

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.confirmation_bundles import (
    ConfirmationBundleMode,
    ConfirmationBundleSettings,
)
from ditto.db.errors import IntegrityError as DbIntegrityError
from ditto.db.models import (
    Agent,
    BenchmarkRolloutMember,
    ConfirmationBundle,
    ConfirmationBundleSettingsRevision,
    ConfirmationBundleSubject,
    ConfirmationBundleTicket,
    ConfirmationScore,
    EvaluationPayment,
    OwnerAttestation,
    Score,
)
from ditto.db.queries.agents import get_agent_by_id
from ditto.db.queries.inference import LeaseModelUsage
from ditto.score_order import score_order_key, score_order_terms

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


# A run must administer the *full* benchmark to be ranked on the leaderboard and
# to earn emissions. The dittobench-api run-size profiles are small = 6 tool + 6
# memory = 12 cases, medium ~= 42, full = 60 tool + 50 memory + 4 isolation ~=
# 114 (dittobench-api internal/gen/gen.go Profiles). A smaller profile omits the
# hard anti-overfit memory categories (injection-resistance, aggregation-count,
# assistant-recall) entirely — its 6-case memory suite is trivially aced (a
# "100% memory" is a small-sample artifact), so the composite is neither
# comparable across miners nor discriminative. This floor cleanly separates full
# (~114) from the smoke/practice profiles (small 12, medium ~42); runs below it
# are surfaced as "provisional" (eligible=False) but never ranked or folded into
# weights. Keep in sync with the validator's MIN_ELIGIBLE_CASES
# (ditto-subnet ditto/validator/weights.py).
MIN_ELIGIBLE_CASES = 100

# The validator pool size: a submission is scored by this many independent
# validators (the k=3 model), and its canonical score is the MEDIAN of their
# composites, so a single generous or harsh validator cannot move it. The
# platform issues at most this many tickets per agent (``validator_tickets``)
# and finalizes an agent (``evaluating -> scored``) once it has this many
# scores. Keep in sync with the ticket-issue cap and the validator quorum.
SCORING_QUORUM = 3


def _is_ranked() -> ColumnElement[bool]:
    """SQL predicate for a *ranked* run: it administered the full benchmark AND
    scored a positive composite. Both gates mirror the validator's weight fold:
    ``filter_eligible`` drops sub-floor runs and ``compute_weights`` then drops
    ``composite <= 0`` (a failed/zero run earns nothing). Without the second gate
    a full run that scored 0.000 is crowned #1 over a higher provisional run,
    which is exactly what the fold refuses to pay. Keep the two in lockstep.
    """
    return and_(Score.n >= MIN_ELIGIBLE_CASES, Score.composite > 0.0)


def _crown_band(
    top_score: ColumnElement[Any], bench_version: ColumnElement[Any]
) -> ColumnElement[Any]:
    """How close an ancestor must be to count as the same score, in SQL.

    This is the *flat* half of the validator's indifference band — the fixed
    ``KOTH_MARGIN``, scaled by the same versioned high-score decay
    (:func:`ditto.api_server.koth._dethrone_band_scale`) — and deliberately not
    the statistical half. The dethrone requirement is
    ``max(margin, z * sqrt(se² + se²))``, so the flat term is its floor: taking
    only the floor admits *fewer* ancestors than the fold would call
    indistinguishable, never more. Seniority is the thing being granted here, so
    the conservative direction is the correct one — and it keeps the anchor off
    the per-row standard errors, which move under every retest and would make a
    lineage's provenance wobble with measurement noise rather than with what it
    actually achieved.
    """
    from ditto.api_server.koth import (
        KOTH_BAND_DECAY_MIN_BENCH_VERSION,
        KOTH_BAND_DECAY_RATE,
        KOTH_BAND_DECAY_START_COMPOSITE,
        KOTH_MARGIN,
    )

    bounded = func.least(func.greatest(top_score, KOTH_BAND_DECAY_START_COMPOSITE), 1.0)
    return case(
        (
            bench_version >= KOTH_BAND_DECAY_MIN_BENCH_VERSION,
            KOTH_MARGIN
            * func.exp(
                -KOTH_BAND_DECAY_RATE * (bounded - KOTH_BAND_DECAY_START_COMPOSITE)
            ),
        ),
        else_=KOTH_MARGIN,
    )


@dataclass(frozen=True)
class LedgerFamilyMember:
    """Minimal identity and score needed to render one grouped board child."""

    agent_id: UUID
    agent_name: str
    agent_version: int | None
    canonical_composite: float


@dataclass(frozen=True)
class LedgerRow:
    """One entry of the best-eligible-score-per-payment-coldkey ledger.

    The immutable value object :func:`list_eligible_ledger` returns and the
    ``GET /scoring/scores`` endpoint maps onto the ``LedgerEntry`` wire model.
    ``first_seen`` is *this agent's* upload time: the anti-copy comparison, the
    scoring gate and the public board all need the literal fact of when this
    tarball arrived. :attr:`crown_first_seen` is the lineage-level answer to the
    same question and is what the KOTH fold orders on — see
    :attr:`fold_first_seen`.
    """

    miner_hotkey: str
    agent_id: UUID
    composite: float
    tool_mean: float
    memory_mean: float
    first_seen: datetime
    sha256: str
    size_bytes: int | None
    run_id: str
    seed: int
    validator_hotkey: str
    signature: str | None
    status: AgentStatus
    miner_coldkey: str | None = None
    """Payment-time owner identity used to enforce one emission position.

    This is moderation-only metadata and is not exposed on the public scoring
    wire model. ``None`` is retained for legacy rows that predate payments.
    """
    bench_version: int = 1
    content_fingerprint: dict | None = None
    """Shingle MinHash sketch of the tarball source (see
    :mod:`ditto.api_server.fingerprint`); the gate's content-level anti-copy
    signal. ``None`` for rows uploaded before fingerprinting or with an
    unreadable tarball. Defaulted so it need not be threaded through the
    validator ledger *wire* model — it is moderation-only, never exposed."""
    structural_fingerprint: dict | None = None
    """AST-level structural sketch of the crate (computed by dittobench, written at
    score time); the gate's rename-resistant anti-copy signal. Same ``{v,k,card,m}``
    shape as ``content_fingerprint``. ``None`` before this landed / no parseable
    Rust. Moderation-only, never exposed on the wire."""
    normalized_source_hash: str | None = None
    """exact-repack hash of the canonicalized source (see
    :func:`ditto.api_server.fingerprint.compute_normalized_source_hash`); the gate's
    equality anti-copy signal, held unconditionally on a match like exact
    ``sha256``. ``None`` before this landed or for an unreadable tarball.
    Moderation-only, never exposed on the wire."""
    prompt_fingerprint: dict | None = None
    """Prompt-surface sketch (see
    :func:`ditto.api_server.fingerprint.compute_prompt_fingerprint`). Shadow signal:
    surfaced so the gate can note a corroborating prompt overlap in a hold's audit
    reason, but not a hold trigger on its own (honest agents share harness
    scaffolding prompts). ``None`` before this landed / no prompt-length literal.
    Moderation-only, never exposed on the wire."""
    code_embedding: list | None = None
    """Code-embedding vector (see :mod:`ditto.api_server.embedding`). Shadow
    signal: surfaced so the gate can cosine-compare it against a candidate (only
    when ``code_embed_model`` matches — a cross-model cosine is meaningless), but not
    yet a hold trigger. ``None`` before this landed / embedder disabled / embed
    failed. Moderation-only, never exposed on the wire."""
    code_embed_model: str | None = None
    """``model@revision`` provenance tag of :attr:`code_embedding`. Gates cosine
    comparisons to same-model vectors and drives re-embed sweeps on a model bump."""
    median_ms: int = 0
    """Median per-case latency (ms) of the winning run — public benchmark telemetry."""
    n: int = 0
    """Number of cases scored in the winning run — public benchmark telemetry."""
    eligible: bool = False
    """Whether this run is *ranked*: it administered the full benchmark
    (``n >= MIN_ELIGIBLE_CASES``) **and** scored a positive composite
    (:func:`_is_ranked`). ``False`` marks either a smoke/practice run (small/medium
    profile) or a full run that scored 0.000; both are surfaced for transparency
    but never ranked or folded into weights, matching the validator's two-gate
    fold — see :data:`MIN_ELIGIBLE_CASES`."""
    details: dict | None = None
    """The winning run's opaque telemetry blob (``scores.details``): models used,
    bench_version, dataset_sha256, per-category means, token spend, and the
    per-case breakdown. The public leaderboard exposes a **safe subset** (never
    ``per_case``, which carries the answer key); validator-gated endpoints may
    read it whole. ``None`` for rows scored before details were persisted."""
    official_composite: float | None = None
    """Continual-retest score used to choose the owner's representative.

    Computed in PostgreSQL before owner-family reduction. Historical reads and
    eras where continual aggregation is inactive carry the canonical median.
    This scalar is safe for list/ranking consumers; it never requires loading
    the score ``details`` JSON.
    """
    stored_composite_stderr: float | None = None
    """Small ranking scalar projected from score details after winner selection."""
    family_members: tuple[LedgerFamilyMember, ...] = ()
    """Compact owner-family rows, populated only for leaderboard list reads."""
    crown_first_seen: datetime | None = None
    """When this owner's lineage *first arrived* at the score it now defends.

    The KOTH fold makes the earliest entry the provisional champion and requires
    every later one to clear an indifference band (:func:`ditto.api_server.koth
    ._dethrone_decision`), so whatever it orders on decides who holds a tie. Left
    on :attr:`first_seen` that anchor is the *winning submission's* upload time,
    which a miner forfeits every time its representative row changes — and the
    representative changes on any resubmission the continual mean prefers, not
    only on an improvement. The consequence was backwards: two agents tied at the
    top, and the one that kept iterating handed the crown to the one that stood
    still.

    So the anchor is the lineage's, not the submission's: the earliest
    :attr:`first_seen` among this owner's ``scored`` agents that are at the
    winner's ``bench_version`` and within the flat dethrone band of the winner's
    official score. Only band-equivalent ancestors count, so an early low-scoring
    submission confers nothing; the version match keeps the comparison on one
    scale; ``scored`` excludes banned and rejected generations. The winner always
    satisfies its own filter, so this can only ever move the anchor *earlier* —
    never later than :attr:`first_seen`.

    ``None`` on rows built outside the owner-family query (provisional
    ``evaluating`` rows, moderation fixtures); :attr:`fold_first_seen` falls back
    to :attr:`first_seen` there, which is exactly the pre-anchor behaviour.
    """
    v9_confirmation: dict[str, Any] | None = None
    """Signed full-confirmation receipt for an enforce-mode Bench v9 row.

    ``None`` for every pre-v9 row and whenever the global confirmation policy
    is off or shadow.  The ordinary signed score fields above are never
    rewritten; consumers independently validate this separate receipt before
    using its derived full composite.
    """

    @property
    def fold_first_seen(self) -> datetime:
        """The anchor the KOTH champion fold orders on.

        Every consumer that reproduces or serves the fold reads this; every
        consumer that means "when did this tarball arrive" — anti-copy priority,
        the scoring gate, the public board's displayed timestamp, the canonical
        rank comparator — keeps reading :attr:`first_seen`. Splitting them is the
        point: those two questions had one answer, and the fold was getting the
        wrong one.
        """
        return self.crown_first_seen or self.first_seen


@dataclass(frozen=True)
class SubmissionFamilyMemberRow:
    """One finalized full-benchmark generation in an owner's ranking family."""

    owner: str
    agent_id: UUID
    agent_name: str
    agent_version: int | None
    miner_hotkey: str
    canonical_composite: float
    submitted_at: datetime


async def list_submission_family_members(
    session: AsyncSession,
    *,
    bench_version: int,
) -> dict[str, list[SubmissionFamilyMemberRow]]:
    """Return every finalized ranked generation grouped by emission owner.

    The leaderboard intentionally publishes one representative per canonical
    owner root. Payment-time coldkeys and active signed owner attestations are
    both edges in that root, so this companion read must use the same grouping
    as rank suppression. Otherwise an attested sibling disappears completely:
    it receives no rank but is also absent from the representative's family.
    """
    rows = (
        await session.execute(
            select(
                Agent.agent_id,
                Agent.name,
                Agent.version,
                Agent.miner_hotkey,
                Agent.created_at,
                EvaluationPayment.miner_coldkey,
                Score.validator_hotkey,
                Score.composite,
            )
            .join(Score, Score.agent_id == Agent.agent_id)
            .outerjoin(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
            .where(
                Agent.status.in_((AgentStatus.SCORED, AgentStatus.LIVE)),
                Score.bench_version == bench_version,
                Score.n >= MIN_ELIGIBLE_CASES,
                Score.composite > 0.0,
            )
            .order_by(Agent.created_at, Agent.agent_id, Score.validator_hotkey)
        )
    ).all()

    by_agent: dict[UUID, list[Any]] = defaultdict(list)
    for row in rows:
        by_agent[row.agent_id].append(row)

    loaded_members: list[SubmissionFamilyMemberRow] = []
    for agent_id, score_rows in by_agent.items():
        if len({row.validator_hotkey for row in score_rows}) < SCORING_QUORUM:
            continue
        first = score_rows[0]
        owner = emission_owner(
            miner_hotkey=first.miner_hotkey,
            miner_coldkey=first.miner_coldkey,
        )
        loaded_members.append(
            SubmissionFamilyMemberRow(
                owner=owner,
                agent_id=agent_id,
                agent_name=first.name,
                agent_version=first.version,
                miner_hotkey=first.miner_hotkey,
                canonical_composite=float(median(row.composite for row in score_rows)),
                submitted_at=first.created_at,
            )
        )

    roots = await attested_emission_owner_roots(
        session,
        [(member.miner_hotkey, member.owner) for member in loaded_members],
    )
    families: dict[str, list[SubmissionFamilyMemberRow]] = defaultdict(list)
    for root, member in zip(roots, loaded_members, strict=True):
        families[root].append(
            SubmissionFamilyMemberRow(
                owner=root,
                agent_id=member.agent_id,
                agent_name=member.agent_name,
                agent_version=member.agent_version,
                miner_hotkey=member.miner_hotkey,
                canonical_composite=member.canonical_composite,
                submitted_at=member.submitted_at,
            )
        )

    for members in families.values():
        members.sort(
            key=lambda member: (
                -member.canonical_composite,
                member.submitted_at,
                str(member.agent_id),
            )
        )
    return dict(families)


@dataclass(frozen=True)
class MemoryLeaderTimelinePoint:
    """One new all-time-high finalized memory score inside a benchmark era."""

    recorded_at: datetime
    bench_version: int
    agent_id: UUID
    agent_name: str
    miner_hotkey: str
    memory_mean: float
    composite: float
    score_count: int


async def list_memory_leader_timeline(
    session: AsyncSession,
    *,
    bench_versions: Sequence[int],
    not_before_by_version: Mapping[int, datetime] | None = None,
) -> list[MemoryLeaderTimelinePoint]:
    """Return the running best finalized miner memory score for each version.

    The score table stores one row per validator, so this read first rebuilds the
    canonical quorum median for every full-size finalized submission. It then
    emits only records that improve the best memory median seen so far within
    that immutable benchmark version. Optional per-version cutoffs are applied
    before the running-high reduction so pre-release rows cannot suppress a
    legitimate record. Third-party reference runs are not stored here and
    cannot enter this result.

    Reduction stays in Python for identical SQLite/Postgres semantics and to
    avoid dialect-specific percentile functions. The covering timeline index
    keeps the source read cheap as historical score eras grow, and the caller
    passes a bounded window of versions rather than the whole ledger.
    """

    versions = tuple(sorted({int(version) for version in bench_versions}))
    if not versions:
        return []

    rows = (
        await session.execute(
            select(
                Score.bench_version,
                Score.agent_id,
                Score.validator_hotkey,
                Score.memory_mean,
                Score.composite,
                Score.updated_at,
                Agent.name,
                Agent.miner_hotkey,
            )
            .join(Agent, Agent.agent_id == Score.agent_id)
            .where(
                Score.bench_version.in_(versions),
                Score.n >= MIN_ELIGIBLE_CASES,
                Agent.status.in_((AgentStatus.SCORED, AgentStatus.LIVE)),
            )
            .order_by(
                Score.bench_version,
                Score.agent_id,
                Score.updated_at,
                Score.validator_hotkey,
            )
        )
    ).all()

    grouped: dict[tuple[int, UUID], list[Any]] = defaultdict(list)
    for row in rows:
        grouped[(int(row.bench_version), row.agent_id)].append(row)

    finalized: list[MemoryLeaderTimelinePoint] = []
    for (bench_version, agent_id), score_rows in grouped.items():
        if len({row.validator_hotkey for row in score_rows}) < SCORING_QUORUM:
            continue
        memory_mean = float(median(row.memory_mean for row in score_rows))
        composite = float(median(row.composite for row in score_rows))
        if composite <= 0.0:
            continue
        finalized.append(
            MemoryLeaderTimelinePoint(
                # A validator may replace its row with a new run. Plot the
                # platform-controlled acceptance time of the current canonical
                # value, not the row's original insert time.
                recorded_at=max(_as_utc(row.updated_at) for row in score_rows),
                bench_version=bench_version,
                agent_id=agent_id,
                agent_name=str(score_rows[0].name),
                miner_hotkey=str(score_rows[0].miner_hotkey),
                memory_mean=memory_mean,
                composite=composite,
                score_count=len(score_rows),
            )
        )

    if not_before_by_version:
        finalized = [
            point
            for point in finalized
            if point.bench_version not in not_before_by_version
            or point.recorded_at >= _as_utc(not_before_by_version[point.bench_version])
        ]

    finalized.sort(
        key=lambda point: (point.recorded_at, point.bench_version, str(point.agent_id))
    )
    best_by_version: dict[int, float] = {}
    timeline: list[MemoryLeaderTimelinePoint] = []
    for point in finalized:
        if point.memory_mean <= best_by_version.get(point.bench_version, -1.0):
            continue
        best_by_version[point.bench_version] = point.memory_mean
        timeline.append(point)
    return timeline


def emission_owner_key(
    *,
    agent: type[Agent] | AliasedClass[Agent] = Agent,
    payment: type[EvaluationPayment] | AliasedClass[EvaluationPayment] = (
        EvaluationPayment
    ),
) -> ColumnElement[str]:
    """Stable owner key for one-slot-per-owner selection.

    New uploads always carry an immutable payment-time coldkey. The hotkey
    fallback preserves legacy/test rows without accidentally collapsing every
    missing-payment row into one owner.

    The single authority for this expression. It is public and takes optional
    aliases because previous-generation carryover dedupes on the same notion of
    "who a miner is" and needs to compare two aliased agents inside one
    statement; a hand-rolled fourth copy of the case expression is exactly how
    the queue-rank preview came to disagree with the allocator about owners.
    Callers must join/outerjoin ``payment`` to ``agent`` themselves.
    """
    return func.coalesce(
        literal("coldkey:") + payment.miner_coldkey,
        literal("hotkey:") + agent.miner_hotkey,
    )


# Module-private alias kept so the two existing emission call sites below read
# unchanged.
_emission_owner_key = emission_owner_key


def emission_owner(*, miner_hotkey: str, miner_coldkey: str | None) -> str:
    """In-Python counterpart of :func:`emission_owner_key`.

    Preview and overlay code that groups already-loaded rows must produce the
    same owner key the allocator's SQL does, or it reports a queue the
    validators will not honour. ``is None`` (not falsiness) mirrors the SQL
    ``IS NOT NULL`` exactly.
    """
    if miner_coldkey is not None:
        return f"coldkey:{miner_coldkey}"
    return f"hotkey:{miner_hotkey}"


async def attested_emission_owner_roots(
    session: AsyncSession,
    identities: Sequence[tuple[str, str]],
) -> list[str]:
    """Canonical owner roots for already-loaded ``(hotkey, raw owner)`` rows.

    Payment-time coldkeys and active, mutually signed owner attestations are
    both ownership edges.  Folding them together here preserves the existing
    one-coldkey/one-slot rule while ensuring a miner cannot occupy another
    emission slot merely by rotating both hotkey and coldkey after proving the
    two hotkeys have the same operator.

    The graph is intentionally evaluated at read time: revoking an attestation
    stops affecting future leaderboard/weight reads without rewriting any
    submission, score, payment, or historical audit row.
    """
    if not identities:
        return []

    from ditto.api_server.attestation import expected_netuid

    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        lo, hi = sorted((left_root, right_root))
        parent[hi] = lo

    for hotkey, raw_owner in identities:
        union(f"hotkey:{hotkey}", raw_owner)

    links = list(
        (
            await session.execute(
                select(OwnerAttestation.hotkey_lo, OwnerAttestation.hotkey_hi).where(
                    OwnerAttestation.netuid == expected_netuid(),
                    OwnerAttestation.revoked_at.is_(None),
                )
            )
        ).tuples()
    )
    for hotkey_lo, hotkey_hi in links:
        union(f"hotkey:{hotkey_lo}", f"hotkey:{hotkey_hi}")

    # A canonical root must not depend on which member of an attested family a
    # caller happened to load. Include the payment-owner edge for every linked
    # hotkey, even when that hotkey's submission was already suppressed from
    # the caller's ranked row set. Without this closure, a one-row leaderboard
    # and a two-row family read chose different roots for the same component.
    linked_hotkeys = {hotkey for pair in links for hotkey in pair}
    if linked_hotkeys:
        payment_owners = await session.execute(
            select(
                EvaluationPayment.miner_hotkey,
                EvaluationPayment.miner_coldkey,
            ).where(
                EvaluationPayment.miner_hotkey.in_(linked_hotkeys),
                EvaluationPayment.miner_coldkey.is_not(None),
            )
        )
        for hotkey, coldkey in payment_owners:
            union(f"hotkey:{hotkey}", f"coldkey:{coldkey}")

    return [find(f"hotkey:{hotkey}") for hotkey, _raw_owner in identities]


@dataclass(frozen=True)
class HealthRollup:
    """Aggregate subnet-health counters for the public dashboard.

    Everything here is derived from what the platform *actually* records —
    submissions and the scores validators report back. Run started/failed counts
    and set-weights latency are validator-side telemetry (wandb), not stored
    here, so this rollup deliberately omits a "success rate": the platform only
    ever sees a *successful* score, so it cannot honestly report failures.
    """

    miners: int
    """Distinct miners who have ever submitted an agent."""
    scored_miners: int
    """Distinct miners with a ``scored`` agent that has a score row (==
    leaderboard size — a stray scored-status row with no score is excluded)."""
    scored_agents: int
    """``scored`` agents that actually carry a score row (eligible submissions)."""
    last_scored_at: datetime | None
    """Newest platform score-write time — when a validator last scored anything."""
    total_scores: int
    """All validator score records currently stored by the platform."""
    scores_24h: int
    """Scores generated in the last 24h — scoring throughput."""
    avg_latency_ms: int | None
    """Mean of the per-score median case latency (ms), across all scores."""


_HEALTH_WINDOW = timedelta(hours=24)


def _as_utc(dt: datetime) -> datetime:
    """Coerce a possibly-naive DB timestamp to aware UTC.

    ``TIMESTAMP(timezone=True)`` round-trips as aware on Postgres but can come
    back naive on the SQLite unit-test fallback; treat naive as UTC so the 24h
    window comparison is dialect-independent.
    """
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def get_public_health(session: AsyncSession, *, now: datetime) -> HealthRollup:
    """Compute the public subnet-health rollup as of ``now`` (aware UTC).

    Two cheap reads: a conditional-aggregate over ``agents`` for the miner/agent
    counts, and a scan of ``(updated_at, median_ms)`` over ``scores`` reduced in
    Python. Public activity uses the platform-controlled write timestamp rather
    than the validator-supplied report ``generated_at`` provenance field. The
    Python reduction (rather than a SQL time filter + window) keeps the 24h
    cutoff and the naive/aware timestamp handling identical across Postgres and
    the SQLite test fallback; the ``scores`` table is small at subnet scale (one
    row per agent per validator).
    """
    total_miners = (
        await session.execute(select(func.count(func.distinct(Agent.miner_hotkey))))
    ).scalar_one()

    # Scored counts require an actual ``scores`` row (INNER JOIN), so they can
    # never contradict the leaderboard: a stray ``scored``-status agent with no
    # score row is not "scored" for public purposes. Distinct-count both because
    # the join fans out one row per validator that scored the agent.
    scored = (
        await session.execute(
            select(
                func.count(func.distinct(Agent.miner_hotkey)),
                func.count(func.distinct(Agent.agent_id)),
            )
            .select_from(Agent)
            .join(Score, Score.agent_id == Agent.agent_id)
            .where(Agent.status == AgentStatus.SCORED)
        )
    ).one()

    rows = (await session.execute(select(Score.updated_at, Score.median_ms))).all()
    cutoff = now - _HEALTH_WINDOW
    generated = [_as_utc(r[0]) for r in rows]
    return HealthRollup(
        miners=int(total_miners),
        scored_miners=int(scored[0]),
        scored_agents=int(scored[1]),
        last_scored_at=max(generated) if generated else None,
        total_scores=len(rows),
        scores_24h=sum(1 for g in generated if g >= cutoff),
        avg_latency_ms=(round(sum(r[1] for r in rows) / len(rows)) if rows else None),
    )


async def upsert_score(
    session: AsyncSession,
    *,
    agent_id: UUID,
    validator_hotkey: str,
    # No default. This writes the score ledger, and a defaulted era is exactly
    # the bug the floor exists to make impossible: a caller that forgot the
    # kwarg used to silently write v2, and would now take a CheckViolation from
    # ``scores_bench_version_floor`` at runtime instead. Required means the
    # mistake is a TypeError at the call site.
    bench_version: int,
    run_id: str,
    seed: int,
    composite: float,
    tool_mean: float,
    memory_mean: float,
    median_ms: int,
    n: int,
    generated_at: datetime,
    signature: str | None = None,
    details: dict | None = None,
    model_usage: LeaseModelUsage | None = None,
) -> None:
    """Insert or update the score for ``(agent_id, validator_hotkey)``.

    Runs inside the caller-owned transaction (``async with
    session.begin():``) so the score write and the agent status transition
    commit atomically. Re-reporting the same ``run_id`` is idempotent; a new
    ``run_id`` overwrites the validator's prior score for this agent.

    ``model_usage`` is the lease's measured language-model spend. When it is
    ``None`` the three ``model_*`` columns are left untouched rather than
    written as ``NULL``: an unmeasured re-report must not erase a measurement
    an earlier report already made.

    Raises:
        DbIntegrityError: Any constraint violation on ``scores`` (the FK to
            ``agents`` when ``agent_id`` is unknown, or a CHECK on a value
            outside its declared range). These indicate a caller bug — the
            handler validates ranges + agent existence first — so the
            envelope catch-all maps them to HTTP 500.
    """
    existing = await session.get(Score, (agent_id, bench_version, validator_hotkey))
    if existing is None:
        session.add(
            Score(
                agent_id=agent_id,
                validator_hotkey=validator_hotkey,
                bench_version=bench_version,
                run_id=run_id,
                seed=seed,
                composite=composite,
                tool_mean=tool_mean,
                memory_mean=memory_mean,
                median_ms=median_ms,
                n=n,
                generated_at=generated_at,
                signature=signature,
                details=details,
                model_calls=None if model_usage is None else model_usage.chat_calls,
                model_prompt_tokens=(
                    None if model_usage is None else model_usage.prompt_tokens
                ),
                model_completion_tokens=(
                    None if model_usage is None else model_usage.completion_tokens
                ),
            )
        )
    else:
        existing.run_id = run_id
        existing.seed = seed
        existing.composite = composite
        existing.tool_mean = tool_mean
        existing.memory_mean = memory_mean
        existing.median_ms = median_ms
        existing.n = n
        existing.generated_at = generated_at
        existing.signature = signature
        existing.details = details
        if model_usage is not None:
            existing.model_calls = model_usage.chat_calls
            existing.model_prompt_tokens = model_usage.prompt_tokens
            existing.model_completion_tokens = model_usage.completion_tokens
    try:
        await session.flush()
    except SAIntegrityError as e:
        raise DbIntegrityError(f"scores upsert violated constraint: {e.orig}") from e


async def list_scores_for_agent(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int | None = None,
) -> list[Score]:
    """Return every validator's score for ``agent_id`` (unordered).

    Used by weight computation / leaderboard reads that aggregate across
    the validator set. Returns an empty list when no validator has scored
    the agent yet.
    """
    if bench_version is None:
        from ditto.db.queries.benchmark_rollout import active_bench_version

        bench_version = await active_bench_version(session)
    stmt = select(Score).where(
        Score.agent_id == agent_id, Score.bench_version == bench_version
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_score_for_validator(
    session: AsyncSession,
    *,
    agent_id: UUID,
    validator_hotkey: str,
) -> Score | None:
    """One validator's recorded score row for an agent, or ``None``.

    Backs the transcript upload path: the declared ``transcript_sha256`` in
    this row's details is what an uploaded artifact's bytes must hash to.
    """
    stmt = select(Score).where(
        Score.agent_id == agent_id,
        Score.validator_hotkey == validator_hotkey,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# Agents whose scoring has settled into a public, non-provisional state. A held
# copy (``ath_pending_review``) is deliberately excluded — its score is not yet
# public and surfacing "under review" before resolution would be premature.
_PUBLIC_SUBMISSION_STATUSES = (AgentStatus.SCORED, AgentStatus.LIVE)


@dataclass(frozen=True)
class SubmissionRow:
    """One finalized submission plus every validator's score for it.

    The value object behind the public per-submission transparency surface: the
    agent's dataset pin (``dataset_seed`` / ``dataset_sha256`` / ``run_size``)
    and the full set of per-validator :class:`Score` rows the median finalized
    on. ``last_scored_at`` is the most recent score time (the recency key the
    index sorts by).
    """

    agent_id: UUID
    miner_hotkey: str
    status: AgentStatus
    dataset_seed: int | None
    dataset_sha256: str | None
    dataset_run_size: str | None
    dataset_seed_block: int | None
    dataset_seed_block_hash: str | None
    last_scored_at: datetime | None
    scores: list[Score]


@dataclass(frozen=True)
class V9ConfirmationPublicProjection:
    """Current public contract state for one v9 subject."""

    result_status: Literal["base_only", "provisional", "full_confirmed"]
    full_confirmed_composite: float | None = None
    evidence_sha256: str | None = None
    receipt: dict[str, Any] | None = None


async def v9_confirmation_public_projections(
    session: AsyncSession, *, agent_ids: Sequence[UUID]
) -> dict[UUID, V9ConfirmationPublicProjection]:
    """Project current mode without rewriting immutable bundle evidence."""

    ids = tuple(dict.fromkeys(agent_ids))
    if not ids:
        return {}
    enforcement_settings = await _v9_confirmation_enforcement_settings(session)
    rows = (
        await session.execute(
            select(
                ConfirmationBundleSubject,
                ConfirmationBundle,
                ConfirmationBundleTicket.deadline.label("ticket_deadline"),
            )
            .outerjoin(
                ConfirmationBundle,
                ConfirmationBundle.bundle_id == ConfirmationBundleSubject.bundle_id,
            )
            .outerjoin(
                ConfirmationBundleTicket,
                ConfirmationBundleTicket.ticket_id
                == ConfirmationBundle.completion_ticket_id,
            )
            .where(
                ConfirmationBundleSubject.agent_id.in_(ids),
                ConfirmationBundleSubject.bench_version == 9,
            )
        )
    ).all()
    projections: dict[UUID, V9ConfirmationPublicProjection] = {}
    for subject, bundle, ticket_deadline in rows:
        completion_is_authoritative = bool(
            bundle is not None
            and (
                bundle.completion_mode == "enforce"
                or (
                    bundle.completion_mode == "shadow"
                    and enforcement_settings is not None
                    and bundle.profile_revision == enforcement_settings.profile_revision
                    and bundle.profile_checksum == enforcement_settings.profile_checksum
                )
            )
        )
        completed = bool(
            enforcement_settings is not None
            and bundle is not None
            and subject.result_status == "full_confirmed"
            and subject.full_effective_micros is not None
            and bundle.state == "completed"
            and bundle.qualification_status == "qualified"
            and completion_is_authoritative
            and bundle.completion_ticket_id is not None
            and ticket_deadline is not None
        )
        if completed:
            assert bundle is not None
            receipt = {
                "mode": "enforce",
                "result_status": "full_confirmed",
                "qualification_status": "qualified",
                "bundle_id": bundle.bundle_id,
                "ticket_id": bundle.completion_ticket_id,
                "ticket_deadline": ticket_deadline,
                "reporter_hotkey": bundle.reporter_hotkey,
                "bundle_signature": bundle.bundle_signature,
                "evidence_sha256": bundle.evidence_sha256,
                "evidence_root": bundle.evidence_root,
                "base_evidence_sha256": subject.base_evidence_sha256,
                "base_quality_micros": subject.base_quality_micros,
                "base_stderr_micros": subject.base_stderr_micros,
                "base_model_factor_bps": subject.base_model_factor_bps,
                "base_tool_factor_bps": subject.base_tool_factor_bps,
                "full_quality_micros": subject.full_quality_micros,
                "full_stderr_micros": subject.full_stderr_micros,
                "semantic_factor_bps": subject.semantic_factor_bps,
                "applied_factor_bps": subject.applied_factor_bps,
                "full_effective_micros": subject.full_effective_micros,
                "verified_at": bundle.verified_at,
            }
            projections[subject.agent_id] = V9ConfirmationPublicProjection(
                result_status="full_confirmed",
                full_confirmed_composite=subject.full_effective_micros / 1_000_000,
                evidence_sha256=bundle.evidence_sha256,
                receipt=receipt,
            )
        else:
            projections[subject.agent_id] = V9ConfirmationPublicProjection(
                result_status=(
                    "provisional" if subject.bundle_id is not None else "base_only"
                )
            )
    return projections


async def get_submission_scores(
    session: AsyncSession, *, agent_id: UUID
) -> SubmissionRow | None:
    """The full k=3 scoring record for one finalized agent, or ``None``.

    ``None`` when the agent does not exist or has not settled into a public
    status (still evaluating, or held for copy review) — callers map that to 404
    so a provisional agent's partial scores are never exposed.
    """
    agent = await get_agent_by_id(session, agent_id=agent_id)
    if agent is None or agent.status not in _PUBLIC_SUBMISSION_STATUSES:
        return None
    # Every version's rows, not just the active bench version: the public record
    # covers older + current runs (each row is version-labeled downstream), and
    # the submissions index batch-fetch is unfiltered the same way.
    scores = list(
        (await session.execute(select(Score).where(Score.agent_id == agent_id)))
        .scalars()
        .all()
    )
    last_scored_at = _as_utc(max(s.generated_at for s in scores)) if scores else None
    return SubmissionRow(
        agent_id=agent.agent_id,
        miner_hotkey=agent.miner_hotkey,
        status=agent.status,
        dataset_seed=agent.dataset_seed,
        dataset_sha256=agent.dataset_sha256,
        dataset_run_size=agent.dataset_run_size,
        dataset_seed_block=agent.dataset_seed_block,
        dataset_seed_block_hash=agent.dataset_seed_block_hash,
        last_scored_at=last_scored_at,
        scores=sorted(scores, key=lambda s: s.validator_hotkey),
    )


async def list_public_submissions(
    session: AsyncSession, *, limit: int = 50
) -> list[SubmissionRow]:
    """Recent finalized submissions, most recently scored first.

    Two queries: pick the finalized agents by latest-score recency (capped to
    ``limit``), then batch-fetch their score rows and group in Python. The
    ``scores`` table is small at subnet scale, so this stays cheap.
    """
    recency = (
        select(
            Score.agent_id.label("agent_id"),
            func.max(Score.generated_at).label("last_scored"),
        )
        .group_by(Score.agent_id)
        .subquery()
    )
    stmt = (
        select(Agent, recency.c.last_scored)
        .join(recency, recency.c.agent_id == Agent.agent_id)
        .where(Agent.status.in_(_PUBLIC_SUBMISSION_STATUSES))
        .order_by(recency.c.last_scored.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return []
    agent_ids = [agent.agent_id for agent, _ in rows]
    score_rows = (
        (await session.execute(select(Score).where(Score.agent_id.in_(agent_ids))))
        .scalars()
        .all()
    )
    by_agent: dict[UUID, list[Score]] = {}
    for s in score_rows:
        by_agent.setdefault(s.agent_id, []).append(s)
    return [
        SubmissionRow(
            agent_id=agent.agent_id,
            miner_hotkey=agent.miner_hotkey,
            status=agent.status,
            dataset_seed=agent.dataset_seed,
            dataset_sha256=agent.dataset_sha256,
            dataset_run_size=agent.dataset_run_size,
            dataset_seed_block=agent.dataset_seed_block,
            dataset_seed_block_hash=agent.dataset_seed_block_hash,
            last_scored_at=_as_utc(last_scored) if last_scored else None,
            scores=sorted(
                by_agent.get(agent.agent_id, []), key=lambda s: s.validator_hotkey
            ),
        )
        for agent, last_scored in rows
    ]


async def list_scores_for_bench_version(
    session: AsyncSession, *, version: int, limit: int = 100, offset: int = 0
) -> tuple[list[tuple[Score, str]], int]:
    """Score rows scored under ``bench_version == version``, plus the total count.

    Returns ``([(score, miner_hotkey), ...], total)``, ordered deterministically
    (generated_at, agent_id, validator_hotkey) and paginated. Powers the retired-
    version corpus release, which serves the FULL unredacted per-case answer keys
    stored in ``scores.details`` — the caller must gate this to retired versions.
    Filters on the first-class version column bound to the ticket and signature;
    the JSON detail is advisory compatibility telemetry only.
    """
    base = (
        select(Score, Agent.miner_hotkey)
        .join(Agent, Agent.agent_id == Score.agent_id)
        .where(Score.bench_version == version)
    )
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(
            base.order_by(
                Score.generated_at.asc(),
                Score.agent_id.asc(),
                Score.validator_hotkey.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [(score, miner) for score, miner in rows], int(total)


async def list_miner_composite_history(
    session: AsyncSession,
    hotkeys: list[str],
    *,
    limit_per: int = 12,
    bench_versions: dict[str, int] | None = None,
) -> dict[str, list[float]]:
    """Per-miner composite trajectory (oldest→newest) for the trend sparkline.

    Returns ``{miner_hotkey: [composite, ...]}`` — every score row for the
    miner's agents, chronological, capped to the most recent ``limit_per``. This
    is the miner's score *over time* (across submissions + re-scores), which is
    aggregate-only (a composite series — no seeds, no per-case content). Empty
    dict for no hotkeys; a miner with a single score simply gets a length-1 list.

    One join + one pass; the ``scores`` table is small at subnet scale.
    """
    if not hotkeys:
        return {}
    stmt = (
        select(
            Agent.miner_hotkey,
            Score.composite,
            Score.generated_at,
            Score.bench_version,
        )
        .select_from(Score)
        .join(Agent, Agent.agent_id == Score.agent_id)
        .where(Agent.miner_hotkey.in_(hotkeys))
        .order_by(Agent.miner_hotkey, Score.generated_at.asc())
    )
    rows = (await session.execute(stmt)).all()
    out: dict[str, list[float]] = {}
    for hotkey, composite, _generated_at, score_version in rows:
        if bench_versions is None or bench_versions.get(hotkey) == score_version:
            out.setdefault(hotkey, []).append(float(composite))
    return {hotkey: series[-limit_per:] for hotkey, series in out.items()}


async def ranked_quorum_agent_ids(
    session: AsyncSession,
    *,
    bench_version: int,
    agent_ids: set[UUID] | None = None,
    require_v9_semantic_pass: bool = False,
) -> set[UUID]:
    """Eligible agent IDs holding a complete, RANKED quorum at ``bench_version``.

    Unlike :func:`count_ranked_quorum_agents`, this deliberately does not fold
    linked emission-owner families. Rollout activation uses it to prove that
    every frozen member completed a rankable full benchmark even when a legacy
    snapshot contains two members from one family.
    """
    ranked_score = _is_ranked()
    if require_v9_semantic_pass:
        if bench_version != 9:
            raise ValueError("v9 semantic evidence can only gate Bench v9")
        # Score ingestion has already verified the signature-bound v9 evidence
        # root and every derived field. Activation must additionally select a
        # median row whose frozen semantic gates passed. Without this predicate,
        # the rollout could promote shadow rows that explicitly report
        # zero-inference or incomplete case attribution merely because their
        # ordinary composite is positive.
        ranked_score = and_(
            ranked_score,
            Score.details["v9_base"]["semantic_gate_factor_bps"].as_integer() == 10_000,
        )
    score_scope = select(
        Score.agent_id.label("agent_id"),
        ranked_score.label("eligible"),
        func.count(Score.agent_id).over(partition_by=Score.agent_id).label("cnt"),
        func.row_number()
        .over(
            partition_by=Score.agent_id,
            order_by=(Score.composite.asc(), Score.validator_hotkey.asc()),
        )
        .label("srn"),
    ).where(Score.bench_version == bench_version)
    if agent_ids is not None:
        if not agent_ids:
            return set()
        score_scope = score_scope.where(Score.agent_id.in_(agent_ids))
    per_version = score_scope.subquery()
    qualifying = (
        select(per_version.c.agent_id)
        .join(Agent, Agent.agent_id == per_version.c.agent_id)
        .where(
            Agent.status == AgentStatus.SCORED,
            per_version.c.cnt >= SCORING_QUORUM,
            per_version.c.eligible,
            or_(
                per_version.c.srn * 2 == per_version.c.cnt,
                per_version.c.srn * 2 == per_version.c.cnt + 1,
            ),
        )
    )
    ranked = set(await session.scalars(qualifying))
    if bench_version != 9:
        return ranked
    enforcement_settings = await _v9_confirmation_enforcement_settings(session)
    if enforcement_settings is None:
        return ranked
    if not ranked:
        return set()
    confirmed = set(
        await session.scalars(
            select(ConfirmationBundleSubject.agent_id)
            .join(
                ConfirmationBundle,
                ConfirmationBundle.bundle_id == ConfirmationBundleSubject.bundle_id,
            )
            .where(
                ConfirmationBundleSubject.agent_id.in_(ranked),
                ConfirmationBundleSubject.bench_version == 9,
                ConfirmationBundleSubject.result_status == "full_confirmed",
                ConfirmationBundleSubject.full_effective_micros > 0,
                ConfirmationBundle.state == "completed",
                ConfirmationBundle.qualification_status == "qualified",
                _v9_confirmation_completion_is_authoritative(enforcement_settings),
            )
        )
    )
    return ranked & confirmed


async def count_ranked_quorum_agents(
    session: AsyncSession,
    *,
    bench_version: int,
    agent_ids: set[UUID] | None = None,
    require_v9_semantic_pass: bool = False,
) -> int:
    """How many owner families hold a complete, RANKED quorum at ``bench_version``.

    One definition, two consumers — they must agree or the guarantee they
    implement has a hole:

    * :func:`list_eligible_ledger` gates the whole-pool authority switch on this
      count (:data:`~ditto.db.queries.benchmark_rollout.MIN_DESIRED_AUTHORITY_AGENTS`).
    * :func:`~ditto.db.queries.benchmark_rollout.maybe_activate_rollout` gates
      activation on it.

    "Eligible" and "ranked" mean exactly what they mean on the ledger: the agent
    is in ``scored`` (so a ``banned`` / ``ath_pending_review`` agent never
    counts), it has at least :data:`SCORING_QUORUM` score rows at this version,
    and its **median** row at this version passes :func:`_is_ranked` — the full
    benchmark (``n >= MIN_ELIGIBLE_CASES``) with a positive composite. A raw
    ``count(scores)`` is NOT a substitute: three smoke-profile rows are a quorum
    by row count but can never rank or earn emissions.

    Deterministic and derived only from committed rows, because it feeds a
    consensus-critical decision: the same DB state must give every reader the
    same answer.
    """
    ranked_ids = await ranked_quorum_agent_ids(
        session,
        bench_version=bench_version,
        agent_ids=agent_ids,
        require_v9_semantic_pass=require_v9_semantic_pass,
    )
    if not ranked_ids:
        return 0
    qualifying = (
        select(
            Agent.miner_hotkey.label("miner_hotkey"),
            _emission_owner_key().label("emission_owner"),
        )
        .outerjoin(
            EvaluationPayment,
            EvaluationPayment.agent_id == Agent.agent_id,
        )
        .where(Agent.agent_id.in_(ranked_ids))
    )
    identities = [
        (row.miner_hotkey, row.emission_owner)
        for row in (await session.execute(qualifying)).all()
    ]
    return len(set(await attested_emission_owner_roots(session, identities)))


async def _v9_confirmation_enforcement_settings(
    session: AsyncSession,
) -> ConfirmationBundleSettings | None:
    """Return the latest valid enforce policy, otherwise fail closed.

    The profile identity is load-bearing when qualified evidence was completed
    in shadow mode: immutable shadow evidence becomes authoritative only while
    the current enforce policy names that exact frozen profile.  This mirrors
    the database subject-authority trigger.
    """

    row = await session.scalar(
        select(ConfirmationBundleSettingsRevision)
        .where(ConfirmationBundleSettingsRevision.scope == "*")
        .order_by(ConfirmationBundleSettingsRevision.revision.desc())
        .limit(1)
    )
    if row is None:
        return None
    try:
        settings = ConfirmationBundleSettings.model_validate(row.settings)
    except ValueError:
        return None
    if settings.mode != ConfirmationBundleMode.ENFORCE:
        return None
    return settings


def _v9_confirmation_completion_is_authoritative(
    settings: ConfirmationBundleSettings,
) -> ColumnElement[bool]:
    """SQL predicate matching the subject-authority trigger's mode clause."""

    assert settings.profile_revision is not None
    assert settings.profile_checksum is not None
    return or_(
        ConfirmationBundle.completion_mode == "enforce",
        and_(
            ConfirmationBundle.completion_mode == "shadow",
            ConfirmationBundle.profile_revision == settings.profile_revision,
            ConfirmationBundle.profile_checksum == settings.profile_checksum,
        ),
    )


async def v9_confirmation_enforcement_active(session: AsyncSession) -> bool:
    """Whether the latest valid immutable policy enables v9 rewards."""

    return await _v9_confirmation_enforcement_settings(session) is not None


async def list_eligible_ledger(
    session: AsyncSession,
    *,
    include_fingerprints: bool = True,
    include_details: bool = True,
    details_keys: tuple[str, ...] | None = None,
    include_family_members: bool = False,
    bench_version: int | None = None,
    owner_score: Literal["official", "canonical"] = "official",
    apply_v9_confirmation_policy: bool = True,
) -> list[LedgerRow]:
    """Return the best eligible score per payment-time coldkey.

    ``owner_score="official"`` (the default) chooses each owner's representative
    with the same continual score used for current ranks and validator weights.
    ``"canonical"`` is reserved for historical snapshots, where the original
    three-validator median is the score that era published.  Keeping this
    choice inside the shared ledger prevents the board, retest scheduler and
    validator fold from selecting different generations for one owner.

    ``include_fingerprints=False`` selects NULL for the anti-copy sketch
    columns (fingerprints + code embedding, several hundred KB per row) —
    the right call for every consumer except the scoring gate, which is the
    only reader that compares them. The public leaderboard, validator ledger
    read, and ticket-eligibility paths were paying that serialization cost on
    every poll for data they never used.

    ``details_keys=(...)`` is the middle setting between the two: the row still
    carries a ``details`` dict, but Postgres builds it from only those keys, so
    a consumer that reads three fields out of a 22KB audit blob stops paying to
    detoast, ship and JSON-decode the other 22KB. Production measured 41% of the
    API worker's CPU inside asyncpg's JSONB decoder with the whole blob selected.

    ``include_details=False`` likewise replaces the large score telemetry JSON
    with NULL. Queue-floor, cleanup, and efficiency-cohort consumers need only
    scalar ranking fields; avoiding the per-case blob keeps those frequently
    polled reads from detoasting and transferring audit payloads they discard.

    ``include_family_members=True`` reuses the already-computed recursive owner
    roots to attach only each member's id, display name/version, and canonical
    score. It deliberately does not load score details, fingerprints, payment
    metadata, artifacts, or histories; the public board uses it solely for its
    compact expandable grouping rows.

    The persistent ledger the validator folds into KOTH+ATH weights (via
    ``GET /scoring/scores``). "Eligible" = agents in ``scored`` — this excludes
    ``ath_pending_review`` holds (suspected copies) and ``banned`` agents, and
    (because scoring flips ``evaluating -> scored``) is served by the partial
    index ``agents_status_scored_idx``.

    Three levels, all deterministic:

    1. ``agent_best`` takes each agent's **median** score row: it orders the
       agent's ``scores`` rows by composite and keeps the middle one (position
       ``(count+1)/2``), so the k=3 pool's canonical score is the median of its
       validators' composites and a single generous or harsh validator cannot
       move it. It keeps the whole **row** (not a per-column ``MEDIAN``), so
       ``composite`` / ``seed`` / ``run_id`` / ``validator_hotkey`` /
       ``signature`` all come from the same physical row and the exposed
       signature still verifies against the exposed composite. An agent with a
       single score is its own median, so pre-quorum agents degrade cleanly.
    2. join to ``agents`` filtered to ``scored``.
    3. after the canonical rows are materialized, rank them by the requested
       owner score and keep each attested payment owner root's first agent.
       This also dedupes one owner across linked hotkeys and names.
    4. before the losing generations are discarded, read the lineage's earliest
       band-equivalent arrival off them into the surviving row's
       :attr:`LedgerRow.crown_first_seen`. The owner family is the only place
       that fact exists, and step 3 is about to throw it away — see the
       attribute for why the KOTH fold needs it.

    Two senses of "eligible" apply. *Pool* eligibility = ``status == scored``
    (excludes ``ath_pending_review`` holds and ``banned`` agents). *Ranking*
    eligibility = the run administered the full benchmark AND scored a positive
    composite (:func:`_is_ranked`), exposed per-row as ``LedgerRow.eligible``: a
    smoke/practice run *or* a full run that scored 0.000 stays in the pool
    (surfaced as *provisional* / unranked) but is ordered **below** every ranked
    run and dropped by the validator's weight fold, so it can never rank or earn
    emissions. Both the per-agent and per-owner selections prefer a ranked
    row/agent, so neither an inflated small run nor a zero-scoring full run
    shadows a miner's real ranked run.

    Ordering (``eligible DESC, composite DESC, first_seen ASC, agent_id ASC``)
    matches the validator fold's eligibility gate + champion/tail tie-breaks.
    During an open rollout the whole ledger sits on ONE benchmark version,
    chosen by the threshold rule below
    (:data:`~ditto.db.queries.benchmark_rollout.MIN_DESIRED_AUTHORITY_AGENTS`);
    a single read never returns a mix of authoritative versions.

    The per-agent selection is median-of-quorum (:data:`SCORING_QUORUM`); the
    per-agent ``n`` is uniform across the pool because all validators score the
    same platform-generated dataset, so the median row's ``eligible`` flag
    represents the agent.
    """
    from ditto.db.queries.benchmark_rollout import (
        MIN_DESIRED_AUTHORITY_AGENTS,
        SCORING_QUORUM,
        active_bench_version,
        open_rollout,
    )

    canonical_version = await active_bench_version(session)
    rollout = None if bench_version is not None else await open_rollout(session)
    desired_version = rollout.desired_version if rollout is not None else None
    candidate_versions = (
        (bench_version,)
        if bench_version is not None
        else tuple({canonical_version, desired_version} - {None})
    )
    enforcement_settings = (
        await _v9_confirmation_enforcement_settings(session)
        if apply_v9_confirmation_policy
        else None
    )
    v9_enforce = enforcement_settings is not None
    confirmed_v9 = and_(
        ConfirmationBundleSubject.result_status == "full_confirmed",
        ConfirmationBundleSubject.full_effective_micros.is_not(None),
        ConfirmationBundle.state == "completed",
        ConfirmationBundle.qualification_status == "qualified",
        _v9_confirmation_completion_is_authoritative(enforcement_settings)
        if enforcement_settings is not None
        else literal(False),
    )
    ranking_composite = cast(ColumnElement[Any], Score.composite)
    ranked_predicate: ColumnElement[bool] = _is_ranked()
    if v9_enforce:
        ranking_composite = case(
            (
                Score.bench_version == 9,
                ConfirmationBundleSubject.full_effective_micros / 1_000_000.0,
            ),
            else_=Score.composite,
        )
        ranked_predicate = and_(
            Score.n >= MIN_ELIGIBLE_CASES,
            ranking_composite > 0.0,
            or_(Score.bench_version != 9, confirmed_v9),
        )
    # Keep the ranking relation scalar-only. Large score JSON and moderation
    # fingerprints are joined only after PostgreSQL has selected one winner per
    # attested owner component.
    agent_best_stmt = select(
        Score.agent_id.label("agent_id"),
        Score.bench_version.label("bench_version"),
        Score.composite.label("composite"),
        ranking_composite.label("ranking_composite"),
        Score.n.label("n"),
        ranked_predicate.label("eligible"),
        Score.validator_hotkey.label("validator_hotkey"),
        # Row count in the agent's pool, so the median position is (cnt+1)/2.
        func.count(Score.agent_id)
        .over(partition_by=(Score.agent_id, Score.bench_version))
        .label("cnt"),
        # Ascending composite so the middle row (by srn) is the median; the
        # validator_hotkey tie-break keeps the pick deterministic.
        func.row_number()
        .over(
            partition_by=(Score.agent_id, Score.bench_version),
            order_by=(
                Score.composite.asc(),
                Score.validator_hotkey.asc(),
            ),
        )
        .label("srn"),
    ).where(Score.bench_version.in_(candidate_versions))
    if v9_enforce:
        agent_best_stmt = (
            agent_best_stmt.outerjoin(
                ConfirmationBundleSubject,
                and_(
                    ConfirmationBundleSubject.agent_id == Score.agent_id,
                    ConfirmationBundleSubject.bench_version == Score.bench_version,
                ),
            )
            .outerjoin(
                ConfirmationBundle,
                ConfirmationBundle.bundle_id == ConfirmationBundleSubject.bundle_id,
            )
            # In enforce mode a base-only or unqualified v9 row is not merely
            # ordered low: it is absent from the authoritative ledger entirely.
            .where(or_(Score.bench_version != 9, confirmed_v9))
        )
    agent_best = agent_best_stmt.subquery()
    per_agent = (
        select(
            Agent.agent_id.label("agent_id"),
            Agent.miner_hotkey.label("miner_hotkey"),
            EvaluationPayment.miner_coldkey.label("miner_coldkey"),
            _emission_owner_key().label("emission_owner"),
            Agent.created_at.label("first_seen"),
            Agent.status.label("status"),
            agent_best.c.composite,
            agent_best.c.ranking_composite,
            agent_best.c.n,
            agent_best.c.eligible,
            agent_best.c.validator_hotkey,
            agent_best.c.bench_version,
            agent_best.c.cnt,
        )
        .join(agent_best, agent_best.c.agent_id == Agent.agent_id)
        .outerjoin(
            EvaluationPayment,
            EvaluationPayment.agent_id == Agent.agent_id,
        )
        # The median row: the middle by ascending composite. Expressed as
        # integer arithmetic (no division, so it is exact + portable across
        # Postgres and the SQLite test path): the lower-middle index m has
        # 2m == cnt (even) or 2m == cnt+1 (odd). A lone score picks itself; a
        # quorum of 3 picks the 2nd (true median).
        .where(
            Agent.status == AgentStatus.SCORED,
            or_(
                agent_best.c.srn * 2 == agent_best.c.cnt,
                agent_best.c.srn * 2 == agent_best.c.cnt + 1,
            ),
        )
        .subquery()
    )
    # During a collecting rollout the ledger is on exactly ONE benchmark version
    # at a time, chosen by a threshold rule evaluated on each read:
    #
    #   fewer than MIN_DESIRED_AUTHORITY_AGENTS frozen priority members hold a
    #   complete, ranked desired-version quorum  ->  the ACTIVE version stays
    #   authoritative for every agent (desired-version medians are collected
    #   and visible as rollout progress only);
    #   all priority members ready  ->  the DESIRED version becomes authoritative
    #   for the whole pool, and an agent without a desired-version quorum has no
    #   authoritative row at all and drops out. That drop-out is the point of the
    #   threshold: it only happens once enough agents have crossed to still fill
    #   the KOTH emission set (see MIN_DESIRED_AUTHORITY_AGENTS).
    #
    # Deliberately NOT a per-agent switch. Ranking a v_next composite against a
    # v_active composite inside one KOTH fold compares incomparable scales — a
    # newer benchmark applies gates the older one does not, so an already-
    # migrated agent would be systematically penalised against a not-yet-migrated
    # peer. Keeping the whole ledger on one version avoids that entirely.
    #
    # The threshold is computed here, inside the read, from committed rows only:
    # every validator folds the ledger this query serves, so a given DB state
    # must yield the same authority decision for every reader. It must never
    # become time-based or config-drifty.
    #
    # Either way partial desired-version samples never affect ranks or weights,
    # and an explicit ``bench_version`` request stays a historical single-version
    # view (``desired_version`` is None for those, so this whole block is skipped).
    authority_filter: ColumnElement[bool] | None = None
    if desired_version is None:
        version_priority: ColumnElement[Any] = per_agent.c.bench_version
    else:
        assert rollout is not None
        desired_at_quorum = and_(
            per_agent.c.bench_version == desired_version,
            per_agent.c.cnt >= SCORING_QUORUM,
        )
        on_canonical = per_agent.c.bench_version == canonical_version
        # Only genuine, ranked 3/3 desired-version PRIORITY members count, so a
        # rank-6 leak, smoke/practice run, or zero-scoring full run cannot push
        # the ledger over. Shared with the rollout activation gate, which must
        # apply the identical definition — see
        # :func:`count_ranked_quorum_agents`.
        priority_ids = set(
            await session.scalars(
                select(BenchmarkRolloutMember.agent_id).where(
                    BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
                    BenchmarkRolloutMember.position <= MIN_DESIRED_AUTHORITY_AGENTS,
                )
            )
        )
        desired_ready_agents = await count_ranked_quorum_agents(
            session,
            bench_version=desired_version,
            agent_ids=priority_ids,
            require_v9_semantic_pass=desired_version == 9,
        )
        if desired_ready_agents >= MIN_DESIRED_AUTHORITY_AGENTS:
            # Whole-pool flip: drop every row that is not a desired-version
            # quorum, so the read cannot return a mix of versions.
            authority_filter = desired_at_quorum
            version_priority = per_agent.c.bench_version
        else:
            version_priority = case((on_canonical, 0), (desired_at_quorum, 1), else_=2)
    selected_version_stmt = select(
        per_agent,
        func.row_number()
        .over(
            partition_by=per_agent.c.agent_id,
            order_by=(version_priority, per_agent.c.bench_version.desc()),
        )
        .label("version_rn"),
    )
    if authority_filter is not None:
        # WHERE is applied before the window function, so the surviving rows are
        # exactly the desired-version medians.
        selected_version_stmt = selected_version_stmt.where(authority_filter)
    selected_version = selected_version_stmt.subquery()
    # PostgreSQL otherwise inlines this relation separately into the quorum and
    # confirmation aggregates. Production EXPLAIN showed that multiplying the
    # median/version window by every candidate (1.29M buffer hits for 322
    # agents). This is the small scalar boundary both aggregates share.
    authoritative = (
        select(selected_version)
        .where(selected_version.c.version_rn == 1)
        .cte("authoritative")
        .prefix_with("MATERIALIZED")
    )
    selection_score: ColumnElement[Any] = authoritative.c.ranking_composite
    official_joins: tuple[Any, ...] = ()
    if owner_score == "official":
        from ditto.db.queries.score_ranking import continual_mean_is_active

        continual_active = await continual_mean_is_active(
            session,
            bench_version=bench_version,
            active_version=canonical_version,
        )
        if continual_active:
            quorum = (
                select(
                    Score.agent_id.label("agent_id"),
                    Score.bench_version.label("bench_version"),
                    func.count().label("quorum_count"),
                    func.sum(Score.composite).label("quorum_sum"),
                )
                .join(
                    authoritative,
                    and_(
                        authoritative.c.agent_id == Score.agent_id,
                        authoritative.c.bench_version == Score.bench_version,
                    ),
                )
                .group_by(Score.agent_id, Score.bench_version)
                .having(func.count() == SCORING_QUORUM)
                .cte("quorum_aggregates")
                .prefix_with("MATERIALIZED")
            )
            confirmation_ranked = (
                select(
                    ConfirmationScore.agent_id.label("agent_id"),
                    ConfirmationScore.bench_version.label("bench_version"),
                    ConfirmationScore.seed.label("seed"),
                    ConfirmationScore.composite.label("composite"),
                    func.count()
                    .over(
                        partition_by=(
                            ConfirmationScore.agent_id,
                            ConfirmationScore.bench_version,
                            ConfirmationScore.seed,
                        )
                    )
                    .label("sample_count"),
                    func.row_number()
                    .over(
                        partition_by=(
                            ConfirmationScore.agent_id,
                            ConfirmationScore.bench_version,
                            ConfirmationScore.seed,
                        ),
                        order_by=(
                            ConfirmationScore.composite.asc(),
                            ConfirmationScore.validator_hotkey.asc(),
                        ),
                    )
                    .label("sample_rank"),
                )
                .join(
                    authoritative,
                    and_(
                        authoritative.c.agent_id == ConfirmationScore.agent_id,
                        authoritative.c.bench_version
                        == ConfirmationScore.bench_version,
                    ),
                )
                .subquery()
            )
            # ``statistics.median`` averages the two middle values for an even
            # validator count. Select the one or two middle rows, then AVG.
            confirmation_medians = (
                select(
                    confirmation_ranked.c.agent_id,
                    confirmation_ranked.c.bench_version,
                    confirmation_ranked.c.seed,
                    func.avg(confirmation_ranked.c.composite).label("seed_median"),
                )
                .where(
                    or_(
                        confirmation_ranked.c.sample_rank * 2
                        == confirmation_ranked.c.sample_count,
                        confirmation_ranked.c.sample_rank * 2
                        == confirmation_ranked.c.sample_count + 1,
                        confirmation_ranked.c.sample_rank * 2
                        == confirmation_ranked.c.sample_count + 2,
                    )
                )
                .group_by(
                    confirmation_ranked.c.agent_id,
                    confirmation_ranked.c.bench_version,
                    confirmation_ranked.c.seed,
                )
                .subquery()
            )
            confirmation = (
                select(
                    confirmation_medians.c.agent_id,
                    confirmation_medians.c.bench_version,
                    func.count().label("confirmation_count"),
                    func.sum(confirmation_medians.c.seed_median).label(
                        "confirmation_sum"
                    ),
                )
                .group_by(
                    confirmation_medians.c.agent_id,
                    confirmation_medians.c.bench_version,
                )
                .cte("confirmation_aggregates")
                .prefix_with("MATERIALIZED")
            )
            official_joins = (quorum, confirmation)
            continual_selection = func.coalesce(
                (quorum.c.quorum_sum + confirmation.c.confirmation_sum)
                / (quorum.c.quorum_count + confirmation.c.confirmation_count),
                authoritative.c.composite,
            )
            selection_score = (
                case(
                    (
                        authoritative.c.bench_version == 9,
                        authoritative.c.ranking_composite,
                    ),
                    else_=continual_selection,
                )
                if v9_enforce
                else continual_selection
            )

    candidates_stmt = select(
        authoritative,
        (literal("hotkey:") + authoritative.c.miner_hotkey).label("hotkey_node"),
        selection_score.label("official_score"),
    )
    if official_joins:
        quorum, confirmation = official_joins
        candidates_stmt = candidates_stmt.outerjoin(
            quorum,
            and_(
                quorum.c.agent_id == authoritative.c.agent_id,
                quorum.c.bench_version == authoritative.c.bench_version,
            ),
        ).outerjoin(
            confirmation,
            and_(
                confirmation.c.agent_id == authoritative.c.agent_id,
                confirmation.c.bench_version == authoritative.c.bench_version,
            ),
        )
    candidates = candidates_stmt.cte("ledger_candidates")

    # Resolve the exact same ownership graph as
    # ``attested_emission_owner_roots`` inside PostgreSQL. The graph contains
    # candidate hotkey→payment-owner edges, active signed hotkey attestations,
    # and payment-owner closure for every actively linked hotkey.
    from ditto.api_server.attestation import expected_netuid

    linked_hotkeys = union(
        select(OwnerAttestation.hotkey_lo.label("hotkey")).where(
            OwnerAttestation.netuid == expected_netuid(),
            OwnerAttestation.revoked_at.is_(None),
        ),
        select(OwnerAttestation.hotkey_hi.label("hotkey")).where(
            OwnerAttestation.netuid == expected_netuid(),
            OwnerAttestation.revoked_at.is_(None),
        ),
    ).cte("linked_hotkeys")
    payment_edges = (
        select(
            (literal("hotkey:") + EvaluationPayment.miner_hotkey).label("left_node"),
            (literal("coldkey:") + EvaluationPayment.miner_coldkey).label("right_node"),
        )
        .where(
            EvaluationPayment.miner_coldkey.is_not(None),
            EvaluationPayment.miner_hotkey.in_(select(linked_hotkeys.c.hotkey)),
        )
        .subquery()
    )
    attestation_edges = select(
        (literal("hotkey:") + OwnerAttestation.hotkey_lo).label("left_node"),
        (literal("hotkey:") + OwnerAttestation.hotkey_hi).label("right_node"),
    ).where(
        OwnerAttestation.netuid == expected_netuid(),
        OwnerAttestation.revoked_at.is_(None),
    )
    owner_edges = union(
        select(
            candidates.c.hotkey_node.label("left_node"),
            candidates.c.emission_owner.label("right_node"),
        ),
        select(
            candidates.c.emission_owner.label("left_node"),
            candidates.c.hotkey_node.label("right_node"),
        ),
        attestation_edges,
        select(
            (literal("hotkey:") + OwnerAttestation.hotkey_hi).label("left_node"),
            (literal("hotkey:") + OwnerAttestation.hotkey_lo).label("right_node"),
        ).where(
            OwnerAttestation.netuid == expected_netuid(),
            OwnerAttestation.revoked_at.is_(None),
        ),
        select(payment_edges.c.left_node, payment_edges.c.right_node),
        select(
            payment_edges.c.right_node.label("left_node"),
            payment_edges.c.left_node.label("right_node"),
        ),
    ).cte("owner_edges")
    owner_walk = select(
        candidates.c.hotkey_node.label("start_node"),
        candidates.c.hotkey_node.label("node"),
    ).cte("owner_walk", recursive=True)
    owner_walk = owner_walk.union(
        select(owner_walk.c.start_node, owner_edges.c.right_node).join(
            owner_edges, owner_edges.c.left_node == owner_walk.c.node
        )
    )
    owner_roots = (
        select(
            owner_walk.c.start_node,
            func.min(owner_walk.c.node).label("owner_root"),
        )
        .group_by(owner_walk.c.start_node)
        .cte("owner_roots")
    )
    rooted = (
        select(candidates, owner_roots.c.owner_root)
        .join(
            owner_roots,
            owner_roots.c.start_node == candidates.c.hotkey_node,
        )
        .cte("rooted_candidates")
    )
    owner_order = score_order_terms(
        eligible=rooted.c.eligible,
        composite=rooted.c.official_score,
        first_seen=rooted.c.first_seen,
        agent_id=rooted.c.agent_id,
    )
    # Rank the owner's family once and read the winner's score and benchmark
    # version back onto every sibling row, so the next level can measure each
    # generation against the one that actually represents the owner. Windows do
    # not nest, which is the only reason this is two CTEs instead of one.
    owner_ranked = select(
        rooted,
        func.row_number()
        .over(partition_by=rooted.c.owner_root, order_by=owner_order)
        .label("owner_rank"),
        func.first_value(rooted.c.official_score)
        .over(partition_by=rooted.c.owner_root, order_by=owner_order)
        .label("owner_top_score"),
        func.first_value(rooted.c.bench_version)
        .over(partition_by=rooted.c.owner_root, order_by=owner_order)
        .label("owner_top_bench_version"),
    ).cte("owner_ranked")
    owner_provenance = select(
        owner_ranked,
        func.min(owner_ranked.c.first_seen)
        .filter(
            and_(
                owner_ranked.c.eligible,
                owner_ranked.c.bench_version == owner_ranked.c.owner_top_bench_version,
                owner_ranked.c.official_score
                >= owner_ranked.c.owner_top_score
                - _crown_band(
                    owner_ranked.c.owner_top_score,
                    owner_ranked.c.owner_top_bench_version,
                ),
            )
        )
        .over(partition_by=owner_ranked.c.owner_root)
        .label("crown_first_seen"),
    ).cte("owner_provenance")
    winners = (
        select(owner_provenance)
        .where(owner_provenance.c.owner_rank == 1)
        .cte("ledger_winners")
    )

    if details_keys is not None:
        # Narrow projection: build an object holding ONLY the requested keys, so
        # Postgres never ships (and asyncpg never decodes) the rest of the blob.
        # A key the row does not have arrives as SQL NULL -> Python None, which
        # every reader of these keys already treats as absent.
        pairs: list[ColumnElement[Any]] = []
        for key in details_keys:
            pairs.append(literal(key))
            pairs.append(Score.details[key])
        details_column = func.jsonb_build_object(*pairs).label("details")
    elif include_details:
        details_column = Score.details.label("details")
    else:
        details_column = null().label("details")
    sketch_columns: tuple[ColumnElement[Any], ...]
    if include_fingerprints:
        sketch_columns = (
            Agent.content_fingerprint.label("content_fingerprint"),
            Agent.structural_fingerprint.label("structural_fingerprint"),
            Agent.prompt_fingerprint.label("prompt_fingerprint"),
            Agent.code_embedding.label("code_embedding"),
            Agent.normalized_source_hash.label("normalized_source_hash"),
            Agent.code_embed_model.label("code_embed_model"),
        )
    else:
        sketch_columns = (
            null().label("content_fingerprint"),
            null().label("structural_fingerprint"),
            null().label("prompt_fingerprint"),
            null().label("code_embedding"),
            null().label("normalized_source_hash"),
            null().label("code_embed_model"),
        )
    family_agent = Agent.__table__.alias("family_agent")
    family_columns: tuple[ColumnElement[Any], ...]
    if include_family_members:
        family_columns = (
            rooted.c.agent_id.label("family_agent_id"),
            family_agent.c.name.label("family_agent_name"),
            family_agent.c.version.label("family_agent_version"),
            rooted.c.composite.label("family_canonical_composite"),
        )
    else:
        family_columns = (
            null().label("family_agent_id"),
            null().label("family_agent_name"),
            null().label("family_agent_version"),
            null().label("family_canonical_composite"),
        )
    if v9_enforce:
        receipt_columns: tuple[ColumnElement[Any], ...] = (
            ConfirmationBundleSubject.result_status.label("v9_result_status"),
            ConfirmationBundleSubject.base_evidence_sha256.label(
                "v9_base_evidence_sha256"
            ),
            ConfirmationBundleSubject.base_quality_micros.label(
                "v9_base_quality_micros"
            ),
            ConfirmationBundleSubject.base_stderr_micros.label("v9_base_stderr_micros"),
            ConfirmationBundleSubject.base_model_factor_bps.label(
                "v9_base_model_factor_bps"
            ),
            ConfirmationBundleSubject.base_tool_factor_bps.label(
                "v9_base_tool_factor_bps"
            ),
            ConfirmationBundleSubject.full_quality_micros.label(
                "v9_full_quality_micros"
            ),
            ConfirmationBundleSubject.full_stderr_micros.label("v9_full_stderr_micros"),
            ConfirmationBundleSubject.semantic_factor_bps.label(
                "v9_semantic_factor_bps"
            ),
            ConfirmationBundleSubject.applied_factor_bps.label("v9_applied_factor_bps"),
            ConfirmationBundleSubject.full_effective_micros.label(
                "v9_full_effective_micros"
            ),
            ConfirmationBundle.bundle_id.label("v9_bundle_id"),
            ConfirmationBundle.qualification_status.label("v9_qualification_status"),
            ConfirmationBundle.completion_mode.label("v9_completion_mode"),
            ConfirmationBundle.evidence_root.label("v9_evidence_root"),
            ConfirmationBundle.evidence_sha256.label("v9_evidence_sha256"),
            ConfirmationBundle.reporter_hotkey.label("v9_reporter_hotkey"),
            ConfirmationBundle.bundle_signature.label("v9_bundle_signature"),
            ConfirmationBundle.verified_at.label("v9_verified_at"),
            ConfirmationBundleTicket.ticket_id.label("v9_ticket_id"),
            ConfirmationBundleTicket.deadline.label("v9_ticket_deadline"),
        )
    else:
        receipt_columns = tuple(
            null().label(name)
            for name in (
                "v9_result_status",
                "v9_base_evidence_sha256",
                "v9_base_quality_micros",
                "v9_base_stderr_micros",
                "v9_base_model_factor_bps",
                "v9_base_tool_factor_bps",
                "v9_full_quality_micros",
                "v9_full_stderr_micros",
                "v9_semantic_factor_bps",
                "v9_applied_factor_bps",
                "v9_full_effective_micros",
                "v9_bundle_id",
                "v9_qualification_status",
                "v9_completion_mode",
                "v9_evidence_root",
                "v9_evidence_sha256",
                "v9_reporter_hotkey",
                "v9_bundle_signature",
                "v9_verified_at",
                "v9_ticket_id",
                "v9_ticket_deadline",
            )
        )
    stmt = (
        select(
            winners.c.owner_root,
            winners.c.official_score,
            Agent.agent_id,
            Agent.miner_hotkey,
            EvaluationPayment.miner_coldkey,
            Agent.sha256,
            Agent.size_bytes,
            *sketch_columns,
            Agent.created_at.label("first_seen"),
            Agent.status,
            Score.composite,
            Score.tool_mean,
            Score.memory_mean,
            Score.seed,
            Score.run_id,
            Score.median_ms,
            Score.n,
            winners.c.eligible,
            Score.details["composite_stderr"]
            .as_float()
            .label("stored_composite_stderr"),
            details_column,
            Score.validator_hotkey,
            Score.signature,
            Score.bench_version,
            winners.c.crown_first_seen,
            *receipt_columns,
        )
        .join(Agent, Agent.agent_id == winners.c.agent_id)
        .join(
            Score,
            and_(
                Score.agent_id == winners.c.agent_id,
                Score.bench_version == winners.c.bench_version,
                Score.validator_hotkey == winners.c.validator_hotkey,
            ),
        )
        .outerjoin(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
    )
    if v9_enforce:
        stmt = (
            stmt.outerjoin(
                ConfirmationBundleSubject,
                and_(
                    ConfirmationBundleSubject.agent_id == winners.c.agent_id,
                    ConfirmationBundleSubject.bench_version == winners.c.bench_version,
                ),
            )
            .outerjoin(
                ConfirmationBundle,
                ConfirmationBundle.bundle_id == ConfirmationBundleSubject.bundle_id,
            )
            .outerjoin(
                ConfirmationBundleTicket,
                ConfirmationBundleTicket.ticket_id
                == ConfirmationBundle.completion_ticket_id,
            )
        )
    if include_family_members:
        # Extract the winner's JSON-backed stderr and join its agent/payment
        # metadata exactly once. Joining the family first made PostgreSQL
        # repeat those projections for every child in the owner component.
        winner_projection = stmt.cte("ledger_winner_rows").prefix_with("MATERIALIZED")
        winner_order = score_order_terms(
            eligible=winner_projection.c.eligible,
            composite=winner_projection.c.official_score,
            first_seen=winner_projection.c.first_seen,
            agent_id=winner_projection.c.agent_id,
        )
        stmt = (
            select(winner_projection, *family_columns)
            .join(rooted, rooted.c.owner_root == winner_projection.c.owner_root)
            .join(family_agent, family_agent.c.agent_id == rooted.c.agent_id)
            .order_by(
                *winner_order,
                rooted.c.composite.desc(),
                rooted.c.first_seen,
                rooted.c.agent_id,
            )
        )
    else:
        stmt = stmt.add_columns(*family_columns).order_by(
            *score_order_terms(
                eligible=winners.c.eligible,
                composite=winners.c.official_score,
                first_seen=winners.c.first_seen,
                agent_id=winners.c.agent_id,
            )
        )

    result = list((await session.execute(stmt)).all())
    grouped: dict[UUID, list[Any]] = defaultdict(list)
    winner_ids: list[UUID] = []
    for row in result:
        if row.agent_id not in grouped:
            winner_ids.append(row.agent_id)
        grouped[row.agent_id].append(row)

    ledger: list[LedgerRow] = []
    for winner_id in winner_ids:
        group_rows = grouped[winner_id]
        row = group_rows[0]
        family_members = tuple(
            LedgerFamilyMember(
                agent_id=member.family_agent_id,
                agent_name=member.family_agent_name,
                agent_version=member.family_agent_version,
                canonical_composite=float(member.family_canonical_composite),
            )
            for member in group_rows
            if member.family_agent_id is not None
        )
        v9_confirmation = None
        if row.bench_version == 9 and row.v9_bundle_id is not None:
            v9_confirmation = {
                "mode": row.v9_completion_mode,
                "result_status": row.v9_result_status,
                "qualification_status": row.v9_qualification_status,
                "bundle_id": row.v9_bundle_id,
                "ticket_id": row.v9_ticket_id,
                "ticket_deadline": row.v9_ticket_deadline,
                "reporter_hotkey": row.v9_reporter_hotkey,
                "bundle_signature": row.v9_bundle_signature,
                "evidence_sha256": row.v9_evidence_sha256,
                "evidence_root": row.v9_evidence_root,
                "base_evidence_sha256": row.v9_base_evidence_sha256,
                "base_quality_micros": row.v9_base_quality_micros,
                "base_stderr_micros": row.v9_base_stderr_micros,
                "base_model_factor_bps": row.v9_base_model_factor_bps,
                "base_tool_factor_bps": row.v9_base_tool_factor_bps,
                "full_quality_micros": row.v9_full_quality_micros,
                "full_stderr_micros": row.v9_full_stderr_micros,
                "semantic_factor_bps": row.v9_semantic_factor_bps,
                "applied_factor_bps": row.v9_applied_factor_bps,
                "full_effective_micros": row.v9_full_effective_micros,
                "verified_at": row.v9_verified_at,
            }
        ledger.append(
            LedgerRow(
                miner_hotkey=row.miner_hotkey,
                agent_id=row.agent_id,
                composite=row.composite,
                tool_mean=row.tool_mean,
                memory_mean=row.memory_mean,
                first_seen=row.first_seen,
                sha256=row.sha256,
                size_bytes=row.size_bytes,
                run_id=row.run_id,
                seed=row.seed,
                validator_hotkey=row.validator_hotkey,
                signature=row.signature,
                status=AgentStatus(row.status),
                miner_coldkey=row.miner_coldkey,
                bench_version=row.bench_version,
                content_fingerprint=row.content_fingerprint,
                structural_fingerprint=row.structural_fingerprint,
                normalized_source_hash=row.normalized_source_hash,
                prompt_fingerprint=row.prompt_fingerprint,
                code_embedding=row.code_embedding,
                code_embed_model=row.code_embed_model,
                median_ms=row.median_ms,
                n=row.n,
                eligible=bool(row.eligible),
                details=row.details,
                official_composite=float(row.official_score),
                stored_composite_stderr=row.stored_composite_stderr,
                family_members=family_members,
                crown_first_seen=row.crown_first_seen,
                v9_confirmation=v9_confirmation,
            )
        )
    return ledger


async def quorum_composites(
    session: AsyncSession,
    agent_ids: Sequence[UUID],
    *,
    bench_versions: dict[UUID, int] | None = None,
) -> dict[UUID, list[float]]:
    """Every accepted composite per agent for the given ids.

    Lets the ledger report the between-validator spread — the k=3 quorum's
    standard error — as the composite's ``composite_stderr`` from data already
    collected, no re-score. One flat read (no aggregation): the SEM is computed
    in Python (:func:`ditto.api_server.endpoints.scoring._quorum_stderr`), so this
    stays portable across Postgres and the SQLite test path (no ``stddev``). The
    row set matches :func:`list_eligible_ledger`'s median (all of the agent's
    score rows), so the SE describes the same quorum the composite came from.
    """
    if not agent_ids:
        return {}
    from ditto.db.queries.benchmark_rollout import active_bench_version

    canonical_version = await active_bench_version(session)
    versions = bench_versions or dict.fromkeys(agent_ids, canonical_version)
    result = await session.execute(
        select(Score.agent_id, Score.composite, Score.bench_version).where(
            Score.agent_id.in_(agent_ids),
            Score.bench_version.in_(set(versions.values())),
        )
    )
    out: dict[UUID, list[float]] = {}
    for agent_id, composite, score_version in result:
        if versions.get(agent_id) == score_version:
            out.setdefault(agent_id, []).append(composite)
    return out


async def quorum_score_rows(
    session: AsyncSession,
    agent_ids: Sequence[UUID],
    *,
    bench_versions: dict[UUID, int],
) -> dict[UUID, list[Score]]:
    """Return every accepted score row backing each authoritative ledger row."""
    if not agent_ids:
        return {}
    result = await session.execute(
        select(Score)
        .where(
            Score.agent_id.in_(agent_ids),
            Score.bench_version.in_(set(bench_versions.values())),
        )
        .order_by(Score.agent_id, Score.composite, Score.validator_hotkey)
    )
    out: dict[UUID, list[Score]] = {}
    for score in result.scalars():
        if bench_versions.get(score.agent_id) == score.bench_version:
            out.setdefault(score.agent_id, []).append(score)
    return out


async def list_provisional_ledger(
    session: AsyncSession,
    *,
    bench_version: int | None = None,
) -> list[tuple[LedgerRow, int]]:
    """Return each unfinalized owner's best partially scored submission.

    This is a public-feedback read only. It deliberately considers only agents
    still in ``evaluating`` with at least one accepted score; validator weights
    continue to use :func:`list_eligible_ledger` and therefore remain gated on
    the finalized ``scored`` status. Numeric fields are medians of the accepted
    reports available so far. Opaque run details are omitted because, before
    quorum, no single validator row is the canonical result.

    One row per *owner*, matching how :func:`list_eligible_ledger` partitions
    the finalized board: a coldkey funding several hotkeys holds one emission
    position, so its provisional overlay must not preview several either.
    """
    from ditto.db.queries.benchmark_rollout import active_bench_version

    canonical_version = bench_version or await active_bench_version(session)
    rows = (
        await session.execute(
            select(Agent, Score, EvaluationPayment.miner_coldkey)
            .join(Score, Score.agent_id == Agent.agent_id)
            .outerjoin(
                EvaluationPayment,
                EvaluationPayment.agent_id == Agent.agent_id,
            )
            .where(
                Agent.status == AgentStatus.EVALUATING,
                Score.bench_version == canonical_version,
            )
            .order_by(
                Agent.created_at.asc(),
                Agent.agent_id.asc(),
                Score.generated_at.asc(),
                Score.validator_hotkey.asc(),
            )
        )
    ).all()

    by_agent: dict[UUID, tuple[Agent, str | None, list[Score]]] = {}
    for agent, score, miner_coldkey in rows:
        if agent.agent_id not in by_agent:
            by_agent[agent.agent_id] = (agent, miner_coldkey, [])
        by_agent[agent.agent_id][2].append(score)

    candidates: list[tuple[LedgerRow, int]] = []
    for agent, miner_coldkey, scores in by_agent.values():
        if not scores:
            continue
        representative = sorted(
            scores, key=lambda score: (score.composite, score.validator_hotkey)
        )[(len(scores) - 1) // 2]
        composite = float(median(score.composite for score in scores))
        tool_mean = float(median(score.tool_mean for score in scores))
        memory_mean = float(median(score.memory_mean for score in scores))
        median_ms = int(median(score.median_ms for score in scores))
        n = int(median(score.n for score in scores))
        candidates.append(
            (
                LedgerRow(
                    miner_hotkey=agent.miner_hotkey,
                    agent_id=agent.agent_id,
                    composite=composite,
                    tool_mean=tool_mean,
                    memory_mean=memory_mean,
                    first_seen=agent.created_at,
                    sha256=agent.sha256,
                    size_bytes=agent.size_bytes,
                    run_id=representative.run_id,
                    seed=representative.seed,
                    validator_hotkey=representative.validator_hotkey,
                    signature=representative.signature,
                    status=AgentStatus.EVALUATING,
                    miner_coldkey=miner_coldkey,
                    bench_version=representative.bench_version,
                    median_ms=median_ms,
                    n=n,
                    eligible=n >= MIN_ELIGIBLE_CASES and composite > 0.0,
                    details=None,
                ),
                len(scores),
            )
        )

    # A provisional row has no continual mean to rank on -- it has not reached
    # quorum -- so the canonical comparator reads its raw composite here.
    candidates.sort(key=lambda candidate: score_order_key(candidate[0]))
    roots = await attested_emission_owner_roots(
        session,
        [
            (
                candidate[0].miner_hotkey,
                emission_owner(
                    miner_hotkey=candidate[0].miner_hotkey,
                    miner_coldkey=candidate[0].miner_coldkey,
                ),
            )
            for candidate in candidates
        ],
    )
    best_by_owner: dict[str, tuple[LedgerRow, int]] = {}
    for root, candidate in zip(roots, candidates, strict=True):
        best_by_owner.setdefault(root, candidate)
    return list(best_by_owner.values())


async def list_scored_bench_versions(session: AsyncSession) -> list[int]:
    """Every benchmark version with at least one accepted score, newest first.

    Backs the dashboard's per-version history pills: a version earns a pill as
    soon as its first score lands and keeps it as a historical view forever.
    ``NULL`` (pre-versioning legacy) rows carry no version to browse by and are
    excluded.
    """
    rows = await session.scalars(
        select(Score.bench_version)
        .where(Score.bench_version.is_not(None))
        .distinct()
        .order_by(Score.bench_version.desc())
    )
    return [int(version) for version in rows]


async def get_score_counts(
    session: AsyncSession,
    agent_ids: list[UUID],
    *,
    bench_versions: dict[UUID, int] | None = None,
) -> dict[UUID, int]:
    """Return accepted validator-score counts for the requested agents."""
    if not agent_ids:
        return {}
    from ditto.db.queries.benchmark_rollout import active_bench_version

    canonical_version = await active_bench_version(session)
    versions = bench_versions or dict.fromkeys(agent_ids, canonical_version)
    rows = (
        await session.execute(
            select(
                Score.agent_id,
                Score.bench_version,
                func.count(Score.validator_hotkey),
            )
            .where(
                Score.agent_id.in_(agent_ids),
                Score.bench_version.in_(set(versions.values())),
            )
            .group_by(Score.agent_id, Score.bench_version)
        )
    ).all()
    return {
        agent_id: int(count)
        for agent_id, score_version, count in rows
        if versions.get(agent_id) == score_version
    }
