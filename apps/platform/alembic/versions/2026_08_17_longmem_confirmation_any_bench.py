"""unpin LongMem confirmation from bench 9 and persist its failure diagnostics

Revision ID: 3f5c81a7d940
Revises: c3f8a1b04e21
Create Date: 2026-08-17

LongMemEval is a permanent DittoBench fixture, but the confirmation lane that
carries it was written against the epoch it shipped in. Three CHECK constraints
installed by ``b4d9e7c2a601`` pin ``bench_version = 9`` on the retest, bundle,
and subject tables, so the moment the network activated bench 11 the lane could
no longer create a candidate for anything that ranks: it kept re-superseding a
frozen v9 cohort whose answer keys are, by then, publicly released.

The application side now derives that rule from one place --
``supports_confirmation``, backed by the ``V9EvidenceBenchVersion`` alias that
already declares which epochs carry the evidence stack -- so these three objects
are relaxed to a floor of ``>= MIN_CONFIRMATION_BENCH_VERSION`` to match. The
schema keeps a coarse floor rather than an ``IN`` list on purpose: it is a
data-integrity backstop against pre-contract rows, and the exact set is policy
that belongs in one Python definition, not in DDL that needs a migration per
epoch. This widens what the schema accepts; it activates nothing on its own.
Issuance still requires an operator settings revision, a frozen profile, and the
daily caps.

Separately, ``confirmation_bundle_tickets`` gains two nullable diagnostic
columns.
``failure_reason`` is a four-value protocol class chosen to drive reissue
policy, so a repeatable lane break — every attempt dying as
``confirmation_execution_failed`` — was indistinguishable from a transient one
and survived only in a single validator's host logs, which nobody can read for
a managed or third-party validator. ``failure_class`` and ``failure_stage`` are
allowlisted, low-cardinality, and bound into the reporter's signature (the v2
fail message), never an error-string channel. Both stay NULL for a reporter
predating the contract.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from ditto_screening_protocol.bench_v9 import MIN_CONFIRMATION_BENCH_VERSION

revision: str = "3f5c81a7d940"
down_revision: str | Sequence[str] | None = "c3f8a1b04e21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FLOOR = f"bench_version >= {MIN_CONFIRMATION_BENCH_VERSION}"

_VERSION_PINS = (
    ("confirmation_retest_authorizations", "confirmation_retest_version_check"),
    ("confirmation_bundles", "confirmation_bundles_version_check"),
    ("confirmation_bundle_subjects", "confirmation_subjects_version_check"),
)

_FAILURE_CLASS_VALUES = (
    "'sandbox_oom', 'lease_revoked', 'validator_infrastructure', "
    "'platform_infrastructure', 'dittobench', 'platform', 'validator', "
    "'evidence_schema', 'timeout', 'transport', 'unclassified'"
)
_FAILURE_STAGE_VALUES = (
    "'preparing', 'running_confirmation', 'finalizing', "
    "'submitting_result', 'failed_retrying', 'unknown'"
)

_DIAGNOSTIC_CONSTRAINTS = (
    (
        "confirmation_tickets_failure_diagnostic_pair_check",
        "(failure_class IS NULL) = (failure_stage IS NULL)",
    ),
    (
        "confirmation_tickets_failure_diagnostic_requires_reason_check",
        "failure_class IS NULL OR failure_reason IS NOT NULL",
    ),
    (
        "confirmation_tickets_failure_class_check",
        f"failure_class IS NULL OR failure_class IN ({_FAILURE_CLASS_VALUES})",
    ),
    (
        "confirmation_tickets_failure_stage_check",
        f"failure_stage IS NULL OR failure_stage IN ({_FAILURE_STAGE_VALUES})",
    ),
)


def upgrade() -> None:
    for table, constraint in _VERSION_PINS:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, _FLOOR)

    op.add_column(
        "confirmation_bundle_tickets",
        sa.Column("failure_class", sa.Text(), nullable=True),
    )
    op.add_column(
        "confirmation_bundle_tickets",
        sa.Column("failure_stage", sa.Text(), nullable=True),
    )
    for constraint, expression in _DIAGNOSTIC_CONSTRAINTS:
        op.create_check_constraint(
            constraint, "confirmation_bundle_tickets", expression
        )


def downgrade() -> None:
    for constraint, _expression in _DIAGNOSTIC_CONSTRAINTS:
        op.drop_constraint(constraint, "confirmation_bundle_tickets", type_="check")
    op.drop_column("confirmation_bundle_tickets", "failure_stage")
    op.drop_column("confirmation_bundle_tickets", "failure_class")

    # Re-pinning to exactly 9 would orphan any confirmation evidence already
    # recorded above it, and bundles/subjects are immutable signed history by
    # construction. Refuse the lossy transition rather than strand those rows.
    bind = op.get_bind()
    for table, _constraint in _VERSION_PINS:
        has_above_v9 = bind.execute(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE bench_version > 9)")  # noqa: S608
        ).scalar()
        if has_above_v9:
            raise RuntimeError(
                f"cannot re-pin {table} to bench 9 after bench-v10+ rows exist"
            )

    for table, constraint in _VERSION_PINS:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, "bench_version = 9")
