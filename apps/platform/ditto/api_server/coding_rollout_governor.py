"""Finite cohort scheduling and durable single-host stop state; no selection."""

from __future__ import annotations

import asyncio
import fcntl
import os
import re
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ditto.api_models.coding_bounded_rollout import BoundedRolloutApproval
from ditto.api_server.coding_hosted_runtime_io import (
    private_directory,
    read_json,
    read_private,
    write_private,
)


class RolloutError(ValueError):
    """Safe failure; active state is retained for explicit reconciliation."""


class RolloutState:
    def __init__(self, root: Path, approval_sha: str):
        private_directory(root)
        if re.fullmatch(r"[0-9a-f]{64}", approval_sha) is None:
            raise RolloutError("rollout approval identity invalid")
        self.root, self.approval_sha = root, approval_sha
        self.fd = os.open(
            root / ".lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        try:
            info = os.fstat(self.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise RolloutError("rollout lock is unsafe")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if (root / "active").exists() or (root / "active").is_symlink():
                raise RolloutError("rollout requires reconciliation")
        except BaseException:
            os.close(self.fd)
            raise
        self.active = False
        self.inode: tuple[int, int] | None = None

    def consume(self, plan: bytes | None = None) -> None:
        if self.active:
            raise RolloutError("rollout already consumed")
        write_private(
            self.root / (self.approval_sha + ".consumed"),
            (self.approval_sha + "\n").encode(),
        )
        if plan is not None:
            write_private(self.root / (self.approval_sha + ".plan.json"), plan)
        write_private(self.root / "active", (self.approval_sha + "\n").encode())
        info = (self.root / "active").lstat()
        self.inode = (info.st_dev, info.st_ino)
        self.active = True

    def finish(self, report: bytes) -> None:
        if (
            not self.active
            or read_private(self.root / "active", 128)
            != (self.approval_sha + "\n").encode()
        ):
            raise RolloutError("rollout active identity differs")
        info = (self.root / "active").lstat()
        if (info.st_dev, info.st_ino) != self.inode:
            raise RolloutError("rollout active marker changed")
        write_private(self.root / (self.approval_sha + ".result.json"), report)
        (self.root / "active").unlink()
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self.active = False

    def record(self, index: int, phase: str, body: bytes) -> None:
        if (
            not self.active
            or type(index) is not int
            or not 0 <= index < 64
            or phase not in {"launch", "finish", "failure"}
        ):
            raise RolloutError("rollout progress identity invalid")
        write_private(self.root / f"{self.approval_sha}.{index:02}.{phase}.json", body)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def inspect_state(root: Path, approval_sha: str) -> dict:
    """Read only named bounded records; retained state is not process liveness."""
    private_directory(root)
    if re.fullmatch(r"[0-9a-f]{64}", approval_sha) is None:
        raise RolloutError("rollout approval identity invalid")

    def present(name: str) -> bool:
        try:
            read_private(root / name, 128)
            return True
        except FileNotFoundError:
            return False

    def projection(value: object) -> dict:
        if not isinstance(value, dict):
            raise RolloutError("rollout progress invalid")
        result = {}
        for key in ("evaluation_id", "attempt_id"):
            if key in value:
                raw = value[key]
                if (
                    not isinstance(raw, str)
                    or str(UUID(raw)) != raw
                    or not UUID(raw).int
                ):
                    raise RolloutError("rollout progress identity invalid")
                result[key] = raw
        for key in ("terminal_sha256", "outcome"):
            if key in value:
                raw = value[key]
                if not isinstance(raw, str) or (
                    key == "terminal_sha256"
                    and re.fullmatch(r"[0-9a-f]{64}", raw) is None
                ):
                    raise RolloutError("rollout progress outcome invalid")
                if key == "outcome" and raw not in {
                    "completed",
                    "candidate_failure",
                    "infrastructure_failure",
                    "integrity_failure",
                }:
                    raise RolloutError("rollout progress outcome invalid")
                result[key] = raw
        return result

    records = []
    for index in range(64):
        for phase in ("launch", "finish", "failure"):
            try:
                value = read_json(
                    root / f"{approval_sha}.{index:02}.{phase}.json", 8192
                )
            except FileNotFoundError:
                continue
            records.append(
                {"index": index, "phase": phase, "record": projection(value)}
            )
    try:
        result = read_json(root / f"{approval_sha}.result.json")
    except FileNotFoundError:
        result = None
    if result is not None:
        if (
            not isinstance(result, dict)
            or result.get("approval_sha256") != approval_sha
            or result.get("schema") != "dittobench-coding-bounded-rollout-result-v2"
            or result.get("weight_eligible") is not False
            or result.get("shadow_only") is not True
            or not isinstance(result.get("attempts"), list)
            or len(result["attempts"]) > 64
        ):
            raise RolloutError("rollout result invalid")
        cost = result.get("reserved_cost_ceiling_usd_micros")
        if type(cost) is not int or not 1 <= cost <= 6_400_000_000:
            raise RolloutError("rollout cost record invalid")
        result = {
            "attempts": [projection(item) for item in result["attempts"]],
            "reserved_cost_ceiling_usd_micros": cost,
            "weight_eligible": False,
            "shadow_only": True,
        }
    try:
        plan = read_json(root / f"{approval_sha}.plan.json", 8192)
    except FileNotFoundError:
        plan = None
    if plan is not None:
        limits = {
            "max_attempts": 64,
            "max_parallel": 4,
            "reserved_cost_ceiling_usd_micros": 6_400_000_000,
            "expires_at_unix": 253402300799,
        }
        if not isinstance(plan, dict) or any(
            type(plan.get(key)) is not int or not 1 <= plan[key] <= maximum
            for key, maximum in limits.items()
        ):
            raise RolloutError("rollout plan record invalid")
        plan = {key: plan[key] for key in limits}
    return {
        "schema": "dittobench-coding-bounded-rollout-status-v2",
        "approval_sha256": approval_sha,
        "consumed": present(approval_sha + ".consumed"),
        "active_or_reconciliation_required": present("active"),
        "process_liveness_verified": False,
        "records": records,
        "plan": plan,
        "result": result,
        "shadow_only": True,
        "weight_eligible": False,
    }


@dataclass(frozen=True)
class AttemptOutcome:
    terminal_sha256: str
    outcome: str


async def govern(
    approval: BoundedRolloutApproval,
    execute: Callable[[int], Awaitable[AttemptOutcome]],
    stop: asyncio.Event,
    *,
    drain_seconds: float = 1800,
    resources: tuple[str, ...] | None = None,
) -> list[AttemptOutcome]:
    """Never retry an item. Stop admission first, then cancel/drain active runtimes."""
    start_wall, start_mono = time.time(), time.monotonic()
    if not approval.issued_at_unix <= start_wall < approval.expires_at_unix:
        raise RolloutError("rollout approval is not current")
    deadline = start_mono + approval.expires_at_unix - start_wall
    pending: dict[asyncio.Task[AttemptOutcome], int] = {}
    records: dict[int, AttemptOutcome] = {}
    remaining = list(range(len(approval.attempts)))
    if resources is None:
        resources = tuple(str(i) for i in remaining)
    if len(resources) != len(remaining) or any(
        not isinstance(key, str) or not key for key in resources
    ):
        raise RolloutError("rollout resource identities invalid")
    wake = asyncio.create_task(stop.wait())

    async def invoke(index: int) -> AttemptOutcome:
        return await execute(index)

    def current() -> bool:
        return (
            not stop.is_set()
            and time.time() < approval.expires_at_unix
            and time.monotonic() < deadline
        )

    try:
        while remaining or pending:
            if not current():
                raise RolloutError("rollout stopped or expired")
            while remaining and len(pending) < approval.max_parallel:
                busy = {resources[index] for index in pending.values()}
                index = next(
                    (index for index in remaining if resources[index] not in busy), None
                )
                if index is None:
                    break
                if (
                    not current()
                    or time.time() >= approval.attempts[index].deadline_unix
                ):
                    raise RolloutError("rollout attempt expired before launch")
                pending[asyncio.create_task(invoke(index))] = index
                remaining.remove(index)
            done, _ = await asyncio.wait(
                [*pending, wake],
                return_when=asyncio.FIRST_COMPLETED,
                timeout=max(
                    0,
                    min(
                        deadline - time.monotonic(),
                        approval.expires_at_unix - time.time(),
                    ),
                ),
            )
            if not done or wake in done:
                raise RolloutError("rollout stopped or expired")
            # Check all completed tasks before admitting any replacement work.
            for task in tuple(pending):
                if task not in done:
                    continue
                item = pending.pop(task)
                result = task.result()
                if (
                    not isinstance(result, AttemptOutcome)
                    or re.fullmatch(r"[0-9a-f]{64}", result.terminal_sha256) is None
                    or result.outcome
                    not in (
                        {"completed", "candidate_failure"}
                        if approval.allow_candidate_failure
                        else {"completed"}
                    )
                ):
                    raise RolloutError("rollout attempt did not complete acceptably")
                records[item] = result
        if not current():
            raise RolloutError("rollout expired before acceptance")
        return [records[i] for i in range(len(approval.attempts))]
    finally:
        wake.cancel()
        await asyncio.gather(wake, return_exceptions=True)
        for task in pending:
            task.cancel()
        if pending:
            # No new work during drain; cancellation resistance is not success.
            drained, unresolved = await asyncio.wait(pending, timeout=drain_seconds)
            for task in drained:
                if not task.cancelled():
                    task.exception()
            if unresolved:
                raise RolloutError("rollout drain unconfirmed")
