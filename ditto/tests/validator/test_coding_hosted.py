from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import bittensor
import pytest

from ditto.api_models.coding_hosted import (
    HostedCodingRequest,
    HostedCodingResult,
    HostedCodingStatus,
    hosted_message_digest,
    hosted_signing_bytes,
)
from ditto.validator.coding_hosted import (
    HostedCodingVerificationError,
    HostedResultExpectation,
    verify_hosted_request,
    verify_hosted_result,
    verify_hosted_status,
)

NOW = 1788590000


def _case() -> tuple[HostedCodingResult, HostedResultExpectation, bittensor.Keypair]:
    platform = bittensor.Keypair.create_from_uri("//Alice")
    validator = bittensor.Keypair.create_from_uri("//Bob")
    result = HostedCodingResult.model_validate(
        {
            "schema": "dittobench-coding-hosted-result-v2",
            "coding_contract_version": 2,
            "shadow_only": True,
            "weight_eligible": False,
            "evaluation_id": "10000000-0000-4000-8000-000000000001",
            "attempt_id": "20000000-0000-4000-8000-000000000002",
            "validator_hotkey": validator.ss58_address,
            "platform_hotkey": platform.ss58_address,
            "request_sha256": "1" * 64,
            "artifact_sha256": "2" * 64,
            "assignment_sha256": "3" * 64,
            "policy_sha256": "4" * 64,
            "execution_profile_sha256": "5" * 64,
            "grading_profile_sha256": "6" * 64,
            "evidence_sha256": "7" * 64,
            "outcome": "completed",
            "issued_at_unix": NOW,
            "expires_at_unix": NOW + 120,
            "signature": "0" * 128,
        }
    )
    result = result.model_copy(
        update={"signature": platform.sign(hosted_signing_bytes(result)).hex()}
    )
    expected = HostedResultExpectation(
        **{
            name: getattr(result, name)
            for name in HostedResultExpectation.__dataclass_fields__
        }
    )
    return result, expected, platform


def _body(result: HostedCodingResult | HostedCodingStatus, **extra: object) -> bytes:
    return (
        json.dumps(
            {**result.model_dump(mode="json", by_alias=True), **extra},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def test_result_requires_real_signature_and_bound_assignment() -> None:
    result, expected, platform = _case()
    keys = {platform.ss58_address: platform}
    assert (
        verify_hosted_result(
            body=_body(result), expected=expected, trusted_verifiers=keys, now_unix=NOW
        )
        == result
    )
    for changed in (
        replace(expected, artifact_sha256="9" * 64),
        replace(
            expected,
            validator_hotkey=bittensor.Keypair.create_from_uri(
                "//Charlie"
            ).ss58_address,
        ),
        replace(expected, attempt_id=UUID(int=3)),
    ):
        with pytest.raises(HostedCodingVerificationError):
            verify_hosted_result(
                body=_body(result),
                expected=changed,
                trusted_verifiers=keys,
                now_unix=NOW,
            )
    with pytest.raises(HostedCodingVerificationError):
        verify_hosted_result(
            body=_body(result, evidence_sha256="9" * 64),
            expected=expected,
            trusted_verifiers=keys,
            now_unix=NOW,
        )
    with pytest.raises(HostedCodingVerificationError):
        verify_hosted_result(
            body=_body(result), expected=expected, trusted_verifiers={}, now_unix=NOW
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"logs": "PRIVATE_MARKER"},
        {"condition": "v1"},
        {"artifact_url": "https://invalid.example/PRIVATE_MARKER"},
        {"weight_eligible": True},
    ],
)
def test_private_fields_and_activation_do_not_leave_verified_projection(
    extra: dict[str, object],
) -> None:
    result, expected, platform = _case()
    with pytest.raises(HostedCodingVerificationError) as caught:
        verify_hosted_result(
            body=_body(result, **extra),
            expected=expected,
            trusted_verifiers={platform.ss58_address: platform},
            now_unix=NOW,
        )
    assert "PRIVATE_MARKER" not in str(caught.value)


@pytest.mark.parametrize("now", [NOW - 1, NOW + 120])
def test_result_time_window_is_enforced(now: int) -> None:
    result, expected, platform = _case()
    with pytest.raises(HostedCodingVerificationError):
        verify_hosted_result(
            body=_body(result),
            expected=expected,
            trusted_verifiers={platform.ss58_address: platform},
            now_unix=now,
        )


@pytest.mark.parametrize(
    "field,value",
    [("shadow_only", 1), ("weight_eligible", 0), ("coding_contract_version", 2.0)],
)
def test_flags_reject_numeric_coercion(field: str, value: object) -> None:
    result, _, _ = _case()
    with pytest.raises(ValueError):
        HostedCodingResult.model_validate(
            {**result.model_dump(mode="json", by_alias=True), field: value}
        )


