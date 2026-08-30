from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest

from ditto.api_models.coding import (
    CodingAuthoringLeaseResponse,
    CodingGradingLeaseResponse,
)
from ditto.validator.coding_attempt import (
    CodingAttemptIntegrityError,
    CodingAttemptRuntime,
)
from ditto.validator.coding_supervisor import CodingSupervisorRuntime
from ditto.validator.errors import ValidatorInfrastructureError

_ROOT = Path(__file__).resolve().parents[3]
_TESTDATA = _ROOT / "packages/dittobench-coding-contract/testdata"
_SUPERVISOR = json.loads((_TESTDATA / "coding_attempt_supervisor_v1.json").read_text())
_FREEZE = json.loads((_TESTDATA / "coding_authoring_freeze_v1.json").read_text())
_CONTRACT = json.loads((_TESTDATA / "coding_contract_v1.json").read_text())


def _config() -> Any:
    return SimpleNamespace(
        dittobench_api_url="https://dittobench-api:8000",
        dittobench_control_token="coding-supervisor-control-token-000000000000",
    )


def _response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json", "cache-control": "no-store"},
        json=payload,
    )


def _lease(model: type[Any], *, grading: bool = False) -> Any:
    request = _SUPERVISOR["requests"]["grade" if grading else "author"]
    values: dict[str, Any] = {
        "ticket_id": UUID(request["ticket_id"]),
        "ticket_deadline": datetime.fromisoformat(
            request["deadline"].replace("Z", "+00:00")
        ),
        "coding_run_id": request["coding_run_id"],
    }
    if grading:
        values.update(
            agent_id=UUID("11111111-1111-4111-8111-111111111111"),
            run_row_id=UUID("22222222-2222-4222-8222-222222222222"),
        )
    return model.model_construct(**values)


def _dynamic_response(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    operation = payload["operation"]
    template = json.loads(json.dumps(_SUPERVISOR["responses"][operation]))
    template.update(
        operation_id=payload["operation_id"],
        ticket_id=payload["ticket_id"],
        coding_run_id=payload["coding_run_id"],
    )
    if operation == "author":
        evidence = _FREEZE["request"]["evidence"]
        template["authoring"] = {
            "evidence": evidence,
            "authoring_transcript_object_key": _FREEZE["request"][
                "authoring_transcript_object_key"
            ],
            "authoring_transcript_bytes": _FREEZE["request"][
                "authoring_transcript_bytes"
            ],
            "authoring_event_count": _FREEZE["request"]["authoring_event_count"],
            "frozen_submission_object_key": _FREEZE["request"][
                "frozen_submission_object_key"
            ],
            "capabilities_revoked": True,
            "authoring_environment_destroyed": True,
        }
    elif operation == "grade":
        task_evidence = json.loads(json.dumps(_CONTRACT["task_evidence"]))
        task_evidence["coding_run_id"] = payload["coding_run_id"]
        task_evidence["validator_ticket_id"] = payload["ticket_id"]
        template["grading"] = {
            "task_evidence": [task_evidence],
            "grading_environment_destroyed": True,
        }
    return _response(template)


@pytest.mark.asyncio
async def test_runtime_author_grade_abort_and_recover() -> None:
    observed: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return _dynamic_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        runtime = CodingSupervisorRuntime(_config(), client)
        runtime_port: CodingAttemptRuntime = runtime
        assert runtime_port is runtime
        authoring_lease = _lease(CodingAuthoringLeaseResponse)
        authoring = await runtime.author(authoring_lease)
        assert authoring.capabilities_revoked is True
        assert authoring.authoring_environment_destroyed is True

        grading_lease = _lease(CodingGradingLeaseResponse, grading=True)
        grading = await runtime.grade(grading_lease, authoring)
        assert len(grading.task_evidence) == 1
        assert grading.grading_environment_destroyed is True

        await runtime.abort_authoring(authoring_lease)
        await runtime.abort_grading(grading_lease)
        recovery = await runtime.recover(
            ticket_id=authoring_lease.ticket_id,
            coding_run_id=authoring_lease.coding_run_id,
            deadline=authoring_lease.ticket_deadline,
        )
        assert recovery.state == "terminal_pending"

    assert [json.loads(request.content)["operation"] for request in observed] == [
        "author",
        "grade",
        "abort_authoring",
        "abort_grading",
        "recover",
    ]
    for request in observed:
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["cache-control"] == "no-store"
        assert request.url.host == "dittobench-api"
    assert "control-token" not in repr(runtime)


@pytest.mark.asyncio
async def test_runtime_rejects_redirect_error_oversize_and_identity_drift() -> None:
    authoring_lease = _lease(CodingAuthoringLeaseResponse)

    async def run(response: httpx.Response) -> Exception:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: response)
        ) as client:
            runtime = CodingSupervisorRuntime(_config(), client)
            with pytest.raises(Exception) as captured:
                await runtime.author(authoring_lease)
            return captured.value

    assert isinstance(
        await run(httpx.Response(307, headers={"location": "https://evil"})),
        ValidatorInfrastructureError,
    )
    assert isinstance(
        await run(httpx.Response(503, json={"detail": "private"})),
        ValidatorInfrastructureError,
    )

    drifted = json.loads(json.dumps(_SUPERVISOR["responses"]["author"]))
    drifted["ticket_id"] = "99999999-9999-4999-8999-999999999999"
    assert isinstance(await run(_response(drifted)), CodingAttemptIntegrityError)

    huge = httpx.Response(
        200,
        headers={"cache-control": "no-store"},
        content=b"x" * ((8 << 20) + 1),
    )
    assert isinstance(await run(huge), ValidatorInfrastructureError)


def test_runtime_configuration_and_vector_shape() -> None:
    invalid: Any = SimpleNamespace(
        dittobench_api_url="https://user:password@example.com?x=1",
        dittobench_control_token="short",
    )
    with pytest.raises(ValueError):
        CodingSupervisorRuntime(
            invalid,
            object(),  # type: ignore[arg-type]
        )
    plaintext: Any = SimpleNamespace(
        dittobench_api_url="http://dittobench-api:8000",
        dittobench_control_token="coding-supervisor-control-token-000000000000",
    )
    with pytest.raises(ValueError):
        CodingSupervisorRuntime(plaintext, object())  # type: ignore[arg-type]
    loopback: Any = SimpleNamespace(
        dittobench_api_url="http://127.0.0.1:8000",
        dittobench_control_token="coding-supervisor-control-token-000000000000",
    )
    CodingSupervisorRuntime(loopback, object())  # type: ignore[arg-type]
    for operation, request in _SUPERVISOR["requests"].items():
        assert request["schema"] == "dittobench-coding-attempt-supervisor-request-v1"
        assert request["operation"] == operation
        assert (
            datetime.fromisoformat(request["deadline"].replace("Z", "+00:00")).tzinfo
            is not None
        )
    assert datetime.now(UTC).tzinfo is UTC


def test_supervisor_remains_unmounted_and_unconstructed() -> None:
    worker = (_ROOT / "ditto/validator/worker.py").read_text()
    scorer_main = (
        _ROOT / "services/dittobench-api/cmd/dittobench-api/main.go"
    ).read_text()
    assert "CodingSupervisorRuntime" not in worker
    assert "internal/codingsupervisor" not in scorer_main
