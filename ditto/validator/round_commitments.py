"""Publish generator-round seed commitments this validator is elected for.

The generator competition commits each round's seed before it opens
(``commitment = RoundCommitment(r, secret)``). Platform records it, and when
``onchain_commitment`` is on it elects up to three fresh validators per round
to put a public, block-stamped copy on chain: ``dgr1:{r}:{commitment}`` with
``Commitments.set_commitment`` from the validator's own Pylon identity.

This lane polls Platform for an elected job, publishes the exact payload,
reads its own commitment back until the chain shows it, and reports the block
(or the failure) to Platform. It runs beside the weight and scoring loops and
is best effort: every failure is logged and the lane keeps going. It never
holds a lock or flag the weight loop waits on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from ditto.api_models.generator_round_validator import (
    MAX_COMMITMENT_PAYLOAD_BYTES,
    ROUND_COMMITMENT_TAG,
    GeneratorRoundCommitmentJobResponse,
    round_commitment_payload,
)

logger = logging.getLogger(__name__)

POLL_SECONDS = 120.0
READBACK_TIMEOUT_SECONDS = 180.0
READBACK_INTERVAL_SECONDS = 6.0

LaneOutcome = Literal["unsupported", "idle", "confirmed", "failed", "error"]


def _enabled_by_env() -> bool:
    value = os.environ.get("VALIDATOR_ROUND_COMMITMENTS", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _payload_bytes(job: GeneratorRoundCommitmentJobResponse) -> bytes:
    """The exact bytes to publish, or ``ValueError`` for anything else."""
    expected = round_commitment_payload(job.round_index, job.commitment)
    if job.payload != expected or not expected.startswith(f"{ROUND_COMMITMENT_TAG}:"):
        raise ValueError("Platform offered a payload that is not the round commitment")
    data = expected.encode("ascii")
    if len(data) > MAX_COMMITMENT_PAYLOAD_BYTES:
        raise ValueError("round commitment payload exceeds the pallet limit")
    return data


class RoundCommitmentPublisher:
    """One validator's round-commitment lane (stateless between polls)."""

    def __init__(
        self,
        platform: Any,
        chain: Any,
        validator_hotkey: str,
        *,
        poll_seconds: float = POLL_SECONDS,
        readback_timeout: float = READBACK_TIMEOUT_SECONDS,
        readback_interval: float = READBACK_INTERVAL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._platform = platform
        self._chain = chain
        self._hotkey = validator_hotkey
        self._poll_seconds = poll_seconds
        self._readback_timeout = readback_timeout
        self._readback_interval = readback_interval
        self._sleep = sleep
        self._clock = clock
        self.last_outcome: LaneOutcome | None = None

    def supported(self) -> bool:
        """Whether this host can publish: a Pylon identity and the lane enabled.

        Advertised as the ``round_commitments`` heartbeat capability; Platform
        elects only validators that advertise it.
        """
        if not _enabled_by_env() or self._chain is None:
            return False
        if not callable(getattr(self._chain, "set_commitment", None)):
            return False
        if not callable(getattr(self._chain, "get_own_commitment", None)):
            return False
        if not callable(
            getattr(self._platform, "request_generator_round_commitment_job", None)
        ):
            return False
        has_identity = getattr(self._chain, "has_identity", None)
        return bool(has_identity()) if callable(has_identity) else True

    async def run_once(self) -> LaneOutcome:
        """Publish at most one elected commitment. Never raises."""
        outcome = await self._run_once()
        self.last_outcome = outcome
        return outcome

    async def _run_once(self) -> LaneOutcome:
        if not self.supported():
            return "unsupported"
        try:
            job = await self._platform.request_generator_round_commitment_job()
        except Exception as exc:  # noqa: BLE001 - the lane is best effort
            logger.warning("round commitment job request failed: %s", exc)
            return "error"
        if job is None:
            return "idle"
        try:
            data = _payload_bytes(job)
        except ValueError as exc:
            logger.error("refusing round %s commitment job: %s", job.round_index, exc)
            await self._report(job, "failed", detail=f"refused payload: {exc}")
            return "failed"
        logger.info(
            "publishing generator round %s commitment on chain", job.round_index
        )
        try:
            await self._chain.set_commitment(data)
        except Exception as exc:  # noqa: BLE001 - recorded on Platform
            detail = f"set_commitment failed: {type(exc).__name__}: {exc}"
            logger.warning("round %s commitment: %s", job.round_index, detail)
            await self._report(job, "failed", detail=detail)
            return "failed"
        block = await self._await_on_chain(data)
        if block is None:
            detail = (
                f"commitment not observed on chain within {self._readback_timeout:.0f}s"
            )
            logger.warning("round %s commitment: %s", job.round_index, detail)
            await self._report(job, "failed", detail=detail)
            return "failed"
        logger.info(
            "generator round %s commitment confirmed on chain at block %s",
            job.round_index,
            block,
        )
        await self._report(job, "confirmed", block_number=block)
        return "confirmed"

    async def _await_on_chain(self, data: bytes) -> int | None:
        """Block at which this hotkey's commitment equals ``data``, or ``None``."""
        deadline = self._clock() + self._readback_timeout
        while True:
            try:
                current = await self._chain.get_own_commitment(self._hotkey)
            except Exception as exc:  # noqa: BLE001 - keep polling until the deadline
                logger.debug("commitment read-back failed: %s", exc)
                current = None
            if current is not None and current[1] == data:
                return int(current[0])
            if self._clock() >= deadline:
                return None
            await self._sleep(self._readback_interval)

    async def _report(
        self,
        job: GeneratorRoundCommitmentJobResponse,
        outcome: Literal["confirmed", "failed"],
        *,
        block_number: int | None = None,
        detail: str | None = None,
    ) -> None:
        try:
            await self._platform.report_generator_round_commitment(
                job, outcome=outcome, block_number=block_number, detail=detail
            )
        except Exception as exc:  # noqa: BLE001 - Platform re-elects after the claim
            logger.warning(
                "round %s commitment report (%s) failed: %s",
                job.round_index,
                outcome,
                exc,
            )

    async def run_forever(
        self,
        stop: asyncio.Event,
        *,
        drain_requested: asyncio.Event | None = None,
    ) -> None:
        """Poll until ``stop``. Starts no new publication while draining."""
        while not stop.is_set():
            if drain_requested is None or not drain_requested.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - never kill the worker
                    logger.exception("round commitment lane failed; retrying")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)
