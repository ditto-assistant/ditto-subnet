"""Agent submission lifecycle used by the miner and validator clients.

Keep this enum local: the public subnet package must remain installable without
credentials for the private screener repository.  The platform-generated
validator contract golden guards these wire values against drift.
"""

from enum import StrEnum


class AgentStatus(StrEnum):
    """Lifecycle state returned by the platform API."""

    UPLOADED = "uploaded"
    SCREENING = "screening"
    SCREENING_PASSED = "screening_passed"
    SCREENING_FAILED = "screening_failed"
    REJECTED = "rejected"
    EVALUATING = "evaluating"
    SCORED = "scored"
    LIVE = "live"
    ATH_PENDING_REVIEW = "ath_pending_review"
    BANNED = "banned"


__all__ = ["AgentStatus"]
