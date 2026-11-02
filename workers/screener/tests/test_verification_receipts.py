"""Mechanical receipt digest bindings stay narrow and deterministic."""

import pytest

from ditto_screener.verification_receipts import mechanical_evidence_sha256


def test_mechanical_receipts_bind_check_artifact_and_image() -> None:
    archive = mechanical_evidence_sha256(
        check_code="archive_sha", artifact_sha256="ab" * 32
    )
    image = mechanical_evidence_sha256(
        check_code="build_image_digest",
        artifact_sha256="ab" * 32,
        image_sha256="cd" * 32,
    )
    assert archive == mechanical_evidence_sha256(
        check_code="archive_sha", artifact_sha256="ab" * 32
    )
    assert archive != image
    assert image != mechanical_evidence_sha256(
        check_code="build_image_digest",
        artifact_sha256="ab" * 32,
        image_sha256="ef" * 32,
    )


@pytest.mark.parametrize(
    ("check_code", "image_sha256"),
    [
        ("private_metamorphic", None),
        ("archive_sha", "cd" * 32),
        ("build_image_digest", None),
    ],
)
def test_mechanical_receipts_reject_unsupported_or_unbound_checks(
    check_code: str, image_sha256: str | None
) -> None:
    with pytest.raises(ValueError):
        mechanical_evidence_sha256(
            check_code=check_code,
            artifact_sha256="ab" * 32,
            image_sha256=image_sha256,
        )
