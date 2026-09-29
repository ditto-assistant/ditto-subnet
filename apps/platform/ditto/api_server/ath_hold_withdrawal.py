"""Manual ATH hold withdrawal constants; rewards use the canonical evaluator."""

MANUAL_HOLD_SNAPSHOT = "manual-admin-hold"
WITHDRAW_CONFIRMATION = "WITHDRAW ATH HOLD"


def is_manual_precautionary_hold(provenance: dict | None) -> bool:
    """True only for an operator-opened manual hold, not an automated gate."""
    return provenance is not None and provenance.get("snapshot") == MANUAL_HOLD_SNAPSHOT
