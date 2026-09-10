"""Shadow-first triage court for copy-kind ATH holds.

Periodically selects every pending copy hold, classifies it from stored
artifact data, and records one non-authoritative recommendation per
(review, settings revision). A recommendation never changes agent status or
review resolution: in shadow mode the operator queue is unchanged and the
recommendation feed is evidence for the operator. Enforce mode resolves
through the same guarded callable an operator uses, citing the
recommendation id in the resolution reason.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.copy_court_settings import CopyCourtSettings
from ditto.api_server.copy_court_rules import (
    HOLD_CLASS_BYTE_IDENTICAL,
    HOLD_CLASS_REPACK,
    HOLD_CLASS_UNKNOWN,
    AncestorIdentity,
    CandidateIdentity,
    CourtVerdict,
    byte_identical_verdict,
    classify_hold,
    escalate_verdict,
    repack_verdict,
)
from ditto.db.models import (
    Agent,
    AgentStatus,
    AthCopyCourtRecommendation,
    AthReview,
    CopyCourtSettingsRevision,
)
from ditto.db.queries.payments import get_miner_coldkeys_for_agents

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)

DEFAULT_COPY_COURT_INTERVAL_SECONDS = 300
COURT_ACTOR = "platform:copy-hold-court"


class CopyHoldCourt:
    """Periodically triage pending copy holds into recommendation rows."""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker,
        interval_seconds: float = DEFAULT_COPY_COURT_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("copy hold court interval must be positive")
        self._session_maker = session_maker
        self._interval_seconds = interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="copy-hold-court")

    async def aclose(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            with suppress(Exception):
                await self.tick()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue

    async def tick(self) -> dict[str, int]:
        """One bounded pass. Returns counters for tests and tick logs."""
        started = monotonic()
        async with self._session_maker() as session:
            settings, revision, checksum = await self._current_settings(session)
        counters = {"recommended": 0, "skipped_stranded": 0, "skipped_mode_off": 0}
        if revision is None or settings.mode == "off":
            return counters
        async with self._session_maker() as session:
            rows = await self._select_holds(
                session, limit=settings.max_recommendations_per_tick
            )
            for review, agent in rows:
                if agent.status != AgentStatus.ATH_PENDING_REVIEW:
                    counters["skipped_stranded"] += 1
                    continue
                verdict = await self._triage(session, review, agent)
                # Class mode gates only mechanical verdicts; an escalate is
                # always recorded when the court runs at all — it is operator
                # evidence, not a resolution.
                effective = (
                    settings.mode
                    if verdict.verdict == "escalate"
                    else settings.effective_mode(verdict.hold_class)
                )
                if effective == "off":
                    counters["skipped_mode_off"] += 1
                    continue
                await self._record(
                    session,
                    review=review,
                    agent=agent,
                    verdict=verdict,
                    settings_revision=revision,
                    settings_checksum=checksum or "",
                )
                counters["recommended"] += 1
                if effective == "enforce":
                    await self._enforce(
                        session, review=review, agent=agent, verdict=verdict
                    )
        logger.info(
            "copy hold court tick: %.1fs %s",
            monotonic() - started,
            counters,
        )
        return counters

    async def _current_settings(
        self, session: AsyncSession
    ) -> tuple[CopyCourtSettings, int | None, str | None]:
        row = await session.scalar(
            select(CopyCourtSettingsRevision)
            .order_by(CopyCourtSettingsRevision.revision.desc())
            .limit(1)
        )
        if row is None:
            return CopyCourtSettings(), None, None
        settings = CopyCourtSettings.model_validate(row.settings)
        return settings, row.revision, row.checksum

    async def _select_holds(
        self,
        session: AsyncSession,
        *,
        limit: int,
    ) -> list[tuple[AthReview, Agent]]:
        """Pending copy-kind holds, oldest first, generation-unfiltered.

        The kind filter matches the queue's fallback exactly: legacy rows with
        no ``review_kind`` key render as ``copy`` and must not be dropped.
        Upload-time copy holds have no scores, so the selection never filters
        on score presence.
        """
        stored_kind = AthReview.algorithm_provenance["review_kind"].as_string()
        known_kinds = (
            "copy",
            "benchmark_overfit",
            "deferred_source_review",
            "anomalous_score",
        )
        copy_kind = (
            stored_kind.is_(None)
            | stored_kind.not_in(known_kinds)
            | (stored_kind == "copy")
        )
        rows = (
            await session.execute(
                select(AthReview, Agent)
                .join(Agent, Agent.agent_id == AthReview.agent_id)
                .where(
                    AthReview.status == "pending",
                    Agent.status == AgentStatus.ATH_PENDING_REVIEW,
                    copy_kind,
                )
                .order_by(AthReview.opened_at.asc(), AthReview.review_id.asc())
                .limit(limit)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def _triage(
        self,
        session: AsyncSession,
        review: AthReview,
        agent: Agent,
    ) -> CourtVerdict:
        candidate = CandidateIdentity(
            agent_id=str(agent.agent_id),
            miner_hotkey=agent.miner_hotkey,
            sha256=agent.sha256,
            normalized_source_hash=agent.normalized_source_hash,
        )
        ancestor_row = None
        if review.original_duplicate_of is not None:
            ancestor_row = await session.get(Agent, review.original_duplicate_of)
        if ancestor_row is None:
            return escalate_verdict(
                HOLD_CLASS_UNKNOWN,
                "Matched reference agent could not be loaded; the hold's "
                "identity claim cannot be verified from stored data. Left "
                "for operator review.",
                {
                    "duplicate_of": (
                        str(review.original_duplicate_of)
                        if review.original_duplicate_of is not None
                        else None
                    )
                },
            )
        coldkeys = await get_miner_coldkeys_for_agents(
            session, agent_ids={agent.agent_id, ancestor_row.agent_id}
        )
        prior_review = await session.scalar(
            select(AthReview).where(
                AthReview.agent_id == ancestor_row.agent_id,
                AthReview.status == "resolved",
                AthReview.resolution == "reject",
            )
        )
        ancestor = AncestorIdentity(
            agent_id=str(ancestor_row.agent_id),
            name=ancestor_row.name,
            version=ancestor_row.version,
            miner_hotkey=ancestor_row.miner_hotkey,
            sha256=ancestor_row.sha256,
            normalized_source_hash=ancestor_row.normalized_source_hash,
            reject_reason=(
                prior_review.resolution_reason if prior_review is not None else None
            ),
            resolved_at=(
                prior_review.resolved_at.isoformat()
                if prior_review is not None and prior_review.resolved_at is not None
                else None
            ),
        )
        hold_class = classify_hold(
            review.original_reason,
            candidate,
            ancestor if prior_review is not None else None,
        )
        if hold_class == HOLD_CLASS_BYTE_IDENTICAL:
            return byte_identical_verdict(candidate, ancestor)
        if hold_class == HOLD_CLASS_REPACK:
            return repack_verdict(candidate, ancestor)
        return escalate_verdict(
            hold_class,
            "Not mechanically decidable from stored artifact data; left for "
            "operator review with the recorded hold evidence.",
            {
                "original_reason": review.original_reason,
                "duplicate_of": (
                    str(review.original_duplicate_of)
                    if review.original_duplicate_of is not None
                    else None
                ),
                "candidate_sha256": candidate.sha256,
                "candidate_coldkey": coldkeys.get(agent.agent_id),
            },
        )

    async def _record(
        self,
        session: AsyncSession,
        *,
        review: AthReview,
        agent: Agent,
        verdict: CourtVerdict,
        settings_revision: int,
        settings_checksum: str,
    ) -> None:
        row = AthCopyCourtRecommendation(
            recommendation_id=uuid4(),
            review_id=review.review_id,
            agent_id=agent.agent_id,
            verdict=verdict.verdict,
            hold_class=verdict.hold_class,
            reason=verdict.reason,
            citations=verdict.citations,
            evidence=verdict.evidence,
            settings_revision=settings_revision,
            settings_checksum=settings_checksum,
            model=verdict.evidence.get("model"),
            prompt_revision=verdict.evidence.get("prompt_revision"),
        )
        session.add(row)
        await session.commit()

    async def _enforce(
        self,
        session: AsyncSession,
        *,
        review: AthReview,
        agent: Agent,
        verdict: CourtVerdict,
    ) -> None:
        """Enforce mode: resolve through the operator path, citing the court.

        Deliberately unimplemented while every class runs in shadow: the
        mutation core of ``resolve_copy_review`` is extracted into a shared
        callable in the same change that first enables enforce on one class.
        """
        raise NotImplementedError(
            "copy-hold court enforce mode is not enabled; resolve "
            f"{agent.agent_id} through resolve_copy_review"
        )


__all__ = ["COURT_ACTOR", "DEFAULT_COPY_COURT_INTERVAL_SECONDS", "CopyHoldCourt"]
