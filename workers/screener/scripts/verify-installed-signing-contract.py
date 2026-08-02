"""Fail deployment when the installed screener/protocol signing APIs diverge."""

from __future__ import annotations

import json
from uuid import UUID

from ditto_screener.signing import sign_verdict
from ditto_screening_protocol import SCREENING_POLICY_VERSION, ScreenResultOutcome


class _ProbeKeypair:
    def __init__(self) -> None:
        self.message: bytes | None = None

    def sign(self, message: bytes) -> bytes:
        self.message = message
        return b"\xa5" * 64


def main() -> None:
    keypair = _ProbeKeypair()
    signature = sign_verdict(
        keypair,
        screener_hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        agent_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
        attempt_id=UUID("776a3bb8-5847-40db-b2af-42f93f20233c"),
        passed=False,
        policy_version=SCREENING_POLICY_VERSION,
        outcome=ScreenResultOutcome.RETRYABLE_INFRA,
        review_settings_revision=1,
        review_settings_instance_id="deploy-contract-probe",
        review_settings_scope="*",
        review_settings_checksum="56" * 32,
    )
    if signature != "a5" * 64 or keypair.message is None:
        raise RuntimeError("installed signing contract returned an invalid signature")

    prefix = b"ditto-screen-result:v5:"
    if not keypair.message.startswith(prefix):
        raise RuntimeError(
            "installed signing contract returned an invalid payload version"
        )
    payload = json.loads(keypair.message.removeprefix(prefix))
    expected = {
        "review_settings_revision": 1,
        "review_settings_instance_id": "deploy-contract-probe",
        "review_settings_scope": "*",
        "review_settings_checksum": "56" * 32,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise RuntimeError(
            "installed signing contract omitted reviewer settings binding"
        )


if __name__ == "__main__":
    main()
