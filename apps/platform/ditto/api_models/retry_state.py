"""The shared validator-retry triage vocabulary (wire + internal).

One below-quorum submission is always in exactly one of these states. Failed
work parks as ``exhausted`` until an operator grants a retry.
Read ``recommended_action`` on an exhausted row: ``retry`` is a grant after
verified infrastructure failure, and ``withdraw`` is the documented terminal
path for named agent-attributable failures.
``RetryDisposition`` is the same verdict in miner-facing words, so a public
surface can say whether a stalled submission is waiting on Ditto or has
actually failed without republishing operator vocabulary.
"""

from __future__ import annotations

from typing import Literal

RetryState = Literal[
    "running",
    "retry_available",
    "cooling_down",
    "exhausted",
    "queued",
]

# How a miner should read an exhausted row. ``operator_hold`` means the fleet
# owes this submission an attempt: the remaining slots died on something Ditto
# owns, and only an operator grant restarts them. ``terminal_artifact_failure``
# means every remaining slot died on a named agent-attributable code, so no
# lease of the same image can finish it.
#
# Fail-closed: a row whose cause is mixed, unnamed, or still unfolding reads as
# ``operator_hold``. Publishing the wrong one blames a miner for a fleet
# failure, which is the more expensive mistake of the two.
RetryDisposition = Literal["operator_hold", "terminal_artifact_failure"]

# Next operator step on an exhausted row. ``withdraw`` is the documented
# terminal path for named agent-attributable failures; ``retry`` is a grant
# after verified infrastructure failure.
RecommendedRetryAction = Literal["retry", "withdraw"]

RETRY_STATES: tuple[RetryState, ...] = (
    "running",
    "retry_available",
    "cooling_down",
    "exhausted",
    "queued",
)

# Operator-attention order: most urgent first.
RETRY_STATE_ORDER: dict[RetryState, int] = {
    "exhausted": 0,
    "cooling_down": 1,
    "retry_available": 2,
    "running": 3,
    "queued": 4,
}
