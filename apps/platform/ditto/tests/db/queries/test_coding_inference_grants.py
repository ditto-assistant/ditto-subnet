from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from ditto.api_models.coding_inference import (
    CodingInferencePolicy,
    effective_inference_request_budget,
    policy_digest,
)
from ditto.api_models.coding_selection import (
    CodingCatalogBudgets,
    CodingCatalogIssue,
    CodingCatalogRuntimePolicy,
    CodingSelectionRunManifest,
    CodingTaskSetManifest,
)
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingInferenceGrant,
    CodingShadowRun,
    CodingShadowTicket,
)
from ditto.db.queries import coding_inference_grants
from ditto.db.queries.coding_inference_grants import (
    CodingInferenceGrantConflictError,
    CodingInferenceGrantIntegrityError,
    activate_coding_inference_grant,
    coding_inference_bearer_digest,
    ensure_coding_inference_grant,
    revoke_coding_inference_grant,
    revoke_ticket_coding_inference,
)
from ditto.db.queries.coding_task_leases import CodingShadowTaskLeaseCore

_ROOT = Path(__file__).parents[6]
_POLICY_PATH = (
    _ROOT
    / "packages/dittobench-coding-contract/testdata/coding_inference_policy_v1.json"
)
_SELECTION_PATH = (
    _ROOT / "packages/dittobench-coding-contract/testdata/coding_selection_v1.json"
)
_NOW = datetime(2026, 8, 22, 18, tzinfo=UTC)
_VALIDATOR = "5" + "V" * 47


def _fixture() -> SimpleNamespace:
    policy_vector = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    selection = json.loads(_SELECTION_PATH.read_text(encoding="utf-8"))
    policy = CodingInferencePolicy.model_validate(policy_vector["policy"])
    digest = policy_digest(policy)
    manifest = CodingSelectionRunManifest.model_validate(
        {
            **selection["run_manifest"],
            "inference_grant_sha256": digest,
        }
    )
    budgets = CodingCatalogBudgets.model_validate(selection["budgets"])
    run_row_id = uuid4()
    ticket_id = UUID("33333333-3333-4333-8333-333333333333")
    agent_id = UUID(manifest.agent_id)
    ticket = SimpleNamespace(
        ticket_id=ticket_id,
        run_row_id=run_row_id,
        task_count=1,
        validator_hotkey=_VALIDATOR,
        certification_row_id=uuid4(),
        issued_at=_NOW,
        deadline=_NOW + timedelta(hours=1),
    )
    run = SimpleNamespace(
        run_row_id=run_row_id,
        task_count=1,
        agent_id=agent_id,
        artifact_sha256=manifest.agent_artifact_sha256,
        screened_image_sha256="ab" * 32,
        bench_version=11,
        coding_contract_version=1,
        coding_run_id=manifest.coding_run_id,
        inference_grant_sha256=digest,
        weight_eligible=False,
    )
    certification = SimpleNamespace(
        validator_hotkey=_VALIDATOR,
        agent_id=agent_id,
        artifact_sha256=run.artifact_sha256,
        screened_image_sha256=run.screened_image_sha256,
        bench_version=run.bench_version,
        coding_contract_version=1,
        expires_at=ticket.deadline + timedelta(hours=1),
    )
    agent = SimpleNamespace(
        sha256=run.artifact_sha256,
        screened_image_sha256=run.screened_image_sha256,
    )
    lease = CodingShadowTaskLeaseCore(
        ticket_id=ticket_id,
        validator_hotkey=_VALIDATOR,
        issued_at=ticket.issued_at,
        deadline=ticket.deadline,
        run_row_id=run_row_id,
        run_manifest=manifest,
        task_set_manifest=CodingTaskSetManifest.model_validate(
            selection["task_set_manifest"]
        ),
        repository_epoch=selection["task_version"]["payload"]["repository_epoch"],
        issue=CodingCatalogIssue.model_validate(selection["issue"]),
        runtime_policy=CodingCatalogRuntimePolicy.model_validate(
            selection["runtime_policy"]
        ),
        budgets=budgets,
    )
    return SimpleNamespace(
        policy=policy,
        lease=lease,
        ticket=ticket,
        run=run,
        certification=certification,
        agent=agent,
    )


class _Session:
    def __init__(
        self,
        fixture: SimpleNamespace,
        *,
        scalars: list[object],
        grant: CodingInferenceGrant | None = None,
    ) -> None:
        self.fixture = fixture
        self.values = list(scalars)
        self.grant = grant
        self.added: CodingInferenceGrant | None = None
        self.flushes = 0

    async def get(self, model, identity, **kwargs):
        del kwargs
        if model is CodingInferenceGrant:
            return (
                self.grant
                if self.grant is not None and identity == self.grant.grant_id
                else None
            )
        return {
            CodingShadowTicket: self.fixture.ticket,
            CodingShadowRun: self.fixture.run,
            CodingCapabilityCertification: self.fixture.certification,
            Agent: self.fixture.agent,
        }.get(model)

    async def scalar(self, statement):
        del statement
        if not self.values:
            raise AssertionError("unexpected scalar query")
        return self.values.pop(0)

    def add(self, value: CodingInferenceGrant) -> None:
        self.added = value
        self.grant = value

    async def flush(self) -> None:
        self.flushes += 1


