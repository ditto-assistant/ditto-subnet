"""Compatibility import for the shared agent lifecycle enum."""

from ditto_screening_protocol import AgentStatus

SCOREABLE_AGENT_STATUSES = (
    AgentStatus.EVALUATING,
    AgentStatus.SCORED,
    AgentStatus.LIVE,
    AgentStatus.ATH_PENDING_REVIEW,
)

__all__ = ["AgentStatus", "SCOREABLE_AGENT_STATUSES"]
