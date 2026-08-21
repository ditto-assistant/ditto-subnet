"""The shared validator-retry triage vocabulary (wire + internal).

One below-quorum submission is always in exactly one of these states. Only
``exhausted`` needs an operator; every other state advances on its own.
Read ``recommended_action`` on an exhausted row: ``retry`` is a grant after
verified infrastructure failure, and ``withdraw`` is the documented terminal
path for named agent-attributable failures.
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
