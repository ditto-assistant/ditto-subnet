from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from ditto.api_models.coding_inference import CodingInferencePolicy, policy_digest
from ditto.db.models import (
    Agent,
    CodingCertificationInferenceGrant,
    CodingCertificationLease,
)
from ditto.db.queries import coding_certification_inference_grants
from ditto.db.queries.coding_certification_inference_grants import (
    activate_coding_certification_inference_grant,
    ensure_coding_certification_inference_grant,
    revoke_coding_certification_inference_grant,
    revoke_coding_certification_inference_grant_by_capability,
)
from ditto.db.queries.coding_inference_grants import coding_inference_bearer_digest

_ROOT = Path(__file__).parents[6]
_POLICY_PATH = (
    _ROOT
    / "packages/dittobench-coding-contract/testdata/coding_inference_policy_v1.json"
)
_NOW = datetime(2026, 8, 30, 18, tzinfo=UTC)
_VALIDATOR = "5" + "V" * 47
_LEASE = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_AGENT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _policy() -> CodingInferencePolicy:
    return CodingInferencePolicy.model_validate(
        json.loads(_POLICY_PATH.read_text(encoding="utf-8"))["policy"]
    )


def _fixture() -> SimpleNamespace:
    policy = _policy()
    lease = SimpleNamespace(
        lease_id=_LEASE,
        validator_hotkey=_VALIDATOR,
        agent_id=_AGENT,
        status="claimed",
        weight_eligible=False,
        deadline=_NOW + timedelta(minutes=20),
        artifact_sha256="aa" * 32,
        screened_image_sha256="bb" * 32,
    )
    agent = SimpleNamespace(
        sha256=lease.artifact_sha256,
        screened_image_sha256=lease.screened_image_sha256,
    )
    return SimpleNamespace(policy=policy, lease=lease, agent=agent)


class _Session:
    def __init__(
        self,
        fixture: SimpleNamespace,
        *,
        scalars: list[object],
        grant: CodingCertificationInferenceGrant | None = None,
    ) -> None:
        self.fixture = fixture
        self.values = list(scalars)
        self.grant = grant
        self.added: CodingCertificationInferenceGrant | None = None
        self.flushes = 0

    async def get(self, model, identity, **kwargs):
        del kwargs
        if model is CodingCertificationInferenceGrant:
            return (
                self.grant
                if self.grant is not None and identity == self.grant.grant_id
                else None
            )
        if model is CodingCertificationLease and identity == _LEASE:
            return self.fixture.lease
        if model is Agent and identity == _AGENT:
            return self.fixture.agent
        return None

    async def scalar(self, statement):
        del statement
        if not self.values:
            raise AssertionError("unexpected scalar query")
        return self.values.pop(0)

    def add(self, value: CodingCertificationInferenceGrant) -> None:
        self.added = value
        self.grant = value

    async def flush(self) -> None:
        self.flushes += 1


async def _create(
    monkeypatch,
) -> tuple[SimpleNamespace, CodingCertificationInferenceGrant]:
    fixture = _fixture()
    monkeypatch.setattr(
        coding_certification_inference_grants,
        "authorize_coding_certification_harness_delivery",
        _async_ok,
    )
    session = _Session(fixture, scalars=[_NOW, None])
    result = await ensure_coding_certification_inference_grant(
        session,  # type: ignore[arg-type]
        lease_id=_LEASE,
        validator_hotkey=_VALIDATOR,
        policy=fixture.policy,
    )
    assert session.values == []
    assert session.flushes == 1
    return fixture, result.grant


async def _async_ok(*_args: object, **_kwargs: object) -> None:
    return None


async def test_canary_grant_binds_claimed_lease_without_prior_certification(
    monkeypatch,
) -> None:
    fixture, grant = await _create(monkeypatch)
    assert grant.lease_id == _LEASE
    assert grant.case_id == "PRACTICE-LEDGER-001"
    assert grant.profile_capability_id == "public-certification-v1"
    assert grant.inference_grant_sha256 == policy_digest(fixture.policy)
    assert grant.status == "pending" and grant.generation == 0
    assert grant.weight_eligible is False
    replay = await ensure_coding_certification_inference_grant(
        _Session(fixture, scalars=[_NOW, grant], grant=grant),  # type: ignore[arg-type]
        lease_id=_LEASE,
        validator_hotkey=_VALIDATOR,
        policy=fixture.policy,
    )
    assert replay.idempotent is True and replay.grant is grant


async def test_canary_exchange_and_capability_revoke_are_generation_exact(
    monkeypatch,
) -> None:
    fixture, grant = await _create(monkeypatch)
    activated = await activate_coding_certification_inference_grant(
        _Session(fixture, scalars=[_NOW, grant], grant=grant),  # type: ignore[arg-type]
        grant_id=grant.grant_id,
        validator_hotkey=_VALIDATOR,
        broker_public_key="A" * 43 + "=",
        policy=fixture.policy,
    )
    assert activated.grant.status == "active" and activated.grant.generation == 1
    assert activated.grant.bearer_digest == coding_inference_bearer_digest(
        activated.bearer
    )
    revoked = await revoke_coding_certification_inference_grant_by_capability(
        _Session(fixture, scalars=[activated.grant, _NOW], grant=activated.grant),  # type: ignore[arg-type]
        grant_id=activated.grant.grant_id,
        lease_id=_LEASE,
        generation=1,
        revoke_bearer=activated.revoke_bearer,
    )
    assert revoked is not None
    assert revoked.grant.status == "revoked"
    signed = await revoke_coding_certification_inference_grant(
        _Session(fixture, scalars=[activated.grant, _NOW], grant=activated.grant),  # type: ignore[arg-type]
        grant_id=activated.grant.grant_id,
        validator_hotkey=_VALIDATOR,
        generation=1,
    )
    assert signed.idempotent is True
