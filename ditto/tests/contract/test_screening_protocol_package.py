"""The subnet must consume the standalone screening protocol package."""

from uuid import UUID

from ditto.api_models.agent_status import AgentStatus as CompatibilityAgentStatus
from ditto.api_models.screener import ScreenResultRequest as CompatibilityRequest
from ditto.screener.signing import verdict_signing_message as worker_message
from ditto_screening_protocol import (
    SCREENING_POLICY_VERSION,
    AgentStatus,
    ScreenResultRequest,
    verdict_signing_message,
)


def test_compatibility_imports_are_shared_package_types() -> None:
    assert CompatibilityAgentStatus is AgentStatus
    assert CompatibilityRequest is ScreenResultRequest


def test_worker_uses_canonical_signing_message() -> None:
    hotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    expected = verdict_signing_message(
        screener_hotkey=hotkey,
        agent_id=agent_id,
        passed=True,
        policy_version=SCREENING_POLICY_VERSION,
    )
    assert (
        worker_message(
            screener_hotkey=hotkey,
            agent_id=agent_id,
            passed=True,
            policy_version=SCREENING_POLICY_VERSION,
        )
        == expected
    )
