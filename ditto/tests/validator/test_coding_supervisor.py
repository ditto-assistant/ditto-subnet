from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

from ditto.api_models.coding import (
    CodingAuthoringLeaseResponse,
    CodingBudgets,
    CodingGradingLeaseResponse,
    CodingRunManifest,
)
from ditto.api_models.coding_harness import CodingHarnessLaunchResponse
from ditto.api_models.coding_inference_grants import (
    CodingInferenceExchangeResponse,
    CodingInferenceGrantOffer,
    CodingInferenceRevokeResponse,
)
from ditto.validator.coding_attempt import (
    CodingAttemptIntegrityError,
    CodingAttemptRuntime,
)
from ditto.validator.coding_supervisor import (
    CodingInferencePlatform,
    CodingSupervisorRecovery,
    CodingSupervisorRuntime,
)
from ditto.validator.errors import ValidatorInfrastructureError
from ditto.validator.platform import PlatformClient

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


def _clock() -> datetime:
    deadline = datetime.fromisoformat(
        _SUPERVISOR["requests"]["author"]["deadline"].replace("Z", "+00:00")
    )
    return deadline - timedelta(minutes=5)


def _response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json", "cache-control": "no-store"},
        json=payload,
    )


def test_authoring_published_recovery_requires_exact_freeze_authority() -> None:
    recovery = CodingSupervisorRecovery(
        state="authoring_published",
        publication_stage="authoring_freeze",
        request_sha256="aa" * 32,
    )
    assert recovery.publication_stage == "authoring_freeze"
    with pytest.raises(ValueError):
        CodingSupervisorRecovery(
            state="authoring_published",
            publication_stage="terminal_result",
            request_sha256="aa" * 32,
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
    else:
        manifest = CodingRunManifest.model_validate_json(
            json.dumps(_CONTRACT["manifest"])
        ).model_copy(
            update={
                "coding_run_id": request["coding_run_id"],
                "agent_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            }
        )
        budgets = CodingBudgets.model_validate_json(
            json.dumps(_CONTRACT["run_request"]["budgets"])
        )
        values.update(run_manifest=manifest, budgets=budgets)
    return model.model_construct(**values)


def _harness(lease: CodingAuthoringLeaseResponse) -> CodingHarnessLaunchResponse:
    return CodingHarnessLaunchResponse.model_validate(
        {
            **_SUPERVISOR["requests"]["author"]["harness"],
            "agent_id": lease.run_manifest.agent_id,
            "ticket_id": str(lease.ticket_id),
            "ticket_deadline": lease.ticket_deadline,
            "agent_artifact_sha256": lease.run_manifest.agent_artifact_sha256,
        }
    )


class _Platform:
    def __init__(self, lease: CodingAuthoringLeaseResponse) -> None:
        task = lease.run_manifest.tasks[0]
        common: Any = {
            "coding_contract_version": 1,
            "weight_eligible": False,
            "grant_id": UUID("88888888-8888-4888-8888-888888888888"),
            "ticket_id": lease.ticket_id,
            "run_row_id": UUID("99999999-9999-4999-8999-999999999999"),
            "case_id": task.case_id,
            "profile_capability_id": task.profile_capability_id,
            "inference_grant_sha256": lease.run_manifest.inference_grant_sha256,
            "model": "openai/gpt-5.6-luna",
            "provider_api": "openrouter",
            "provider_route": "azure/eu",
            "receipt_provider": "Azure",
            "provider_route_profile": "luna-azure-eu-zdr-v1",
            "provider_account_guardrail": "openrouter_private_account_v1",
            "provider_pipeline_policy": "no_plugins_no_transforms_v1",
            "provider_cache_policy": "disabled_v1",
            "reasoning_effort": "medium",
            "request_budget": min(lease.budgets.workspace_tool_calls + 16, 256),
            "prompt_token_budget": lease.budgets.model_input_tokens,
            "completion_token_budget": lease.budgets.model_output_tokens,
            "cost_budget_usd_micros": 100_000_000,
            "expires_at": lease.ticket_deadline,
        }
        self.offer = CodingInferenceGrantOffer.model_construct(
            schema_name="dittobench-coding-inference-grant-offer-v1",
            status="pending",
            generation=0,
            exchange_url="https://platform.invalid/api/v1/validator/coding-shadow/inference-exchange",
            **common,
        )
        self.exchange = CodingInferenceExchangeResponse.model_construct(
            schema_name="dittobench-coding-inference-exchange-v1",
            status="active",
            generation=1,
            bearer="synthetic-coding-bearer-0000000000000000",
            proxy_url="https://relay.invalid/api/v1/inference/coding/chat/completions",
            revoke_bearer="synthetic-revoke-bearer-0000000000000000",
            revoke_url="https://platform.invalid/api/v1/validator/coding-shadow/inference-revoke-capability",
            **common,
        )
        self.broker_public_key: str | None = None
        self.revoked: list[tuple[UUID, int]] = []

    async def request_coding_inference_grant(
        self, ticket_id: UUID
    ) -> CodingInferenceGrantOffer:
        assert ticket_id == self.offer.ticket_id
        return self.offer

    async def exchange_coding_inference_grant(
        self, offer: CodingInferenceGrantOffer, *, broker_public_key: str
    ) -> CodingInferenceExchangeResponse:
        assert offer is self.offer
        self.broker_public_key = broker_public_key
        return self.exchange

    async def revoke_coding_inference_grant(
        self, *, grant_id: UUID, generation: int
    ) -> CodingInferenceRevokeResponse:
        self.revoked.append((grant_id, generation))
        return CodingInferenceRevokeResponse.model_construct(
            schema_name="dittobench-coding-inference-revocation-v1",
            coding_contract_version=1,
            weight_eligible=False,
            grant_id=grant_id,
            ticket_id=self.offer.ticket_id,
            status="revoked",
            generation=generation,
            revoked_at=datetime.now(UTC),
            idempotent=False,
        )


class _FailingRevocationPlatform(_Platform):
    async def revoke_coding_inference_grant(
        self, *, grant_id: UUID, generation: int
    ) -> CodingInferenceRevokeResponse:
        del grant_id, generation
        raise RuntimeError("injected revocation failure")


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
        authoring_lease = _lease(CodingAuthoringLeaseResponse)
        platform = _Platform(authoring_lease)
        runtime = CodingSupervisorRuntime(_config(), client, platform, clock=_clock)
        runtime_port: CodingAttemptRuntime = runtime
        assert runtime_port is runtime
        authoring = await runtime.author(authoring_lease, _harness(authoring_lease))
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
        "prepare",
        "author",
        "grade",
        "abort_authoring",
        "abort_grading",
        "recover",
    ]
    assert (
        platform.broker_public_key
        == (_SUPERVISOR["responses"]["prepare"]["preparation"]["broker_public_key"])
    )
    assert platform.revoked == [(platform.exchange.grant_id, 1)]
    for request in observed:
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["cache-control"] == "no-store"
        assert request.url.host == "dittobench-api"
        timeout = request.extensions["timeout"]
        assert timeout["read"] > 30
        assert timeout["connect"] <= 10
    assert "control-token" not in repr(runtime)


@pytest.mark.asyncio
async def test_runtime_rejects_redirect_error_oversize_and_identity_drift() -> None:
    authoring_lease = _lease(CodingAuthoringLeaseResponse)

    async def run(response: httpx.Response) -> Exception:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: response)
        ) as client:
            runtime = CodingSupervisorRuntime(
                _config(), client, _Platform(authoring_lease), clock=_clock
            )
            with pytest.raises(Exception) as captured:
                await runtime.author(authoring_lease, _harness(authoring_lease))
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


