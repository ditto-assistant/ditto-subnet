"""Scored agents whose quorum expired with its dueling season (bench v16+).

The two-season window (:mod:`ditto.db.queries.dueling_season_window`) is a
deliberate cost control: a score counts for the season it was taken in and
the one after, then leaves the authoritative ledger, so the platform never
pays to re-score the whole board on every rotation. Nothing here changes that
window, queues work, or re-admits a row into a pin. It only answers, from
stored rows, *why* a scored agent is no longer on the live ledger and what
its last valid quorum was, so the public board can label it instead of
silently dropping it and diagnostics can name the expiry.

Every helper is inert below the dueling floor.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import Agent, EvaluationPayment, Score
from ditto.db.queries.dueling_season_window import (
    DuelingSeasonWindow,
    dueling_season_window,
)
from ditto.db.queries.scores import MIN_ELIGIBLE_CASES, SCORING_QUORUM, LedgerRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class SeasonScore:
    """One accepted ``scores`` row, reduced to what expiry reads."""

    validator_hotkey: str
    dueling_season_id: UUID | None
    composite: float
    n: int


@dataclass(frozen=True)
class SeasonCoverage:
    """Where one agent's accepted scores sit relative to the live window."""

    agent_id: UUID
    bench_version: int
    window: DuelingSeasonWindow
    scores: tuple[SeasonScore, ...]

    @property
    def in_window_count(self) -> int:
        return sum(
            1 for row in self.scores if row.dueling_season_id in self.window.comparable
        )

    @property
    def expired(self) -> bool:
        """A full quorum was accepted, but the window no longer holds one."""
        return (
            len(self.scores) >= SCORING_QUORUM and self.in_window_count < SCORING_QUORUM
        )

    @property
    def score_dueling_season_ids(self) -> tuple[UUID, ...]:
        """Distinct seasons the accepted scores were taken in, sorted."""
        return tuple(
            sorted(
                {
                    row.dueling_season_id
                    for row in self.scores
                    if row.dueling_season_id is not None
                },
                key=str,
            )
        )

    @property
    def median(self) -> SeasonScore | None:
        """The ledger's median row (lower middle by composite, then hotkey)
        over every accepted score, so the last valid quorum is reported with
        the same arithmetic the ledger used while it counted."""
        if not self.scores:
            return None
        ordered = sorted(
            self.scores, key=lambda row: (row.composite, row.validator_hotkey)
        )
        return ordered[(len(ordered) + 1) // 2 - 1]

    @property
    def ranked(self) -> bool:
        """Whether the last valid quorum was a full, positive benchmark."""
        median = self.median
        return (
            median is not None
            and median.n >= MIN_ELIGIBLE_CASES
            and median.composite > 0.0
        )


async def season_coverage(
    session: AsyncSession,
    *,
    bench_version: int | None,
    agent_ids: Iterable[UUID] | None = None,
) -> dict[UUID, SeasonCoverage]:
    """Coverage of every ``scored``, non-shadow agent at ``bench_version``.

    ``agent_ids`` narrows the read. Empty below the dueling floor.
    """
    window = await dueling_season_window(session, bench_version)
    if window is None or bench_version is None:
        return {}
    statement = (
        select(
            Score.agent_id,
            Score.validator_hotkey,
            Score.dueling_season_id,
            Score.composite,
            Score.n,
        )
        .join(Agent, Agent.agent_id == Score.agent_id)
        .where(
            Score.bench_version == bench_version,
            Agent.status == AgentStatus.SCORED,
            Agent.shadow.is_(False),
        )
    )
    if agent_ids is not None:
        ids = list(dict.fromkeys(agent_ids))
        if not ids:
            return {}
        statement = statement.where(Score.agent_id.in_(ids))
    grouped: dict[UUID, list[SeasonScore]] = {}
    for agent_id, validator_hotkey, season_id, composite, n in await session.execute(
        statement
    ):
        grouped.setdefault(agent_id, []).append(
            SeasonScore(
                validator_hotkey=validator_hotkey,
                dueling_season_id=season_id,
                composite=float(composite),
                n=int(n),
            )
        )
    return {
        agent_id: SeasonCoverage(
            agent_id=agent_id,
            bench_version=int(bench_version),
            window=window,
            scores=tuple(sorted(rows, key=lambda row: row.validator_hotkey)),
        )
        for agent_id, rows in grouped.items()
    }


async def expired_season_ledger_rows(
    session: AsyncSession,
    *,
    bench_version: int | None,
    agent_ids: Iterable[UUID] | None = None,
    limit: int | None = None,
) -> list[tuple[LedgerRow, SeasonCoverage]]:
    """Display rows for scored agents whose quorum expired with its season.

    Each row is the agent's last valid quorum median, exactly as stored, with
    ``eligible=False``: it is never an input to ranking, a pin or weights.
    Ordered by that median, best first, and cut to ``limit`` before any row
    is loaded. Only full, positive quorums qualify.
    """
    ranked = sorted(
        (
            value
            for value in (
                await season_coverage(
                    session, bench_version=bench_version, agent_ids=agent_ids
                )
            ).values()
            if value.expired and value.ranked and value.median is not None
        ),
        key=lambda value: (
            -(value.median.composite if value.median is not None else 0.0),
            str(value.agent_id),
        ),
    )
    if limit is not None:
        ranked = ranked[: max(0, limit)]
    if not ranked:
        return []
    medians = {
        value.agent_id: value.median.validator_hotkey
        for value in ranked
        if value.median is not None
    }
    scores = {
        (row.agent_id, row.validator_hotkey): row
        for row in (
            await session.execute(
                select(
                    Score.agent_id,
                    Score.validator_hotkey,
                    Score.composite,
                    Score.tool_mean,
                    Score.memory_mean,
                    Score.run_id,
                    Score.seed,
                    Score.signature,
                    Score.median_ms,
                    Score.n,
                    Score.dueling_season_id,
                ).where(
                    Score.bench_version == bench_version,
                    Score.agent_id.in_(list(medians)),
                )
            )
        ).all()
    }
    agents = {
        row.agent_id: row
        for row in (
            await session.execute(
                select(
                    Agent.agent_id,
                    Agent.miner_hotkey,
                    Agent.created_at,
                    Agent.sha256,
                    Agent.size_bytes,
                    Agent.status,
                    EvaluationPayment.miner_coldkey,
                )
                .outerjoin(
                    EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id
                )
                .where(Agent.agent_id.in_(list(medians)))
            )
        ).all()
    }
    out: list[tuple[LedgerRow, SeasonCoverage]] = []
    for value in ranked:
        score = scores.get((value.agent_id, medians.get(value.agent_id, "")))
        agent = agents.get(value.agent_id)
        if score is None or agent is None:
            continue
        out.append(
            (
                LedgerRow(
                    miner_hotkey=agent.miner_hotkey,
                    agent_id=value.agent_id,
                    composite=float(score.composite),
                    tool_mean=float(score.tool_mean),
                    memory_mean=float(score.memory_mean),
                    first_seen=_aware(agent.created_at),
                    sha256=agent.sha256,
                    size_bytes=agent.size_bytes,
                    run_id=score.run_id,
                    seed=int(score.seed),
                    validator_hotkey=score.validator_hotkey,
                    signature=score.signature,
                    status=agent.status,
                    miner_coldkey=agent.miner_coldkey,
                    bench_version=int(value.bench_version),
                    median_ms=int(score.median_ms or 0),
                    n=int(score.n),
                    eligible=False,
                    dueling_season_id=score.dueling_season_id,
                ),
                value,
            )
        )
    return out


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
