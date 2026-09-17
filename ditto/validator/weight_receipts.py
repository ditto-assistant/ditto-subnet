"""Durable Pylon receipt relay; never substitute acknowledgements for earnings."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from time import monotonic
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from ditto.api_models.weight_receipt import (
    FinalizedWeightReceipt,
    WeightProvenance,
    weight_receipt_digest,
    weight_vector_digest,
)

logger = logging.getLogger(__name__)


class WeightReceiptRelay:
    def __init__(self, setter: Any, platform: Any, hotkey: str, netuid: int) -> None:
        self.setter = setter
        self.platform = platform
        self.hotkey = hotkey
        self.netuid = netuid
        self.cursor = 0
        self._recovery_lock = asyncio.Lock()
        self._recovery_task: asyncio.Task[None] | None = None
        self._last_scheduled_recovery: float | None = None

    def schedule_recovery(self) -> None:
        """Heartbeat-triggered recovery cannot delay heartbeat delivery."""
        now = monotonic()
        if (
            self._last_scheduled_recovery is not None
            and now - self._last_scheduled_recovery < 30
        ):
            return
        if self._recovery_task is None or self._recovery_task.done():
            self._last_scheduled_recovery = now
            self._recovery_task = asyncio.create_task(self.recover())

    async def recover(self) -> None:
        if self._recovery_lock.locked():
            return
        async with self._recovery_lock:
            await self._recover()

    async def _recover(self) -> None:
        """Bounded recovery includes old jobs after a stateless worker restart."""
        read = getattr(self.setter, "list_weight_receipts", None)
        report = getattr(self.platform, "submit_weight_receipt", None)
        acknowledge = getattr(self.setter, "acknowledge_weight_receipt", None)
        if not callable(read) or not callable(report) or not callable(acknowledge):
            return
        try:
            async with asyncio.timeout(10):
                page = await read(after_task_id=self.cursor, limit=20)
                if page is None:
                    return
                if not isinstance(page, dict) or not isinstance(
                    page.get("receipts"), list
                ):
                    raise ValueError("invalid Pylon receipt page")
                for envelope in page["receipts"]:
                    if not isinstance(envelope, dict) or envelope.get("acknowledged"):
                        continue
                    try:
                        for attempt in envelope.get("attempts", []):
                            if attempt.get("status") != "finalized":
                                continue
                            claim = FinalizedWeightReceipt.model_validate(
                                {**envelope, "attempt": attempt}
                            )
                            if (
                                claim.validator_hotkey != self.hotkey
                                or claim.netuid != self.netuid
                            ):
                                raise ValueError("Pylon receipt identity mismatch")
                            ack = await report(claim)
                            expected = weight_receipt_digest(claim)
                            if (
                                str(ack.request_id) != str(claim.request_id)
                                or str(ack.attempt_id) != str(claim.attempt.attempt_id)
                                or ack.receipt_digest != expected
                                or ack.stored is not True
                            ):
                                raise ValueError(
                                    "Platform did not acknowledge exact receipt"
                                )
                            await acknowledge(
                                str(claim.request_id),
                                {
                                    "request_digest": claim.request_digest,
                                    "attempt_id": str(claim.attempt.attempt_id),
                                    "receipt_digest": expected,
                                },
                            )
                    except Exception as exc:  # noqa: BLE001 - keep other receipts moving
                        logger.warning(
                            "individual weight receipt deferred: %s", type(exc).__name__
                        )
                next_cursor = page.get("next_after_task_id")
                if next_cursor is not None and (
                    type(next_cursor) is not int or next_cursor <= self.cursor
                ):
                    raise ValueError("invalid receipt recovery cursor")
                self.cursor = next_cursor or 0
        except Exception as exc:  # noqa: BLE001 - evidence outages cannot stop weights
            logger.warning("weight receipt recovery deferred: %s", type(exc).__name__)

    async def submit(
        self, weights: dict[str, float], ledger: Any, champion: Any
    ) -> bool | None:
        """None permits legacy submission only before any receipt acceptance.

        Timeout, malformed acknowledgement, and other uncertain outcomes return
        False: retry the deterministic request next time, never duplicate it via
        legacy submission. A new epoch is a distinct request and may proceed.
        """
        submit = getattr(self.setter, "put_weights_with_receipt", None)
        snapshot = getattr(ledger, "ledger_snapshot_id", None)
        epoch = getattr(ledger, "epoch_index", None)
        digest = getattr(ledger, "ledger_digest", None)
        if (
            not callable(submit)
            or champion is None
            or snapshot is None
            or epoch is None
            or digest is None
        ):
            return None
        try:
            provenance = {
                "ledger_snapshot_id": str(snapshot),
                "epoch_index": epoch,
                "ledger_digest": digest,
                "champion_agent_id": str(champion.agent_id),
                "champion_artifact_sha256": champion.sha256,
                "bench_version": getattr(ledger, "active_bench_version", None),
                "vector_digest": weight_vector_digest(weights),
            }
            provenance = WeightProvenance.model_validate(provenance).model_dump(
                mode="json"
            )
            body = {
                "schema_version": 1,
                "mechanism_id": 0,
                "weights": {k: float(v) for k, v in weights.items()},
                "provenance": provenance,
            }
            request_digest = hashlib.sha256(
                json.dumps(
                    body, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode()
            ).hexdigest()
            request_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"ditto-weight-receipt:v1:{self.hotkey}:{self.netuid}:{request_digest}",
                )
            )
        except (AttributeError, ValueError, TypeError, OverflowError):
            # No request was sent. Old/incomplete ledgers retain ordinary
            # weight liveness but cannot manufacture disclosure provenance.
            return None
        try:
            result = await submit(request_id, body)
            if result is None:
                return None  # Explicit unsupported/rejected-before-create only.
            if (
                not isinstance(result, dict)
                or result.get("request_id") != request_id
                or result.get("request_digest") != request_digest
            ):
                raise ValueError("Pylon did not acknowledge exact request")
            return True
        except Exception as exc:  # noqa: BLE001 - do not duplicate uncertain submission
            logger.warning(
                "weight receipt submission outcome uncertain: %s", type(exc).__name__
            )
            return False
