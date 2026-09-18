"""Durable Pylon receipt relay; never substitute acknowledgements for earnings."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, replace
from time import monotonic, time
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from ditto.api_models.receipt_diagnostics import ReceiptDiagnosticObservation
from ditto.api_models.weight_receipt import (
    FinalizedWeightReceipt,
    WeightProvenance,
    weight_receipt_digest,
    weight_vector_digest,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReceiptRelayDiagnostics:
    """Bounded process-local observations, never payout or eligibility evidence.

    No request bodies, ciphertext, tokens, signatures, or exception messages are
    retained. The transport must authenticate these observations and expose their
    timestamp; a restart resets them to unknown.
    """

    submission_status: str = "not_attempted"
    submission_observed_at: int | None = None
    recovery_status: str = "not_attempted"
    recovery_observed_at: int | None = None
    page_receipts: int = 0
    page_finalized: int = 0
    page_forwarded: int = 0
    page_deferred: int = 0


class WeightReceiptRelay:
    def __init__(self, setter: Any, platform: Any, hotkey: str, netuid: int) -> None:
        self.setter = setter
        self.platform = platform
        self.hotkey = hotkey
        self.netuid = netuid
        self.cursor = 0
        self.diagnostics = ReceiptRelayDiagnostics()
        self._recovery_lock = asyncio.Lock()
        self._recovery_task: asyncio.Task[None] | None = None
        self._last_scheduled_recovery: float | None = None

    def _submission_observed(self, status: str) -> None:
        self.diagnostics = replace(
            self.diagnostics,
            submission_status=status,
            submission_observed_at=int(time()),
        )

    def _recovery_observed(self, status: str, **counts: int) -> None:
        self.diagnostics = replace(
            self.diagnostics,
            recovery_status=status,
            recovery_observed_at=int(time()),
            page_receipts=counts.get("page_receipts", self.diagnostics.page_receipts),
            page_finalized=counts.get(
                "page_finalized", self.diagnostics.page_finalized
            ),
            page_forwarded=counts.get(
                "page_forwarded", self.diagnostics.page_forwarded
            ),
            page_deferred=counts.get("page_deferred", self.diagnostics.page_deferred),
        )

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
            report = getattr(self.platform, "submit_receipt_diagnostics", None)
            if callable(report):
                try:
                    async with asyncio.timeout(3):
                        await report(
                            ReceiptDiagnosticObservation.model_validate(
                                asdict(self.diagnostics)
                            )
                        )
                except Exception:  # noqa: BLE001 - diagnostic failure never blocks weights
                    logger.debug("receipt diagnostic reporting deferred")

    async def _recover(self) -> None:
        """Bounded recovery includes old jobs after a stateless worker restart."""
        read = getattr(self.setter, "list_weight_receipts", None)
        report = getattr(self.platform, "submit_weight_receipt", None)
        acknowledge = getattr(self.setter, "acknowledge_weight_receipt", None)
        self._recovery_observed(
            "reading_pylon",
            page_receipts=0,
            page_finalized=0,
            page_forwarded=0,
            page_deferred=0,
        )
        if not callable(read) or not callable(report) or not callable(acknowledge):
            self._recovery_observed("unsupported")
            return
        stage = "reading_pylon"
        deferred_stage: str | None = None
        try:
            async with asyncio.timeout(10):
                page = await read(after_task_id=self.cursor, limit=20)
                if page is None:
                    self._recovery_observed("unsupported")
                    return
                if not isinstance(page, dict) or not isinstance(
                    page.get("receipts"), list
                ):
                    raise ValueError("invalid Pylon receipt page")
                self._recovery_observed(
                    "reading_pylon", page_receipts=len(page["receipts"])
                )
                for envelope in page["receipts"]:
                    if not isinstance(envelope, dict) or envelope.get("acknowledged"):
                        continue
                    try:
                        stage = "validating_claim"
                        for attempt in envelope.get("attempts", []):
                            if attempt.get("status") != "finalized":
                                continue
                            self._recovery_observed(
                                "validating_claim",
                                page_finalized=self.diagnostics.page_finalized + 1,
                            )
                            stage = "validating_claim"
                            claim = FinalizedWeightReceipt.model_validate(
                                {**envelope, "attempt": attempt}
                            )
                            if (
                                claim.validator_hotkey != self.hotkey
                                or claim.netuid != self.netuid
                            ):
                                raise ValueError("Pylon receipt identity mismatch")
                            stage = "forwarding_platform"
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
                            stage = "acknowledging_pylon"
                            await acknowledge(
                                str(claim.request_id),
                                {
                                    "request_digest": claim.request_digest,
                                    "attempt_id": str(claim.attempt.attempt_id),
                                    "receipt_digest": expected,
                                },
                            )
                            self._recovery_observed(
                                "forwarded",
                                page_forwarded=self.diagnostics.page_forwarded + 1,
                            )
                    except Exception as exc:  # noqa: BLE001 - keep other receipts moving
                        deferred_stage = stage + "_failed"
                        self._recovery_observed(
                            deferred_stage,
                            page_deferred=self.diagnostics.page_deferred + 1,
                        )
                        logger.warning(
                            "individual weight receipt deferred: %s", type(exc).__name__
                        )
                stage = "validating_page"
                next_cursor = page.get("next_after_task_id")
                if next_cursor is not None and (
                    type(next_cursor) is not int or next_cursor <= self.cursor
                ):
                    raise ValueError("invalid receipt recovery cursor")
                self.cursor = next_cursor or 0
                self._recovery_observed(deferred_stage or "page_complete")
        except Exception as exc:  # noqa: BLE001 - evidence outages cannot stop weights
            self._recovery_observed(stage + "_failed")
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
            self._submission_observed("missing_provenance_or_transport")
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
            self._submission_observed("invalid_provenance")
            # No request was sent. Old/incomplete ledgers retain ordinary
            # weight liveness but cannot manufacture disclosure provenance.
            return None
        self._submission_observed("submitting_pylon")
        try:
            result = await submit(request_id, body)
            if result is None:
                self._submission_observed("unsupported")
                return None  # Explicit unsupported/rejected-before-create only.
            if (
                not isinstance(result, dict)
                or result.get("request_id") != request_id
                or result.get("request_digest") != request_digest
            ):
                raise ValueError("Pylon did not acknowledge exact request")
            self._submission_observed("accepted")
            return True
        except Exception as exc:  # noqa: BLE001 - do not duplicate uncertain submission
            self._submission_observed("uncertain")
            logger.warning(
                "weight receipt submission outcome uncertain: %s", type(exc).__name__
            )
            return False
