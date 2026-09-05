"""Verify bounded hosted Coding result receipts against a trusted assignment."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, TypeVar
from uuid import UUID

from ditto.api_models.coding_hosted import (
    HostedCodingRequest,
    HostedCodingResult,
    HostedCodingStatus,
    hosted_message_digest,
    hosted_signing_bytes,
)

MAX_HOSTED_RESULT_BYTES = 8192
_Receipt = TypeVar("_Receipt", HostedCodingResult, HostedCodingStatus)


class HostedCodingVerificationError(ValueError):
    """Safe error with no remote response bytes or private data attached."""


class SignatureVerifier(Protocol):
    def verify(self, data: bytes, signature: bytes) -> bool: ...


@dataclass(frozen=True)
class HostedResultExpectation:
    evaluation_id: UUID
    attempt_id: UUID
    validator_hotkey: str
    platform_hotkey: str
    artifact_sha256: str
    assignment_sha256: str
    policy_sha256: str
    execution_profile_sha256: str
    grading_profile_sha256: str
    request_sha256: str


def verify_hosted_result(
    *,
    body: bytes,
    expected: HostedResultExpectation,
    trusted_verifiers: Mapping[str, SignatureVerifier],
    now_unix: int,
) -> HostedCodingResult:
    """Accept only a canonical projection signed by an out-of-band trusted key.

    The HTTP client must bound streamed bytes and enforce no-store/TLS before
    calling it. No default worker consumes these shadow receipts.
    """

    return _verify_projection(
        body=body,
        expected=expected,
        trusted_verifiers=trusted_verifiers,
        now_unix=now_unix,
        model=HostedCodingResult,
    )


def verify_hosted_status(
    *,
    body: bytes,
    expected: HostedResultExpectation,
    trusted_verifiers: Mapping[str, SignatureVerifier],
    now_unix: int,
) -> HostedCodingStatus:
    """Verify a short-lived pending projection, never a terminal result."""
    return _verify_projection(
        body=body,
        expected=expected,
        trusted_verifiers=trusted_verifiers,
        now_unix=now_unix,
        model=HostedCodingStatus,
    )


def _verify_projection(  # noqa: UP047 - mirrored Platform source supports Python 3.11
    *,
    body: bytes,
    expected: HostedResultExpectation,
    trusted_verifiers: Mapping[str, SignatureVerifier],
    now_unix: int,
    model: type[_Receipt],
) -> _Receipt:
    if type(now_unix) is not int or not body or len(body) > MAX_HOSTED_RESULT_BYTES:
        raise HostedCodingVerificationError("hosted Coding response bounds failed")
    verifier = trusted_verifiers.get(expected.platform_hotkey)
    if verifier is None:
        raise HostedCodingVerificationError("hosted Coding signer is not trusted")
    try:
        result = model.model_validate_json(body)
        canonical = (
            json.dumps(
                result.model_dump(mode="json", by_alias=True),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode()
        # Reject unknown response fields rather than inadvertently retaining them.
        # Request parsing remains forward compatible and drops unknown fields.
        if canonical != body:
            raise ValueError("noncanonical")
        if any(
            getattr(result, field) != getattr(expected, field)
            for field in expected.__dataclass_fields__
        ):
            raise ValueError("assignment drift")
        if not result.issued_at_unix <= now_unix < result.expires_at_unix:
            raise ValueError("expired")
        if not verifier.verify(
            hosted_signing_bytes(result), bytes.fromhex(result.signature)
        ):
            raise ValueError("signature")
    except Exception:
        raise HostedCodingVerificationError(
            "hosted Coding result verification failed"
        ) from None
    return result


def verify_hosted_request(
    *,
    request: HostedCodingRequest,
    expected_validator: str,
    verifier: SignatureVerifier,
    now_unix: int,
) -> str:
    """Verify request origin and expiry; durable admission still owns replay rules."""

    if type(now_unix) is not int:
        raise HostedCodingVerificationError("hosted Coding request clock is invalid")
    try:
        request = HostedCodingRequest.model_validate(
            request.model_dump(mode="json", by_alias=True)
        )
        if request.validator_hotkey != expected_validator:
            raise ValueError("audience")
        if not request.issued_at_unix <= now_unix < request.expires_at_unix:
            raise ValueError("expired")
        if not verifier.verify(
            hosted_signing_bytes(request), bytes.fromhex(request.signature)
        ):
            raise ValueError("signature")
    except Exception:
        raise HostedCodingVerificationError(
            "hosted Coding request verification failed"
        ) from None
    return hosted_message_digest(request)
