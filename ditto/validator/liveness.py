"""Liveness watchdog and live asyncio diagnostics for the validator worker.

A validator that stops heartbeating is worse than one that crashes. Platform
reads the stale heartbeat as an offline validator. When the silent validator is
the enforcing treasury fleet's managed member, Platform also refuses every
validator's ledger read (428) after ``TREASURY_FLEET_FRESHNESS`` (15 minutes),
so weights freeze subnet-wide. A crash is better because Docker's
``restart: unless-stopped`` recovers it within a minute.

:class:`LivenessWatchdog` turns a silent worker into a crash. It runs on its own
thread, so it still fires when the event loop is blocked as well as when every
coroutine waits on something that never completes. Before exiting it logs every
asyncio task's await chain and every thread's stack. ``py-spy`` cannot show
either: it sees the loop idle in ``select``.

:func:`install_task_dump_signal` logs the same dump on ``SIGQUIT`` without
exiting, so a live hang can be diagnosed before anyone restarts it. ``SIGUSR1``
and ``SIGUSR2`` are already the updater's drain and resume signals::

    docker kill --signal=QUIT src-ditto-subnet-1
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
import traceback
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# ``uv run`` (the image's PID 1) forwards it to the worker. The JVM and the Go
# runtime use the same signal for a stack dump.
TASK_DUMP_SIGNAL = signal.SIGQUIT
# EX_SOFTWARE. Any non-zero exit lets Docker's restart policy recover the worker.
WATCHDOG_EXIT_CODE = 70
# Default silence limit. It is well under the 15-minute treasury fleet
# freshness, which leaves time for the restart and its first heartbeat.
DEFAULT_LIVENESS_WATCHDOG_SECONDS = 600.0

_MAX_AWAIT_DEPTH = 64


def _frame_line(frame: Any) -> str:
    code = frame.f_code
    return (
        f'    File "{code.co_filename}", line {frame.f_lineno}, '
        f"in {getattr(code, 'co_qualname', code.co_name)}"
    )


def _describe_awaitable(awaitable: Any) -> str:
    if isinstance(awaitable, asyncio.Task):
        return f"task {awaitable.get_name()!r}"
    text = repr(awaitable)
    return text if len(text) <= 300 else f"{text[:297]}..."


def _await_chain(task: asyncio.Task[Any]) -> list[str]:
    """Return the task's suspended frames, outermost first, and its leaf future.

    ``Task.get_stack()`` stops at the task's own coroutine frame. Following
    ``cr_await`` also shows the inner coroutine that is suspended, such as
    ``Lock.acquire``, ``Event.wait`` or a shielded task, and the future that
    never resolves.
    """
    lines: list[str] = []
    current: Any = task.get_coro()
    for _ in range(_MAX_AWAIT_DEPTH):
        if current is None:
            break
        frame = (
            getattr(current, "cr_frame", None)
            or getattr(current, "gi_frame", None)
            or getattr(current, "ag_frame", None)
        )
        if frame is not None:
            lines.append(_frame_line(frame))
        awaited = (
            getattr(current, "cr_await", None)
            or getattr(current, "gi_yieldfrom", None)
            or getattr(current, "ag_await", None)
        )
        # ``await future`` suspends on the future's iterator, which does not
        # expose the future. The task records that future as ``_fut_waiter``.
        if awaited is None or not (
            hasattr(awaited, "cr_frame")
            or hasattr(awaited, "gi_frame")
            or hasattr(awaited, "ag_frame")
        ):
            break
        current = awaited
    waiter = getattr(task, "_fut_waiter", None)
    if waiter is not None:
        lines.append(f"    awaiting {_describe_awaitable(waiter)}")
    return lines


def format_asyncio_task_dump(loop: asyncio.AbstractEventLoop | None = None) -> str:
    """Render every pending task on ``loop`` with its full await chain."""
    if loop is None:
        loop = asyncio.get_running_loop()
    tasks = sorted(asyncio.all_tasks(loop), key=lambda task: task.get_name())
    lines = [f"asyncio task dump: {len(tasks)} pending task(s)"]
    for task in tasks:
        lines.append(f"  task {task.get_name()!r} coro={task.get_coro()!r}")
        chain = _await_chain(task)
        lines.extend(chain or ["    (no suspended frame)"])
    return "\n".join(lines)


def format_thread_dump() -> str:
    """Render every Python thread's current stack."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    lines = ["thread dump:"]
    for ident, frame in sorted(sys._current_frames().items()):
        lines.append(f"  thread {names.get(ident, '?')!r} ident={ident}")
        lines.extend(
            f"    {line}"
            for entry in traceback.format_stack(frame)
            for line in entry.rstrip().splitlines()
        )
    return "\n".join(lines)


