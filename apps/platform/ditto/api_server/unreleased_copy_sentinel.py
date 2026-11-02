"""Shadow loop for the unreleased-copy signal.

Each tick does two bounded things:

1. **Index.** Agents with no distinctive-item row (uploads that predate the
   index, or whose upload-time extraction failed) are read from storage and
   indexed, oldest first.
2. **Evaluate.** Indexed agents without a signal are scored against every
   earlier agent, newest first, but only once the whole history before them is
   indexed, so a missing source can never read as a clean result.

The audited mode (:func:`ditto.db.queries.unreleased_copy.effective_settings`)
decides what happens next. ``off`` does nothing; ``shadow`` records each
evaluation in ``unreleased_copy_signals``; ``enforce`` also routes a flag on an
agent uploaded after enforcement began into a copy-kind ATH hold (now if it is
scored, at score finalization otherwise) for an operator ruling. Nothing here
ever rejects.
Pairs at or above half the threshold get ``path:line`` evidence on both sides,
re-extracted from the two artifacts, so an operator can confirm a match
without downloading anything. Enforcement is a separate, later change.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
from array import array
from concurrent.futures import Executor, ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ditto.api_server import unreleased_copy_worker as worker
from ditto.api_server.source_inspect import MAX_TARBALL_BYTES
from ditto.api_server.unreleased_copy import (
    DEFAULT_THRESHOLD,
    SIGNAL_VERSION,
    CopySignal,
)
from ditto.db.queries.unreleased_copy import (
    EffectiveSettings,
    SignalContext,
    agents_missing_items,
    effective_settings,
    enforce_signal,
    index_rows_for,
    load_context,
    pending_candidates,
    record_enforcement,
    record_signal,
    released_agent_ids,
    signal_payload,
    store_items,
    stream_index_rows,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ditto.api_server.copy_court_triage import ArtifactStore

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30.0
# Extraction runs in a separate process, so the batch bounds wall time and
# storage reads per tick, not API latency.
DEFAULT_INDEX_BATCH = 24
DEFAULT_EVALUATE_BATCH = 6
BACKLOG_INTERVAL_SECONDS = 1.0
INDEX_WORKERS = 2
EVIDENCE_MATCHES = 3
# Released artifacts read into the worker's fuzzy cache per idle tick.
WARM_RELEASED_BATCH = 8
# Storage failures before an artifact is recorded as unreadable, so one
# missing blob cannot stall evaluation of everything uploaded after it.
MAX_READ_ATTEMPTS = 3


async def _read(storage: ArtifactStore, agent_id: UUID | str) -> bytes:
    return await storage.get_object(
        key=f"{agent_id}/agent.tar.gz", max_bytes=MAX_TARBALL_BYTES
    )


async def score_signal(
    context: SignalContext,
    storage: ArtifactStore | None,
    *,
    threshold: float,
    call: Callable[..., Any],
) -> tuple[CopySignal, str]:
    """Score, refine against released text, and attach evidence.

    ``call(fn, *args)`` runs one :mod:`unreleased_copy_worker` function (in the
    worker process for the sentinel); this coroutine only does storage reads.
    The fuzzy released pass and evidence run when the best pair is at least half
    the threshold, since only those can flag.
    """
    signal = await call(
        worker.score, context.candidate, context.others, context.overlaps, threshold
    )
    if not signal.matches or signal.top_score < threshold / 2:
        return await call(worker.truncate, signal), "not_needed"
    if storage is None:
        return await call(worker.truncate, signal), "unavailable"
    try:
        released = [str(a) for a in context.released_before]
        for agent_id in await call(worker.missing_released, released):
            await call(worker.add_released, agent_id, await _read(storage, agent_id))
        refined = await call(
            worker.refine,
            signal,
            await _read(storage, signal.candidate_agent_id),
            released,
            context.owner_names,
        )
        if refined is None:
            return await call(worker.truncate, signal), "unavailable"
        matched = {
            str(m.matched_agent_id): await _read(storage, m.matched_agent_id)
            for m in refined.matches[:EVIDENCE_MATCHES]
        }
        return await call(worker.attach, refined, matched), "attached"
    except Exception as error:  # storage outage: keep the score, say so
        logger.warning("unreleased-copy evidence unavailable: %s", type(error).__name__)
        return await call(worker.truncate, signal), "unavailable"


async def inline_call(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a worker function on a thread (tests, one-off operator reads)."""
    return await asyncio.to_thread(fn, *args)


