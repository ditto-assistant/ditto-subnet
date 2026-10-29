"""Liveness watchdog and live asyncio task dumps for the validator worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading

import pytest

from ditto.validator import liveness
from ditto.validator.liveness import (
    TASK_DUMP_SIGNAL,
    WATCHDOG_EXIT_CODE,
    LivenessWatchdog,
    format_asyncio_task_dump,
    install_task_dump_signal,
)


async def _wait_until(predicate, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout=timeout)


async def test_task_dump_follows_the_await_chain_to_the_held_lock() -> None:
    lock = asyncio.Lock()
    await lock.acquire()

    async def waits_for_heartbeat_lock() -> None:
        async with lock:
            pass

    task = asyncio.create_task(waits_for_heartbeat_lock(), name="stuck-sweep")
    await asyncio.sleep(0)
    try:
        dump = format_asyncio_task_dump()
    finally:
        task.cancel()
        lock.release()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert "task 'stuck-sweep'" in dump
    # get_stack() alone stops at the task's own frame; the chain reaches the
    # inner Lock.acquire and the future that never resolves.
    assert "waits_for_heartbeat_lock" in dump
    assert "Lock.acquire" in dump
    assert "awaiting <Future pending" in dump


async def test_watchdog_dumps_tasks_and_exits_when_heartbeats_stop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exits: list[int] = []
    never = asyncio.Event()
    stuck = asyncio.create_task(never.wait(), name="validator-weights")
    watchdog = LivenessWatchdog(
        loop=asyncio.get_running_loop(),
        age_seconds=lambda: 601.0,
        timeout_seconds=600.0,
        check_seconds=0.01,
        exit_process=exits.append,
    )
    caplog.set_level(logging.WARNING, logger="ditto.validator.liveness")
    watchdog.start()
    try:
        await _wait_until(lambda: bool(exits))
    finally:
        watchdog.stop()
        stuck.cancel()

    assert exits == [WATCHDOG_EXIT_CODE]
    assert "no heartbeat attempt for 601s" in caplog.text
    assert "task 'validator-weights'" in caplog.text
    assert "thread dump:" in caplog.text


async def test_watchdog_dumps_from_its_thread_when_the_loop_is_blocked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exits: list[int] = []
    blocked_loop = asyncio.new_event_loop()  # never runs, like a blocked loop
    watchdog = LivenessWatchdog(
        loop=blocked_loop,
        age_seconds=lambda: 700.0,
        timeout_seconds=600.0,
        check_seconds=0.01,
        loop_answer_seconds=0.05,
        exit_process=exits.append,
    )
    caplog.set_level(logging.WARNING, logger="ditto.validator.liveness")
    try:
        watchdog.start()
        await _wait_until(lambda: bool(exits))
    finally:
        watchdog.stop()
        blocked_loop.close()

    assert exits == [WATCHDOG_EXIT_CODE]
    assert "event loop did not answer" in caplog.text


async def test_watchdog_stays_quiet_while_heartbeats_are_attempted() -> None:
    exits: list[int] = []
    reads = threading.Event()

    def fresh() -> float:
        reads.set()
        return 5.0

    watchdog = LivenessWatchdog(
        loop=asyncio.get_running_loop(),
        age_seconds=fresh,
        timeout_seconds=600.0,
        check_seconds=0.01,
        exit_process=exits.append,
    )
    watchdog.start()
    try:
        await _wait_until(reads.is_set)
        await asyncio.sleep(0.05)
    finally:
        watchdog.stop()
    assert exits == []

    disabled = LivenessWatchdog(
        loop=asyncio.get_running_loop(),
        age_seconds=lambda: 10_000.0,
        timeout_seconds=0,
        check_seconds=0.01,
        exit_process=exits.append,
    )
    disabled.start()
    await asyncio.sleep(0.05)
    disabled.stop()
    assert exits == []


async def test_sigquit_logs_a_live_dump_without_exiting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop = asyncio.get_running_loop()
    caplog.set_level(logging.WARNING, logger="ditto.validator.liveness")
    never = asyncio.Event()
    waiting = asyncio.create_task(never.wait(), name="validator-longmem")
    install_task_dump_signal(loop)
    try:
        os.kill(os.getpid(), TASK_DUMP_SIGNAL)
        await _wait_until(lambda: "SIGQUIT received" in caplog.text)
    finally:
        loop.remove_signal_handler(TASK_DUMP_SIGNAL)
        waiting.cancel()
    assert "task 'validator-longmem'" in caplog.text
    assert not waiting.done() or waiting.cancelled()


def test_module_documents_why_sigusr1_is_not_the_dump_signal() -> None:
    assert liveness.TASK_DUMP_SIGNAL.name == "SIGQUIT"
    assert "USR1" in (liveness.__doc__ or "")
