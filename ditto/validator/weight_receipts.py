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

from pydantic import ValidationError

from ditto.api_models.receipt_diagnostics import (
    ReceiptDiagnosticObservation,
    ReceiptValidationDiagnostic,
)
from ditto.api_models.weight_receipt import (
    FinalizedWeightAttempt,
    FinalizedWeightReceipt,
    WeightProvenance,
    weight_receipt_digest,
    weight_vector_digest,
)
from ditto.validator.errors import WeightReceiptConflictError
from ditto_screening_protocol.treasury import TreasuryLedgerPin
from ditto_screening_protocol.treasury_enforcement import EnforcingTreasuryPin

logger = logging.getLogger(__name__)

RECEIPT_CONFLICT_DROP_THRESHOLD = 5
"""Consecutive platform conflicts before an envelope is acked-and-dropped.

A receipt that conflicts with an immutable pin fails deterministically: the
same signed body re-validates to the same provenance every time. Retrying it
forever floods the platform with 409s (issue #2712) and rides in front of
genuine late-arriving receipts. The dropped count rides the relay diagnostics
so the drop is observable without retaining any receipt content.
"""

_RECEIPT_DIAGNOSTIC_FIELDS = frozenset(
    FinalizedWeightReceipt.model_fields
    | FinalizedWeightAttempt.model_fields
    | WeightProvenance.model_fields
    | EnforcingTreasuryPin.model_fields
)
_RECEIPT_VALIDATION_RULES = {
    "Value error, champion identity and artifact must be paired": "champion_pair",
    "Value error, normalized weights contain duplicate UIDs": "duplicate_uids",
    "Value error, normalized weights have no positive value": "empty_weights",
    "Value error, ciphertext hash does not match ciphertext": "ciphertext_digest",
    (
        "Value error, legacy receipt requires champion and no enforcing pin"
    ): "legacy_authority",
    (
        "Value error, enforcing receipt differs from pinned treasury authority"
    ): "pinned_authority",
    (
        "Value error, enforcing receipt does not conserve service allocation"
    ): "service_allocation",
    "Value error, weight vector digest does not match request weights": "vector_digest",
    (
        "Value error, Pylon request digest does not match immutable request"
    ): "request_digest",
}