class UnreleasedCopySentinel:
    def __init__(
        self,
        *,
        session_maker: async_sessionmaker,
        storage: ArtifactStore | None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        index_batch: int = DEFAULT_INDEX_BATCH,
        evaluate_batch: int = DEFAULT_EVALUATE_BATCH,
        threshold: float = DEFAULT_THRESHOLD,
        isolate_extraction: bool = True,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("unreleased-copy sentinel interval must be positive")
        self._session_maker = session_maker
        self._storage = storage
        self._interval_seconds = interval_seconds
        self._index_batch = index_batch
        self._evaluate_batch = evaluate_batch
        self.threshold = threshold
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._read_failures: dict[UUID, int] = {}
        self._isolate_extraction = isolate_extraction
        self._executor: Executor | None = None
        self._index_executor: Executor | None = None
        # Rows the scoring worker holds, by ``indexed_at`` watermark.
        self._worker_watermark: datetime | None = None
        self._worker_rows = 0
        self._last_started: datetime | None = None
        self._last_finished: datetime | None = None
        self._last_counters: dict[str, int] = {}
        self._last_error: str | None = None
        self._last_settings: EffectiveSettings | None = None
        # The scoring worker caches one candidate between refine and attach, so
        # a tick and an operator's live read must not interleave there.
        self._score_lock = asyncio.Lock()

    async def score(
        self, context: SignalContext, *, threshold: float
    ) -> tuple[CopySignal, str]:
        """:func:`score_signal` on this sentinel's worker, one candidate at a time."""
        async with self._score_lock:
            return await score_signal(
                context, self._storage, threshold=threshold, call=self.call
            )

    def runtime(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "mode": self._last_settings.mode if self._last_settings else "shadow",
            "signal_version": SIGNAL_VERSION,
            "threshold": (
                self._last_settings.threshold if self._last_settings else self.threshold
            ),
            "interval_seconds": self._interval_seconds,
            "storage_configured": self._storage is not None,
            "last_tick_started_at": self._last_started,
            "last_tick_finished_at": self._last_finished,
            "last_tick_counters": dict(self._last_counters),
            "last_tick_error": self._last_error,
        }

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="unreleased-copy-sentinel"
            )

    async def aclose(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task
        for executor in (self._executor, self._index_executor):
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
        self._executor = None
        self._index_executor = None

    async def call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run one worker function in the worker process (or a thread)."""
        executor = self._extraction_executor()
        if executor is None:
            return await asyncio.to_thread(fn, *args)
        return await asyncio.get_running_loop().run_in_executor(executor, fn, *args)

    async def sync_worker_index(self) -> None:
        """Stream index rows the scoring worker lacks (all of them after a restart)."""
        if (
            await self.call(worker.index_count) < self._worker_rows
            or not self._worker_rows
        ):
            self._worker_watermark = None  # fresh or restarted worker: full load
        # A small overlap with the previous watermark absorbs clock skew between
        # writers; re-sending a row is idempotent.
        since = (
            self._worker_watermark - timedelta(minutes=5)
            if self._worker_watermark is not None
            else None
        )
        async with self._session_maker() as session:
            async for rows in stream_index_rows(session, since=since, batch=100):
                self._worker_rows = await self.call(
                    worker.index_put,
                    [(str(a), array("q", keys).tobytes()) for a, keys, _ in rows],
                )
                newest = max(t for _, _, t in rows)
                if self._worker_watermark is None or newest > self._worker_watermark:
                    self._worker_watermark = newest

    async def warm(self, *, limit: int = WARM_RELEASED_BATCH) -> int:
        """Sync the worker index and cache a few more released fingerprints."""
        await self.sync_worker_index()
        if self._storage is None:
            return 0
        async with self._session_maker() as session:
            released = await released_agent_ids(session, at=datetime.now(UTC))
        missing = await self.call(worker.missing_released, [str(a) for a in released])
        for agent_id in missing[:limit]:
            raw = await _read(self._storage, agent_id)
            await self.call(worker.add_released, agent_id, raw)
        return min(len(missing), limit)

    async def overlap(self, keys: list[int], eligible: list[UUID]) -> dict[UUID, array]:
        """Overlap source for :func:`load_context`, served by the scoring worker."""
        packed = array("q", keys).tobytes()
        ids = [str(e) for e in eligible]
        found, missing = await self.call(worker.overlap, packed, ids)
        if missing:
            async with self._session_maker() as session:
                rows = await index_rows_for(session, [UUID(m) for m in missing])
            self._worker_rows = await self.call(
                worker.index_put,
                [(str(a), array("q", k).tobytes()) for a, k, _ in rows],
            )
            more, _ = await self.call(worker.overlap, packed, missing)
            found.update(more)
        return {UUID(a): v for a, v in found.items()}

    async def _index_call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Stateless extraction on the indexing pool (separate from scoring).

        Scoring keeps one worker because it caches released fingerprints and
        the candidate between calls; indexing has no state, so it fans out.
        """
        if not self._isolate_extraction:
            return await asyncio.to_thread(fn, *args)
        if self._index_executor is None:
            self._index_executor = ProcessPoolExecutor(
                max_workers=INDEX_WORKERS,
                mp_context=multiprocessing.get_context("spawn"),
            )
        return await asyncio.get_running_loop().run_in_executor(
            self._index_executor, fn, *args
        )

    def _extraction_executor(self) -> Executor | None:
        if not self._isolate_extraction:
            return None
        if self._executor is None:
            self._executor = ProcessPoolExecutor(
                max_workers=1, mp_context=multiprocessing.get_context("spawn")
            )
        return self._executor

    async def _run(self) -> None:
        while not self._stop.is_set():
            backlog = False
            try:
                counters = await self.tick()
                # A full batch means more work is waiting: run again almost at
                # once. Extraction is in the worker process, so draining a
                # backlog costs that process's core, not API latency.
                # Only an index backlog runs back to back: an evaluation scans
                # the whole index in Postgres, so it keeps the normal cadence.
                backlog = (
                    counters["indexed"] + counters["unreadable"] >= self._index_batch
                )
            except Exception as error:
                self._last_error = f"{type(error).__name__}: {error}"[:500]
                self._last_finished = datetime.now(UTC)
                logger.exception("unreleased-copy sentinel tick failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=BACKLOG_INTERVAL_SECONDS
                    if backlog
                    else self._interval_seconds,
                )
            except TimeoutError:
                continue

    async def tick(self) -> dict[str, int]:
        started = monotonic()
        self._last_started = datetime.now(UTC)
        self._last_error = None
        counters = {
            "indexed": 0,
            "unreadable": 0,
            "read_retry": 0,
            "evaluated": 0,
            "flagged": 0,
            "held": 0,
        }
        async with self._session_maker() as session:
            settings = await effective_settings(session)
        self._last_settings = settings
        if settings.mode == "off":
            self._finish(counters, started)
            return counters
        if self._storage is not None:
            async with self._session_maker() as session:
                missing = await agents_missing_items(session, limit=self._index_batch)
            gate = asyncio.Semaphore(INDEX_WORKERS)

            async def index_one(agent_id: UUID) -> None:
                async with gate:
                    await self._index(agent_id, counters)

            await asyncio.gather(*(index_one(a.agent_id) for a in missing))
            if missing:
                # Evaluation waits for the whole history before an agent to be
                # indexed anyway, so while a backlog remains, index only.
                self._finish(counters, started)
                return counters
        async with self._session_maker() as session:
            candidates = await pending_candidates(
                session, signal_version=SIGNAL_VERSION, limit=self._evaluate_batch
            )
        if candidates:
            await self.sync_worker_index()
        else:
            # Idle: keep the worker warm so the first upload (or an operator's
            # live read) after a restart does not pay for loading everything.
            try:
                await self.warm()
            except Exception as error:  # a cache, never a tick failure
                logger.warning("unreleased-copy warm-up: %s", type(error).__name__)
        for agent in candidates:
            async with self._session_maker() as session:
                context = await load_context(session, agent, overlap=self.overlap)
            if context is None:
                continue
            signal, evidence_status = await self.score(
                context, threshold=settings.threshold
            )
            async with self._session_maker() as session, session.begin():
                await record_signal(
                    session,
                    signal=signal,
                    mode=settings.mode,
                    payload=signal_payload(signal, evidence_status=evidence_status),
                )
                if signal.flagged and settings.mode == "enforce":
                    outcome = await enforce_signal(
                        session,
                        agent_id=agent.agent_id,
                        settings=settings,
                        now=datetime.now(UTC),
                    )
                    await record_enforcement(
                        session,
                        agent_id=agent.agent_id,
                        outcome=outcome,
                        settings=settings,
                    )
                    counters["held"] += int(outcome == "held")
            counters["evaluated"] += 1
            counters["flagged"] += int(signal.flagged)
            if signal.flagged:
                logger.info(
                    "unreleased-copy flag mode=%s agent=%s top=%.2f matched=%s",
                    settings.mode,
                    agent.agent_id,
                    signal.top_score,
                    signal.matches[0].matched_agent_id,
                )
        self._finish(counters, started)
        return counters

    def _finish(self, counters: dict[str, int], started: float) -> None:
        self._last_counters = {
            **counters,
            "elapsed_ms": int((monotonic() - started) * 1000),
        }
        self._last_finished = datetime.now(UTC)

    async def _index(self, agent_id: UUID, counters: dict[str, int]) -> None:
        assert self._storage is not None
        error: str | None = None
        keys: list[int] | None = None
        try:
            keys = await self.call(
                worker.index_keys, await _read(self._storage, agent_id)
            )
            if keys is None:
                error = "unreadable artifact"
        except Exception as exc:
            attempts = self._read_failures.get(agent_id, 0) + 1
            self._read_failures[agent_id] = attempts
            if attempts < MAX_READ_ATTEMPTS:
                counters["read_retry"] += 1
                return
            error = f"{type(exc).__name__}"
        async with self._session_maker() as session, session.begin():
            await store_items(session, agent_id=agent_id, keys=keys, error=error)
        self._read_failures.pop(agent_id, None)
        counters["unreadable" if error else "indexed"] += 1