@pytest.mark.asyncio
async def test_authoring_rejects_grant_drift_and_requires_terminal_revocation() -> None:
    lease = _lease(CodingAuthoringLeaseResponse)
    observed: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        observed.append(json.loads(request.content)["operation"])
        return _dynamic_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        drifted = _Platform(lease)
        drifted.exchange = drifted.exchange.model_copy(update={"case_id": "case-other"})
        runtime = CodingSupervisorRuntime(_config(), client, drifted, clock=_clock)
        with pytest.raises(CodingAttemptIntegrityError):
            await runtime.author(lease, _harness(lease))
        assert observed == ["prepare"]
        assert drifted.revoked == [(drifted.exchange.grant_id, 1)]

        failing = _FailingRevocationPlatform(lease)
        runtime = CodingSupervisorRuntime(_config(), client, failing, clock=_clock)
        with pytest.raises(ValidatorInfrastructureError):
            await runtime.author(lease, _harness(lease))


async def test_authoring_rejects_harness_drift_before_prepare() -> None:
    lease = _lease(CodingAuthoringLeaseResponse)
    observed: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return _dynamic_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        runtime = CodingSupervisorRuntime(
            _config(), client, _Platform(lease), clock=_clock
        )
        harness = _harness(lease).model_copy(
            update={"agent_artifact_sha256": "ff" * 32}
        )
        with pytest.raises(CodingAttemptIntegrityError, match="harness authority"):
            await runtime.author(lease, harness)
    assert observed == []


