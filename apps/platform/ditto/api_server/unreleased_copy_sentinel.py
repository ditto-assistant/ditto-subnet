"""Shadow loop for the unreleased-copy signal.

Each tick does two bounded things:

1. **Index.** Agents with no distinctive-item row (uploads that predate the
   index, or whose upload-time extraction failed) are read from storage and
   indexed, oldest first.
2. **Evaluate.** Indexed agents without a signal are scored against every
   earlier agent, newest first, but only once the whole history before them is
   indexed, so a missing source can never read as a clean result.

Every evaluation is recorded in ``unreleased_copy_signals`` with ``mode =
"shadow"``: it never changes agent status, opens a hold, or touches a review.
Pairs at or above half the threshold get ``path:line`` evidence on both sides,
re-extracted from the two artifacts, so an operator can confirm a match
without downloading anything. Enforcement is a separate, later change.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
from concurrent.futures import Executor, ProcessPoolExecutor
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ditto.api_server.distinctive_content import (
    ITEMS_VERSION,
    ArtifactItems,
    Item,
    extract_items,
)
from ditto.api_server.fingerprint import _excluded_from_fingerprints
from ditto.api_server.source_inspect import MAX_TARBALL_BYTES
from ditto.api_server.unreleased_copy import (
    DEFAULT_THRESHOLD,
    SIGNAL_VERSION,
    CopySignal,
    attach_evidence,
    evaluate,
)
from ditto.db.queries.unreleased_copy import (
    agents_missing_items,
    load_context,
    pending_candidates,
    record_signal,
    signal_payload,
    store_items,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ditto.api_server.copy_court_triage import ArtifactStore

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30.0
DEFAULT_INDEX_BATCH = 8
DEFAULT_EVALUATE_BATCH = 4
EVIDENCE_MATCHES = 3
MODE = "shadow"
# Storage failures before an artifact is recorded as unreadable, so one
# missing blob cannot stall evaluation of everything uploaded after it.
MAX_READ_ATTEMPTS = 3


def extract_for_index(raw: bytes) -> dict[int, Item] | None:
    """Top-level (picklable) extraction used in the sentinel's worker process."""
    extracted = extract_items(raw, excluded=_excluded_from_fingerprints)
    return None if extracted is None else dict(extracted.items)


async def read_items(
    storage: ArtifactStore, agent_id: UUID, *, executor: Executor | None = None
) -> dict[int, Item] | None:
    """Read and extract one stored artifact.

    With ``executor`` (the sentinel's single-worker process pool) the CPU work
    runs outside this interpreter, so a multi-hour backfill of multi-megabyte
    artifacts never holds the API process's GIL. Without one it uses a thread,
    which is right for the one-off operator read.
    """
    raw = await storage.get_object(
        key=f"{agent_id}/agent.tar.gz", max_bytes=MAX_TARBALL_BYTES
    )
    if executor is not None:
        return await asyncio.get_running_loop().run_in_executor(
            executor, extract_for_index, raw
        )
    return await asyncio.to_thread(extract_for_index, raw)


async def with_evidence(
    signal: CopySignal,
    storage: ArtifactStore | None,
    *,
    threshold: float,
    executor: Executor | None = None,
) -> tuple[CopySignal, str]:
    """Attach ``path:line`` evidence when the best pair is worth reading."""
    if not signal.matches or signal.top_score < threshold / 2:
        return signal, "not_needed"
    if storage is None:
        return signal, "unavailable"
    try:
        candidate = await read_items(
            storage, signal.candidate_agent_id, executor=executor
        )
        matched: dict[UUID, dict[int, Item]] = {}
        for match in signal.matches[:EVIDENCE_MATCHES]:
            items = await read_items(storage, match.matched_agent_id, executor=executor)
            if items is not None:
                matched[match.matched_agent_id] = items
    except Exception as error:  # storage outage: keep the score, say so
        logger.warning("unreleased-copy evidence unavailable: %s", type(error).__name__)
        return signal, "unavailable"
    if candidate is None:
        return signal, "unavailable"
    return attach_evidence(signal, candidate, matched), "attached"


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
        self._last_started: datetime | None = None
        self._last_finished: datetime | None = None
        self._last_counters: dict[str, int] = {}
        self._last_error: str | None = None

    def runtime(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "mode": MODE,
            "signal_version": SIGNAL_VERSION,
            "threshold": self.threshold,
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
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

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
            try:
                await self.tick()
            except Exception as error:
                self._last_error = f"{type(error).__name__}: {error}"[:500]
                self._last_finished = datetime.now(UTC)
                logger.exception("unreleased-copy sentinel tick failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
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
        }
        if self._storage is not None:
            async with self._session_maker() as session:
                missing = await agents_missing_items(session, limit=self._index_batch)
            for agent in missing:
                await self._index(agent.agent_id, counters)
        async with self._session_maker() as session:
            candidates = await pending_candidates(
                session, signal_version=SIGNAL_VERSION, limit=self._evaluate_batch
            )
        for agent in candidates:
            async with self._session_maker() as session:
                context = await load_context(session, agent)
            if context is None:
                continue
            signal = evaluate(
                context.candidate, context.others, threshold=self.threshold
            )
            signal, evidence_status = await with_evidence(
                signal,
                self._storage,
                threshold=self.threshold,
                executor=self._extraction_executor(),
            )
            async with self._session_maker() as session, session.begin():
                await record_signal(
                    session,
                    signal=signal,
                    mode=MODE,
                    payload=signal_payload(signal, evidence_status=evidence_status),
                )
            counters["evaluated"] += 1
            counters["flagged"] += int(signal.flagged)
            if signal.flagged:
                logger.info(
                    "unreleased-copy shadow flag agent=%s top=%.2f matched=%s",
                    agent.agent_id,
                    signal.top_score,
                    signal.matches[0].matched_agent_id,
                )
        self._last_counters = {
            **counters,
            "elapsed_ms": int((monotonic() - started) * 1000),
        }
        self._last_finished = datetime.now(UTC)
        return counters

    async def _index(self, agent_id: UUID, counters: dict[str, int]) -> None:
        assert self._storage is not None
        error: str | None = None
        items = None
        try:
            items = await read_items(
                self._storage, agent_id, executor=self._extraction_executor()
            )
            if items is None:
                error = "unreadable artifact"
        except Exception as exc:
            attempts = self._read_failures.get(agent_id, 0) + 1
            self._read_failures[agent_id] = attempts
            if attempts < MAX_READ_ATTEMPTS:
                counters["read_retry"] += 1
                return
            error = f"storage: {type(exc).__name__}"
        async with self._session_maker() as session, session.begin():
            await store_items(
                session,
                agent_id=agent_id,
                extracted=None
                if items is None
                else ArtifactItems(ITEMS_VERSION, items),
                error=error,
            )
        self._read_failures.pop(agent_id, None)
        counters["unreadable" if error else "indexed"] += 1
