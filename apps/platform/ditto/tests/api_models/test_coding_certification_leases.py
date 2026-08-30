from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ditto.api_models.coding_certification_leases import (
    CodingCertificationLeaseAuthority,
    CodingCertificationLeaseIssueRequest,
)


def _authority(**updates: object) -> dict[str, object]:
    issued = datetime.now(UTC)
    value: dict[str, object] = {
        "schema": "dittobench-coding-certification-lease-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "lease_id": uuid4(),
        "validator_hotkey": "5" * 48,
        "agent_id": uuid4(),
        "agent_artifact_sha256": "a" * 64,
        "screened_image_sha256": "b" * 64,
        "bench_version": 9,
        "core_qualification_observation_id": uuid4(),
        "core_qualification_policy_checksum": "c" * 64,
        "canary_manifest_sha256": "d" * 64,
        "runner_plan_sha256": "e" * 64,
        "grader_plan_sha256": "f" * 64,
        "resource_profile_sha256": "1" * 64,
        "inference_policy_sha256": "2" * 64,
        "issued_at": issued,
        "deadline": issued + timedelta(minutes=20),
    }
    value.update(updates)
    return value


def test_qualified_certification_lease_wire_is_shadow_only() -> None:
    authority = CodingCertificationLeaseAuthority.model_validate(_authority())
    assert authority.weight_eligible is False
    assert authority.schema_name == "dittobench-coding-certification-lease-v1"


def test_qualified_certification_lease_issue_rejects_nil_agent() -> None:
    with pytest.raises(ValueError):
        CodingCertificationLeaseIssueRequest.model_validate(
            {
                "validator_hotkey": "5" * 48,
                "agent_id": "00000000-0000-0000-0000-000000000000",
                "bench_version": 9,
                "nonce": uuid4(),
                "requested_at": datetime.now(UTC),
                "signature": "ab" * 64,
            }
        )


def test_qualified_certification_lease_rejects_weight_or_nil_authority() -> None:
    with pytest.raises(ValueError):
        CodingCertificationLeaseAuthority.model_validate(
            _authority(weight_eligible=True)
        )
    with pytest.raises(ValueError):
        CodingCertificationLeaseAuthority.model_validate(
            _authority(lease_id="00000000-0000-0000-0000-000000000000")
        )
