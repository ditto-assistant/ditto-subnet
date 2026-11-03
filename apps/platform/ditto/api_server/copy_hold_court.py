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
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.copy_court_settings import (
    AdminCopyCourtRuntime,
    CopyCourtSettings,
)
from ditto.api_server.copy_court_family import TRIAGE_REVISION
from ditto.api_server.copy_court_rules import (
    HOLD_CLASS_BYTE_IDENTICAL,
    HOLD_CLASS_NEAR_DUPLICATE,
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
from ditto.api_server.copy_court_triage import (
    ArtifactStore,
    TriageUnavailableError,
    triage_family,
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
RULES_REVISION = "rules-v1"
MAX_HOLD_ERRORS = 10
UNAVAILABLE_BACKOFF_SECONDS = 3600.0


class CopyHoldCourt:
    """Periodically triage pending copy holds into recommendation rows."""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker,
        interval_seconds: float = DEFAULT_COPY_COURT_INTERVAL_SECONDS,
        storage: ArtifactStore | None = None,
        app_state: Any = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("copy hold court interval must be positive")
        self._session_maker = session_maker
        self._interval_seconds = interval_seconds
        # Without storage the near-duplicate family triage cannot read
        # artifacts; those holds keep the rules engine's escalation.
        self._storage = storage
        # Enforce mode needs the app state for the crown projection.
        self._app_state = app_state
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_started: datetime | None = None
        self._last_finished: datetime | None = None
        self._last_counters: dict[str, int] = {}
        self._last_error: str | None = None
        self._last_hold_errors: list[dict[str, str]] = []
        # review_id -> monotonic time before which an unreadable hold is not
        # retried, so a permanently unreadable artifact cannot starve the
        # oldest-first queue.
        self._backoff: dict[UUID, float] = {}

    def runtime(self) -> AdminCopyCourtRuntime:
        """The last tick as this process saw it (Backroom visibility)."""
        return AdminCopyCourtRuntime(
            running=self._task is not None and not self._task.done(),
            interval_seconds=self._interval_seconds,
            last_tick_started_at=self._last_started,
            last_tick_finished_at=self._last_finished,
            last_tick_counters=dict(self._last_counters),
            last_tick_error=self._last_error,
            last_hold_errors=list(self._last_hold_errors),
        )

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
            try:
                await self.tick()
            except Exception as error:
                # A failed tick used to vanish under suppress(); keep the loop
                # alive but make the failure visible in logs and Backroom.
                self._last_error = f"{type(error).__name__}: {error}"[:500]
                self._last_finished = datetime.now(UTC)
                logger.exception("copy hold court tick failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue

    async def tick(self) -> dict[str, int]:
        """One bounded pass. Returns counters for tests and tick logs."""
        started = monotonic()
        self._last_started = datetime.now(UTC)
        self._last_error = None
        self._last_hold_errors = []
        async with self._session_maker() as session:
            settings, revision, checksum = await self._current_settings(session)
        counters = {
            "recommended": 0,
            "skipped_stranded": 0,
            "skipped_mode_off": 0,
            "unavailable": 0,
            "errors": 0,
            "applied": 0,
            "apply_skipped": 0,
        }
        if revision is None or settings.mode == "off":
            self._finish(counters)
            return counters
        async with self._session_maker() as session:
            candidates = await self._select_holds(
                session,
                settings_revision=revision,
                limit=settings.max_recommendations_per_tick * 3,
            )
        now = monotonic()
        rows = [
            (review, agent)
            for review, agent in candidates
            if self._backoff.get(review.review_id, 0.0) <= now
        ][: settings.max_recommendations_per_tick]
        for review, agent in rows:
            if agent.status != AgentStatus.ATH_PENDING_REVIEW:
                counters["skipped_stranded"] += 1
                continue
            try:
                async with self._session_maker() as session:
                    verdict, triage_revision, auto_clear = await self._triage(
                        session, review, agent
                    )
                    recommendation_id = await self._record(
                        session,
                        review=review,
                        agent=agent,
                        verdict=verdict,
                        triage_revision=triage_revision,
                        settings_revision=revision,
                        settings_checksum=checksum or "",
                    )
                counters["recommended"] += 1
                if (
                    settings.effective_mode(verdict.hold_class) == "enforce"
                    and verdict.hold_class == HOLD_CLASS_NEAR_DUPLICATE
                    and verdict.verdict == "clear"
                ):
                    applied = await self._enforce(
                        recommendation_id=recommendation_id,
                        agent=agent,
                        verdict=verdict,
                        auto_clear=auto_clear,
                    )
                    counters["applied" if applied else "apply_skipped"] += 1
            except TriageUnavailableError as error:
                counters["unavailable"] += 1
                self._backoff[review.review_id] = (
                    monotonic() + UNAVAILABLE_BACKOFF_SECONDS
                )
                self._note_hold_error(agent, f"unavailable: {error}")
                logger.warning(
                    "copy hold court: agent_id=%s triage unavailable: %s",
                    agent.agent_id,
                    error,
                )
            except Exception as error:
                # One bad hold never blocks the rest of the queue.
                counters["errors"] += 1
                self._note_hold_error(agent, f"{type(error).__name__}: {error}")
                logger.exception(
                    "copy hold court: agent_id=%s triage failed", agent.agent_id
                )
        logger.info(
            "copy hold court tick: %.1fs %s",
            monotonic() - started,
            counters,
        )
        self._finish(counters)
        return counters

    def _finish(self, counters: dict[str, int]) -> None:
        self._last_counters = dict(counters)
        self._last_finished = datetime.now(UTC)

    def _note_hold_error(self, agent: Agent, message: str) -> None:
        if len(self._last_hold_errors) < MAX_HOLD_ERRORS:
            self._last_hold_errors.append(
                {"agent_id": str(agent.agent_id), "error": message[:300]}
            )

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
        settings_revision: int,
        limit: int,
    ) -> list[tuple[AthReview, Agent]]:
        """Pending, not-yet-triaged copy-kind holds, oldest first.

        A hold is done for this settings revision once it carries a
        recommendation from its engine: any rules-engine class row except a
        near-duplicate escalation, which the family triage re-triages. Before
        this filter the oldest already-recommended hold hit the unique key on
        every tick and starved every newer hold (no recommendation landed
        from 2026-10-05 on).

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
        rec = AthCopyCourtRecommendation
        finished = [
            rec.triage_revision == TRIAGE_REVISION,
            and_(
                rec.triage_revision == RULES_REVISION,
                rec.hold_class != HOLD_CLASS_NEAR_DUPLICATE,
            ),
        ]
        if self._storage is None:
            # Without storage the family triage cannot run: the rules
            # escalation is the final word for this process.
            finished.append(rec.triage_revision == RULES_REVISION)
        done = exists().where(
            rec.review_id == AthReview.review_id,
            rec.settings_revision == settings_revision,
            or_(*finished),
        )
        rows = (
            await session.execute(
                select(AthReview, Agent)
                .join(Agent, Agent.agent_id == AthReview.agent_id)
                .where(
                    AthReview.status == "pending",
                    Agent.status == AgentStatus.ATH_PENDING_REVIEW,
                    copy_kind,
                    ~done,
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
    ) -> tuple[CourtVerdict, str, bool]:
        """``(verdict, triage revision, auto-clear eligible)`` for one hold."""
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
            return (
                escalate_verdict(
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
                ),
                RULES_REVISION,
                False,
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
            return byte_identical_verdict(candidate, ancestor), RULES_REVISION, False
        if hold_class == HOLD_CLASS_REPACK:
            return repack_verdict(candidate, ancestor), RULES_REVISION, False
        if hold_class == HOLD_CLASS_NEAR_DUPLICATE and self._storage is not None:
            result = await triage_family(
                session, self._storage, review=review, held=agent
            )
            return (
                CourtVerdict(
                    verdict=result.verdict.verdict,
                    hold_class=HOLD_CLASS_NEAR_DUPLICATE,
                    reason=result.verdict.reason,
                    citations=result.verdict.citations,
                    evidence=result.evidence,
                ),
                TRIAGE_REVISION,
                result.verdict.auto_clear_eligible,
            )
        return (
            escalate_verdict(
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
            ),
            RULES_REVISION,
            False,
        )

    async def preview(
        self, session: AsyncSession, review: AthReview, agent: Agent
    ) -> tuple[CourtVerdict, str, bool]:
        """Dry-run triage for any copy-kind review; records nothing."""
        return await self._triage(session, review, agent)

    async def _record(
        self,
        session: AsyncSession,
        *,
        review: AthReview,
        agent: Agent,
        verdict: CourtVerdict,
        triage_revision: str,
        settings_revision: int,
        settings_checksum: str,
    ) -> UUID:
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
            triage_revision=triage_revision,
            model=verdict.evidence.get("model"),
            prompt_revision=verdict.evidence.get("prompt_revision"),
        )
        session.add(row)
        await session.commit()
        return row.recommendation_id

    async def _enforce(
        self,
        *,
        recommendation_id: UUID,
        agent: Agent,
        verdict: CourtVerdict,
        auto_clear: bool,
    ) -> bool:
        """Apply the narrow deterministic family CLEAR through the ruling path.

        Only a near-duplicate CLEAR whose triage marked it auto-clear eligible
        is applied, and only when the guarded ATH ruling preview is ready,
        clears the pending hold in one step, and would not change the crown.
        Every outcome, applied or skipped, is written back to the
        recommendation so Backroom shows what the court did.
        """
        from ditto.api_server.endpoints.admin_ath_rulings import apply_court_clear

        if not auto_clear:
            outcome: dict[str, Any] = {
                "status": "skipped",
                "detail": "not auto-clear eligible: "
                + "; ".join(verdict.evidence.get("auto_clear_blockers") or []),
            }
        elif self._app_state is None:
            outcome = {
                "status": "skipped",
                "detail": "no app state for the crown projection",
            }
        else:
            async with self._session_maker() as session:
                outcome = await apply_court_clear(
                    session,
                    self._app_state,
                    agent_id=agent.agent_id,
                    expected_sha256=agent.sha256,
                    reason=(
                        f"{verdict.reason} Applied by the copy-hold court "
                        f"(recommendation {recommendation_id}) under the "
                        "operator's near_duplicate_mode=enforce setting."
                    ),
                    actor=COURT_ACTOR,
                )
        async with self._session_maker() as session:
            row = await session.get(AthCopyCourtRecommendation, recommendation_id)
            if row is not None:
                row.application = outcome
                row.applied_at = datetime.now(UTC)
                await session.commit()
        logger.info(
            "copy hold court enforce agent_id=%s recommendation=%s outcome=%s",
            agent.agent_id,
            recommendation_id,
            outcome.get("status"),
        )
        return outcome.get("status") == "applied"


__all__ = ["COURT_ACTOR", "DEFAULT_COPY_COURT_INTERVAL_SECONDS", "CopyHoldCourt"]