def test_oversized_and_duplicate_field_results_fail_safely() -> None:
    result, expected, platform = _case()
    body = _body(result)
    for invalid in (b"PRIVATE_MARKER" * 8192, b'{"outcome":"completed",' + body[1:]):
        with pytest.raises(HostedCodingVerificationError) as caught:
            verify_hosted_result(
                body=invalid,
                expected=expected,
                trusted_verifiers={platform.ss58_address: platform},
                now_unix=NOW,
            )
        assert "PRIVATE_MARKER" not in str(caught.value)


def test_verifier_backend_errors_are_redacted() -> None:
    class BrokenVerifier:
        def verify(self, data: bytes, signature: bytes) -> bool:
            assert data and signature
            raise RuntimeError("PRIVATE_MARKER")

    result, expected, platform = _case()
    with pytest.raises(HostedCodingVerificationError) as caught:
        verify_hosted_result(
            body=_body(result),
            expected=expected,
            trusted_verifiers={platform.ss58_address: BrokenVerifier()},
            now_unix=NOW,
        )
    assert "PRIVATE_MARKER" not in str(caught.value)


def test_request_drops_unknown_fields_and_binds_nonce() -> None:
    validator = bittensor.Keypair.create_from_uri("//Bob")
    request = HostedCodingRequest.model_validate(
        {
            "schema": "dittobench-coding-hosted-request-v2",
            "coding_contract_version": 2,
            "shadow_only": True,
            "weight_eligible": False,
            "evaluation_id": "10000000-0000-4000-8000-000000000001",
            "validator_hotkey": validator.ss58_address,
            "artifact_sha256": "2" * 64,
            "assignment_sha256": "3" * 64,
            "policy_sha256": "4" * 64,
            "operation": "evaluate",
            "result_sha256": None,
            "nonce": "20000000-0000-4000-8000-000000000002",
            "issued_at_unix": NOW,
            "expires_at_unix": NOW + 120,
            "signature": "0" * 128,
            "private_bundle": "PRIVATE_MARKER",
        }
    )
    assert b"PRIVATE_MARKER" not in hosted_signing_bytes(request)
    signed = request.model_copy(
        update={"signature": validator.sign(hosted_signing_bytes(request)).hex()}
    )
    assert verify_hosted_request(
        request=signed,
        expected_validator=validator.ss58_address,
        verifier=validator,
        now_unix=NOW,
    ) == hosted_message_digest(request)
    with pytest.raises(HostedCodingVerificationError):
        verify_hosted_request(
            request=signed.model_copy(update={"nonce": UUID(int=3)}),
            expected_validator=validator.ss58_address,
            verifier=validator,
            now_unix=NOW,
        )


def test_platform_and_validator_use_identical_contract_and_verifier() -> None:
    root = Path(__file__).parents[3]
    vector = json.loads(
        (
            root
            / "packages/dittobench-coding-contract/testdata"
            / "coding_hosted_control_v2.json"
        ).read_bytes()
    )
    assert (
        hosted_message_digest(HostedCodingRequest.model_validate(vector["request"]))
        == vector["request_signing_sha256"]
    )
    assert (
        hosted_message_digest(HostedCodingResult.model_validate(vector["result"]))
        == vector["result_signing_sha256"]
    )
    assert (root / "ditto/api_models/coding_hosted.py").read_bytes() == (
        root / "apps/platform/ditto/api_models/coding_hosted.py"
    ).read_bytes()
    assert (root / "ditto/validator/coding_hosted.py").read_bytes() == (
        root / "apps/platform/ditto/api_server/coding_hosted_verification.py"
    ).read_bytes()
    assert (
        hosted_message_digest(HostedCodingStatus.model_validate(vector["status"]))
        == vector["status_signing_sha256"]
    )


def test_pending_status_is_signed_but_cannot_be_terminal_evidence() -> None:
    result, expected, platform = _case()
    status = HostedCodingStatus.model_validate(
        {
            **{
                key: value
                for key, value in result.model_dump(mode="json", by_alias=True).items()
                if key not in {"schema", "outcome", "evidence_sha256"}
            },
            "schema": "dittobench-coding-hosted-status-v2",
            "state": "admitted",
        }
    )
    status = status.model_copy(
        update={"signature": platform.sign(hosted_signing_bytes(status)).hex()}
    )
    keys = {platform.ss58_address: platform}
    assert (
        verify_hosted_status(
            body=_body(status), expected=expected, trusted_verifiers=keys, now_unix=NOW
        )
        == status
    )
    with pytest.raises(HostedCodingVerificationError):
        verify_hosted_result(
            body=_body(status), expected=expected, trusted_verifiers=keys, now_unix=NOW
        )
    with pytest.raises(HostedCodingVerificationError):
        verify_hosted_status(
            body=_body(status, state="started"),
            expected=expected,
            trusted_verifiers=keys,
            now_unix=NOW,
        )
