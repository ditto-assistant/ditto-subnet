"""Pin the assignment digest the validator control command recomputes."""

from uuid import UUID

from ditto.db.queries.coding_hosted_admission import HostedAssignmentAuthority

# Mirrored in ditto/tests/validator/test_coding_hosted_control.py
# (GOLDEN_SHA256); the validator hashes the same projection independently.
GOLDEN_SHA256 = "3dbb4daee1b6563f57aece30bfcf54a5a146a9d641fc6c6f263d4f4d1ef5ec8e"


def test_assignment_projection_digest_matches_the_validator_vector() -> None:
    authority = HostedAssignmentAuthority(
        evaluation_id=UUID("10000000-0000-4000-8000-000000000001"),
        attempt_id=UUID("20000000-0000-4000-8000-000000000002"),
        release_row_id=UUID("30000000-0000-4000-8000-000000000003"),
        registration_sha256="a" * 64,
        agent_id=UUID("40000000-0000-4000-8000-000000000004"),
        validator_hotkey="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
        artifact_sha256="2" * 64,
        screened_image_sha256="b" * 64,
        selection_sha256="c" * 64,
        policy_sha256="4" * 64,
        execution_profile_sha256="5" * 64,
        grading_profile_sha256="6" * 64,
        deadline_unix=1788593600,
    )
    assert authority.digest() == GOLDEN_SHA256
    assert set(authority.projection()) == {
        "schema",
        "coding_contract_version",
        "shadow_only",
        "weight_eligible",
        "evaluation_id",
        "attempt_id",
        "release_row_id",
        "registration_sha256",
        "agent_id",
        "validator_hotkey",
        "artifact_sha256",
        "screened_image_sha256",
        "selection_sha256",
        "policy_sha256",
        "execution_profile_sha256",
        "grading_profile_sha256",
        "deadline_unix",
    }