@pytest.mark.asyncio
async def test_authoring_cancellation_still_revokes_active_grant() -> None:
    lease = _lease(CodingAuthoringLeaseResponse)
    platform = _Platform(lease)
    entered = asyncio.Event()

    async def transport(request: httpx.Request) -> httpx.Response:
        operation = json.loads(request.content)["operation"]
        if operation == "author":
            entered.set()
            await asyncio.Event().wait()
        return _dynamic_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        runtime = CodingSupervisorRuntime(_config(), client, platform, clock=_clock)
        task = asyncio.create_task(runtime.author(lease, _harness(lease)))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert platform.revoked == [(platform.exchange.grant_id, 1)]


def test_runtime_configuration_and_vector_shape() -> None:
    invalid: Any = SimpleNamespace(
        dittobench_api_url="https://user:password@example.com?x=1",
        dittobench_control_token="short",
    )
    with pytest.raises(ValueError):
        CodingSupervisorRuntime(
            invalid,
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        )
    plaintext: Any = SimpleNamespace(
        dittobench_api_url="http://dittobench-api:8000",
        dittobench_control_token="coding-supervisor-control-token-000000000000",
    )
    with pytest.raises(ValueError):
        CodingSupervisorRuntime(
            plaintext,
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        )
    loopback: Any = SimpleNamespace(
        dittobench_api_url="http://127.0.0.1:8000",
        dittobench_control_token="coding-supervisor-control-token-000000000000",
    )
    CodingSupervisorRuntime(
        loopback,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    for operation, request in _SUPERVISOR["requests"].items():
        assert request["schema"] == "dittobench-coding-attempt-supervisor-request-v1"
        assert request["operation"] == operation
        assert (
            datetime.fromisoformat(request["deadline"].replace("Z", "+00:00")).tzinfo
            is not None
        )
    assert datetime.now(UTC).tzinfo is UTC

    def accepts_platform(value: CodingInferencePlatform) -> None:
        del value

    accepts_platform(cast(PlatformClient, object()))


def test_supervisor_remains_unmounted_and_unconstructed() -> None:
    worker = (_ROOT / "ditto/validator/worker.py").read_text()
    scorer_main = (
        _ROOT / "services/dittobench-api/cmd/dittobench-api/main.go"
    ).read_text()
    assert "CodingSupervisorRuntime" not in worker
    assert "internal/codingsupervisor" not in scorer_main
