"""The shared validator-retry triage vocabulary (wire + internal).

One below-quorum submission is always in exactly one of these states. Only
``exhausted`` needs an operator; every other state advances on its own.
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
