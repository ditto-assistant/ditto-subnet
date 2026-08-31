import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto.api_models.coding_executor_control import (
    CodingExecutorControlEnvelope,
    CodingExecutorOperation,
    coding_executor_control_signing_message,
)
from ditto.validator.signing import sign_coding_executor_control


def envelope(**overrides: object) -> CodingExecutorControlEnvelope:
    issued = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    value: dict[str, object] = {
        "schema": "dittobench-coding-executor-control-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "validator_hotkey": "5" + "A" * 47,
        "agent_id": UUID("10000000-0000-4000-8000-000000000001"),
        "agent_artifact_sha256": "1" * 64,
        "coding_run_id": "coding-run-001",
        "ticket_id": UUID("20000000-0000-4000-8000-000000000002"),
        "operation": "supervisor.author",
        "method": "POST",
        "request_body_sha256": "2" * 64,
        "nonce": UUID("30000000-0000-4000-8000-000000000003"),
        "issued_at": issued,
        "expires_at": issued + timedelta(minutes=1),
        "signature": "ab" * 64,
    }
    value.update(overrides)
    return CodingExecutorControlEnvelope.model_validate(value)


def test_control_message_binds_every_authority_field() -> None:
    control = envelope()
    assert coding_executor_control_signing_message(control) == (
        b"dittobench-coding-executor-control:v1\x00"
        b"5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\x00"
        b"10000000-0000-4000-8000-000000000001\x00"
        + b"1"
        * 64
        + b"\x00coding-run-001\x0020000000-0000-4000-8000-000000000002\x00"
        b"supervisor.author\x00POST\x00"
        + b"2"
        * 64
        + b"\x0030000000-0000-4000-8000-000000000003\x00"
        b"2026-09-01T00:00:00.000000+00:00\x00"
        b"2026-09-01T00:01:00.000000+00:00"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"operation": "supervisor.shell"},
        {"method": "GET"},
        {"nonce": UUID(int=0)},
        {"coding_run_id": "bad run"},
        {"expires_at": datetime(2026, 9, 1, 0, 3, tzinfo=UTC)},
    ],
)
def test_control_envelope_rejects_unbounded_authority(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        envelope(**overrides)


def test_control_operation_allowlist_matches_scorer_routes() -> None:
    assert {operation.value for operation in CodingExecutorOperation} == {
        "supervisor.prepare",
        "supervisor.author",
        "supervisor.grade",
        "supervisor.abort-authoring",
        "supervisor.abort-grading",
        "supervisor.recover",
        "publications.prepare",
        "publications.acknowledge",
        "publications.pending",
        "publications.open",
        "publications.lookup",
    }


def test_validator_signer_signs_the_canonical_message() -> None:
    class Keypair:
        message = b""

        def sign(self, message: bytes) -> bytes:
            self.message = message
            return b"\xaa" * 64

    keypair = Keypair()
    control = sign_coding_executor_control(
        keypair,
        validator_hotkey="5" + "A" * 47,
        agent_id=UUID("10000000-0000-4000-8000-000000000001"),
        agent_artifact_sha256="1" * 64,
        coding_run_id="coding-run-001",
        ticket_id=UUID("20000000-0000-4000-8000-000000000002"),
        operation=CodingExecutorOperation.SUPERVISOR_AUTHOR,
        request_body_sha256="2" * 64,
        nonce=UUID("30000000-0000-4000-8000-000000000003"),
        issued_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert keypair.message == coding_executor_control_signing_message(control)
    assert control.signature == "aa" * 64


def test_python_replays_the_shared_control_vector() -> None:
    vector = json.loads(
        (
            Path(__file__).parents[3]
            / "packages/dittobench-coding-contract/testdata"
            / "coding_executor_control_v1.json"
        ).read_text()
    )
    control = CodingExecutorControlEnvelope.model_validate(vector["envelope"])
    assert (
        coding_executor_control_signing_message(control).hex()
        == vector["expected_signing_message_hex"]
    )
