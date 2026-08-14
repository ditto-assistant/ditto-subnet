"""Compatibility re-export for the shared confirmation heartbeat contract."""

from ditto_screening_protocol.confirmation_progress import (
    MAX_CONFIRMATION_SLOTS,
    ConfirmationProgress,
    ConfirmationProgressStage,
    confirmation_progress_signing_token,
)

__all__ = [
    "MAX_CONFIRMATION_SLOTS",
    "ConfirmationProgress",
    "ConfirmationProgressStage",
    "confirmation_progress_signing_token",
]