def log_runtime_dump(
    reason: str, loop: asyncio.AbstractEventLoop | None = None
) -> None:
    """Log the task and thread dumps; never raises."""
    try:
        tasks = format_asyncio_task_dump(loop)
    except Exception as error:  # noqa: BLE001 - a diagnostic must not crash
        tasks = f"asyncio task dump failed: {type(error).__name__}: {error}"
    logger.warning("%s\n%s\n%s", reason, tasks, format_thread_dump())
    _flush_logging()


def install_task_dump_signal(loop: asyncio.AbstractEventLoop) -> None:
    """Log a live task dump on :data:`TASK_DUMP_SIGNAL`; the worker keeps running."""
    with contextlib.suppress(NotImplementedError, AttributeError, RuntimeError):
        loop.add_signal_handler(
            TASK_DUMP_SIGNAL,
            lambda: log_runtime_dump(
                f"{signal.Signals(TASK_DUMP_SIGNAL).name} received; "
                "dumping validator runtime state",
                loop,
            ),
        )


def _flush_logging() -> None:
    for name in ("", "ditto"):
        for handler in logging.getLogger(name).handlers:
            with contextlib.suppress(Exception):
                handler.flush()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()


class LivenessWatchdog:
    """Exit the process when the worker stops attempting heartbeats.

    ``age_seconds`` returns how long ago the worker last finished a heartbeat
    attempt. A refused or failed heartbeat still counts, so a Platform outage
    cannot cause a restart loop. Only a worker that stopped trying exits.
    """

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        age_seconds: Callable[[], float],
        timeout_seconds: float,
        check_seconds: float = 15.0,
        loop_answer_seconds: float = 5.0,
        exit_process: Callable[[int], object] = os._exit,
    ) -> None:
        self._loop = loop
        self._age_seconds = age_seconds
        self._timeout_seconds = timeout_seconds
        self._check_seconds = check_seconds
        self._loop_answer_seconds = loop_answer_seconds
        self._exit_process = exit_process
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self.fired = threading.Event()

    def start(self) -> None:
        if self._timeout_seconds <= 0 or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="validator-liveness-watchdog", daemon=True
        )
        self._thread.start()
        logger.info(
            "liveness watchdog armed: exit %d after %.0fs without a heartbeat "
            "attempt; send %s for a live task dump",
            WATCHDOG_EXIT_CODE,
            self._timeout_seconds,
            signal.Signals(TASK_DUMP_SIGNAL).name,
        )

    def stop(self) -> None:
        self._stopped.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=self._check_seconds + 1)

    def _run(self) -> None:
        while not self._stopped.wait(self._check_seconds):
            try:
                age = self._age_seconds()
            except Exception:  # noqa: BLE001 - keep watching
                logger.exception("liveness watchdog could not read heartbeat age")
                continue
            if age >= self._timeout_seconds:
                self._fire(age)
                return

    def _fire(self, age: float) -> None:
        self.fired.set()
        reason = (
            f"liveness watchdog: no heartbeat attempt for {age:.0f}s "
            f"(limit {self._timeout_seconds:.0f}s); exiting "
            f"{WATCHDOG_EXIT_CODE} so Docker restarts the worker"
        )
        logger.critical("%s", reason)
        # asyncio.all_tasks is safest on the loop's own thread. An async
        # deadlock leaves the loop free to answer. A blocked loop does not, and
        # then the dump is read from this thread instead.
        answered = threading.Event()

        def dump_on_loop() -> None:
            try:
                log_runtime_dump(reason, self._loop)
            finally:
                answered.set()

        with contextlib.suppress(RuntimeError):  # a closed loop cannot answer
            self._loop.call_soon_threadsafe(dump_on_loop)
        if not answered.wait(self._loop_answer_seconds):
            log_runtime_dump(
                f"{reason}; the event loop did not answer within "
                f"{self._loop_answer_seconds:.0f}s, so it is blocked",
                self._loop,
            )
        _flush_logging()
        # No graceful shutdown: it would await the same stuck work.
        self._exit_process(WATCHDOG_EXIT_CODE)