def _receipt_validation_fields(exc: ValidationError) -> str:
    """Only schema-owned names; dict keys, values and messages stay private."""
    fields = []
    for error in exc.errors(
        include_url=False, include_context=False, include_input=False
    )[:5]:
        location = (
            ".".join(
                part
                if isinstance(part, str) and part in _RECEIPT_DIAGNOSTIC_FIELDS
                else "*"
                for part in error["loc"][:8]
            )
            or "root"
        )
        # Pydantic's built-in validation types are fixed by the receipt model.
        # Unknown/custom types must not become a channel for arbitrary text.
        kind = error["type"]
        if kind not in {
            "missing",
            "int_type",
            "int_parsing",
            "float_type",
            "float_parsing",
            "finite_number",
            "greater_than",
            "greater_than_equal",
            "less_than_equal",
            "string_type",
            "string_too_short",
            "string_too_long",
            "string_pattern_mismatch",
            "uuid_parsing",
            "uuid_type",
            "list_type",
            "tuple_type",
            "dict_type",
            "model_type",
            "literal_error",
            "too_short",
            "too_long",
            "value_error",
        }:
            kind = "other"
        rule = _RECEIPT_VALIDATION_RULES.get(error["msg"])
        fields.append(f"{location}:{kind}" + (f":{rule}" if rule else ""))
    return ",".join(fields)


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
    conflicts_dropped: int = 0
    last_validation: ReceiptValidationDiagnostic | None = None


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
        self._conflict_counts: dict[tuple[str, str], int] = {}

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
            conflicts_dropped=counts.get(
                "conflicts_dropped", self.diagnostics.conflicts_dropped
            ),
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
        self.diagnostics = replace(self.diagnostics, last_validation=None)
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
                            self._conflict_counts.pop(
                                (str(claim.request_id), str(claim.attempt.attempt_id)),
                                None,
                            )
                    except Exception as exc:  # noqa: BLE001 - keep other receipts moving
                        deferred_stage = stage + "_failed"
                        self._recovery_observed(
                            deferred_stage,
                            page_deferred=self.diagnostics.page_deferred + 1,
                        )
                        reason = type(exc).__name__
                        if isinstance(exc, WeightReceiptConflictError):
                            reason += f"({exc.code})"
                        if stage == "validating_claim" and isinstance(
                            exc, ValidationError
                        ):
                            self.diagnostics = replace(
                                self.diagnostics,
                                last_validation=ReceiptValidationDiagnostic(
                                    error_count=exc.error_count(),
                                    fields=_receipt_validation_fields(exc).split(","),
                                ),
                            )
                        logger.warning(
                            "individual weight receipt deferred: %s stage=%s "
                            "validation_fields=%s validation_error_count=%d",
                            reason,
                            stage,
                            _receipt_validation_fields(exc)
                            if isinstance(exc, ValidationError)
                            else "none",
                            exc.error_count()
                            if isinstance(exc, ValidationError)
                            else 0,
                        )
                        if stage == "forwarding_platform" and isinstance(
                            exc, WeightReceiptConflictError
                        ):
                            # A conflict is deterministic: the same signed body
                            # re-validates to the same mismatched provenance on
                            # every retry. Ack-and-drop after a bounded streak
                            # so the envelope cannot 409 forever (#2712).
                            key = (str(claim.request_id), str(claim.attempt.attempt_id))
                            count = self._conflict_counts.get(key, 0) + 1
                            self._conflict_counts[key] = count
                            if count >= RECEIPT_CONFLICT_DROP_THRESHOLD:
                                self._conflict_counts.pop(key, None)
                                logger.warning(
                                    "weight receipt dropped after %d consecutive "
                                    "conflicts: dropping poisoned envelope",
                                    count,
                                )
                                try:
                                    stage = "acknowledging_pylon"
                                    await acknowledge(
                                        str(claim.request_id),
                                        {
                                            "request_digest": claim.request_digest,
                                            "attempt_id": str(claim.attempt.attempt_id),
                                            "receipt_digest": weight_receipt_digest(
                                                claim
                                            ),
                                        },
                                    )
                                    self._recovery_observed(
                                        "conflict_dropped",
                                        conflicts_dropped=self.diagnostics.conflicts_dropped
                                        + 1,
                                    )
                                    stage = "validating_claim"
                                except Exception as ack_exc:  # noqa: BLE001 - drop retry waits for the next sweep
                                    logger.warning(
                                        "conflict-drop acknowledgement deferred: %s",
                                        type(ack_exc).__name__,
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
        self,
        weights: dict[str, float],
        ledger: Any,
        champion: Any,
        chain_epoch_block: int | None = None,
    ) -> bool | None:
        """None permits legacy submission only before any receipt acceptance.

        Timeout, malformed acknowledgement, and other uncertain outcomes return
        False: retry the deterministic request next time, never duplicate it via
        legacy submission. A new epoch is a distinct request and may proceed.

        ``chain_epoch_block`` is the chain's ``LastEpochBlock``. A stale pin
        repeats the previous epoch's provenance, so without this the request id
        is reused and Pylon acknowledges the duplicate without committing.
        It is part of the request id only, not the receipt body: the body stays
        the pin that was folded.
        """
        submit = getattr(self.setter, "put_weights_with_receipt", None)
        if chain_epoch_block is not None and (
            type(chain_epoch_block) is not int or chain_epoch_block < 0
        ):
            self._submission_observed("invalid_provenance")
            return False
        treasury_pin = getattr(ledger, "treasury_pin", None)
        # Unknown/malformed pins never downgrade to ordinary submission. V1
        # must revalidate as the historical shadow contract before fallback.
        try:
            if isinstance(treasury_pin, TreasuryLedgerPin):
                treasury_pin = TreasuryLedgerPin.model_validate(treasury_pin)
            elif treasury_pin is not None:
                treasury_pin = EnforcingTreasuryPin.model_validate(treasury_pin)
        except (ValueError, TypeError):
            self._submission_observed("invalid_provenance")
            return False
        enforcing = isinstance(treasury_pin, EnforcingTreasuryPin)
        fallback = False if enforcing else None
        snapshot = getattr(ledger, "ledger_snapshot_id", None)
        epoch = getattr(ledger, "epoch_index", None)
        digest = getattr(ledger, "ledger_digest", None)
        if (
            not callable(submit)
            or (champion is None and not enforcing)
            or snapshot is None
            or epoch is None
            or digest is None
        ):
            self._submission_observed("missing_provenance_or_transport")
            return fallback
        try:
            if enforcing:
                treasury_pin = EnforcingTreasuryPin.model_validate(treasury_pin)
                if (
                    treasury_pin.epoch_index != epoch
                    or treasury_pin.policy.netuid != self.netuid
                ):
                    raise ValueError("enforcing pin differs from receipt ledger")
            provenance = {
                "ledger_snapshot_id": str(snapshot),
                "epoch_index": epoch,
                "ledger_digest": digest,
                "champion_agent_id": str(champion.agent_id)
                if champion is not None
                else None,
                "champion_artifact_sha256": champion.sha256
                if champion is not None
                else None,
                "bench_version": getattr(ledger, "active_bench_version", None),
                "vector_digest": weight_vector_digest(weights),
            }
            provenance = WeightProvenance.model_validate(provenance).model_dump(
                mode="json"
            )
            body = {
                "schema_version": 2 if enforcing else 1,
                "mechanism_id": 0,
                "weights": {k: float(v) for k, v in weights.items()},
                "provenance": provenance,
            }
            if enforcing:
                body["treasury_pin"] = EnforcingTreasuryPin.model_validate(
                    treasury_pin
                ).model_dump(mode="json")
            request_digest = hashlib.sha256(
                json.dumps(
                    body, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode()
            ).hexdigest()
            identity = (
                f"ditto-weight-receipt:v{body['schema_version']}:"
                f"{self.hotkey}:{self.netuid}:{request_digest}"
            )
            if chain_epoch_block is not None:
                identity = f"{identity}:chain-epoch:{chain_epoch_block}"
            request_id = str(uuid5(NAMESPACE_URL, identity))
        except (AttributeError, ValueError, TypeError, OverflowError):
            self._submission_observed("invalid_provenance")
            # No request was sent. Old/incomplete ledgers retain ordinary
            # weight liveness but cannot manufacture disclosure provenance.
            return fallback
        self._submission_observed("submitting_pylon")
        try:
            result = await submit(request_id, body)
            if result is None:
                self._submission_observed("unsupported")
                return fallback  # Enforcing requests never fall back to legacy.
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
