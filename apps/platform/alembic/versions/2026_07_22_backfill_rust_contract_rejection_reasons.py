"""Backfill actionable reasons for opaque Rust contract rejections.

Revision ID: e6b1a4c92d70
Revises: f3a7c91d2e04
Create Date: 2026-07-22

The original verdicts predate the public Rust-diagnostic mapping, so the
platform persisted only ``Screening failed``. Fresh verdicts are handled by the
normal result path; this guarded repair is only for the historical agent and
attempt rows whose immutable artifacts were rechecked with the same contract
validator.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e6b1a4c92d70"
down_revision: str | None = "f3a7c91d2e04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DUPLICATE_PATH_REASON = (
    "Rust harness contract failed (SCR-RUST-002): archive contains a duplicate "
    "path. Package each path exactly once."
)
_MISSING_DOCKERFILE_REASON = (
    "Rust harness contract failed (SCR-RUST-005): Dockerfile is missing from the "
    "archive root. Package the crate contents so Dockerfile is at the top level."
)
_MISSING_MANIFEST_REASON = (
    "Rust harness contract failed (SCR-RUST-006): Cargo.toml is missing from the "
    "archive root. Package the crate contents, not the directory containing the "
    "crate."
)

# agent_id, immutable artifact SHA-256, public reason, matching attempt IDs
_TARGETS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "c2f047cb-c8f0-4fab-9321-05604a059889",
        "7dbdfd0c530d517ad2235004a74164c3746c105a9d98a81d4fc5ac78f8bd7b50",
        _DUPLICATE_PATH_REASON,
        ("37ca2cad-9917-41d2-ab06-39a8fc198c8a",),
    ),
    (
        "9d2f5c34-d0e4-4600-a02f-e0971169210b",
        "0e30d9e0a4ffefab97c0652a5a8bc4803305366461cfaff2160947c33a5e9e32",
        _DUPLICATE_PATH_REASON,
        ("78a49957-4bd0-4bce-baf7-7a3bce03f970",),
    ),
    (
        "7b4f0810-6e58-4eae-9db6-0209a9116dfb",
        "f4cd50798b930d96b4c7838c70f10e6765f192935544d5c34759461a7b2b655f",
        _MISSING_DOCKERFILE_REASON,
        ("e2b491a8-89a7-4749-8f55-9b6d705f3d9f",),
    ),
    (
        "f7ed0f72-5c7d-43a4-a077-70667580b06c",
        "7b43cb91e587bbd3fd6aa061c609982fc35165c3cff527017f206ccfe3acbc17",
        _MISSING_MANIFEST_REASON,
        (
            "a5bbdc49-7cb9-417a-9306-40e318874100",
            "840b7b81-e516-4225-a36e-50672a74bad7",
        ),
    ),
    (
        "3574755a-a20b-4025-bbb2-b9c622e2ce4e",
        "7e3220dcb5395dbe1eec77d164a41eee7353a9cf681ddc01756aefcde9588909",
        _MISSING_MANIFEST_REASON,
        (
            "274f0f05-051b-4db0-a637-0a075c15e567",
            "a84eeec5-2b16-44c5-a0b0-6a8a5d2fd130",
        ),
    ),
    (
        "4f2a1309-f763-4d40-9326-9eb7d13339e8",
        "4a92577ff5a534c20e5a22b336c2c70c5a2092722862b21a2afd98d6137e9d6b",
        _MISSING_MANIFEST_REASON,
        (
            "4bb39845-7a96-48e3-83ab-e17a6065e565",
            "1943fa7e-8397-4bbd-ac98-92edeff31d2a",
        ),
    ),
    (
        "09898a18-20f2-4d4e-aa88-a0f4e1568d48",
        "b827f07cb197aecceb09e50147ee45646bb1cb43aaa9af6b373a15af572dece2",
        _MISSING_MANIFEST_REASON,
        (
            "e6bfd02c-c2f6-4e48-b796-29d8e5a3e30b",
            "507d1557-9dc7-44e0-a99e-1a29c46a5c84",
        ),
    ),
    (
        "977dd6c7-09d2-44b9-86ea-c246bfc8adaa",
        "b827f07cb197aecceb09e50147ee45646bb1cb43aaa9af6b373a15af572dece2",
        _MISSING_MANIFEST_REASON,
        (
            "861369f2-1bf1-4800-94be-ffdb9c87b176",
            "1fb97728-aca4-45de-aa2e-1e25bd03a3c4",
        ),
    ),
    (
        "13082ef8-fe3e-4c24-84f3-b2a47dbdfa5c",
        "b827f07cb197aecceb09e50147ee45646bb1cb43aaa9af6b373a15af572dece2",
        _MISSING_MANIFEST_REASON,
        (
            "deed5e5c-eb0b-4a77-847b-20b3a3ab5a8d",
            "62a3aa46-0a7d-4c12-8dda-0c32f5aebc59",
        ),
    ),
    (
        "efc45418-12a6-444d-aa65-4d4ad4ff2fb3",
        "b827f07cb197aecceb09e50147ee45646bb1cb43aaa9af6b373a15af572dece2",
        _MISSING_MANIFEST_REASON,
        (
            "1ea9486b-d4e7-40f4-9b06-43b81adb2b17",
            "a22162f0-e883-4706-9ec7-9a806d06e1a0",
        ),
    ),
    (
        "57c9a709-07a4-43aa-93b7-269e787deea6",
        "b827f07cb197aecceb09e50147ee45646bb1cb43aaa9af6b373a15af572dece2",
        _MISSING_MANIFEST_REASON,
        (
            "e81db620-89fb-4402-93ad-2218d673d69c",
            "c651391c-3436-4810-9a1e-8a9ca64cd9cd",
        ),
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    agent_targets = [
        {"agent_id": agent_id, "sha256": sha256, "reason": reason}
        for agent_id, sha256, reason, _ in _TARGETS
    ]
    attempt_targets = [
        {
            "agent_id": agent_id,
            "attempt_id": attempt_id,
            "sha256": sha256,
            "reason": reason,
        }
        for agent_id, sha256, reason, attempt_ids in _TARGETS
        for attempt_id in attempt_ids
    ]
    bind.execute(
        sa.text(
            """
            UPDATE agents
            SET screening_reason = :reason
            WHERE agent_id = CAST(:agent_id AS uuid)
              AND sha256 = :sha256
              AND status::text = 'rejected'
              AND screening_reason = 'Screening failed'
              AND screening_reason_code = 'rust-harness-contract'
            """
        ),
        agent_targets,
    )
    bind.execute(
        sa.text(
            """
            UPDATE screening_attempts AS attempt
            SET public_reason = :reason
            FROM agents AS agent
            WHERE attempt.attempt_id = CAST(:attempt_id AS uuid)
              AND attempt.agent_id = CAST(:agent_id AS uuid)
              AND attempt.status = 'rejected'
              AND attempt.public_reason = 'Screening failed'
              AND attempt.reason_code = 'rust-harness-contract'
              AND agent.agent_id = attempt.agent_id
              AND agent.sha256 = :sha256
            """
        ),
        attempt_targets,
    )


def downgrade() -> None:
    # The repaired messages are accurate public metadata. Reintroducing the
    # opaque fallback on rollback would be a data regression, so retain them.
    pass
