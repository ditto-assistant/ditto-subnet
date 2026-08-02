"""Whether a scored run actually used the language model.

Wire + persisted value, so it lives here rather than in ``ditto/db`` -- same
reasoning as ``AgentStatus``.
"""

from __future__ import annotations

from enum import StrEnum


class ModelUseVerdict(StrEnum):
    """The model-use finding for one scored run.

    Three states, not two. ``UNMEASURED`` is load-bearing: it is what a run
    reports when no inference grant was bound to its lease (the proxy was
    disabled, or the score predates measurement). Collapsing it into
    ``NOT_USED`` would accuse every historical and every unmetered run of
    something it was never measured for.
    """

    UNMEASURED = "unmeasured"
    """No metered grant for this lease. Never grounds for a penalty."""

    USED = "used"
    """Measured, and the run carried real case context to the model."""

    NOT_USED = "not_used"
    """Measured, and the model was absent or answered nothing of substance."""