async def _create(monkeypatch) -> tuple[SimpleNamespace, CodingInferenceGrant]:
    fixture = _fixture()
    monkeypatch.setattr(
        coding_inference_grants,
        "coding_certification_stale_reason",
        lambda *_args, **_kwargs: "active",
    )
    session = _Session(fixture, scalars=[_NOW, None, None, None])
    result = await ensure_coding_inference_grant(
        session,  # type: ignore[arg-type]
        lease=fixture.lease,
        policy=fixture.policy,
    )
    assert session.values == []
    assert session.flushes == 1
    return fixture, result.grant


async def test_grant_creation_and_replay_bind_exact_task_policy_and_budgets(
    monkeypatch,
) -> None:
    fixture, grant = await _create(monkeypatch)
    selected = fixture.lease.run_manifest.tasks[0]
    assert grant.ticket_id == fixture.ticket.ticket_id
    assert grant.case_id == selected.case_id
    assert grant.profile_capability_id == selected.profile_capability_id
    assert grant.inference_grant_sha256 == policy_digest(fixture.policy)
    assert grant.request_budget == effective_inference_request_budget(
        fixture.lease.budgets.workspace_tool_calls
    )
    assert grant.prompt_token_budget == fixture.lease.budgets.model_input_tokens
    assert grant.completion_token_budget == fixture.lease.budgets.model_output_tokens
    assert grant.status == "pending" and grant.generation == 0
    assert grant.bearer_digest is None and grant.broker_public_key is None
    assert grant.weight_eligible is False

    replay_session = _Session(
        fixture,
        scalars=[_NOW, None, None, grant],
        grant=grant,
    )
    replay = await ensure_coding_inference_grant(
        replay_session,  # type: ignore[arg-type]
        lease=fixture.lease,
        policy=fixture.policy,
    )
    assert replay.idempotent is True and replay.grant is grant
    assert replay_session.flushes == 0


async def test_exchange_rotates_bearer_digest_and_revocation_is_exact(
    monkeypatch,
) -> None:
    fixture, grant = await _create(monkeypatch)
    activation = _Session(
        fixture,
        scalars=[grant, _NOW, None, None],
        grant=grant,
    )
    active, bearer = await activate_coding_inference_grant(
        activation,  # type: ignore[arg-type]
        grant_id=grant.grant_id,
        validator_hotkey=_VALIDATOR,
        broker_public_key="A" * 43 + "=",
        policy=fixture.policy,
    )
    assert active.status == "active" and active.generation == 1
    assert active.bearer_digest == coding_inference_bearer_digest(bearer)
    assert bearer not in active.bearer_digest
    assert active.broker_public_key == "A" * 43

    revoke_session = _Session(
        fixture,
        scalars=[active, _NOW],
        grant=active,
    )
    revoked = await revoke_coding_inference_grant(
        revoke_session,  # type: ignore[arg-type]
        grant_id=active.grant_id,
        validator_hotkey=_VALIDATOR,
        generation=1,
    )
    assert revoked.idempotent is False
    assert active.status == "revoked"
    assert active.bearer_digest is None and active.broker_public_key is None
    assert active.revoked_at == _NOW

    replay_session = _Session(
        fixture,
        scalars=[active, _NOW],
        grant=active,
    )
    replay = await revoke_coding_inference_grant(
        replay_session,  # type: ignore[arg-type]
        grant_id=active.grant_id,
        validator_hotkey=_VALIDATOR,
        generation=1,
    )
    assert replay.idempotent is True
    with pytest.raises(CodingInferenceGrantConflictError):
        await revoke_coding_inference_grant(
            _Session(fixture, scalars=[active, _NOW], grant=active),  # type: ignore[arg-type]
            grant_id=active.grant_id,
            validator_hotkey=_VALIDATOR,
            generation=0,
        )


async def test_existing_authority_drift_is_revoked_and_fails_closed(
    monkeypatch,
) -> None:
    fixture, grant = await _create(monkeypatch)
    grant.case_id = "drifted-case"
    session = _Session(
        fixture,
        scalars=[_NOW, None, None, grant],
        grant=grant,
    )
    with pytest.raises(CodingInferenceGrantIntegrityError):
        await ensure_coding_inference_grant(
            session,  # type: ignore[arg-type]
            lease=fixture.lease,
            policy=fixture.policy,
        )
    assert grant.status == "revoked"
    assert grant.revoked_at == _NOW


async def test_freeze_or_result_transition_revokes_active_grant(monkeypatch) -> None:
    fixture, grant = await _create(monkeypatch)
    grant.status = "active"
    grant.generation = 1
    grant.bearer_digest = "aa" * 32
    grant.broker_public_key = "A" * 43
    session = _Session(fixture, scalars=[grant, _NOW], grant=grant)
    assert await revoke_ticket_coding_inference(
        session,  # type: ignore[arg-type]
        ticket_id=fixture.ticket.ticket_id,
    )
    assert grant.status == "revoked"
    assert grant.bearer_digest is None and grant.broker_public_key is None
